"""P1-A domain 模型单元测试。"""
from __future__ import annotations

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


def test_source_from_locator_is_stable():
    s1 = Source.from_locator(SourceKind.FILE, "/tmp/a.md")
    s2 = Source.from_locator(SourceKind.FILE, "/tmp/A.md ")  # lowercased
    # locator 归一化后应一致
    assert s1.stable_id == s2.stable_id
    assert s1.kind == SourceKind.FILE
    assert len(s1.stable_id) == 16


def test_document_new_has_unique_id():
    d1 = Document.new("src1", "a.md")
    d2 = Document.new("src1", "a.md")
    assert d1.id != d2.id
    assert d1.status == DocumentStatus.DISCOVERED


def test_document_version_from_content_hash():
    v = DocumentVersion.from_content("d1", 1, "hello world")
    assert v.version_no == 1
    assert len(v.content_hash) == 64
    # 同样内容同样 hash
    v2 = DocumentVersion.from_content("d1", 2, "hello world")
    assert v2.content_hash == v.content_hash


def test_chunk_new_keeps_ordinal():
    c = Chunk.new("d1", "v1", 0, "para", start=10, end=20)
    assert c.ordinal == 0
    assert c.start_offset == 10
    assert c.end_offset == 20


def test_operation_kind_enum():
    assert OperationKind.INGEST.value == "ingest"
    assert OperationKind.TOMBSTONE.value == "tombstone"
    op = Operation.new(OperationKind.INGEST, "d1", "agent:test:adhoc",
                        {"k": "v"})
    assert op.kind == OperationKind.INGEST
    assert op.payload == {"k": "v"}


def test_knowledge_kind_covers_v1_types():
    names = {k.value for k in KnowledgeKind}
    # 覆盖 V1 store.TYPES
    for t in ("pitfall", "decision", "workflow", "standard",
              "preference", "bizrule", "reference"):
        assert t in names
