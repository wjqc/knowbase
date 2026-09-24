"""提炼流水线：脱敏 → 切分 → 校验 → 宽召回。

语义判断交宿主 Agent；MCP 只做确定性校验和材料准备。
"""

from .sanitizer import sanitize
from .segmenter import segment_text, Segment
from .validator import validate_candidate, CandidateErrors
from .recall import wide_recall

__all__ = ["sanitize", "segment_text", "Segment", "validate_candidate",
           "CandidateErrors", "wide_recall"]
