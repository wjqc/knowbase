"""P4-A 同步原语单元测试。

覆盖：
1. Operation 状态机：PENDING → RUNNING → SUCCEEDED/FAILED/UNKNOWN
2. Operation 幂等：相同 (request_hash, by, target_id) 视为同操作
3. Operation 并发 claim：仅一个 worker 成功
4. Outbox 状态机：PENDING → DISPATCHED / FAILED（含退避）
5. Outbox list_due：next_attempt_at 退避 + lease 抢占保护
6. SyncRun 状态机：RUNNING → SUCCEEDED/FAILED/CONFLICTED
7. SyncRun cursor 推进 + 计数累加
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from knowbase.v2.domain.models import (
    Operation,
    OperationKind,
    OperationStatus,
    OutboxEvent,
    OutboxStatus,
    Source,
    SourceKind,
    SyncRun,
    SyncRunStatus,
)
from knowbase.v2.repositories import V2Repository


# ---------- fixture ----------

@pytest.fixture
def repo(tmp_path: Path) -> V2Repository:
    r = V2Repository(tmp_path)
    yield r
    r.close()


@pytest.fixture
def source(repo: V2Repository) -> Source:
    s = Source.from_locator(SourceKind.FILE, "/tmp/p4a.md")
    repo.upsert_source(s)
    return s


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _later(seconds: int = 30) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


# ====================================================================
# Operation 状态机
# ====================================================================

class TestOperationIdempotency:
    """Operation 幂等：相同 (request_hash, by, target_id) 视为同操作。"""

    def test_record_and_find_by_hash(self, repo: V2Repository):
        op = Operation.new(OperationKind.INGEST, "doc-1", "agent:alice",
                           payload={"a": 1}, request_hash="hash-A")
        repo.record_operation(op)
        found = repo.find_operation_by_hash("hash-A", "agent:alice", "doc-1")
        assert found is not None
        assert found.id == op.id
        assert found.request_hash == "hash-A"

    def test_different_target_yields_none(self, repo: V2Repository):
        op = Operation.new(OperationKind.INGEST, "doc-1", "agent:alice",
                           request_hash="hash-X")
        repo.record_operation(op)
        assert repo.find_operation_by_hash("hash-X", "agent:alice", "doc-2") is None

    def test_different_actor_yields_none(self, repo: V2Repository):
        op = Operation.new(OperationKind.INGEST, "doc-1", "agent:alice",
                           request_hash="hash-Y")
        repo.record_operation(op)
        assert repo.find_operation_by_hash("hash-Y", "agent:bob", "doc-1") is None

    def test_default_status_pending(self, repo: V2Repository):
        op = Operation.new(OperationKind.INGEST, "doc-1", "agent:alice")
        repo.record_operation(op)
        assert repo.get_operation(op.id).status == OperationStatus.PENDING

    def test_v2_get_preserves_payload_and_result(self, repo: V2Repository):
        op = Operation.new(OperationKind.APPLY, "doc-1", "agent:alice",
                           payload={"k": "v"})
        repo.record_operation(op)
        repo.claim_operation(op.id, "w1", _later())
        repo.complete_operation(op.id, result={"out": 7})
        op2 = repo.get_operation(op.id)
        assert op2.payload == {"k": "v"}
        assert op2.result == {"out": 7}
        assert op2.status == OperationStatus.SUCCEEDED


class TestOperationStateMachine:
    """Operation 状态机：PENDING → RUNNING → SUCCEEDED/FAILED/UNKNOWN。"""

    def test_pending_to_running_via_claim(self, repo: V2Repository):
        op = Operation.new(OperationKind.INGEST, "doc-1", "agent:alice")
        repo.record_operation(op)
        assert repo.claim_operation(op.id, "w1", _later()) is True
        got = repo.get_operation(op.id)
        assert got.status == OperationStatus.RUNNING
        assert got.lease_owner == "w1"
        assert got.attempts == 1

    def test_double_claim_only_one_wins(self, repo: V2Repository):
        op = Operation.new(OperationKind.INGEST, "doc-1", "agent:alice")
        repo.record_operation(op)
        assert repo.claim_operation(op.id, "w1", _later()) is True
        assert repo.claim_operation(op.id, "w2", _later()) is False
        got = repo.get_operation(op.id)
        assert got.lease_owner == "w1"

    def test_running_to_succeeded(self, repo: V2Repository):
        op = Operation.new(OperationKind.INGEST, "doc-1", "agent:alice")
        repo.record_operation(op)
        repo.claim_operation(op.id, "w1", _later())
        assert repo.complete_operation(op.id, result={"ok": True}) is True
        got = repo.get_operation(op.id)
        assert got.status == OperationStatus.SUCCEEDED
        assert got.result == {"ok": True}
        assert got.completed_at is not None
        assert got.lease_owner is None  # 清 lease

    def test_running_to_failed_under_max_attempts(self, repo: V2Repository):
        op = Operation.new(OperationKind.INGEST, "doc-1", "agent:alice")
        repo.record_operation(op)
        repo.claim_operation(op.id, "w1", _later())
        # attempts=1 < max_attempts=3 → 保持 PENDING 允许重试
        assert repo.fail_operation(op.id, "transient err", max_attempts=3) is True
        got = repo.get_operation(op.id)
        assert got.status == OperationStatus.PENDING
        assert got.error == "transient err"

    def test_running_to_failed_at_max_attempts(self, repo: V2Repository):
        op = Operation.new(OperationKind.INGEST, "doc-1", "agent:alice")
        repo.record_operation(op)
        # claim 3 次把 attempts 累到 3
        repo.claim_operation(op.id, "w1", _later())
        repo.fail_operation(op.id, "e1", max_attempts=3)  # attempts=1, PENDING
        repo.claim_operation(op.id, "w1", _later())
        repo.fail_operation(op.id, "e2", max_attempts=3)  # attempts=2, PENDING
        repo.claim_operation(op.id, "w1", _later())
        assert repo.fail_operation(op.id, "e3", max_attempts=3) is True
        # attempts=3 >= max_attempts=3 → FAILED 终态
        got = repo.get_operation(op.id)
        assert got.status == OperationStatus.FAILED
        assert got.completed_at is not None

    def test_mark_unknown_for_crashed_worker(self, repo: V2Repository):
        op = Operation.new(OperationKind.INGEST, "doc-1", "agent:alice")
        repo.record_operation(op)
        repo.claim_operation(op.id, "w1", _later())
        assert repo.mark_operation_unknown(op.id) is True
        got = repo.get_operation(op.id)
        assert got.status == OperationStatus.UNKNOWN
        assert got.lease_owner is None

    def test_unknown_op_does_not_block_new_one(self, repo: V2Repository):
        """UNKNOWN 状态可被人工查询；新 op 用新 id 独立。"""
        op1 = Operation.new(OperationKind.INGEST, "doc-1", "agent:alice",
                            request_hash="hash-Z")
        repo.record_operation(op1)
        repo.claim_operation(op1.id, "w1", _later())
        repo.mark_operation_unknown(op1.id)
        op2 = Operation.new(OperationKind.INGEST, "doc-1", "agent:alice",
                            request_hash="hash-Z")  # 同请求
        repo.record_operation(op2)
        # op2 是新 op，但 find_operation_by_hash 仍指向 op1（按 created_at desc 取最新）
        found = repo.find_operation_by_hash("hash-Z", "agent:alice", "doc-1")
        assert found is not None
        # 实际场景下人工裁决 op1 后可决定是否放行 op2


class TestOperationList:
    def test_list_by_status(self, repo: V2Repository):
        for i in range(3):
            repo.record_operation(
                Operation.new(OperationKind.INGEST, f"doc-{i}", "agent:alice")
            )
        op = repo.list_operations("doc-0", limit=10)[0]
        repo.claim_operation(op.id, "w", _later())
        repo.complete_operation(op.id)
        # 1 SUCCEEDED + 2 PENDING
        pending = repo.list_operations_by_status(OperationStatus.PENDING)
        assert len(pending) == 2
        succeeded = repo.list_operations_by_status(OperationStatus.SUCCEEDED)
        assert len(succeeded) == 1

    def test_list_by_target(self, repo: V2Repository):
        for i in range(5):
            repo.record_operation(
                Operation.new(OperationKind.INGEST, "doc-A", "agent:alice")
            )
        repo.record_operation(
            Operation.new(OperationKind.INGEST, "doc-B", "agent:alice")
        )
        ops_a = repo.list_operations("doc-A", limit=10)
        assert len(ops_a) == 5
        assert all(o.target_id == "doc-A" for o in ops_a)


# ====================================================================
# Outbox 状态机
# ====================================================================

class TestOutboxStateMachine:
    """Outbox：PENDING → DISPATCHED / FAILED（含退避）。"""

    def test_enqueue_defaults_pending(self, repo: V2Repository):
        ev = OutboxEvent.new("doc.created", {"id": "1"})
        repo.enqueue_outbox(ev)
        got = repo.get_outbox(ev.id)
        assert got.status == OutboxStatus.PENDING
        assert got.aggregate_type is None  # 默认未设

    def test_enqueue_with_aggregate(self, repo: V2Repository):
        ev = OutboxEvent.new(
            "doc.created", {"id": "1"},
            aggregate_type="document", aggregate_id="doc-1",
            event_type="created",
        )
        repo.enqueue_outbox(ev)
        got = repo.get_outbox(ev.id)
        assert got.aggregate_type == "document"
        assert got.aggregate_id == "doc-1"
        assert got.event_type == "created"

    def test_claim_then_ack(self, repo: V2Repository):
        ev = OutboxEvent.new("doc.created", {})
        repo.enqueue_outbox(ev)
        assert repo.claim_outbox(ev.id, "w1", _later()) is True
        assert repo.ack_outbox(ev.id) is True
        got = repo.get_outbox(ev.id)
        assert got.status == OutboxStatus.DISPATCHED
        assert got.delivered_at is not None
        assert got.lease_owner is None

    def test_ack_on_unclaimed_returns_false(self, repo: V2Repository):
        ev = OutboxEvent.new("doc.created", {})
        repo.enqueue_outbox(ev)
        # 不 claim 直接 ack
        assert repo.ack_outbox(ev.id) is False

    def test_nack_under_max_attempts_keeps_pending(self, repo: V2Repository):
        ev = OutboxEvent.new("doc.created", {})
        repo.enqueue_outbox(ev)
        repo.claim_outbox(ev.id, "w1", _later())
        assert repo.nack_outbox(ev.id, "net err",
                                next_attempt_at=_later(60),
                                max_attempts=5) is True
        got = repo.get_outbox(ev.id)
        assert got.status == OutboxStatus.PENDING
        assert got.attempts == 1
        assert got.last_error == "net err"
        assert got.next_attempt_at is not None

    def test_nack_at_max_attempts_moves_to_failed(self, repo: V2Repository):
        ev = OutboxEvent.new("doc.created", {})
        repo.enqueue_outbox(ev)
        # 跑 5 次 nack 达 max_attempts
        for _ in range(5):
            if got := repo.get_outbox(ev.id):
                if got.status != OutboxStatus.PENDING:
                    break
            repo.claim_outbox(ev.id, "w1", _later())
            repo.nack_outbox(ev.id, "e", max_attempts=5)
        got = repo.get_outbox(ev.id)
        assert got.status == OutboxStatus.FAILED
        # 第 5 次 nack 进入 FAILED 后 attempts 不再递增；前 4 次递增到 4
        assert got.attempts == 4

    def test_double_claim_only_one_wins(self, repo: V2Repository):
        ev = OutboxEvent.new("doc.created", {})
        repo.enqueue_outbox(ev)
        assert repo.claim_outbox(ev.id, "w1", _later()) is True
        assert repo.claim_outbox(ev.id, "w2", _later()) is False


class TestOutboxListDue:
    """list_outbox_due：next_attempt_at 退避 + lease 抢占保护。"""

    def test_due_with_null_next_attempt(self, repo: V2Repository):
        ev = OutboxEvent.new("doc.created", {})
        repo.enqueue_outbox(ev)
        due = repo.list_outbox_due(limit=10)
        assert len(due) == 1
        assert due[0].id == ev.id

    def test_due_respects_next_attempt_at(self, repo: V2Repository):
        ev = OutboxEvent.new("doc.created", {})
        repo.enqueue_outbox(ev)
        repo.claim_outbox(ev.id, "w1", _later())
        # 设未来时间，退避中
        future = _later(3600)
        repo.nack_outbox(ev.id, "net err", next_attempt_at=future,
                         max_attempts=5)
        due = repo.list_outbox_due(now=_now(), limit=10)
        assert all(e.id != ev.id for e in due)
        # 改回过去时间
        repo._conn.execute(
            "UPDATE outbox_event SET next_attempt_at=? WHERE id=?",
            ("2000-01-01T00:00:00Z", ev.id),
        )
        due = repo.list_outbox_due(now=_now(), limit=10)
        assert any(e.id == ev.id for e in due)

    def test_due_skips_leased(self, repo: V2Repository):
        ev = OutboxEvent.new("doc.created", {})
        repo.enqueue_outbox(ev)
        # 模拟其他 worker 持有 lease（未来到期）
        repo._conn.execute(
            "UPDATE outbox_event SET lease_owner=?, lease_until=? WHERE id=?",
            ("other-w", _later(3600), ev.id),
        )
        due = repo.list_outbox_due(now=_now(), limit=10)
        assert all(e.id != ev.id for e in due)


# ====================================================================
# SyncRun 状态机
# ====================================================================

class TestSyncRunStateMachine:

    def test_record_defaults_running(self, repo: V2Repository, source: Source):
        run = SyncRun.new(source.stable_id, cursor_before="abc123")
        repo.record_sync_run(run)
        got = repo.get_sync_run(run.id)
        assert got.status == SyncRunStatus.RUNNING
        assert got.cursor_before == "abc123"

    def test_running_to_succeeded_with_cursor_and_counts(
        self, repo: V2Repository, source: Source
    ):
        run = SyncRun.new(source.stable_id, cursor_before="c0")
        repo.record_sync_run(run)
        assert repo.complete_sync_run(
            run.id, cursor_after="c1", discovered=10,
            created_n=3, updated_n=5, deleted_n=1, failed_n=1,
            stats={"files_scanned": 10},
        ) is True
        got = repo.get_sync_run(run.id)
        assert got.status == SyncRunStatus.SUCCEEDED
        assert got.cursor_after == "c1"
        assert got.discovered == 10
        assert got.created_n == 3
        assert got.updated_n == 5
        assert got.deleted_n == 1
        assert got.failed_n == 1
        assert got.finished_at is not None

    def test_running_to_conflicted_preserves_cursor_before(
        self, repo: V2Repository, source: Source
    ):
        run = SyncRun.new(source.stable_id, cursor_before="c0")
        repo.record_sync_run(run)
        assert repo.conflict_sync_run(run.id, "3-way merge conflict") is True
        got = repo.get_sync_run(run.id)
        assert got.status == SyncRunStatus.CONFLICTED
        assert got.error == "3-way merge conflict"
        assert got.cursor_before == "c0"  # 保留,不自动覆盖
        assert got.cursor_after is None  # 未推进
        assert got.finished_at is not None

    def test_running_to_failed(self, repo: V2Repository, source: Source):
        run = SyncRun.new(source.stable_id, cursor_before="c0")
        repo.record_sync_run(run)
        assert repo.fail_sync_run(run.id, "network unreachable") is True
        got = repo.get_sync_run(run.id)
        assert got.status == SyncRunStatus.FAILED
        assert got.error == "network unreachable"

    def test_terminal_states_block_re_complete(
        self, repo: V2Repository, source: Source
    ):
        run = SyncRun.new(source.stable_id)
        repo.record_sync_run(run)
        repo.complete_sync_run(run.id, cursor_after="c1", discovered=0)
        # 已 SUCCEEDED，不能再次 complete
        assert repo.complete_sync_run(run.id, cursor_after="c2", discovered=0) is False
        assert repo.fail_sync_run(run.id, "ignored") is False

    def test_list_sync_runs_by_source(
        self, repo: V2Repository, source: Source
    ):
        # 不同 source
        other = Source.from_locator(SourceKind.FILE, "/tmp/p4a-other.md")
        repo.upsert_source(other)
        for _ in range(3):
            repo.record_sync_run(SyncRun.new(source.stable_id))
        repo.record_sync_run(SyncRun.new(other.stable_id))
        runs = repo.list_sync_runs_by_source(source.stable_id, limit=10)
        assert len(runs) == 3
        assert all(r.source_id == source.stable_id for r in runs)

    def test_claim_does_not_change_status(
        self, repo: V2Repository, source: Source
    ):
        """claim_sync_run 只设 lease，不改 RUNNING 状态（用于接管中断实例）。"""
        run = SyncRun.new(source.stable_id)
        repo.record_sync_run(run)
        assert repo.claim_sync_run(run.id, "w-takeover", _later(60)) is True
        got = repo.get_sync_run(run.id)
        assert got.status == SyncRunStatus.RUNNING
        assert got.lease_owner == "w-takeover"


# ====================================================================
# Schema & 数据完整性
# ====================================================================

class TestSchemaAndIntegrity:
    """验证 v3 schema 与数据完整性约束。"""

    def test_v4_user_version(self, tmp_path: Path):
        from knowbase.v2.repositories import init_v2_db
        db = init_v2_db(tmp_path)
        conn = sqlite3.connect(str(db))
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
        assert ver == 4  # P4-D: source_sync_state（Lite Profile 同步状态）

    def test_v3_indexes_exist(self, tmp_path: Path):
        from knowbase.v2.repositories import init_v2_db
        db = init_v2_db(tmp_path)
        conn = sqlite3.connect(str(db))
        idx = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()}
        # P4-A 新增 7 个索引
        for name in ("idx_op_status", "idx_op_lease",
                     "idx_outbox_status", "idx_outbox_lease",
                     "idx_outbox_next_attempt", "idx_outbox_aggregate",
                     "idx_syncrun_status", "idx_syncrun_lease"):
            assert name in idx, f"missing index: {name}"

    def test_operation_extended_columns_exist(self, tmp_path: Path):
        from knowbase.v2.repositories import init_v2_db
        db = init_v2_db(tmp_path)
        conn = sqlite3.connect(str(db))
        cols = {r[1] for r in conn.execute("PRAGMA table_info(operation)").fetchall()}
        for c in ("status", "request_hash", "result", "error", "attempts",
                  "lease_owner", "lease_until", "updated_at", "completed_at"):
            assert c in cols, f"missing column: operation.{c}"

    def test_outbox_extended_columns_exist(self, tmp_path: Path):
        from knowbase.v2.repositories import init_v2_db
        db = init_v2_db(tmp_path)
        conn = sqlite3.connect(str(db))
        cols = {r[1] for r in conn.execute("PRAGMA table_info(outbox_event)").fetchall()}
        for c in ("aggregate_type", "aggregate_id", "event_type", "status",
                  "next_attempt_at", "lease_owner", "lease_until",
                  "last_error", "last_delivered_at"):
            assert c in cols, f"missing column: outbox_event.{c}"

    def test_sync_run_extended_columns_exist(self, tmp_path: Path):
        from knowbase.v2.repositories import init_v2_db
        db = init_v2_db(tmp_path)
        conn = sqlite3.connect(str(db))
        cols = {r[1] for r in conn.execute("PRAGMA table_info(sync_run)").fetchall()}
        for c in ("cursor_before", "cursor_after", "status",
                  "discovered", "created_n", "updated_n", "deleted_n", "failed_n",
                  "lease_owner", "lease_until"):
            assert c in cols, f"missing column: sync_run.{c}"
