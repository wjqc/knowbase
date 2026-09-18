"""knowbase 单库多格式文档解析器。

公共 API：
- DocumentParser: 抽象接口
- ParserRegistry: 按扩展名 / MIME 查找解析器
- registry(): 全局单例
- register_builtin()：注册 10 个内置解析器（Markdown / HTML / Code / Log
  / TXT / PDF / DOCX / XLSX / PPTX / OCR）

解析器只负责把 bytes/Path → 纯文本（或带元数据的结构化结果）；
SHA-256 幂等 / version CAS / chunking 由 ingestion.service 负责。
"""
from .base import (
    DocumentParser,
    ParsedDocument,
    ParserRegistry,
    registry,
    register_builtin,
)

__all__ = [
    "DocumentParser",
    "ParsedDocument",
    "ParserRegistry",
    "registry",
    "register_builtin",
]
