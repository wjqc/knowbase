"""governance_factor（P2，I 阶段）。

公式（V2 计划 §6.3）：

    governance_factor = confidence × freshness × feedback × source_authority
    final_score       = RRF_score × governance_factor

设计要点：
- 因子可独立为 0 / 1，便于审计与回放
- stale / archived 直接因子 = 0（默认排除，可配置）
- 输入 doc 字段缺失时使用中性默认（不放大也不缩小）
- 全部纯函数，无 IO 副作用，便于单测与回放

doc 可选字段：
- confidence: float   [0, 1]，默认 1.0
- created_at: ISO 字符串或 epoch 秒，默认 now
- feedback_score: float [-1, 1]，默认 0
- authority: str ∈ {authoritative, validated, observed, staging}，默认 observed
- stale: bool，默认 False
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass


# ---------- 字段与默认值 ----------

_VALID_AUTHORITIES = {"authoritative", "validated", "observed", "staging"}
_AUTHORITY_WEIGHT = {
    "authoritative": 1.20,
    "validated": 1.00,
    "observed": 0.85,
    "staging": 0.50,  # 即便能召回也降权，避免误注入
}


@dataclass
class GovernanceBreakdown:
    """每个 hit 的因子明细，便于审计 / search_explain。"""

    confidence: float
    freshness: float
    feedback: float
    source_authority: float
    factor: float
    stale: bool = False
    authority: str = "observed"

    def as_dict(self) -> dict:
        return {
            "confidence": round(self.confidence, 4),
            "freshness": round(self.freshness, 4),
            "feedback": round(self.feedback, 4),
            "source_authority": round(self.source_authority, 4),
            "factor": round(self.factor, 4),
            "stale": self.stale,
            "authority": self.authority,
        }


# ---------- 单因子计算 ----------


def _parse_timestamp(value) -> float:
    """接受 ISO 字符串 / epoch 秒 / epoch 毫秒；返回 epoch 秒。失败返回 now。"""
    if value is None:
        return time.time()
    if isinstance(value, (int, float)):
        v = float(value)
        # 毫秒判定：> 1e12 视为毫秒
        return v / 1000.0 if v > 1e12 else v
    if isinstance(value, str):
        # 兼容 ISO 8601（'2026-09-18' / '2026-09-18T10:30:00'）
        try:
            import datetime as _dt

            for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    dt = _dt.datetime.strptime(value, fmt)
                    return dt.replace(tzinfo=_dt.timezone.utc).timestamp()
                except ValueError:
                    continue
        except Exception:
            pass
    return time.time()


def confidence_factor(doc: dict) -> float:
    """confidence ∈ [0, 1]，越接近 1 越可信。缺失 = 1.0（中性）。"""
    raw = doc.get("confidence", 1.0)
    try:
        c = float(raw)
    except (TypeError, ValueError):
        return 1.0
    return max(0.0, min(1.0, c))


def freshness_factor(doc: dict, *, now: float | None = None,
                     half_life_days: float = 365.0) -> float:
    """指数衰减：half_life_days 后因子 = 0.5。

    缺失时间戳 → 1.0（中性，不放大也不缩小）。
    """
    created = _parse_timestamp(doc.get("created_at"))
    cur = now if now is not None else time.time()
    age_days = max(0.0, (cur - created) / 86400.0)
    return math.pow(0.5, age_days / half_life_days)


def feedback_factor(doc: dict) -> float:
    """feedback_score ∈ [-1, 1] → [0, 1.2]。

    0（无反馈）→ 1.0；+1（全好评）→ 1.2；-1（全差评）→ 0.0。
    缺失 = 1.0（中性）。
    """
    raw = doc.get("feedback_score", 0.0)
    try:
        s = float(raw)
    except (TypeError, ValueError):
        return 1.0
    s = max(-1.0, min(1.0, s))
    return 1.0 + 0.2 * s


def source_authority_factor(doc: dict) -> tuple[float, str]:
    """权威性权重；返回 (factor, authority_str)。"""
    auth = (doc.get("authority") or "observed").lower()
    if auth not in _VALID_AUTHORITIES:
        auth = "observed"
    return _AUTHORITY_WEIGHT[auth], auth


# ---------- 复合因子 ----------


def compute_governance_factor(doc: dict, *, now: float | None = None,
                              drop_stale: bool = True) -> GovernanceBreakdown:
    """计算单个文档的 governance_factor。

    drop_stale=True 时，stale / archived 文档因子直接 = 0（默认排除）。
    """
    c = confidence_factor(doc)
    f = freshness_factor(doc, now=now)
    fb = feedback_factor(doc)
    sa, auth = source_authority_factor(doc)
    stale = bool(doc.get("stale")) or (auth == "staging")
    if stale and drop_stale:
        factor = 0.0
    else:
        factor = c * f * fb * sa
    return GovernanceBreakdown(
        confidence=c,
        freshness=f,
        feedback=fb,
        source_authority=sa,
        factor=factor,
        stale=stale,
        authority=auth,
    )


__all__ = [
    "GovernanceBreakdown",
    "confidence_factor",
    "freshness_factor",
    "feedback_factor",
    "source_authority_factor",
    "compute_governance_factor",
]
