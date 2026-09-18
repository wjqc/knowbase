"""P1-B / P1-C 切块 + 增量摄取单元测试。"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from knowbase.v2.domain import DocumentStatus
from knowbase.v2 import features as _features
from knowbase.v2.features import FlagRegistry
from knowbase.v2.ingestion import IngestionService
from knowbase.v2.ingestion.chunking import ChunkingConfig, split_into_chunks
from knowbase.v2.ingestion.parsers import register_builtin, registry
from knowbase.v2.repositories import V2Repository


@pytest.fixture
def repo(tmp_path: Path):
    return V2Repository(tmp_path)


@pytest.fixture
def v2_flags(monkeypatch):
    """启用 v2_ingestion + v2_idempotent_ingest；恢复全局。"""
    monkeypatch.setenv("KNOWBASE_V2_INGESTION", "true")
    monkeypatch.setenv("KNOWBASE_V2_IDEMPOTENT_INGEST", "true")
    _features._global = FlagRegistry()
    yield
    _features._global = FlagRegistry()


# ---------- chunking ----------

def test_split_short_text_into_single_chunk():
    text = "段落一。\n\n段落二。"
    out = split_into_chunks(text)
    assert len(out) >= 1
    assert "段落一" in out[0].text


def test_split_long_text_with_window():
    text = ("这是一段测试。" * 200)
    cfg = ChunkingConfig(max_chars=200, overlap=20, min_chars=10)
    out = split_into_chunks(text, cfg)
    assert len(out) >= 2
    # 全部 text 总长不超过原长 * 1.5（重叠开销）
    total = sum(len(c.text) for c in out)
    assert total >= len(text)
    assert total < len(text) * 2


def test_split_empty_text_returns_empty():
    assert split_into_chunks("") == []


# ---------- ingestion service ----------

def test_ingest_markdown_is_idempotent(tmp_path: Path, v2_flags, repo):
    register_builtin()
    f = tmp_path / "note.md"
    f.write_text("# Title\n\nbody text")
    svc = IngestionService(repo)
    r1 = svc.ingest_file(f)
    assert r1.changed is True
    assert r1.chunks >= 1
    # 再次摄取：内容相同 → changed=False
    r2 = svc.ingest_file(f)
    assert r2.changed is False
    assert r2.version_id == r1.version_id


def test_ingest_creates_new_version_on_content_change(tmp_path: Path, v2_flags, repo):
    register_builtin()
    f = tmp_path / "note.md"
    f.write_text("# v1\n\nbody")
    svc = IngestionService(repo)
    r1 = svc.ingest_file(f)
    f.write_text("# v2\n\nbody new")
    r2 = svc.ingest_file(f)
    assert r2.changed is True
    assert r2.version_id != r1.version_id
    versions = svc.repo.list_versions(r2.doc_id)
    assert len(versions) == 2
    active = svc.repo.active_version(r2.doc_id)
    assert active is not None and active.id == r2.version_id


def test_ingest_pdf_with_real_parser(tmp_path: Path, v2_flags, repo):
    register_builtin()
    pypdf = pytest.importorskip("pypdf")
    from pypdf import PdfWriter
    f = tmp_path / "doc.pdf"
    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    with open(f, "wb") as fp:
        w.write(fp)
    svc = IngestionService(repo)
    r = svc.ingest_file(f)
    assert r.parser == "pdf"
    # 空白 PDF 无文本 → chunks=0；只验解析成功 + 版本入库
    assert r.changed is True
    assert r.version_id != ""


def test_ingest_docx_with_real_parser(tmp_path: Path, v2_flags, repo):
    register_builtin()
    docx = pytest.importorskip("docx")
    from docx import Document as DocxDocument
    f = tmp_path / "doc.docx"
    d = DocxDocument()
    d.add_paragraph("第一段")
    d.add_paragraph("第二段")
    d.save(str(f))
    svc = IngestionService(repo)
    r = svc.ingest_file(f)
    assert r.parser == "docx"
    assert r.chunks >= 1


def test_ingest_unknown_extension_raises(tmp_path: Path, v2_flags, repo):
    register_builtin()
    f = tmp_path / "weird.xyz"
    f.write_text("x")
    svc = IngestionService(repo)
    from knowbase.v2.observability.errors import NotConfiguredError
    with pytest.raises(NotConfiguredError):
        svc.ingest_file(f)


def test_tombstone_marks_status(tmp_path: Path, v2_flags, repo):
    register_builtin()
    f = tmp_path / "a.md"
    f.write_text("# x\n\ny")
    svc = IngestionService(repo)
    r = svc.ingest_file(f)
    svc.tombstone(r.doc_id, reason="removed upstream")
    got = svc.repo.get_document(r.doc_id)
    assert got is not None
    assert got.status == DocumentStatus.TOMBSTONED
    ops = svc.repo.list_operations(r.doc_id)
    assert any(op.payload.get("reason") == "removed upstream" for op in ops)


def test_ingest_disabled_by_default(tmp_path: Path, monkeypatch, repo):
    """v2_ingestion 默认 False，应拒绝服务。"""
    from knowbase.v2.features import FlagRegistry
    monkeypatch.delenv("KNOWBASE_V2_INGESTION", raising=False)
    _features._global = FlagRegistry()
    f = tmp_path / "a.md"
    f.write_text("x")
    from knowbase.v2.observability.errors import FlagDisabledError
    with pytest.raises(FlagDisabledError):
        IngestionService(repo)
