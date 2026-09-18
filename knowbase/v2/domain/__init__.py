"""V2 领域模型（P1-A）。

按 V2 计划 §6 / §11 设计的统一模型：
- Source:     数据来源（文件 / 远程 / API），唯一 stable id
- Document:   Source 内的一份文档（如一个 .md / .pdf / .docx）
- DocumentVersion: 不可变版本，按 SHA-256 内容哈希区分；同 doc 可有多个版本
- Chunk:      切分后的最小检索/索引单元
- Operation:  任何写动作的审计流（apply / ingest / promote / archive / …）
- OutboxEvent: 写后 outbox（Phase 4 启用）
- SyncRun:    同步任务运行记录（Phase 4 启用）

阶段：P1 仅落地核心；ACL、Outbox、SyncRun 的字段在对应 Phase 启用。
"""
from .models import (
    Chunk,
    Document,
    DocumentStatus,
    DocumentVersion,
    KnowledgeKind,
    Operation,
    OperationKind,
    OutboxEvent,
    Source,
    SourceKind,
    SyncRun,
)

__all__ = [
    "Source", "SourceKind",
    "Document", "DocumentStatus",
    "DocumentVersion",
    "Chunk",
    "Operation", "OperationKind",
    "OutboxEvent",
    "SyncRun",
    "KnowledgeKind",
]
