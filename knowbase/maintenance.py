"""治理维护：定期维护的统一入口（P4）。

维护只走已有写入内核，禁止维护路径绕过 base 记录、索引核对或版本事件：
- 崩溃恢复 → commit_adapter.recover_intents（已有）；
- 自动归档 → lifecycle.auto_archive_sweep（已有，内部持锁、写卡、索引、base trailer 提交）；
- review 候选清理 → extraction.materialize.sweep_review_candidates（已有，只动候选行）；
- 冲突复验 → quarantine.readjudicate_card（已有，证据强度裁决；打平保持 open）。

本模块不直接写卡片文件、不直接改索引、不绕过上述函数。
"""

from datetime import date

from . import config, index, store


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
    }


def maintenance_status(repo, conn, *, today=None) -> dict:
    """只读：维护状态快照，供 doctor 与 maintenance status 使用。"""
    plan = plan_maintenance(repo, conn, today=today)
    return {
        "stale_due_for_archive": len(plan["stale_due_for_archive"]),
        "review_candidates_expired": plan["review_candidates_expired"],
        "pending_intents": plan["pending_intents"],
        "open_conflicts": plan["open_conflicts"],
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
    return {
        "recovered_intents": recovered,
        "archived": archived,
        "discarded_review_candidates": discarded,
        "reverified_conflicts": reverified,
    }
