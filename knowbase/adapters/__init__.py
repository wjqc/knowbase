"""宿主适配器：归一化不同客户端的 transcript 和事件格式。

Claude/ZCode 优先基于 Hook + transcript 入口；
Trae 或其他无结束 Hook 的客户端提供显式 memory_capture。
"""

from .normalizer import normalize_event, NormalizedEvent
from .checkpoint import build_checkpoint, checkpoint_hash

__all__ = ["normalize_event", "NormalizedEvent", "build_checkpoint", "checkpoint_hash"]
