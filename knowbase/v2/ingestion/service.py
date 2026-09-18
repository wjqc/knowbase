"""V2 增量摄取核心服务（P1-C）。

特性：
- 幂等：相同 path + content_hash 的文档直接返回「无变化」
- version CAS：新内容作为新 version；旧 active version 标 superseded_by
- tombstone：locator 不再存在 / 显式调用时将 document.status 标 TOMBSTONED
- chunking：基于 ingestion.chunking；写入 document_chunk
- 审计：每次 ingest / tombstone 写一条 Operation
- features：受 v2_ingestion / v2_idempotent_ingest flag 控制（默认关）

使用：
    repo = V2Repository(repo_path)
    svc = IngestionService(repo)
    res = svc.ingest_file(Path("a.md"))
    if res.changed:
        ...
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from ..domain.models import (
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
from ..features import is_enabled
from ..observability.errors import NotConfiguredError, ParseError
from ..repositories import V2Repository
from .chunking import ChunkingConfig, split_into_chunks
from .parsers import (
    DocumentParser,
    ParserRegistry,
    ParsedDocument,
    registry,
    register_builtin,
)


@dataclass
class IngestionResult:
    doc_id: str
    version_id: str
    changed: bool
    chunks: int
    parser: str
    note: str = ""


class IngestionService:
    def __init__(self, repo: V2Repository, *, parsers: ParserRegistry | None = None,
                 chunk_cfg: ChunkingConfig | None = None,
                 auto_register: bool = True):
        if not is_enabled("v2_ingestion"):
            from ..observability.errors import FlagDisabledError
            raise FlagDisabledError("v2_ingestion")
        self.repo = repo
        self.parsers = parsers or registry()
        if auto_register and not list(self.parsers.parsers()):
            register_builtin()
        self.chunk_cfg = chunk_cfg or ChunkingConfig()

    # ---------- 公开入口 ----------

    def ingest_file(self, path: Path, *, source_kind: SourceKind = SourceKind.FILE,
                    by: str = "agent:system:v2-ingest",
                    doc_kind: KnowledgeKind | None = None) -> IngestionResult:
        path = path.resolve()
        if not path.exists():
            raise NotConfiguredError(f"file not found: {path}")

        parser = self.parsers.find(path)
        if parser is None:
            raise NotConfiguredError(
                f"no parser supports {path.suffix or '<no-ext>'}; "
                f"register one via register_builtin() / parsers.register()"
            )
        # 解析
        try:
            parsed = parser.parse(path)
        except ParseError as e:
            # 解析失败：标 ERROR 状态
            doc = self._ensure_document(path, source_kind, doc_kind, title=path.stem)
            self.repo.update_document_status(doc.id, DocumentStatus.ERROR)
            self.repo.record_operation(Operation.new(
                OperationKind.INGEST, doc.id, by,
                {"error": e.reason, "parser": parser.name, "path": str(path)},
            ))
            raise

        src = self._ensure_source(source_kind, str(path))
        doc = self._ensure_document(path, source_kind, doc_kind, src=src,
                                    title=parsed.meta.get("title", path.stem))

        # 幂等：相同内容直接返回
        if is_enabled("v2_idempotent_ingest"):
            active = self.repo.active_version(doc.id)
            if active and active.content_hash == self._hash_text(parsed.text):
                return IngestionResult(
                    doc_id=doc.id, version_id=active.id,
                    changed=False, chunks=len(self.repo.list_chunks(active.id)),
                    parser=parser.name, note="content unchanged",
                )

        # 创建新版本；旧 active 版本 superseded
        versions = self.repo.list_versions(doc.id)
        next_no = (versions[0].version_no + 1) if versions else 1
        new_ver = DocumentVersion.from_content(
            doc_id=doc.id, version_no=next_no, body=parsed.text, meta=parsed.meta,
        )
        new_ver = self.repo.add_version(new_ver)
        superseded = self.repo.supersede_versions(doc.id, new_ver.id)

        # 切块并写入
        chunks = split_into_chunks(parsed.text, self.chunk_cfg,
                                   doc_id=doc.id, version_id=new_ver.id)
        n_chunks = self.repo.replace_chunks(new_ver.id, chunks)

        # 状态推进
        self.repo.update_document_status(
            doc.id, DocumentStatus.READY, content_hash=new_ver.content_hash,
        )

        self.repo.record_operation(Operation.new(
            OperationKind.INGEST, doc.id, by,
            {
                "version_id": new_ver.id,
                "version_no": next_no,
                "superseded": superseded,
                "chunks": n_chunks,
                "parser": parser.name,
            },
        ))

        return IngestionResult(
            doc_id=doc.id, version_id=new_ver.id, changed=True,
            chunks=n_chunks, parser=parser.name,
            note=f"superseded {superseded} prior version(s)",
        )

    def tombstone(self, doc_id: str, by: str = "agent:system:v2-tombstone",
                  reason: str = "") -> None:
        if not is_enabled("v2_ingestion"):
            from ..observability.errors import FlagDisabledError
            raise FlagDisabledError("v2_ingestion")
        doc = self.repo.get_document(doc_id)
        if not doc:
            raise NotConfiguredError(f"document not found: {doc_id}")
        self.repo.update_document_status(doc_id, DocumentStatus.TOMBSTONED)
        self.repo.record_operation(Operation.new(
            OperationKind.TOMBSTONE, doc_id, by, {"reason": reason},
        ))

    def ingest_paths(self, paths: Iterable[Path], **kw) -> list[IngestionResult]:
        out: list[IngestionResult] = []
        for p in paths:
            try:
                out.append(self.ingest_file(p, **kw))
            except (ParseError, NotConfiguredError) as e:
                out.append(IngestionResult(
                    doc_id="", version_id="", changed=False, chunks=0, parser="",
                    note=f"error: {e}",
                ))
        return out

    # ---------- 内部 ----------

    @staticmethod
    def _hash_text(s: str) -> str:
        import hashlib
        return hashlib.sha256(s.encode("utf-8")).hexdigest()

    def _ensure_source(self, kind: SourceKind, locator: str) -> Source:
        existing = self.repo.find_source(kind, locator)
        if existing:
            return existing
        src = Source.from_locator(kind, locator)
        return self.repo.upsert_source(src)

    def _ensure_document(self, path: Path, source_kind: SourceKind,
                         doc_kind: KnowledgeKind | None, *,
                         src: Source | None = None,
                         title: str = "") -> Document:
        src = src or self._ensure_source(source_kind, str(path))
        existing = self.repo.find_document(src.stable_id, str(path))
        if existing:
            return existing
        doc = Document.new(source_id=src.stable_id, path=str(path), kind=doc_kind, title=title)
        # 初始状态由 service 流转；先入 DISCOVERED
        return self.repo.upsert_document(doc)
