"""生命周期状态机：Agent 提交 evidence，服务端决定 state。

once/staging（受治理卡）──(本版本≥3 个已完成独立任务 helpful，≥2 个会话)──▶ verified
旧反馈兼容：once 的无 task_result 反馈保留两工具标签晋升条件。
verified ──(outdated / incorrect 反馈)──▶ stale
active ──(≥2 个独立任务 not_helpful)──▶ stale
stale ──(修订置 revalidation_pending，仍 stale 不可见；新版本证据达标)──▶ active/verified（人工 verify 保留为兼容入口）
revalidation_pending：stale 卡内容修订时由服务端置位、晋升时清除的复活通道标记；未修订的 stale 卡无此标记，永不自动晋升。
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
