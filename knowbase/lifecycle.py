"""生命周期状态机：Agent 提交 evidence，服务端决定 state。

once/staging（受治理卡）──(本版本≥3 个已完成独立任务 helpful，≥2 个会话)──▶ verified
旧反馈兼容：once 的无 task_result 反馈保留两工具标签晋升条件。
verified ──(outdated / incorrect 反馈)──▶ stale
active ──(≥2 个独立任务 not_helpful)──▶ stale
stale ──(修订置 revalidation_pending，仍 stale 不可见；新版本证据达标)──▶ active/verified（人工 verify 保留为兼容入口）
revalidation_pending：stale 卡内容修订时由服务端置位、晋升时清除的复活通道标记；未修订的 stale 卡无此标记，永不自动晋升。
any ──(人工 archive)──▶ archived
stale ──(stale 满 archive_after_stale_days 天且近 X 天内无成功复验证据)──▶ archived（自动归档）
"""

from datetime import date
from pathlib import Path

from . import store

OUTCOMES = ("helpful", "not_helpful", "outdated", "incorrect")
PROMOTE_MIN_HELPFUL = 2
PROMOTE_MIN_TOOLS = 2


def apply_feedback(repo: Path, mid: str, outcome: str, by: str, conn) -> tuple[dict | None, list[str]]:
    """旧内部调用保留为未绑定观察；正式反馈走adapter_feedback版本协议。"""
    from .commit_adapter import adapter_feedback
    result=adapter_feedback(repo,conn,mid,outcome,by)
    if result.get("error"):
        return None,[]
    meta,_,_=store.load(repo,mid)
    return meta,result.get("events",[])


def human_verify(repo: Path, mid: str) -> dict | None:
    """人工确认有效（stale 复活 / once 提级的唯一人工通道之一）。"""
    meta, body, path = store.load(repo, mid)
    if not meta:
        return None
    meta["confidence"] = "verified"
    meta["status"] = "active"
    meta.pop("stale_since", None)
    meta["last_verified"] = date.today().isoformat()
    from .mutations import _compute_revision
    meta["verified_content_revision"] = _compute_revision(meta,body)
    meta["updated"] = meta["last_verified"]
    path.write_text(store.render(meta, body), encoding="utf-8")
    return meta


def human_archive(repo: Path, mid: str) -> dict | None:
    meta, body, path = store.load(repo, mid)
    if not meta:
        return None
    meta["status"] = "archived"
    meta["archived_at"] = date.today().isoformat()
    meta["archive_reason"] = "manual"
    meta["updated"] = date.today().isoformat()
    path.write_text(store.render(meta, body), encoding="utf-8")
    return meta


def _parse_day(value):
    """解析 frontmatter/数据库中的 YYYY-MM-DD 日期；非法值返回 None（保守：不归档）。"""
    if not value or not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def _last_evidence(conn, mid):
    """返回 (最新成功复验证据日期, 最新一条已采纳反馈摘要)，无则 (None, None)。

    成功复验证据 = outcome=helpful 且关联任务已完成（task_outcomes.result='completed'）。
    只看已落盘反馈，不看未完成/失败任务的观察。"""
    rows = conn.execute(
        "SELECT f.outcome, t.result, f.created_at FROM feedback_events f "
        "LEFT JOIN task_outcomes t ON t.task_outcome_id=f.task_outcome_id "
        "WHERE f.memory_id=? AND f.state='applied' "
        "ORDER BY f.created_at DESC", (mid,)).fetchall()
    success_day = None
    summary = None
    for outcome, result, created in rows:
        if summary is None:
            summary = f"{outcome}/{result or 'unknown'}@{created}"
        if success_day is None and outcome == "helpful" and result == "completed":
            success_day = _parse_day(created)
    return success_day, summary


def auto_archive_sweep(repo, conn, *, today=None):
    """Phase 1 自动归档：stale 卡同时满足以下条件时归档——

    1. stale 满 archive_after_stale_days 天（stale_since 起点；老卡无此字段时
       以 updated 保守回退，不凭 wall-clock 猜测）；
    2. 近 archive_after_stale_days 天内无成功复验证据（最后成功证据时间距今
       ≥ X 天，或从无成功证据；stale 之前的证据不计入保护期）。

    归档清单记在卡片 frontmatter（archived_at/archive_reason/last_evidence_summary），
    doctor 与团队报表可查；archived 仍不可检索。返回归档条目列表。
    期限内出现成功证据的卡不归档（负向断言见测试）。"""
    from datetime import timedelta

    from . import config, index
    from .locking import RepoLock
    from . import gitops

    today = today or date.today()
    cfg = config.load_config()
    days = int(cfg.get("evolution", {}).get("archive_after_stale_days", 90))
    archived = []
    archive_bases = []
    with RepoLock(repo, cfg.get("lock_timeout", 10)):
        for meta, body, path in store.iter_all(repo):
            if meta.get("status") != "stale":
                continue
            stale_since = _parse_day(meta.get("stale_since")) or _parse_day(meta.get("updated"))
            if stale_since is None or (today - stale_since).days < days:
                continue
            success_day, ev_summary = _last_evidence(conn, meta["id"])
            if success_day is not None and (today - success_day).days < days:
                continue
            from .mutations import _compute_revision as _cr
            archive_bases.append((meta["id"], _cr(meta, body)))
            meta["status"] = "archived"
            meta["archived_at"] = today.isoformat()
            meta["archive_reason"] = f"no successful revalidation evidence in last {days}d"
            meta["last_evidence_summary"] = ev_summary
            meta["updated"] = today.isoformat()
            path.write_text(store.render(meta, body), encoding="utf-8")
            index.upsert(conn, meta, body, path, commit=False)
            # 价值事件管道启用时记账（未启用为空操作），团队报表可查。
            from .mutations import _compute_revision
            from .value.events import record as _record_value
            _record_value(conn, "knowledge.archived",
                          {"memory_id": meta["id"], "content_revision": _compute_revision(meta, body),
                           "scope": meta.get("scope", ""), "reason": meta["archive_reason"],
                           "stale_since": stale_since.isoformat()},
                          source_key=f"knowledge:{meta['id']}:archived:{today.isoformat()}",
                          operation_id=f"auto-archive:{meta['id']}:{today.isoformat()}")
            archived.append({"id": meta["id"], "title": meta.get("title", ""),
                             "stale_since": stale_since.isoformat(),
                             "archived_at": today.isoformat(),
                             "reason": meta["archive_reason"],
                             "last_evidence": ev_summary})
        if archived:
            conn.commit()
            if cfg.get("git", {}).get("auto_commit", True):
                paths = [store.find_file(repo, a["id"]) for a in archived]
                gitops.commit_paths(repo, gitops.with_base_trailers(
                    f"auto-archive {len(archived)} stale memories", archive_bases),
                    [p for p in paths if p])
    return archived
