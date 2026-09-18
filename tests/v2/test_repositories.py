"""P1-A V2 Repository 单元测试。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from knowbase.v2.domain import (
    Chunk,
    Document,
    DocumentStatus,
    DocumentVersion,
    KnowledgeKind,
    Operation,
    OperationKind,
    Source,
    SourceKind,
)
from knowbase.v2.repositories import V2Repository, init_v2_db


@pytest.fixture
def repo(tmp_path: Path):
    r = V2Repository(tmp_path)
    yield r
    r.close()


def test_init_creates_db_and_schema(tmp_path: Path):
    db = init_v2_db(tmp_path)
    assert db.exists()
    conn = sqlite3.connect(str(db))
    try:
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
        assert ver == 4  # P4-D: source_sync_state（Lite Profile 同步状态）
        # 所有核心表都存在
        for tbl in ("knowledge_source", "document", "document_version",
                    "document_chunk", "operation", "outbox_event", "sync_run",
                    # P3-A ACL 表
                    "organization", "project", "principal",
                    "membership", "project_membership"):
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (tbl,),
            ).fetchone()
            assert row is not None, f"missing table: {tbl}"
        # document 扩列存在
        cols = {r[1] for r in conn.execute("PRAGMA table_info(document)").fetchall()}
        for c in ("org_id", "project_id", "owner_id", "visibility"):
            assert c in cols, f"missing column: {c}"
    finally:
        conn.close()


def test_source_upsert_is_idempotent(repo: V2Repository):
    s1 = Source.from_locator(SourceKind.FILE, "/a.md")
    s2 = Source.from_locator(SourceKind.FILE, "/a.md")
    repo.upsert_source(s1)
    repo.upsert_source(s2)
    rows = repo._conn.execute("SELECT COUNT(*) FROM knowledge_source").fetchone()
    assert rows[0] == 1


def test_document_upsert_and_find_by_path(repo: V2Repository):
    src = repo.upsert_source(Source.from_locator(SourceKind.FILE, "/a.md"))
    doc = Document.new(source_id=src.stable_id, path="/a.md", kind=KnowledgeKind.PITFALL)
    repo.upsert_document(doc)
    found = repo.find_document(src.stable_id, "/a.md")
    assert found is not None
    assert found.id == doc.id
    assert found.kind == KnowledgeKind.PITFALL


def test_version_idempotent_on_same_hash(repo: V2Repository):
    src = repo.upsert_source(Source.from_locator(SourceKind.FILE, "/a.md"))
    doc = Document.new(source_id=src.stable_id, path="/a.md")
    repo.upsert_document(doc)
    v1 = DocumentVersion.from_content(doc.id, 1, "hello")
    v2 = DocumentVersion.from_content(doc.id, 2, "hello")
    added1 = repo.add_version(v1)
    added2 = repo.add_version(v2)  # 重复 hash，应返回已有
    assert added1.id == added2.id
    assert len(repo.list_versions(doc.id)) == 1


def test_version_cas_supersedes_active(repo: V2Repository):
    src = repo.upsert_source(Source.from_locator(SourceKind.FILE, "/a.md"))
    doc = Document.new(source_id=src.stable_id, path="/a.md")
    repo.upsert_document(doc)
    v1 = repo.add_version(DocumentVersion.from_content(doc.id, 1, "v1"))
    v2 = repo.add_version(DocumentVersion.from_content(doc.id, 2, "v2"))
    n = repo.supersede_versions(doc.id, v2.id)
    assert n == 1
    active = repo.active_version(doc.id)
    assert active is not None and active.id == v2.id


def test_replace_chunks_wipes_and_inserts(repo: V2Repository):
    src = repo.upsert_source(Source.from_locator(SourceKind.FILE, "/a.md"))
    doc = Document.new(source_id=src.stable_id, path="/a.md")
    repo.upsert_document(doc)
    v = repo.add_version(DocumentVersion.from_content(doc.id, 1, "x"))
    cs = [Chunk.new(doc.id, v.id, i, f"chunk-{i}") for i in range(3)]
    n = repo.replace_chunks(v.id, cs)
    assert n == 3
    n2 = repo.replace_chunks(v.id, cs[:1])
    assert n2 == 1
    assert len(repo.list_chunks(v.id)) == 1


def test_record_and_list_operations(repo: V2Repository):
    src = repo.upsert_source(Source.from_locator(SourceKind.FILE, "/a.md"))
    doc = Document.new(source_id=src.stable_id, path="/a.md")
    repo.upsert_document(doc)
    for i in range(3):
        repo.record_operation(Operation.new(OperationKind.INGEST, doc.id,
                                            f"agent:t:{i}", {"i": i}))
    ops = repo.list_operations(doc.id)
    assert len(ops) == 3
    assert ops[0].created_at >= ops[-1].created_at  # DESC


def test_update_document_status_records_hash(repo: V2Repository):
    src = repo.upsert_source(Source.from_locator(SourceKind.FILE, "/a.md"))
    doc = Document.new(source_id=src.stable_id, path="/a.md")
    repo.upsert_document(doc)
    repo.update_document_status(doc.id, DocumentStatus.READY, content_hash="abc")
    got = repo.get_document(doc.id)
    assert got is not None
    assert got.status == DocumentStatus.READY
    assert got.content_hash == "abc"


def test_v2_db_isolated_from_v1(tmp_path: Path):
    """V2 库文件独立；不应创建 V1 knowbase.db / index.db。"""
    V2Repository(tmp_path)
    db = tmp_path / ".knowbase" / "v2.db"
    assert db.exists()
    # 没有 V1 索引被创建
    assert not (tmp_path / "index.db").exists()
    assert not (tmp_path / ".knowbase" / "index.db").exists()
