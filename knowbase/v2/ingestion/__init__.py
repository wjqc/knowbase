"""V2 摄取层（P1-B / P1-C）。

子模块：
- parsers      文档解析适配器（DocumentParser 接口 + Markdown/TXT/PDF/DOCX 实现）
- chunking     文本切分（段 / 段落 / 句；带重叠窗口）
- registry     解析器注册表（按扩展名 / MIME 分发）
- service      增量摄取核心（SHA-256 幂等 + version CAS + tombstone）
- hashutil     通用哈希工具

对外仅暴露 service.IngestionService；其他模块供内部使用。
"""
from .service import IngestionResult, IngestionService

__all__ = ["IngestionService", "IngestionResult"]
