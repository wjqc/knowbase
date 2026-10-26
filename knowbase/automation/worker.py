"""P3 后台 supervisor：每库一个调度所有者，OS 负责启动/保活，SQLite 租约负责互斥。

提炼仅在显式启用、scope授权和有效执行器探针回执均满足时运行。
MCP/Hook 只追加任务或发本地唤醒信号，不各自开执行池。

任务按 next_run_at 唤醒；服务空闲时不轮询模型。每个 handler 自行
管理锁与事务：网络调用一律在锁外；失败只记录、不中断其他任务。
"""

import time
import uuid
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import config as app_config
from .. import gitops, index, jobs, migrations

LEASE_SINGLETON = "worker"
LEASE_SECONDS = 300

# (kind, 默认间隔秒, 说明)。提炼需要显式授权与有效执行器探针回执。
TASK_DEFS = (
    ("lease_recovery", 60, "租约/未结算回执恢复"),
    ("knowledge_sync", 60, "知识同步"),
    ("evidence_exchange", 900, "统计/最小证据交换"),
    ("team_sync", 900, "原团队统计采集、同步及看板更新"),
    ("archive_sweep", 86400, "自动归档/候选清理"),
    ("conflict_recheck", 86400, "冲突复验"),
    ("vector_maintenance", 30, "可选向量维护"),
    ("distillation", 60, "自动提炼（授权与探针条件满足时运行）"),
)
TASK_INTERVALS = {kind: interval for kind, interval, _ in TASK_DEFS}
WORKER_LABEL = "com.knowbase.worker"
WORKER_TASK_NAME = "knowbase-worker"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect(repo):
    """已迁移的数据库连接（v26 含 supervisor 运行账本）。"""
    conn = index.connect(repo)
    migrations.apply_all(conn)
    return conn


# ---------- 租约 ----------

def acquire_lease(conn, owner: str, duration: float = LEASE_SECONDS):
    """原子领取 supervisor 租约：BEGIN IMMEDIATE + fencing 递增。

    已被他人持有且未过期 → 返回 None（调用方不得执行任何任务）。
    返回 (owner, fencing_token)。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT owner,lease_until,fencing_token FROM automation_lease WHERE singleton=?",
            (LEASE_SINGLETON,)).fetchone()
        now = datetime.now(timezone.utc)
        stamp = now.isoformat(timespec="seconds")
        if row and row[0] and row[1] and row[1] > stamp:
            conn.rollback()
            return None
        fencing = (row[2] if row else 0) + 1
        expires = (now + timedelta(seconds=max(30, duration))).isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO automation_lease(singleton,owner,lease_until,fencing_token,updated_at)"
            " VALUES(?,?,?,?,?) ON CONFLICT(singleton) DO UPDATE SET owner=excluded.owner,"
            "lease_until=excluded.lease_until,fencing_token=excluded.fencing_token,"
            "updated_at=excluded.updated_at",
            (LEASE_SINGLETON, owner, expires, fencing, stamp))
        conn.commit()
        return owner, fencing
    except Exception:
        conn.rollback()
        raise


def release_lease(conn, owner: str, fencing_token=None) -> bool:
    """只释放自己持有的租约；已被他人接管时不覆盖。"""
    sql = "UPDATE automation_lease SET owner='',lease_until='' WHERE singleton=? AND owner=?"
    params = [LEASE_SINGLETON, owner]
    if fencing_token is not None:
        sql += " AND fencing_token=?"; params.append(fencing_token)
    cur = conn.execute(sql, params)
    conn.commit()
    return cur.rowcount == 1


def _owns_lease(conn, owner, fencing_token):
    return conn.execute("SELECT 1 FROM automation_lease WHERE singleton=? AND owner=? "
                        "AND fencing_token=? AND lease_until>?",
                        (LEASE_SINGLETON, owner, fencing_token, _now())).fetchone() is not None


def renew_lease(conn, owner, fencing_token):
    now = _now()
    until = (datetime.now(timezone.utc) + timedelta(seconds=max(30, LEASE_SECONDS))).isoformat(timespec="seconds")
    cur = conn.execute("UPDATE automation_lease SET lease_until=?,updated_at=? WHERE singleton=? "
                       "AND owner=? AND fencing_token=? AND lease_until>?",
                       (until, now, LEASE_SINGLETON, owner, fencing_token, now))
    conn.commit()
    return cur.rowcount == 1


def _heartbeat(repo, owner, fencing_token, stop, lost):
    while not stop.wait(max(.05, min(30, LEASE_SECONDS / 3))):
        conn = None
        try:
            conn = sqlite3.connect(index.db_path(repo), timeout=1)
            if not renew_lease(conn, owner, fencing_token):
                lost.set(); return
        except sqlite3.Error:
            lost.set(); return
        finally:
            if conn is not None: conn.close()


def lease_status(conn) -> dict:
    row = conn.execute(
        "SELECT owner,lease_until,fencing_token,updated_at FROM automation_lease "
        "WHERE singleton=?", (LEASE_SINGLETON,)).fetchone()
    if not row:
        return {"held": False, "owner": "", "lease_until": "", "fencing_token": 0}
    held = bool(row[0] and row[1] and row[1] > _now())
    return {"held": held, "owner": row[0], "lease_until": row[1],
            "fencing_token": row[2], "updated_at": row[3]}


# ---------- 调度表 ----------

def ensure_schedule(conn) -> list[str]:
    """补齐全部任务键的首行（next_run_at=当前时间，即首次立即到期）。"""
    now = _now()
    created = []
    for kind, _interval, _note in TASK_DEFS:
        cur = conn.execute(
            "INSERT OR IGNORE INTO automation_schedule(task_key,kind,next_run_at,updated_at)"
            " VALUES(?,?,?,?)", (kind, kind, now, now))
        if cur.rowcount:
            created.append(kind)
    conn.commit()
    return created


def due_tasks(conn, now: str = "") -> list[dict]:
    now = now or _now()
    rows = conn.execute(
        "SELECT task_key,kind,next_run_at,last_success_at,last_error FROM automation_schedule"
        " WHERE next_run_at<=? ORDER BY next_run_at", (now,)).fetchall()
    return [{"task_key": r[0], "kind": r[1], "next_run_at": r[2],
             "last_success_at": r[3], "last_error": r[4]} for r in rows]


def _reschedule(conn, kind: str, *, status: str, error: str, lease) -> bool:
    """Persist outcomes/backoff only while this exact supervisor lease is still valid."""
    interval = TASK_INTERVALS.get(kind, 60)
    if kind == "team_sync":
        interval = max(900, int(app_config.load_config().get("team", {}).get("interval_seconds", 900)))
    conn.execute("BEGIN IMMEDIATE")
    try:
        if not _owns_lease(conn, *lease):
            conn.rollback(); return False
        previous = conn.execute("SELECT failure_count FROM automation_schedule WHERE task_key=?", (kind,)).fetchone()
        failures = ((previous[0] if previous else 0) + 1) if status == "error" else 0
        if failures:
            interval = min(3600, interval * (2 ** min(failures, 5)))
        nxt = (datetime.now(timezone.utc) + timedelta(seconds=interval)).isoformat(timespec="seconds")
        conn.execute(
            "UPDATE automation_schedule SET next_run_at=?,last_success_at=CASE WHEN ?='success' THEN ? "
            "ELSE last_success_at END,last_error=?,failure_count=?,last_status=?,updated_at=? WHERE task_key=?",
            (nxt, status, _now(), error[:200] if status in {"error", "blocked"} else "",
             failures, status, _now(), kind))
        conn.commit(); return True
    except Exception:
        conn.rollback(); raise


def _outcome(result):
    """Handlers may return structured failures instead of raising (including nested sends)."""
    if isinstance(result,list):
        for item in result:
            status,reason=_outcome(item)
            if status=='error':return status,reason
        return 'success',''
    if not isinstance(result, dict):
        return "success", ""
    for key in ("error", "errors", "warning"):
        if result.get(key):
            reason = result[key]
            return "error", reason if isinstance(reason, str) and re.fullmatch(r"[\w.-]{1,100}", reason) else "handler_" + key
    for value in result.values():
        if isinstance(value, (dict,list)):
            status, reason = _outcome(value)
            if status == "error": return status, reason
    for key in ("blocked", "skipped"):
        if result.get(key):
            value = str(result[key])
            return key, value if re.fullmatch(r"[\w.-]{1,100}", value) else "handler_" + key
    return "success", ""


# ---------- 各任务 handler（永不向 tick 外抛异常） ----------

def _h_lease_recovery(repo, conn, cfg) -> dict:
    from ..updates.recovery import recover_hooks
    from ..commit_adapter import recover_intents
    from ..extraction.materialize import recover
    from .distillation import prepare_due
    from ..locking import RepoLock
    expired = jobs.expire_leases(conn)
    prepared=prepare_due(repo,conn)
    with RepoLock(repo, cfg.get('lock_timeout',10)):
        intents = recover_intents(repo,conn)
    pending = conn.execute("SELECT DISTINCT job_id FROM job_submissions WHERE state!='done' LIMIT 10").fetchall()
    for (job_id,) in pending: recover(repo,conn,job_id)
    return {"recovered": expired, 'prepared':prepared,'intents': intents['recovered'], 'receipts_checked':len(pending),
            'update_hooks':recover_hooks(repo,conn,cfg)}


def _h_knowledge_sync(repo, conn, cfg) -> dict:
    """本地写后唤醒/周期同步：复用 sync_before_read；失败只记类别，不阻塞其他任务。"""
    try:
        updated, warning = gitops.sync_before_read(repo, cfg)
    except Exception as exc:
        return {"error": "sync_failed", "detail": gitops.diagnose_git_failure(str(exc))}
    out = {"updated": bool(updated)}
    if warning:
        out["warning"] = warning
        out["diagnosis"] = gitops.diagnose_git_failure(warning)
    return out


def _h_evidence_exchange(repo, conn, cfg) -> dict:
    """统计/最小证据交换：未配置聚合角色或远端时跳过，不报错、不自动改配置。"""
    value_cfg = cfg.get("value", {}) or {}
    if not value_cfg.get("events_enabled"):
        return {"skipped": "value_events_disabled"}
    role = str(value_cfg.get("evidence_role") or "off")
    if role not in ("sender", "aggregator"):
        return {"skipped": "evidence_role_off"}
    from ..value import exchange_v2
    from ..team.transport import GitData
    from ..team.service import commit_identity
    remote = str(value_cfg.get("evidence_remote") or "")
    branch = str(value_cfg.get("evidence_branch") or "knowbase-value-events-v2")
    root = Path(str(value_cfg.get("evidence_root") or "~/.knowbase/evidence")).expanduser()
    prefixes = list(cfg.get("git", {}).get("allowed_remote_prefixes", []) or [])
    try:
        git = GitData(root, remote, prefixes,identity=commit_identity(cfg))
    except Exception as exc:
        return {"error": "evidence_transport_unavailable", "detail": type(exc).__name__}
    if role == "sender":
        sent = exchange_v2.send_pending(repo, git, branch=branch)
        acked = exchange_v2.acknowledge_receipts(git, repo, branch=branch)
        return {"sent": sent, "acknowledged": acked}
    agg_db = str(value_cfg.get("aggregate_db") or "~/.knowbase/value-aggregate.db")
    team_id = str(value_cfg.get("evidence_team_id") or "default")
    return exchange_v2.receive_all(git, agg_db, team_id=team_id, branch=branch)


def _h_archive_sweep(repo, conn, cfg) -> dict:
    from ..lifecycle import auto_archive_sweep
    from ..extraction import materialize
    archived = auto_archive_sweep(repo, conn)
    ttl = int(cfg.get("evolution", {}).get("review_candidate_ttl_days", 30))
    discarded = materialize.sweep_review_candidates(conn, ttl_days=ttl)
    return {"archived": len(archived), "review_discarded": discarded}


def _h_team_sync(repo, conn, cfg) -> dict:
    if not cfg.get("team", {}).get("enabled"):
        return {"skipped": "team_disabled"}
    from ..team import service
    return service.run(network=True)


def _h_conflict_recheck(repo, conn, cfg) -> dict:
    """新证据到达后的冲突复验：只重裁 open 冲突，打平保持隔离、不猜赢家。"""
    from .. import quarantine
    rechecked = 0
    outcomes = []
    for record in quarantine.list_open(conn)[:20]:
        outcomes.extend(quarantine.readjudicate_card(repo, conn, record["card_id"]))
        rechecked += 1
    return {"rechecked_cards": rechecked, "outcomes": len(outcomes)}


def _h_vector_maintenance(repo, conn, cfg) -> dict:
    """沿用 serve 的 30 秒向量维护节律与现有退避；本 handler 只做一次 tick。"""
    from ..retrieval_state import maintenance
    maintenance(repo)
    return {"ran": True}


def _h_distillation(repo, conn, cfg) -> dict:
    from .distillation import run_due
    if not cfg.get('automation',{}).get('executors'):
        return {"blocked": "distillation_blocked_no_executor"}
    return run_due(repo,cfg,guard=cfg.get('_lease_valid',lambda:False))


_HANDLERS = {
    "lease_recovery": _h_lease_recovery,
    "knowledge_sync": _h_knowledge_sync,
    "evidence_exchange": _h_evidence_exchange,
    "team_sync": _h_team_sync,
    "archive_sweep": _h_archive_sweep,
    "conflict_recheck": _h_conflict_recheck,
    "vector_maintenance": _h_vector_maintenance,
    "distillation": _h_distillation,
}


# ---------- tick / run / status ----------

def tick(repo, *, owner: str = "") -> dict:
    """一次调度：持租约者执行全部到期任务；未持租约者不做任何事。

    返回 {lease, ran: [{kind, result|error}], held_by}。单个任务失败不中断其余任务。
    """
    repo = Path(repo)
    import os
    update_root=Path(os.environ.get('KNOWBASE_RUNTIME_ROOT',str(Path.home()/'.knowbase/runtime')))
    if (update_root/'update-drain.json').exists():return {'lease':'updating','ran':[]}
    cfg = app_config.load_config()
    conn = _connect(repo)
    stop, lost = threading.Event(), threading.Event()
    heartbeat = None
    leased = None
    try:
        owner = owner or ("worker-" + uuid.uuid4().hex[:12])
        leased = acquire_lease(conn, owner)
        if not leased:
            held = lease_status(conn)
            return {"lease": "held_by_other", "held_by": held["owner"], "ran": []}
        if (update_root/'update-drain.json').exists():return {'lease':'updating','ran':[]}
        heartbeat = threading.Thread(target=_heartbeat, args=(repo, *leased, stop, lost), daemon=True)
        heartbeat.start()
        ensure_schedule(conn)
        ran = []
        for task in due_tasks(conn):
            if lost.is_set() or not renew_lease(conn, *leased):
                lost.set(); break
            kind = task["kind"]
            handler = _HANDLERS.get(kind)
            if handler is None:
                continue
            try:
                def lease_valid():
                    check = sqlite3.connect(index.db_path(repo),timeout=1)
                    try: return _owns_lease(check,*leased)
                    finally: check.close()
                result = handler(repo, conn, {**cfg,'_lease_valid':lease_valid})
                ran.append({"kind": kind, "result": result})
                status, error = _outcome(result)
            except Exception as exc:
                if conn.in_transaction: conn.rollback()
                ran.append({"kind": kind, "error": type(exc).__name__})
                status, error = "error", type(exc).__name__
            ran[-1]["status"] = status
            if lost.is_set() or not _reschedule(conn, kind, status=status, error=error, lease=leased):
                lost.set(); break
        return {"lease": "lost" if lost.is_set() else "acquired", "owner": owner, "ran": ran}
    finally:
        stop.set()
        if heartbeat is not None: heartbeat.join(timeout=2)
        if leased is not None: release_lease(conn, *leased)
        conn.close()


def run_once(repo) -> dict:
    return tick(repo)


def run_forever(repo, *, tick_seconds: float = 5.0) -> dict:
    """OS 负责启动/保活的常驻循环；收到中断即干净退出（租约下次 tick 前过期）。"""
    cfg = app_config.load_config()
    tick_seconds = max(1.0, float(cfg.get("worker", {}).get("tick_seconds", tick_seconds)))
    ticks = 0
    try:
        while True:
            tick(repo)
            ticks += 1
            time.sleep(tick_seconds)
    except KeyboardInterrupt:
        return {"ticks": ticks, "stopped": "interrupted"}
    return {"ticks": ticks, "stopped": "returned"}


def status(repo) -> dict:
    repo = Path(repo)
    conn = _connect(repo)
    try:
        ensure_schedule(conn)
        rows = conn.execute(
            "SELECT task_key,kind,next_run_at,last_success_at,last_error,last_status,failure_count FROM automation_schedule"
            " ORDER BY kind").fetchall()
        gaps = conn.execute("SELECT COUNT(*) FROM capture_gaps").fetchone()[0]
        return {
            "lease": lease_status(conn),
            "schedule": [{"task_key": r[0], "kind": r[1], "next_run_at": r[2],
                          "last_success_at": r[3], "last_error": r[4], "last_status": r[5],
                          "failure_count": r[6]} for r in rows],
            "capture_gaps": gaps,
            "distillation": __import__('knowbase.automation.distillation',fromlist=['ready_executor']).ready_executor(app_config.load_config())[1] or 'ready',
        }
    finally:
        conn.close()


# ---------- 安装 / 停用（复用 team/worker 平台实现） ----------

def install(*, remove: bool = False) -> dict:
    """安装 worker 调度入口；同时迁移旧 team 定时任务（兼容别名）。

    单调度所有者由 automation_lease 在运行时保证：即使旧入口残留，
    同一时刻也只有一个持有者执行任务。
    """
    from ..team import worker as team_worker
    if remove:
        legacy = team_worker.install(remove=True)
        try:
            current = team_worker.install(remove=True, label=WORKER_LABEL,
                                          task_name=WORKER_TASK_NAME,
                                          descriptor_name="knowbase-worker-task.xml")
        except Exception:
            current = {"worker": "remove_failed"}
        return {"worker": "removed", "legacy": legacy, "current": current}
    result = team_worker.install(label=WORKER_LABEL, task_name=WORKER_TASK_NAME,
                                 argv=["worker", "run"],
                                 persistent=True,
                                 description="knowbase background worker",
                                 descriptor_name="knowbase-worker-task.xml")
    legacy_removed = None
    try:
        legacy_removed = team_worker.install(remove=True)
    except Exception as exc:
        legacy_removed = {"warning": f"legacy team entry kept: {type(exc).__name__}"}
    result["legacy_team_entry"] = legacy_removed
    result["single_owner"] = "automation_lease"
    return result


def disable() -> dict:
    return install(remove=True)
