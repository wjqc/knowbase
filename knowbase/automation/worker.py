"""P3 后台 supervisor：每库一个调度所有者，OS 负责启动/保活，SQLite 租约负责互斥。

本模块只做调度与维护任务分派，不启动自动提炼：提炼任务位保持
blocked（P2 因无真实执行器通过而被阻断），worker 绝不调用执行器。
MCP/Hook 只追加任务或发本地唤醒信号，不各自开执行池。

任务按 next_run_at 唤醒；服务空闲时不轮询模型。每个 handler 自行
管理锁与事务：网络调用一律在锁外；失败只记录、不中断其他任务。
"""

import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import config as app_config
from .. import gitops, index, jobs, migrations

LEASE_SINGLETON = "worker"
LEASE_SECONDS = 300

# (kind, 默认间隔秒, 说明)。提炼位只占位，handler 恒返回 blocked，不调用执行器。
TASK_DEFS = (
    ("lease_recovery", 60, "租约/未结算回执恢复"),
    ("knowledge_sync", 60, "知识同步"),
    ("evidence_exchange", 900, "统计/最小证据交换"),
    ("archive_sweep", 86400, "自动归档/候选清理"),
    ("conflict_recheck", 86400, "冲突复验"),
    ("vector_maintenance", 30, "可选向量维护"),
    ("distillation", 60, "自动提炼（P2 阻断中，仅占位）"),
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


def release_lease(conn, owner: str) -> bool:
    """只释放自己持有的租约；已被他人接管时不覆盖。"""
    cur = conn.execute(
        "UPDATE automation_lease SET owner='',lease_until='' WHERE singleton=? AND owner=?",
        (LEASE_SINGLETON, owner))
    conn.commit()
    return cur.rowcount == 1


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


def _reschedule(conn, kind: str, *, success: bool, error: str = "", failures: int = 0) -> None:
    """成功按默认间隔；失败指数退避（上限 1 小时），错误文本只记类别化摘要。"""
    interval = TASK_INTERVALS.get(kind, 60)
    if not success:
        interval = min(3600, interval * (2 ** min(failures, 5)))
    nxt = (datetime.now(timezone.utc) + timedelta(seconds=interval)).isoformat(timespec="seconds")
    if success:
        conn.execute(
            "UPDATE automation_schedule SET next_run_at=?,last_success_at=?,last_error='',"
            "updated_at=? WHERE task_key=?", (nxt, _now(), _now(), kind))
    else:
        conn.execute(
            "UPDATE automation_schedule SET next_run_at=?,last_error=?,updated_at=? "
            "WHERE task_key=?", (nxt, (error or "")[:200], _now(), kind))
    conn.commit()


# ---------- 各任务 handler（永不向 tick 外抛异常） ----------

def _h_lease_recovery(repo, conn, cfg) -> dict:
    return {"recovered": jobs.expire_leases(conn)}


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
    remote = str(value_cfg.get("evidence_remote") or "")
    branch = str(value_cfg.get("evidence_branch") or "knowbase-value-events-v2")
    root = Path(str(value_cfg.get("evidence_root") or "~/.knowbase/evidence")).expanduser()
    prefixes = list(cfg.get("git", {}).get("allowed_remote_prefixes", []) or [])
    try:
        git = GitData(root, remote, prefixes)
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
    """P2 被阻断：调度位只占位，绝不调用执行器、不产生提炼任务。"""
    return {"blocked": "distillation_blocked_no_executor"}


_HANDLERS = {
    "lease_recovery": _h_lease_recovery,
    "knowledge_sync": _h_knowledge_sync,
    "evidence_exchange": _h_evidence_exchange,
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
    cfg = app_config.load_config()
    conn = _connect(repo)
    try:
        owner = owner or ("worker-" + uuid.uuid4().hex[:12])
        leased = acquire_lease(conn, owner)
        if not leased:
            held = lease_status(conn)
            return {"lease": "held_by_other", "held_by": held["owner"], "ran": []}
        ensure_schedule(conn)
        ran = []
        failures: dict[str, int] = {}
        for task in due_tasks(conn):
            kind = task["kind"]
            handler = _HANDLERS.get(kind)
            if handler is None:
                continue
            try:
                result = handler(repo, conn, cfg)
                ran.append({"kind": kind, "result": result})
                _reschedule(conn, kind, success=True)
            except Exception as exc:
                failures[kind] = failures.get(kind, 0) + 1
                ran.append({"kind": kind, "error": type(exc).__name__})
                _reschedule(conn, kind, success=False,
                            error=type(exc).__name__, failures=failures[kind])
        release_lease(conn, owner)
        return {"lease": "acquired", "owner": owner, "ran": ran}
    finally:
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
            "SELECT task_key,kind,next_run_at,last_success_at,last_error FROM automation_schedule"
            " ORDER BY kind").fetchall()
        gaps = conn.execute("SELECT COUNT(*) FROM capture_gaps").fetchone()[0]
        return {
            "lease": lease_status(conn),
            "schedule": [{"task_key": r[0], "kind": r[1], "next_run_at": r[2],
                          "last_success_at": r[3], "last_error": r[4]} for r in rows],
            "capture_gaps": gaps,
            "distillation": "blocked_no_executor",
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
