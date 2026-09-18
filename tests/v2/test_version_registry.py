"""P5-C2 测试：解析器版本注册表与 chunk meta 注入。"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from knowbase.v2.evaluation.version_registry import (
    ParserVersion,
    compute_parser_versions,
    known_parser_names,
    stamp_chunk_meta,
)
from knowbase.v2.ingestion.parsers.base import (
    DocumentParser,
    ParsedDocument,
    ParserRegistry,
)


# ---- 辅助解析器 / 注册器 -----------------------------------------------------

class _StubParser(DocumentParser):
    name = "stub-test"
    def supports(self, path): return path.suffix in {".stub", ".zzz"}
    def parse(self, path): return ParsedDocument(text="stub", meta={})


class _SourceMissingParser(DocumentParser):
    """Type whose source file can't be found — drives the fallback branch."""
    name = "source-missing"
    def supports(self, path): return False
    def parse(self, path): return ParsedDocument(text="", meta={})


class _EmptyRegistry:
    """Duck-typed registry returning no parsers — drives lazy fallback."""
    def parsers(self): return ()


# ---- ParserVersion dataclass ------------------------------------------------

class TestParserVersion:
    def test_is_frozen(self):
        pv = ParserVersion(name="x", version="1", impl_sha256="abc",
                           impl_source=None, extensions=(".md",))
        with pytest.raises(Exception):
            pv.name = "y"  # type: ignore[misc]

    def test_default_extra_is_independent(self):
        a = ParserVersion(name="a", version="1", impl_sha256="x",
                          impl_source=None, extensions=())
        b = ParserVersion(name="b", version="1", impl_sha256="x",
                          impl_source=None, extensions=())
        a.extra["k"] = "v"
        assert b.extra == {}, "default_factory must isolate extra dicts"

    def test_extensions_tuple(self):
        pv = ParserVersion(name="x", version="1", impl_sha256="x",
                           impl_source=None, extensions=(".md", ".markdown"))
        assert pv.extensions == (".md", ".markdown")


# ---- compute_parser_versions ------------------------------------------------

class TestComputeParserVersions:
    def test_lazy_registers_builtins_when_real_registry_empty(self):
        # Only the concrete ParserRegistry triggers the lazy fallback;
        # duck-typed registries do not (would pollute the global singleton).
        reg = ParserRegistry()  # empty
        versions = compute_parser_versions(registry_=reg)
        assert "markdown" in versions
        assert "code" in versions
        assert "pdf" in versions

    def test_duck_typed_registry_does_not_trigger_builtin_registration(self):
        # Counterpart: passing a duck-typed registry that returns ()
        # should be honored as-is and NOT inject builtins.
        versions = compute_parser_versions(_EmptyRegistry())  # type: ignore[arg-type]
        assert versions == {}, "duck-typed empty registry must stay empty"

    def test_returns_mapping_for_default_registry(self):
        versions = compute_parser_versions()
        # Built-ins (P5-A 全覆盖)
        for name in ("markdown", "code", "html", "log", "txt", "pdf",
                     "docx", "xlsx", "pptx", "ocr"):
            assert name in versions, f"missing built-in parser: {name}"

    def test_version_override_applied(self):
        versions = compute_parser_versions(
            parser_versions={"markdown": "2.1.0", "code": "1.5.3"},
        )
        assert versions["markdown"].version == "2.1.0"
        assert versions["code"].version == "1.5.3"
        # others stay default
        assert versions["txt"].version == "0.0.0"

    def test_impl_sha256_is_stable_hex(self):
        a = compute_parser_versions()
        b = compute_parser_versions()
        for name in a:
            assert a[name].impl_sha256 == b[name].impl_sha256, name
            assert a[name].impl_sha256 is None or len(a[name].impl_sha256) == 64

    def test_impl_sha256_matches_source_file(self):
        versions = compute_parser_versions()
        pv = versions["markdown"]
        assert pv.impl_sha256 is not None
        assert pv.impl_source is not None
        # 重新读源文件算 hash，应得到相同结果
        data = Path(pv.impl_source).read_bytes()
        assert hashlib.sha256(data).hexdigest() == pv.impl_sha256

    def test_extensions_sampled_for_markdown(self):
        versions = compute_parser_versions()
        exts = versions["markdown"].extensions
        assert ".md" in exts
        assert ".markdown" in exts

    def test_class_qualname_present(self):
        versions = compute_parser_versions()
        for name, pv in versions.items():
            assert pv.class_qualname, f"{name} has empty class_qualname"
            assert "Parser" in pv.class_qualname

    def test_custom_registry_with_stub(self):
        reg = ParserRegistry()
        reg.register(_StubParser())
        versions = compute_parser_versions(registry_=reg)
        assert "stub-test" in versions
        assert ".stub" in versions["stub-test"].extensions

    def test_source_missing_parser_uses_fallback(self):
        """_SourceMissingParser: getsourcefile raises; must not crash."""
        reg = ParserRegistry()
        reg.register(_SourceMissingParser())
        versions = compute_parser_versions(registry_=reg)
        pv = versions["source-missing"]
        # fallback yields a non-None hash (qualname-based) and a None source
        assert pv.impl_sha256 is not None
        assert len(pv.impl_sha256) == 64
        # source may be None or some sentinel; key is the hash is set


# ---- stamp_chunk_meta -------------------------------------------------------

class TestStampChunkMeta:
    def test_known_parser_emits_full_meta(self):
        meta = stamp_chunk_meta("markdown")
        assert meta["parser"] == "markdown"
        assert meta["parser_version"] == "0.0.0"
        assert meta["parser_impl_sha256"] is not None
        assert ".md" in meta["parser_extensions"]
        assert "MarkdownParser" in meta["parser_class"]

    def test_unknown_parser_graceful_degradation(self):
        meta = stamp_chunk_meta("nonsense-xyz")
        assert meta["parser"] == "nonsense-xyz"
        assert meta["parser_version"] == "unknown"
        assert meta["parser_impl_sha256"] is None
        assert meta["parser_extensions"] == ()
        assert meta["parser_class"] == ""

    def test_extra_merged(self):
        meta = stamp_chunk_meta("markdown", extra={"doc_id": "d42", "ts": 123})
        assert meta["doc_id"] == "d42"
        assert meta["ts"] == 123
        assert meta["parser"] == "markdown"  # standard fields preserved

    def test_extra_overrides_parser_field(self):
        """Caller-passed extra takes precedence (intentional: explicit wins)."""
        meta = stamp_chunk_meta("markdown", extra={"parser": "custom-override"})
        assert meta["parser"] == "custom-override"

    def test_version_override_propagates(self):
        meta = stamp_chunk_meta(
            "markdown",
            parser_versions={"markdown": "9.9.9"},
        )
        assert meta["parser_version"] == "9.9.9"


# ---- known_parser_names -----------------------------------------------------

class TestKnownParserNames:
    def test_returns_tuple(self):
        names = known_parser_names()
        assert isinstance(names, tuple)
        assert "markdown" in names

    def test_accepts_precomputed_versions(self):
        versions = compute_parser_versions(parser_versions={"markdown": "3.0"})
        names = known_parser_names(versions)
        assert "markdown" in names
        assert versions["markdown"].version == "3.0"
