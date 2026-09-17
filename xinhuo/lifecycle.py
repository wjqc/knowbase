"""生命周期状态机：Agent 提交 evidence，服务端决定 state。

once ──(≥2 次 helpful 且来自 ≥2 个不同工具；或 1 次人工确认)──▶ verified
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
    """应用一次使用反馈。conn 为 index 层连接（记录 feedback_log 并做晋升判定）。

    返回 (meta, events)；记忆不存在返回 (None, [])。
    """
    from . import index  # 局部导入避免循环

    meta, body, path = store.load(repo, mid)
    if not meta:
        return None, []
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome 非法: {outcome}")

    events = []
    meta["updated"] = date.today().isoformat()

    if outcome == "helpful":
        meta["helpful_count"] = int(meta.get("helpful_count", 0)) + 1
    else:
        meta["unhelpful_count"] = int(meta.get("unhelpful_count", 0)) + 1
        if outcome in ("outdated", "incorrect") and meta.get("status", "active") == "active":
            meta["status"] = "stale"
            events.append(f"status: active → stale（{outcome}）")

    index.log_feedback(conn, mid, by, outcome)

    if outcome == "helpful" and meta.get("confidence") == "once" and meta.get("status") == "active":
        total, distinct = index.helpful_stats(conn, mid)
        human_ok = by.lower().startswith("human")
        if human_ok or (total >= PROMOTE_MIN_HELPFUL and distinct >= PROMOTE_MIN_TOOLS):
            meta["confidence"] = "verified"
            meta["last_verified"] = date.today().isoformat()
            how = "人工确认" if human_ok else f"helpful×{total} 来自 {distinct} 个工具"
            events.append(f"confidence: once → verified（{how}）")

    path.write_text(store.render(meta, body), encoding="utf-8")
    return meta, events


def human_verify(repo: Path, mid: str) -> dict | None:
    """人工确认有效（stale 复活 / once 提级的唯一人工通道之一）。"""
    meta, body, path = store.load(repo, mid)
    if not meta:
        return None
    meta["confidence"] = "verified"
    meta["status"] = "active"
    meta["last_verified"] = date.today().isoformat()
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
