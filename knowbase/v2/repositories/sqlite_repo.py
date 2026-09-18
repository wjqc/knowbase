"""V2 SQLite 仓储 CRUD（P1-A + P3-A + P4-A + P4-B）。

接口契约：
- V2Repository.connect() 自动 init_v2_db
- Source / Document / DocumentVersion / Chunk / Operation upsert + get + list
- upsert_*：按 (kind, locator) / (source_id, path) / (doc_id, content_hash) 等唯一键幂等
- 同步原语（P4-A）：operation / outbox_event / sync_run 状态机 + lease + 幂等
- 跨线程锁（P4-B）：SQLite connection + RLock 串行化，让 worker 通过
  ``loop.run_in_executor`` 安全访问；所有 SQL 经 ``_e()`` helper 自带 ``_lock``。

JSON 字段（meta / config / payload / stats）以 text(JSON) 存储；通过 loads/dumps 桥接。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Iterable

from ..domain.models import (
    Chunk,
    Document,
    DocumentStatus,
    DocumentVersion,
    Membership,
    Operation,
    OperationKind,
    OperationStatus,
    Organization,
    OutboxEvent,
    OutboxStatus,
    Principal,
    PrincipalKind,
    Project,
    ProjectMembership,
    Source,
    SourceKind,
    SourceSyncState,
    SyncRun,
    SyncRunStatus,
    SyncStatusReport,
)
from .schema import V2_DB_FILENAME, init_v2_db


def _loads(s: str | None) -> dict:
    if not s:
        return {}
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return {}


def _dumps(d: dict) -> str:
    return json.dumps(d or {}, ensure_ascii=False, separators=(",", ":"))


# ---------- 行 → dataclass 映射（P4-A module-level helper）----------

def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row_to_operation(row: tuple) -> Operation:
    return Operation(
        id=row[0], kind=OperationKind(row[1]), target_id=row[2], by=row[3],
        payload=_loads(row[4]), created_at=row[5],
        status=OperationStatus(row[6]), request_hash=row[7],
        result=_loads(row[8]) if row[8] else {}, error=row[9],
        attempts=row[10] or 0,
        lease_owner=row[11], lease_until=row[12],
        updated_at=row[13], completed_at=row[14],
    )


def _row_to_outbox(row: tuple) -> OutboxEvent:
    return OutboxEvent(
        id=row[0], topic=row[1], payload=_loads(row[2]),
        created_at=row[3], delivered_at=row[4], attempts=row[5] or 0,
        aggregate_type=row[6], aggregate_id=row[7], event_type=row[8],
        status=OutboxStatus(row[9]),
        next_attempt_at=row[10], lease_owner=row[11], lease_until=row[12],
        last_error=row[13], last_delivered_at=row[14],
    )


def _row_to_sync_run(row: tuple) -> SyncRun:
    return SyncRun(
        id=row[0], source_id=row[1],
        started_at=row[2], finished_at=row[3],
        stats=_loads(row[4]) if row[4] else {}, error=row[5],
        cursor_before=row[6], cursor_after=row[7],
        status=SyncRunStatus(row[8]),
        discovered=row[9] or 0, created_n=row[10] or 0,
        updated_n=row[11] or 0, deleted_n=row[12] or 0,
        failed_n=row[13] or 0,
        lease_owner=row[14], lease_until=row[15],
    )


def _row_to_sync_state(row: tuple) -> SourceSyncState:
    """P4-D：source_sync_state → SourceSyncState。"""
    return SourceSyncState(
        source_id=row[0],
        last_remote_check_at=row[1],
        last_remote_revision=row[2],
        local_revision=row[3],
        last_pull_at=row[4],
        last_push_at=row[5],
        last_failed_at=row[6],
        last_error=row[7],
        pending_ops=row[8] or 0,
        conflicted_docs=row[9] or 0,
        last_sync_run_id=row[10],
        updated_at=row[11],
    )


class V2Repository:
    """V2 仓储；单进程内复用同一连接。"""

    def __init__(self, repo: Path, db_path: Path | None = None):
        self.repo = repo
        self.db_path = db_path or (repo / ".knowbase" / V2_DB_FILENAME)
        if not self.db_path.exists():
            init_v2_db(repo, self.db_path)
        # check_same_thread=False 让连接可在 executor 线程使用；
        # 应用层通过 _lock 串行化所有 DB 操作，避免 SQLite "Recursive use of cursors not allowed"。
        self._conn = sqlite3.connect(
            str(self.db_path), isolation_level=None, check_same_thread=False
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.RLock()

    def _e(self, sql: str, params: tuple = ()):
        """带锁的执行（用于 worker 跨线程调用）。"""
        with self._lock:
            return self._conn.execute(sql, params)

    # ---------- lifecycle ----------

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __del__(self):
        self.close()

    # ---------- Source ----------

    def upsert_source(self, src: Source) -> Source:
        with self._conn:
            self._e(
                "INSERT OR IGNORE INTO knowledge_source(stable_id, kind, locator, config, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (src.stable_id, src.kind.value, src.locator, _dumps(src.config), src.created_at),
            )
        return self.get_source(src.stable_id) or src

    def get_source(self, stable_id: str) -> Source | None:
        row = self._e(
            "SELECT stable_id, kind, locator, config, created_at FROM knowledge_source WHERE stable_id=?",
            (stable_id,),
        ).fetchone()
        if not row:
            return None
        return Source(
            stable_id=row[0], kind=SourceKind(row[1]), locator=row[2],
            config=_loads(row[3]), created_at=row[4],
        )

    def find_source(self, kind: SourceKind, locator: str) -> Source | None:
        row = self._e(
            "SELECT stable_id, kind, locator, config, created_at FROM knowledge_source "
            "WHERE kind=? AND locator=?",
            (kind.value, locator),
        ).fetchone()
        if not row:
            return None
        return Source(
            stable_id=row[0], kind=SourceKind(row[1]), locator=row[2],
            config=_loads(row[3]), created_at=row[4],
        )

    # ---------- Document ----------

    def upsert_document(self, doc: Document) -> Document:
        with self._conn:
            self._e(
                "INSERT OR REPLACE INTO document("
                "id, source_id, path, kind, title, content_hash, status, meta, "
                "created_at, updated_at, org_id, project_id, owner_id, visibility) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    doc.id, doc.source_id, doc.path,
                    doc.kind.value if doc.kind else None,
                    doc.title, doc.content_hash, doc.status.value,
                    _dumps(doc.meta), doc.created_at, doc.updated_at,
                    doc.org_id, doc.project_id, doc.owner_id, doc.visibility,
                ),
            )
        return self.get_document(doc.id) or doc

    def get_document(self, doc_id: str) -> Document | None:
        row = self._e(
            "SELECT id, source_id, path, kind, title, content_hash, status, meta, "
            "created_at, updated_at, org_id, project_id, owner_id, visibility "
            "FROM document WHERE id=?",
            (doc_id,),
        ).fetchone()
        if not row:
            return None
        from ..domain.models import KnowledgeKind
        return Document(
            id=row[0], source_id=row[1], path=row[2],
            kind=KnowledgeKind(row[3]) if row[3] else None,
            title=row[4], content_hash=row[5],
            status=DocumentStatus(row[6]),
            meta=_loads(row[7]), created_at=row[8], updated_at=row[9],
            org_id=row[10], project_id=row[11],
            owner_id=row[12], visibility=row[13] or "organization-global",
        )

    def find_document(self, source_id: str, path: str) -> Document | None:
        row = self._e(
            "SELECT id, source_id, path, kind, title, content_hash, status, meta, "
            "created_at, updated_at, org_id, project_id, owner_id, visibility "
            "FROM document WHERE source_id=? AND path=?",
            (source_id, path),
        ).fetchone()
        if not row:
            return None
        from ..domain.models import KnowledgeKind
        return Document(
            id=row[0], source_id=row[1], path=row[2],
            kind=KnowledgeKind(row[3]) if row[3] else None,
            title=row[4], content_hash=row[5],
            status=DocumentStatus(row[6]),
            meta=_loads(row[7]), created_at=row[8], updated_at=row[9],
            org_id=row[10], project_id=row[11],
            owner_id=row[12], visibility=row[13] or "organization-global",
        )

    def update_document_status(self, doc_id: str, status: DocumentStatus,
                                content_hash: str = "") -> None:
        if content_hash:
            self._e(
                "UPDATE document SET status=?, content_hash=?, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE id=?",
                (status.value, content_hash, doc_id),
            )
        else:
            self._e(
                "UPDATE document SET status=?, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
                (status.value, doc_id),
            )

    def list_documents(self, source_id: str | None = None,
                       status: DocumentStatus | None = None) -> list[Document]:
        clauses, args = [], []
        if source_id:
            clauses.append("source_id=?")
            args.append(source_id)
        if status:
            clauses.append("status=?")
            args.append(status.value)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self._e(
            f"SELECT id, source_id, path, kind, title, content_hash, status, meta, "
            f"created_at, updated_at, org_id, project_id, owner_id, visibility "
            f"FROM document {where} ORDER BY updated_at DESC", tuple(args),
        ).fetchall()
        from ..domain.models import KnowledgeKind
        out = []
        for r in rows:
            out.append(Document(
                id=r[0], source_id=r[1], path=r[2],
                kind=KnowledgeKind(r[3]) if r[3] else None,
                title=r[4], content_hash=r[5], status=DocumentStatus(r[6]),
                meta=_loads(r[7]), created_at=r[8], updated_at=r[9],
                org_id=r[10], project_id=r[11],
                owner_id=r[12], visibility=r[13] or "organization-global",
            ))
        return out

    # ---------- DocumentVersion ----------

    def add_version(self, ver: DocumentVersion) -> DocumentVersion:
        """幂等：相同 (doc_id, content_hash) 已存在则直接返回。"""
        row = self._e(
            "SELECT id FROM document_version WHERE doc_id=? AND content_hash=?",
            (ver.doc_id, ver.content_hash),
        ).fetchone()
        if row:
            return self.get_version(row[0]) or ver
        with self._conn:
            self._e(
                "INSERT INTO document_version("
                "id, doc_id, version_no, content_hash, body, meta, created_at, superseded_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (ver.id, ver.doc_id, ver.version_no, ver.content_hash, ver.body,
                 _dumps(ver.meta), ver.created_at, ver.superseded_by),
            )
        return self.get_version(ver.id) or ver

    def get_version(self, ver_id: str) -> DocumentVersion | None:
        row = self._e(
            "SELECT id, doc_id, version_no, content_hash, body, meta, created_at, superseded_by "
            "FROM document_version WHERE id=?", (ver_id,),
        ).fetchone()
        if not row:
            return None
        return DocumentVersion(
            id=row[0], doc_id=row[1], version_no=row[2], content_hash=row[3],
            body=row[4], meta=_loads(row[5]), created_at=row[6], superseded_by=row[7],
        )

    def active_version(self, doc_id: str) -> DocumentVersion | None:
        """返回未被取代的最新版本（superseded_by IS NULL）。"""
        row = self._e(
            "SELECT id, doc_id, version_no, content_hash, body, meta, created_at, superseded_by "
            "FROM document_version WHERE doc_id=? AND superseded_by IS NULL "
            "ORDER BY version_no DESC LIMIT 1",
            (doc_id,),
        ).fetchone()
        if not row:
            return None
        return DocumentVersion(
            id=row[0], doc_id=row[1], version_no=row[2], content_hash=row[3],
            body=row[4], meta=_loads(row[5]), created_at=row[6], superseded_by=row[7],
        )

    def supersede_versions(self, doc_id: str, new_version_id: str) -> int:
        """将所有活跃版本标记为被 new_version_id 取代（排除 new_version_id 自身）；返回受影响行数。"""
        with self._conn:
            cur = self._e(
                "UPDATE document_version SET superseded_by=? "
                "WHERE doc_id=? AND superseded_by IS NULL AND id != ?",
                (new_version_id, doc_id, new_version_id),
            )
        return cur.rowcount

    def list_versions(self, doc_id: str) -> list[DocumentVersion]:
        rows = self._e(
            "SELECT id, doc_id, version_no, content_hash, body, meta, created_at, superseded_by "
            "FROM document_version WHERE doc_id=? ORDER BY version_no DESC", (doc_id,),
        ).fetchall()
        return [DocumentVersion(
            id=r[0], doc_id=r[1], version_no=r[2], content_hash=r[3],
            body=r[4], meta=_loads(r[5]), created_at=r[6], superseded_by=r[7],
        ) for r in rows]

    # ---------- Chunk ----------

    def replace_chunks(self, version_id: str, chunks: Iterable[Chunk]) -> int:
        """幂等：先删后插。返回写入条数。"""
        n = 0
        with self._conn:
            self._e("DELETE FROM document_chunk WHERE version_id=?", (version_id,))
            for c in chunks:
                self._e(
                    "INSERT INTO document_chunk("
                    "id, doc_id, version_id, ordinal, text, start_offset, end_offset, meta, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (c.id, c.doc_id, c.version_id, c.ordinal, c.text,
                     c.start_offset, c.end_offset, _dumps(c.meta), c.created_at),
                )
                n += 1
        return n

    def list_chunks(self, version_id: str) -> list[Chunk]:
        rows = self._e(
            "SELECT id, doc_id, version_id, ordinal, text, start_offset, end_offset, meta, created_at "
            "FROM document_chunk WHERE version_id=? ORDER BY ordinal", (version_id,),
        ).fetchall()
        return [Chunk(
            id=r[0], doc_id=r[1], version_id=r[2], ordinal=r[3], text=r[4],
            start_offset=r[5], end_offset=r[6], meta=_loads(r[7]), created_at=r[8],
        ) for r in rows]

    # ---------- Operation（P4-A 状态机 + 幂等 + lease）----------

    def record_operation(self, op: Operation) -> None:
        """插入新 operation；默认 status=PENDING（兼容旧调用方仍填 succeeded）。"""
        with self._conn:
            self._e(
                "INSERT INTO operation("
                "id, kind, target_id, by_actor, payload, created_at, "
                "status, request_hash, result, error, attempts, "
                "lease_owner, lease_until, updated_at, completed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (op.id, op.kind.value, op.target_id, op.by,
                 _dumps(op.payload), op.created_at,
                 op.status.value, op.request_hash, _dumps(op.result), op.error,
                 op.attempts, op.lease_owner, op.lease_until,
                 op.updated_at, op.completed_at),
            )

    def get_operation(self, op_id: str) -> Operation | None:
        row = self._e(
            "SELECT id, kind, target_id, by_actor, payload, created_at, "
            "status, request_hash, result, error, attempts, "
            "lease_owner, lease_until, updated_at, completed_at "
            "FROM operation WHERE id=?", (op_id,),
        ).fetchone()
        if not row:
            return None
        return _row_to_operation(row)

    def find_operation_by_hash(self, request_hash: str, by: str,
                                target_id: str) -> Operation | None:
        """幂等查重：相同 (request_hash, by, target_id) 视为重复 operation。"""
        row = self._e(
            "SELECT id, kind, target_id, by_actor, payload, created_at, "
            "status, request_hash, result, error, attempts, "
            "lease_owner, lease_until, updated_at, completed_at "
            "FROM operation WHERE request_hash=? AND by_actor=? AND target_id=? "
            "ORDER BY created_at DESC LIMIT 1",
            (request_hash, by, target_id),
        ).fetchone()
        return _row_to_operation(row) if row else None

    def list_operations(self, target_id: str, limit: int = 50) -> list[Operation]:
        rows = self._e(
            "SELECT id, kind, target_id, by_actor, payload, created_at, "
            "status, request_hash, result, error, attempts, "
            "lease_owner, lease_until, updated_at, completed_at "
            "FROM operation WHERE target_id=? ORDER BY created_at DESC LIMIT ?",
            (target_id, limit),
        ).fetchall()
        return [_row_to_operation(r) for r in rows]

    def list_operations_by_status(self, status: OperationStatus,
                                    limit: int = 100) -> list[Operation]:
        rows = self._e(
            "SELECT id, kind, target_id, by_actor, payload, created_at, "
            "status, request_hash, result, error, attempts, "
            "lease_owner, lease_until, updated_at, completed_at "
            "FROM operation WHERE status=? ORDER BY created_at LIMIT ?",
            (status.value, limit),
        ).fetchall()
        return [_row_to_operation(r) for r in rows]

    def claim_operation(self, op_id: str, owner: str, lease_until: str) -> bool:
        """原子地将 status=PENDING → RUNNING 并设置 lease。

        返回是否成功抢占。已被抢占或已终结的 op 返回 False。
        """
        cur = self._e(
            "UPDATE operation SET status='running', lease_owner=?, lease_until=?, "
            "attempts=attempts+1, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE id=? AND status='pending'",
            (owner, lease_until, op_id),
        )
        return cur.rowcount > 0

    def renew_operation_lease(self, op_id: str, owner: str,
                                new_until: str) -> bool:
        """心跳续约：仅持有 lease_owner 的 worker 才能延长 RUNNING op 的 lease_until。"""
        cur = self._e(
            "UPDATE operation SET lease_until=?, "
            "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE id=? AND status='running' AND lease_owner=?",
            (new_until, op_id, owner),
        )
        return cur.rowcount > 0

    def complete_operation(self, op_id: str, *, result: dict | None = None) -> bool:
        """RUNNING → SUCCEEDED 并清 lease + 写 completed_at。"""
        cur = self._e(
            "UPDATE operation SET status='succeeded', result=?, "
            "lease_owner=NULL, lease_until=NULL, "
            "completed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
            "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE id=? AND status='running'",
            (_dumps(result or {}), op_id),
        )
        return cur.rowcount > 0

    def fail_operation(self, op_id: str, error: str, *,
                         max_attempts: int = 3) -> bool:
        """RUNNING → FAILED 或 PENDING（未达 max_attempts）允许重试。

        - attempts >= max_attempts：标记 FAILED（终端）
        - attempts < max_attempts：保持 PENDING，释放 lease，下次可被重新 claim
        """
        op = self.get_operation(op_id)
        if op is None or op.status != OperationStatus.RUNNING:
            return False
        if op.attempts >= max_attempts:
            self._e(
                "UPDATE operation SET status='failed', error=?, "
                "lease_owner=NULL, lease_until=NULL, "
                "completed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE id=? AND status='running'",
                (error, op_id),
            )
        else:
            self._e(
                "UPDATE operation SET status='pending', error=?, "
                "lease_owner=NULL, lease_until=NULL, "
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE id=? AND status='running'",
                (error, op_id),
            )
        return True

    def mark_operation_unknown(self, op_id: str) -> bool:
        """将 RUNNING 标记为 UNKNOWN（worker 崩溃后无法确定结果）。

        必须由监控或人工定期扫描 lease_until 过期的 RUNNING op 触发。
        """
        cur = self._e(
            "UPDATE operation SET status='unknown', "
            "lease_owner=NULL, lease_until=NULL, "
            "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE id=? AND status='running'",
            (op_id,),
        )
        return cur.rowcount > 0

    # ---------- Outbox（P4-A 状态机 + lease + 退避）----------

    def enqueue_outbox(self, ev: OutboxEvent) -> None:
        """插入 outbox 事件；默认 status=PENDING。"""
        with self._conn:
            self._e(
                "INSERT INTO outbox_event("
                "id, topic, payload, created_at, delivered_at, attempts, "
                "aggregate_type, aggregate_id, event_type, status, "
                "next_attempt_at, lease_owner, lease_until, last_error, last_delivered_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ev.id, ev.topic, _dumps(ev.payload), ev.created_at, ev.delivered_at,
                 ev.attempts, ev.aggregate_type, ev.aggregate_id, ev.event_type,
                 ev.status.value, ev.next_attempt_at, ev.lease_owner, ev.lease_until,
                 ev.last_error, ev.last_delivered_at),
            )

    def list_outbox_due(self, *, now: str | None = None,
                        limit: int = 50) -> list[OutboxEvent]:
        """返回 status=PENDING 且 next_attempt_at <= now 的事件。

        - next_attempt_at 为 NULL 视为「立即可派发」
        - lease_until > now 视为「已被其它 worker 抢占」
        """
        rows = self._e(
            "SELECT id, topic, payload, created_at, delivered_at, attempts, "
            "aggregate_type, aggregate_id, event_type, status, "
            "next_attempt_at, lease_owner, lease_until, last_error, last_delivered_at "
            "FROM outbox_event "
            "WHERE status='pending' "
            "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) "
            "AND (lease_until IS NULL OR lease_until <= ?) "
            "ORDER BY created_at LIMIT ?",
            (now or _now_iso(), now or _now_iso(), limit),
        ).fetchall()
        return [_row_to_outbox(r) for r in rows]

    def claim_outbox(self, ev_id: str, owner: str, lease_until: str) -> bool:
        """原子地 claim 一个 outbox 事件（status=PENDING → 持有 lease 但保持 PENDING）。

        只有当现有 lease 已过期（lease_until < 当前 lease_until）才能重新抢占。
        """
        cur = self._e(
            "UPDATE outbox_event SET lease_owner=?, lease_until=? "
            "WHERE id=? AND status='pending' "
            "AND (lease_until IS NULL OR lease_until < ?)",
            (owner, lease_until, ev_id, lease_until),
        )
        return cur.rowcount > 0

    def renew_outbox_lease(self, ev_id: str, owner: str,
                            new_until: str) -> bool:
        """心跳续约：仅持有 lease_owner 的 worker 才能延长 lease_until。

        返回 False 表示该 worker 已不持有 lease（被抢占或已终结）。
        """
        cur = self._e(
            "UPDATE outbox_event SET lease_until=? "
            "WHERE id=? AND status='pending' AND lease_owner=?",
            (new_until, ev_id, owner),
        )
        return cur.rowcount > 0

    def ack_outbox(self, ev_id: str) -> bool:
        """PENDING → DISPATCHED；成功投递终态。

        必须先 claim（lease_owner IS NOT NULL）才能 ack，避免重复投递。
        """
        cur = self._e(
            "UPDATE outbox_event SET status='dispatched', "
            "delivered_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
            "last_delivered_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
            "lease_owner=NULL, lease_until=NULL "
            "WHERE id=? AND status='pending' AND lease_owner IS NOT NULL",
            (ev_id,),
        )
        return cur.rowcount > 0

    def nack_outbox(self, ev_id: str, error: str, *,
                    next_attempt_at: str | None = None,
                    max_attempts: int = 5) -> bool:
        """投递失败：未达 max_attempts → 保持 PENDING + 退避；达上限 → FAILED（死信）。"""
        ev = self.get_outbox(ev_id)
        if ev is None or ev.status != OutboxStatus.PENDING:
            return False
        new_attempts = ev.attempts + 1
        if new_attempts >= max_attempts:
            self._e(
                "UPDATE outbox_event SET status='failed', last_error=?, "
                "lease_owner=NULL, lease_until=NULL "
                "WHERE id=? AND status='pending'",
                (error, ev_id),
            )
        else:
            self._e(
                "UPDATE outbox_event SET attempts=?, last_error=?, next_attempt_at=?, "
                "lease_owner=NULL, lease_until=NULL "
                "WHERE id=? AND status='pending'",
                (new_attempts, error, next_attempt_at, ev_id),
            )
        return True

    def get_outbox(self, ev_id: str) -> OutboxEvent | None:
        row = self._e(
            "SELECT id, topic, payload, created_at, delivered_at, attempts, "
            "aggregate_type, aggregate_id, event_type, status, "
            "next_attempt_at, lease_owner, lease_until, last_error, last_delivered_at "
            "FROM outbox_event WHERE id=?", (ev_id,),
        ).fetchone()
        return _row_to_outbox(row) if row else None

    # ---------- SyncRun（P4-A 状态机 + cursor + lease）----------

    def record_sync_run(self, run: SyncRun) -> None:
        """插入 sync_run；默认 status=RUNNING。"""
        with self._conn:
            self._e(
                "INSERT INTO sync_run("
                "id, source_id, started_at, finished_at, stats, error, "
                "cursor_before, cursor_after, status, "
                "discovered, created_n, updated_n, deleted_n, failed_n, "
                "lease_owner, lease_until) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run.id, run.source_id, run.started_at, run.finished_at,
                 _dumps(run.stats), run.error,
                 run.cursor_before, run.cursor_after, run.status.value,
                 run.discovered, run.created_n, run.updated_n,
                 run.deleted_n, run.failed_n,
                 run.lease_owner, run.lease_until),
            )

    def get_sync_run(self, run_id: str) -> SyncRun | None:
        row = self._e(
            "SELECT id, source_id, started_at, finished_at, stats, error, "
            "cursor_before, cursor_after, status, "
            "discovered, created_n, updated_n, deleted_n, failed_n, "
            "lease_owner, lease_until "
            "FROM sync_run WHERE id=?", (run_id,),
        ).fetchone()
        return _row_to_sync_run(row) if row else None

    def claim_sync_run(self, run_id: str, owner: str, lease_until: str) -> bool:
        """抢占一个 sync_run（保持 RUNNING + 设置 lease；用于接管中断的实例）。"""
        cur = self._e(
            "UPDATE sync_run SET lease_owner=?, lease_until=? "
            "WHERE id=? AND status='running' "
            "AND (lease_until IS NULL OR lease_until <= ?)",
            (owner, lease_until, run_id, lease_until),
        )
        return cur.rowcount > 0

    def complete_sync_run(self, run_id: str, *, cursor_after: str,
                            discovered: int = 0, created_n: int = 0,
                            updated_n: int = 0, deleted_n: int = 0,
                            failed_n: int = 0,
                            stats: dict | None = None) -> bool:
        """RUNNING → SUCCEEDED 并推进 cursor + 写计数。"""
        cur = self._e(
            "UPDATE sync_run SET status='succeeded', cursor_after=?, "
            "discovered=?, created_n=?, updated_n=?, deleted_n=?, failed_n=?, "
            "stats=?, finished_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
            "lease_owner=NULL, lease_until=NULL "
            "WHERE id=? AND status='running'",
            (cursor_after, discovered, created_n, updated_n, deleted_n,
             failed_n, _dumps(stats or {}), run_id),
        )
        return cur.rowcount > 0

    def conflict_sync_run(self, run_id: str, error: str) -> bool:
        """RUNNING → CONFLICTED（保留双方版本，不自动覆盖；终端）。"""
        cur = self._e(
            "UPDATE sync_run SET status='conflicted', error=?, "
            "finished_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
            "lease_owner=NULL, lease_until=NULL "
            "WHERE id=? AND status='running'",
            (error, run_id),
        )
        return cur.rowcount > 0

    def fail_sync_run(self, run_id: str, error: str) -> bool:
        """RUNNING → FAILED（非冲突失败；终端）。"""
        cur = self._e(
            "UPDATE sync_run SET status='failed', error=?, "
            "finished_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
            "lease_owner=NULL, lease_until=NULL "
            "WHERE id=? AND status='running'",
            (error, run_id),
        )
        return cur.rowcount > 0

    def list_sync_runs_by_source(self, source_id: str, limit: int = 50) -> list[SyncRun]:
        rows = self._e(
            "SELECT id, source_id, started_at, finished_at, stats, error, "
            "cursor_before, cursor_after, status, "
            "discovered, created_n, updated_n, deleted_n, failed_n, "
            "lease_owner, lease_until "
            "FROM sync_run WHERE source_id=? "
            "ORDER BY started_at DESC LIMIT ?",
            (source_id, limit),
        ).fetchall()
        return [_row_to_sync_run(r) for r in rows]

    # ---------- SourceSyncState（P4-D Lite Profile）----------

    def upsert_sync_state(self, state: SourceSyncState) -> SourceSyncState:
        """插入或更新 source_sync_state；按 source_id PK 幂等。"""
        with self._conn:
            self._e(
                "INSERT OR REPLACE INTO source_sync_state("
                "source_id, last_remote_check_at, last_remote_revision, local_revision, "
                "last_pull_at, last_push_at, last_failed_at, last_error, "
                "pending_ops, conflicted_docs, last_sync_run_id, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (state.source_id, state.last_remote_check_at, state.last_remote_revision,
                 state.local_revision, state.last_pull_at, state.last_push_at,
                 state.last_failed_at, state.last_error, state.pending_ops,
                 state.conflicted_docs, state.last_sync_run_id, state.updated_at),
            )
        return self.get_sync_state(state.source_id) or state

    def get_sync_state(self, source_id: str) -> SourceSyncState | None:
        row = self._e(
            "SELECT source_id, last_remote_check_at, last_remote_revision, "
            "local_revision, last_pull_at, last_push_at, last_failed_at, "
            "last_error, pending_ops, conflicted_docs, last_sync_run_id, updated_at "
            "FROM source_sync_state WHERE source_id=?",
            (source_id,),
        ).fetchone()
        return _row_to_sync_state(row) if row else None

    def update_sync_state_fields(self, source_id: str, **fields: Any) -> bool:
        """按字段名更新 source_sync_state；未指定字段保持原值。

        允许字段：last_remote_check_at / last_remote_revision / local_revision /
                  last_pull_at / last_push_at / last_failed_at / last_error /
                  pending_ops / conflicted_docs / last_sync_run_id。

        返回 True 表示更新成功；不存在该 source 返回 False。
        """
        allowed = {
            "last_remote_check_at", "last_remote_revision", "local_revision",
            "last_pull_at", "last_push_at", "last_failed_at", "last_error",
            "pending_ops", "conflicted_docs", "last_sync_run_id",
        }
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"unsupported sync_state fields: {sorted(bad)}")
        if not fields:
            return self.get_sync_state(source_id) is not None
        sets = ", ".join(f"{k}=?" for k in fields)
        params = list(fields.values())
        params.extend([source_id])
        cur = self._e(
            f"UPDATE source_sync_state SET {sets}, "
            f"updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE source_id=?",
            tuple(params),
        )
        return cur.rowcount > 0

    def list_sync_states(self) -> list[SourceSyncState]:
        rows = self._e(
            "SELECT source_id, last_remote_check_at, last_remote_revision, "
            "local_revision, last_pull_at, last_push_at, last_failed_at, "
            "last_error, pending_ops, conflicted_docs, last_sync_run_id, updated_at "
            "FROM source_sync_state ORDER BY source_id"
        ).fetchall()
        return [_row_to_sync_state(r) for r in rows]

    def count_pending_outbox(self, source_id: str) -> int:
        """该 source 关联的未完成 outbox 事件数（pending + dispatched）。"""
        # outbox.aggregate_id 形如 source:<stable_id>:<doc_id>，或直接的 doc_id
        # 这里简化：按 source_id 关联到 document → aggregate_id = doc_id
        rows = self._e(
            "SELECT COUNT(*) FROM outbox_event o "
            "WHERE o.status IN ('pending', 'dispatched') "
            "AND EXISTS (SELECT 1 FROM document d "
            "             WHERE d.id = COALESCE(o.aggregate_id, '') "
            "             AND d.source_id = ?)",
            (source_id,),
        ).fetchone()
        return int(rows[0]) if rows else 0

    def count_conflicted_docs(self, source_id: str) -> int:
        """该 source 当前处于冲突态的 doc 数（status=error 且 meta.conflict）。"""
        rows = self._e(
            "SELECT COUNT(*) FROM document WHERE source_id=? "
            "AND (status='error' OR json_extract(meta, '$.conflict') = 1)",
            (source_id,),
        ).fetchone()
        return int(rows[0]) if rows else 0

    def compute_sync_status(self, source_id: str, *,
                           staleness_sla_seconds: int = 60) -> SyncStatusReport:
        """聚合 sync_status：state + 实时 outbox + 实时冲突 + 最近 sync_run。

        - is_stale: last_remote_check_at + sla < now
        - behind_count / ahead_count: 用 last_remote_revision 与 local_revision 比对
          （state 字段；不在 repo 算 ancestry，避免额外 git 调用）
        - diverged: behind > 0 AND ahead > 0
        - pending_ops: 实时查 outbox
        - conflicted_docs: 实时查 document
        - last_sync_run_id / status: 从 state + sync_run join
        """
        state = self.get_sync_state(source_id)
        pending_ops = self.count_pending_outbox(source_id)
        conflicted_docs = self.count_conflicted_docs(source_id)

        # 落败 / 滞后的判定
        now = _now_iso()
        is_stale = True
        if state and state.last_remote_check_at:
            try:
                from datetime import datetime, timezone
                t_check = datetime.strptime(
                    state.last_remote_check_at, "%Y-%m-%dT%H:%M:%fZ"
                ).replace(tzinfo=timezone.utc)
                t_now = datetime.strptime(now, "%Y-%m-%dT%H:%M:%fZ").replace(
                    tzinfo=timezone.utc
                )
                is_stale = (t_now - t_check).total_seconds() > staleness_sla_seconds
            except ValueError:
                is_stale = True

        # 计数：behind / ahead（基于 state 上次 fetch 的 revision；不在此处调 git）
        # 若 state 未记录 revision，记 0
        behind = ahead = 0
        # diverged / behind / ahead 仅在 state 字段层面有意义；不在 repo 算 ancestry
        # 这里只基于字段是否有变更做粗略标记，留给 LiteSyncClient 在 sync 时维护
        diverged = bool(
            state and state.last_failed_at and
            (state.last_remote_revision != state.local_revision) and
            state.last_remote_revision and state.local_revision
        )

        # 最近 sync_run
        last_run_id = state.last_sync_run_id if state else None
        last_run_status: str | None = None
        if last_run_id:
            run = self.get_sync_run(last_run_id)
            if run is not None:
                last_run_status = run.status.value

        return SyncStatusReport(
            source_id=source_id,
            local_revision=state.local_revision if state else None,
            remote_revision=state.last_remote_revision if state else None,
            behind_count=behind,
            ahead_count=ahead,
            diverged=diverged,
            last_remote_check_at=state.last_remote_check_at if state else None,
            last_pull_at=state.last_pull_at if state else None,
            last_push_at=state.last_push_at if state else None,
            last_failed_at=state.last_failed_at if state else None,
            last_error=state.last_error if state else None,
            pending_ops=pending_ops,
            conflicted_docs=conflicted_docs,
            is_stale=is_stale,
            last_sync_run_id=last_run_id,
            last_sync_run_status=last_run_status,
        )

    # ---------- Organization（P3-A ACL）----------

    def upsert_organization(self, org: Organization) -> Organization:
        with self._conn:
            self._e(
                "INSERT OR REPLACE INTO organization(id, name, slug, created_at) "
                "VALUES (?, ?, ?, ?)",
                (org.id, org.name, org.slug, org.created_at),
            )
        return self.get_organization(org.id) or org

    def get_organization(self, org_id: str) -> Organization | None:
        row = self._e(
            "SELECT id, name, slug, created_at FROM organization WHERE id=?",
            (org_id,),
        ).fetchone()
        if not row:
            return None
        return Organization(id=row[0], name=row[1], slug=row[2], created_at=row[3])

    def find_organization_by_slug(self, slug: str) -> Organization | None:
        row = self._e(
            "SELECT id, name, slug, created_at FROM organization WHERE slug=?",
            (slug,),
        ).fetchone()
        if not row:
            return None
        return Organization(id=row[0], name=row[1], slug=row[2], created_at=row[3])

    def list_organizations(self) -> list[Organization]:
        rows = self._e(
            "SELECT id, name, slug, created_at FROM organization ORDER BY created_at"
        ).fetchall()
        return [Organization(id=r[0], name=r[1], slug=r[2], created_at=r[3]) for r in rows]

    # ---------- Project ----------

    def upsert_project(self, proj: Project) -> Project:
        with self._conn:
            self._e(
                "INSERT OR REPLACE INTO project(id, org_id, name, slug, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (proj.id, proj.org_id, proj.name, proj.slug, proj.created_at),
            )
        return self.get_project(proj.id) or proj

    def get_project(self, project_id: str) -> Project | None:
        row = self._e(
            "SELECT id, org_id, name, slug, created_at FROM project WHERE id=?",
            (project_id,),
        ).fetchone()
        if not row:
            return None
        return Project(id=row[0], org_id=row[1], name=row[2], slug=row[3], created_at=row[4])

    def find_project_by_slug(self, org_id: str, slug: str) -> Project | None:
        row = self._e(
            "SELECT id, org_id, name, slug, created_at FROM project WHERE org_id=? AND slug=?",
            (org_id, slug),
        ).fetchone()
        if not row:
            return None
        return Project(id=row[0], org_id=row[1], name=row[2], slug=row[3], created_at=row[4])

    def list_projects(self, org_id: str | None = None) -> list[Project]:
        if org_id:
            rows = self._e(
                "SELECT id, org_id, name, slug, created_at FROM project WHERE org_id=? ORDER BY created_at",
                (org_id,),
            ).fetchall()
        else:
            rows = self._e(
                "SELECT id, org_id, name, slug, created_at FROM project ORDER BY created_at"
            ).fetchall()
        return [Project(id=r[0], org_id=r[1], name=r[2], slug=r[3], created_at=r[4]) for r in rows]

    # ---------- Principal ----------

    def upsert_principal(self, p: Principal) -> Principal:
        with self._conn:
            self._e(
                "INSERT OR REPLACE INTO principal(id, display_name, kind, created_at) "
                "VALUES (?, ?, ?, ?)",
                (p.id, p.display_name, p.kind.value, p.created_at),
            )
        return self.get_principal(p.id) or p

    def get_principal(self, principal_id: str) -> Principal | None:
        row = self._e(
            "SELECT id, display_name, kind, created_at FROM principal WHERE id=?",
            (principal_id,),
        ).fetchone()
        if not row:
            return None
        return Principal(
            id=row[0], display_name=row[1],
            kind=PrincipalKind(row[2]), created_at=row[3],
        )

    def list_principals(self) -> list[Principal]:
        rows = self._e(
            "SELECT id, display_name, kind, created_at FROM principal ORDER BY created_at"
        ).fetchall()
        return [
            Principal(id=r[0], display_name=r[1],
                      kind=PrincipalKind(r[2]), created_at=r[3])
            for r in rows
        ]

    # ---------- Membership（组织）----------

    def upsert_membership(self, m: Membership) -> Membership:
        with self._conn:
            self._e(
                "INSERT OR REPLACE INTO membership(principal_id, org_id, role) VALUES (?, ?, ?)",
                (m.principal_id, m.org_id, m.role),
            )
        return self.get_membership(m.principal_id, m.org_id) or m

    def get_membership(self, principal_id: str, org_id: str) -> Membership | None:
        row = self._e(
            "SELECT id, principal_id, org_id, role FROM membership WHERE principal_id=? AND org_id=?",
            (principal_id, org_id),
        ).fetchone()
        if not row:
            return None
        return Membership(id=row[0], principal_id=row[1], org_id=row[2], role=row[3])

    def list_org_memberships(self, principal_id: str) -> list[Membership]:
        rows = self._e(
            "SELECT id, principal_id, org_id, role FROM membership WHERE principal_id=?",
            (principal_id,),
        ).fetchall()
        return [Membership(id=r[0], principal_id=r[1], org_id=r[2], role=r[3]) for r in rows]

    def remove_membership(self, principal_id: str, org_id: str) -> int:
        cur = self._e(
            "DELETE FROM membership WHERE principal_id=? AND org_id=?",
            (principal_id, org_id),
        )
        return cur.rowcount

    # ---------- ProjectMembership（项目）----------

    def upsert_project_membership(self, pm: ProjectMembership) -> ProjectMembership:
        with self._conn:
            self._e(
                "INSERT OR REPLACE INTO project_membership(principal_id, project_id, role) "
                "VALUES (?, ?, ?)",
                (pm.principal_id, pm.project_id, pm.role),
            )
        return self.get_project_membership(pm.principal_id, pm.project_id) or pm

    def get_project_membership(self, principal_id: str, project_id: str) -> ProjectMembership | None:
        row = self._e(
            "SELECT id, principal_id, project_id, role FROM project_membership "
            "WHERE principal_id=? AND project_id=?",
            (principal_id, project_id),
        ).fetchone()
        if not row:
            return None
        return ProjectMembership(id=row[0], principal_id=row[1], project_id=row[2], role=row[3])

    def list_project_memberships(self, principal_id: str) -> list[ProjectMembership]:
        rows = self._e(
            "SELECT id, principal_id, project_id, role FROM project_membership WHERE principal_id=?",
            (principal_id,),
        ).fetchall()
        return [
            ProjectMembership(id=r[0], principal_id=r[1], project_id=r[2], role=r[3])
            for r in rows
        ]

    def list_project_members(self, project_id: str) -> list[ProjectMembership]:
        rows = self._e(
            "SELECT id, principal_id, project_id, role FROM project_membership WHERE project_id=?",
            (project_id,),
        ).fetchall()
        return [
            ProjectMembership(id=r[0], principal_id=r[1], project_id=r[2], role=r[3])
            for r in rows
        ]

    def remove_project_membership(self, principal_id: str, project_id: str) -> int:
        cur = self._e(
            "DELETE FROM project_membership WHERE principal_id=? AND project_id=?",
            (principal_id, project_id),
        )
        return cur.rowcount
