"""生命周期状态机：Agent 提交 evidence，服务端决定 state。

once ──(本版本≥2 个任务结果 helpful 且来自 ≥2 个工具标签；或可信CLI人工确认)──▶ verified
verified ──(outdated / incorrect 反馈)──▶ stale
stale ──(人工修订 + verify)──▶ verified
any ──(人工 archive)──▶ archived
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
    meta["updated"] = date.today().isoformat()
    path.write_text(store.render(meta, body), encoding="utf-8")
    return meta
