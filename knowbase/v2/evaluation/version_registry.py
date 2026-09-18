"""P5-C2: 解析器 / 模型版本注册表与 meta 注入。

目标：
- 在评测 / 上线 / 复现环节里，每条 chunk / operation 必须能回答：
  "这是哪个 parser / 哪个 parser_version 产出的？"
- 这层把 parser 的实现指纹（impl_sha256）固化下来，便于：
  - 周期性回归检测（baseline vs candidate 实现 diff）
  - 升级流程里"新旧版本并轨"，旧 chunk 还能回溯到旧实现
  - 排障时定位某版本解析器的边界

设计要点：
- ParserVersion 是 dataclass(frozen)：name / version / impl_sha256 / extensions
- compute_parser_versions(registry) 一次性扫所有解析器（包含懒注册 fallback）
- stamp_chunk_meta(parser_name, ...) 把版本信息塞进 chunk 的 meta，
  与 ingestion.service 已有的 `parser` 字段对齐，不引入 schema migration
- _impl_sha256(cls) 用 inspect.getsourcefile 读源文件 hash；cls 不在文件里
  时退化为 type(cls).__module__ + qualname，hash 仍稳定但语义偏弱
- 任何 IO 异常都吞掉并写 None，避免在评测 / 上线路径上崩
"""
from __future__ import annotations

import hashlib
import inspect
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from ..ingestion.parsers.base import (
    DocumentParser,
    ParserRegistry,
    register_builtin,
    registry as default_registry,
)


# 单个解析器的版本快照（frozen，便于 dict 化 / 序列化）
@dataclass(frozen=True)
class ParserVersion:
    name: str
    version: str              # 语义化版本，由调用方注入；缺省 "0.0.0"
    impl_sha256: str | None   # 解析器实现文件的 SHA-256，读不到则 None
    impl_source: str | None   # 解析器源文件路径（相对工作目录或绝对）
    extensions: tuple[str, ...] = ()  # 该 parser 识别的扩展名（采样得到）
    class_qualname: str = ""  # e.g. "MarkdownParser"，用于日志可读化
    extra: dict = field(default_factory=dict)


def _impl_sha256(parser: DocumentParser) -> tuple[str | None, str | None]:
    """Return (sha256_hex, source_path_str) for the parser's implementation.

    Strategy:
    1. inspect.getsourcefile(type(parser)) — works for concrete subclasses
       whose source lives in a real .py file.
    2. Fallback: hash the qualified name + module so we still get a stable
       fingerprint for synthetic / dynamically-built parsers; semantically
       weaker (same qualname ⇒ same hash) but never raises.
    """
    try:
        cls = type(parser)
        src = inspect.getsourcefile(cls)
    except (TypeError, OSError):
        cls = type(parser)
        fallback = f"{cls.__module__}::{cls.__qualname__}"
        digest = hashlib.sha256(fallback.encode("utf-8")).hexdigest()
        return digest, None
    if not src:
        cls = type(parser)
        fallback = f"{cls.__module__}::{cls.__qualname__}"
        digest = hashlib.sha256(fallback.encode("utf-8")).hexdigest()
        return digest, None
    try:
        data = Path(src).read_bytes()
    except OSError:
        cls = type(parser)
        fallback = f"{cls.__module__}::{cls.__qualname__}::{src}"
        digest = hashlib.sha256(fallback.encode("utf-8")).hexdigest()
        return digest, None
    return hashlib.sha256(data).hexdigest(), src


def _probe_extensions(parser: DocumentParser) -> tuple[str, ...]:
    """Ask the parser which extensions it supports via synthetic Path objects.

    We try a small, fixed set of common extensions; whichever the parser
    reports `supports=True` for is included. Pure functional check, no IO.
    """
    candidates = (
        ".md", ".markdown", ".txt", ".log", ".html", ".htm", ".py", ".js",
        ".ts", ".java", ".go", ".rs", ".c", ".cpp", ".h", ".json", ".yaml",
        ".yml", ".xml", ".csv", ".pdf", ".docx", ".xlsx", ".pptx", ".png",
        ".jpg", ".jpeg", ".tiff", ".bmp", ".svg", ".ini", ".conf",
        ".stub", ".zzz", "",
    )
    found: list[str] = []
    for ext in candidates:
        try:
            ok = parser.supports(Path(f"sample{ext}"))
        except Exception:
            continue
        if ok:
            found.append(ext)
    return tuple(found)


def compute_parser_versions(
    registry_: ParserRegistry | None = None,
    *,
    parser_versions: Optional[dict[str, str]] = None,
) -> dict[str, ParserVersion]:
    """Enumerate every parser in `registry_` and snapshot its version info.

    Args:
        registry_: registry to scan; defaults to the global singleton. If it
            is empty we lazily call `register_builtin()` so callers don't have
            to remember that dance (same convention as parser_runner).
        parser_versions: optional name → version override map. Lets callers
            tag parsers with semver (e.g. {"markdown": "2.1.0"}) without
            forcing every parser class to declare its own version constant.

    Returns:
        Mapping of parser.name → ParserVersion. Order is registry insertion
        order (Python 3.7+ dict preserves insertion order).
    """
    if registry_ is None:
        registry_ = default_registry()
    # Lazy fallback: only when the caller passes a real but empty
    # ParserRegistry. Inject builtins *into that registry* (not the global)
    # so the caller can inspect / mutate it without polluting other tests.
    # Duck-typed registries (e.g. test fixtures returning ()) must NOT
    # trigger this branch — they should be honored as-is.
    if isinstance(registry_, ParserRegistry) and not list(registry_.parsers()):
        from ..ingestion.parsers.markdown_parser import MarkdownParser
        from ..ingestion.parsers.html_parser import HtmlParser
        from ..ingestion.parsers.code_parser import CodeParser
        from ..ingestion.parsers.log_parser import LogParser
        from ..ingestion.parsers.txt_parser import TxtParser
        from ..ingestion.parsers.pdf_parser import PdfParser
        from ..ingestion.parsers.docx_parser import DocxParser
        from ..ingestion.parsers.xlsx_parser import XlsxParser
        from ..ingestion.parsers.pptx_parser import PptxParser
        from ..ingestion.parsers.ocr_parser import OcrParser
        for p in (MarkdownParser(), HtmlParser(), CodeParser(), LogParser(),
                  TxtParser(), PdfParser(), DocxParser(),
                  XlsxParser(), PptxParser(), OcrParser()):
            registry_.register(p)
    overrides = parser_versions or {}
    out: dict[str, ParserVersion] = {}
    for parser in registry_.parsers():
        sha, source = _impl_sha256(parser)
        cls = type(parser)
        out[parser.name] = ParserVersion(
            name=parser.name,
            version=overrides.get(parser.name, "0.0.0"),
            impl_sha256=sha,
            impl_source=source,
            extensions=_probe_extensions(parser),
            class_qualname=f"{cls.__module__}.{cls.__qualname__}",
        )
    return out


def stamp_chunk_meta(
    parser_name: str,
    *,
    extra: dict[str, Any] | None = None,
    registry_: ParserRegistry | None = None,
    parser_versions: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """Build the `meta` dict that ingestion.service should attach to each chunk.

    Returns a fresh dict — callers should not mutate the result in place after
    passing it onward, but it's safe to read or copy.

    Always-present keys (even for unknown parsers):
    - parser: parser name string
    - parser_version: "unknown" when the parser isn't registered
    - parser_impl_sha256: None when unknown
    - parser_extensions: empty tuple when unknown
    """
    versions = compute_parser_versions(
        registry_=registry_,
        parser_versions=parser_versions,
    )
    pv = versions.get(parser_name)
    meta: dict[str, Any] = {
        "parser": parser_name,
        "parser_version": pv.version if pv else "unknown",
        "parser_impl_sha256": pv.impl_sha256 if pv else None,
        "parser_extensions": pv.extensions if pv else (),
        "parser_class": pv.class_qualname if pv else "",
    }
    if extra:
        meta.update(extra)
    return meta


def known_parser_names(versions: dict[str, ParserVersion] | None = None) -> tuple[str, ...]:
    """Convenience: list of parser names currently registered."""
    if versions is None:
        versions = compute_parser_versions()
    return tuple(versions.keys())


__all__ = [
    "ParserVersion",
    "compute_parser_versions",
    "stamp_chunk_meta",
    "known_parser_names",
]
