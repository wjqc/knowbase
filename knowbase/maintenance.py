"""治理维护：定期维护的统一入口（P4）。

维护只走已有写入内核，禁止维护路径绕过 base 记录、索引核对或版本事件：
- 崩溃恢复 → commit_adapter.recover_intents（已有）；
- 自动归档 → lifecycle.auto_archive_sweep（已有，内部持锁、写卡、索引、base trailer 提交）；
- review 候选清理 → extraction.materialize.sweep_review_candidates（已有，只动候选行）；
- 冲突复验 → quarantine.readjudicate_card（已有，证据强度裁决；打平保持 open）。

本模块不直接写卡片文件、不直接改索引、不绕过上述函数。
"""

from datetime import date, datetime, timedelta, timezone

from . import config, index, store


RETENTION_DEFAULTS = {
    "enabled": True,
    "context_events_days": 30,
    "command_calls_days": 365,
    "usage_log_days": 180,
    "retrieval_observations_days": 90,
    "job_spool_terminal_days": 7,
    "db_size_warn_mb": 512,
}
# 只清理这些终态作业的原始材料；awaiting/claimed 等活跃作业的 spool 永不删除。
_JOB_SPOOL_TERMINAL_STATES = ("succeeded", "no_candidate", "failed")


def _retention(cfg) -> dict:
    raw = cfg.get("retention") or {}
    spec = dict(RETENTION_DEFAULTS)
    spec["enabled"] = bool(raw.get("enabled", spec["enabled"]))
    for key in spec:
        if key == "enabled":
            continue
        try:
            spec[key] = max(1, int(raw.get(key, spec[key])))
        except (TypeError, ValueError):
            pass
    return spec


def _cutoff(now, days: int) -> str:
    return (now - timedelta(days=days)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _tables(conn) -> set[str]:
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    except Exception:
        return set()


def _count(conn, sql: str, params) -> int:
    try:
        return conn.execute(sql, params).fetchone()[0]
    except Exception:
        return 0


def _context_event_delete_sql(tables) -> tuple[str, str]:
    base = "FROM context_events WHERE datetime(created_at) < datetime(?)"
    if "retrieval_deliveries" in tables:
        base += (" AND NOT EXISTS (SELECT 1 FROM retrieval_deliveries d "
                 "WHERE d.context_event_id = context_events.event_id)")
    return base, base.replace("FROM context_events", "DELETE FROM context_events", 1)


def retention_plan(conn, *, cfg=None, now=None) -> dict:
    """只读：各运行账本按保留期可清理的行数。"""
    spec = _retention(cfg or config.load_config())
    if not spec["enabled"]:
        return {"enabled": False, "context_events": 0, "command_calls": 0,
                "usage_log": 0, "retrieval_observations": 0, "job_spool": 0}
    now = now or datetime.now(timezone.utc)
    tables = _tables(conn)
    plan = {"enabled": True}
    if "context_events" in tables:
        where, _ = _context_event_delete_sql(tables)
        plan["context_events"] = _count(conn, f"SELECT COUNT(*) {where}",
                                        (_cutoff(now, spec["context_events_days"]),))
    else:
        plan["context_events"] = 0
    plan["command_calls"] = _count(
        conn, "SELECT COUNT(*) FROM command_calls WHERE datetime(ts) < datetime(?)",
        (_cutoff(now, spec["command_calls_days"]),)) if "command_calls" in tables else 0
    plan["usage_log"] = _count(
        conn, "SELECT COUNT(*) FROM usage_log WHERE datetime(ts) < datetime(?)",
        (_cutoff(now, spec["usage_log_days"]),)) if "usage_log" in tables else 0
    plan["retrieval_observations"] = _count(
        conn, "SELECT COUNT(*) FROM retrieval_observations WHERE datetime(occurred_at) < datetime(?)",
        (_cutoff(now, spec["retrieval_observations_days"]),)) if "retrieval_observations" in tables else 0
    plan["job_spool"] = _count(
        conn,
        "SELECT COUNT(*) FROM job_spool WHERE datetime(created_at) < datetime(?) "
        "AND job_id IN (SELECT job_id FROM jobs WHERE state IN (%s))" %
        ",".join("?" * len(_JOB_SPOOL_TERMINAL_STATES)),
        (_cutoff(now, spec["job_spool_terminal_days"]), *_JOB_SPOOL_TERMINAL_STATES),
    ) if {"job_spool", "jobs"} <= tables else 0
    return plan


def apply_retention(conn, *, cfg=None, now=None) -> dict:
    """按保留期清理运行账本；返回实际删除行数。等待 Agent 的作业材料不动。"""
    spec = _retention(cfg or config.load_config())
    plan = retention_plan(conn, cfg=cfg, now=now)
    if not spec["enabled"]:
        return plan
    now = now or datetime.now(timezone.utc)
    tables = _tables(conn)
    if "context_events" in tables:
        _, delete = _context_event_delete_sql(tables)
        conn.execute(delete, (_cutoff(now, spec["context_events_days"]),))
    if "command_calls" in tables:
        conn.execute("DELETE FROM command_calls WHERE datetime(ts) < datetime(?)",
                     (_cutoff(now, spec["command_calls_days"]),))
    if "usage_log" in tables:
        conn.execute("DELETE FROM usage_log WHERE datetime(ts) < datetime(?)",
                     (_cutoff(now, spec["usage_log_days"]),))
    if "retrieval_observations" in tables:
        conn.execute("DELETE FROM retrieval_observations WHERE datetime(occurred_at) < datetime(?)",
                     (_cutoff(now, spec["retrieval_observations_days"]),))
    if {"job_spool", "jobs"} <= tables:
        conn.execute(
            "DELETE FROM job_spool WHERE datetime(created_at) < datetime(?) "
            "AND job_id IN (SELECT job_id FROM jobs WHERE state IN (%s))" %
            ",".join("?" * len(_JOB_SPOOL_TERMINAL_STATES)),
            (_cutoff(now, spec["job_spool_terminal_days"]), *_JOB_SPOOL_TERMINAL_STATES))
    conn.commit()
    return plan


def _days(cfg) -> int:
    return int(cfg.get("evolution", {}).get("archive_after_stale_days", 90))


def _ttl_days(cfg) -> int:
    return int(cfg.get("evolution", {}).get("review_candidate_ttl_days", 30))


def _stale_due(repo, conn, *, today, days: int) -> list[dict]:
    """只读：列出本次维护会归档的 stale 卡（与 auto_archive_sweep 同口径）。"""
    from .lifecycle import _last_evidence, _parse_day
    from datetime import timedelta
    due = []
    for meta, _body, _path in store.iter_all(repo):
        if meta.get("status") != "stale":
            continue
        stale_since = _parse_day(meta.get("stale_since")) or _parse_day(meta.get("updated"))
        if stale_since is None or (today - stale_since).days < days:
            continue
        success_day, ev_summary = _last_evidence(conn, meta["id"])
        if success_day is not None and (today - success_day).days < days:
            continue
        due.append({"id": meta["id"], "title": meta.get("title", ""),
                    "stale_since": stale_since.isoformat(),
                    "last_evidence": ev_summary})
    return due


def _review_expired(conn, ttl: int, today) -> int:
    """只读：超期 needs_review 候选数（与 sweep_review_candidates 同口径）。"""
    try:
        rows = conn.execute(
            "SELECT created_at FROM candidates "
            "WHERE materialization_state='needs_review'").fetchall()
    except Exception:
        return 0
    from datetime import datetime
    n = 0
    for (created,) in rows:
        try:
            day = datetime.fromisoformat(created.replace("Z", "+00:00")).date()
        except (ValueError, AttributeError):
            continue
        if (today - day).days > ttl:
            n += 1
    return n


def _pending_intents(conn) -> int:
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM write_intents WHERE status='pending'").fetchone()[0]
    except Exception:
        return 0


def _open_conflicts(conn) -> list[dict]:
    """open 冲突及其年龄；card_conflicts 表不存在时返回空。"""
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        if "card_conflicts" not in tables:
            return []
        rows = conn.execute(
            "SELECT conflict_id,card_id,created_at,updated_at FROM card_conflicts "
            "WHERE status='open' ORDER BY created_at").fetchall()
    except Exception:
        return []
    from datetime import datetime
    out = []
    today = date.today()
    for cid, card_id, created, updated in rows:
        age = None
        try:
            day = datetime.fromisoformat((created or "").replace("Z", "+00:00")).date()
            age = (today - day).days
        except (ValueError, AttributeError):
            pass
        out.append({"conflict_id": cid, "card_id": card_id, "age_days": age,
                    "updated_at": updated})
    return out


def _last_archived_at(repo) -> str | None:
    dates = [m.get("archived_at") for m, _, _ in store.iter_all(repo)
             if m.get("status") == "archived" and m.get("archived_at")]
    return max(dates) if dates else None


def _last_discarded_at(conn) -> str | None:
    try:
        row = conn.execute(
            "SELECT MAX(updated_at) FROM candidates WHERE decision='discarded'").fetchone()
        return row[0] if row else None
    except Exception:
        return None


def plan_maintenance(repo, conn, *, today=None) -> dict:
    """只读：本次维护会做什么（不写库、不写文件、不提交）。"""
    cfg = config.load_config()
    today = today or date.today()
    return {
        "stale_due_for_archive": _stale_due(repo, conn, today=today, days=_days(cfg)),
        "review_candidates_expired": _review_expired(conn, _ttl_days(cfg), today),
        "pending_intents": _pending_intents(conn),
        "open_conflicts": _open_conflicts(conn),
        "retention": retention_plan(conn, cfg=cfg),
    }


def maintenance_status(repo, conn, *, today=None) -> dict:
    """只读：维护状态快照，供 doctor 与 maintenance status 使用。"""
    plan = plan_maintenance(repo, conn, today=today)
    return {
        "stale_due_for_archive": len(plan["stale_due_for_archive"]),
        "review_candidates_expired": plan["review_candidates_expired"],
        "pending_intents": plan["pending_intents"],
        "open_conflicts": plan["open_conflicts"],
        "retention": plan["retention"],
        "last_archived_at": _last_archived_at(repo),
        "last_discarded_at": _last_discarded_at(conn),
    }


def reverify_conflicts(repo, conn) -> list[dict]:
    """冲突复验：在受限环境核对相关代码/版本和既有任务证据。

    对每张有 open 冲突的卡调用已有 readjudicate_card；无可执行验证器
    （无新证据、证据打平）时冲突保持 open，这里只报告其年龄与原因，
    绝不为"零待办"强选赢家。返回 [{conflict_id, card_id, status, note, age_days}]。
    """
    from .quarantine import list_open, readjudicate_card
    ages = {c["conflict_id"]: c["age_days"] for c in _open_conflicts(conn)}
    card_ids = sorted({c["card_id"] for c in list_open(conn)})
    out = []
    for card_id in card_ids:
        for cid, status, note in readjudicate_card(repo, conn, card_id):
            out.append({"conflict_id": cid, "card_id": card_id, "status": status,
                        "note": note, "age_days": ages.get(cid)})
    return out


def run_maintenance(repo, conn, *, today=None) -> dict:
    """执行一次定期维护（真实入口，非演练）。

    顺序：崩溃恢复 → 自动归档 → review 候选清理 → 冲突复验。
    每一步都走已有函数与统一写入内核；单步异常不静默吞掉——异常会向
    上抛出，调用方（CLI）据此返回非零退出码并呈现错误。
    """
    from .commit_adapter import recover_intents
    from .extraction import materialize
    from .lifecycle import auto_archive_sweep
    cfg = config.load_config()
    recovered = recover_intents(repo, conn)
    archived = auto_archive_sweep(repo, conn, today=today)
    discarded = materialize.sweep_review_candidates(
        conn, ttl_days=_ttl_days(cfg))
    reverified = reverify_conflicts(repo, conn)
    retention = apply_retention(conn, cfg=cfg)
    return {
        "recovered_intents": recovered,
        "archived": archived,
        "discarded_review_candidates": discarded,
        "reverified_conflicts": reverified,
        "retention": retention,
    }
