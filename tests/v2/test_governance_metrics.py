"""Tests for knowbase.v2.governance.metrics + api (P5-B2).

Coverage:
- compute_quality_metrics: status/kind/flag/freshness buckets
- compute_freshness_metrics: expiry distribution + implicit TTL
- compute_permission_metrics: failed+permission errors, unknown ops
- compute_sync_backlog_metrics: stale detection, pending/conflicted sums
- GovernanceSnapshot / collect_governance_snapshot via FakeRepo
- as_dict round-trip; empty inputs; dataclass immutability
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from knowbase.v2.domain.models import (
    Document,
    DocumentStatus,
    KnowledgeKind,
    Operation,
    OperationKind,
    OperationStatus,
    SourceSyncState,
    SyncRun,
    SyncRunStatus,
)
from knowbase.v2.governance.api import (
    DEFAULT_OPERATION_SAMPLE_LIMIT,
    DEFAULT_SYNC_RUN_SAMPLE_LIMIT,
    GovernanceSnapshot,
    collect_governance_snapshot,
)
from knowbase.v2.governance.metrics import (
    DEFAULT_SYNC_STALENESS_SLA_SECONDS,
    FreshnessMetrics,
    PERMISSION_ERROR_TOKENS,
    PermissionMetrics,
    QUALITY_AGING_DAYS,
    QUALITY_FRESH_DAYS,
    QualityMetrics,
    SyncBacklogMetrics,
    compute_freshness_metrics,
    compute_permission_metrics,
    compute_quality_metrics,
    compute_sync_backlog_metrics,
)


FROZEN_NOW = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
FROZEN_EPOCH = FROZEN_NOW.timestamp()


# ---------- helpers ----------


def _doc(
    doc_id: str,
    *,
    status: DocumentStatus = DocumentStatus.READY,
    kind: KnowledgeKind | None = KnowledgeKind.BIZRULE,
    meta: dict | None = None,
    updated_at: str = "2026-09-15T00:00:00Z",
) -> Document:
    return Document(
        id=doc_id,
        source_id="s1",
        path=f"p{doc_id}",
        kind=kind,
        title=f"title-{doc_id}",
        status=status,
        meta=meta or {},
        updated_at=updated_at,
    )


def _op(
    op_id: str,
    *,
    status: OperationStatus = OperationStatus.SUCCEEDED,
    kind: OperationKind = OperationKind.APPLY,
    error: str | None = None,
    created_at: str = "2026-09-17T00:00:00Z",
) -> Operation:
    return Operation(
        id=op_id,
        kind=kind,
        target_id="doc1",
        by="agent:test",
        status=status,
        error=error,
        created_at=created_at,
    )


def _state(
    source_id: str = "s1",
    *,
    last_remote_check_at: str | None = "2026-09-18T11:30:00Z",
    pending_ops: int = 0,
    conflicted_docs: int = 0,
    last_failed_at: str | None = None,
) -> SourceSyncState:
    return SourceSyncState(
        source_id=source_id,
        last_remote_check_at=last_remote_check_at,
        pending_ops=pending_ops,
        conflicted_docs=conflicted_docs,
        last_failed_at=last_failed_at,
    )


# ---------- compute_quality_metrics ----------


class TestComputeQualityMetrics:
    def test_empty_input(self):
        m = compute_quality_metrics([], now=FROZEN_NOW)
        assert m.total_documents == 0
        assert m.by_status == {}
        assert m.flagged == {}
        assert m.freshness_buckets == {}

    def test_counts_by_status(self):
        docs = [
            _doc("a", status=DocumentStatus.READY),
            _doc("b", status=DocumentStatus.READY),
            _doc("c", status=DocumentStatus.ERROR),
        ]
        m = compute_quality_metrics(docs, now=FROZEN_NOW)
        assert m.by_status == {"ready": 2, "error": 1}
        # error counted in flagged too
        assert m.flagged.get("error") == 1

    def test_counts_by_kind(self):
        docs = [
            _doc("a", kind=KnowledgeKind.BIZRULE),
            _doc("b", kind=KnowledgeKind.STANDARD),
            _doc("c", kind=None),  # unknown
        ]
        m = compute_quality_metrics(docs, now=FROZEN_NOW)
        assert m.by_kind == {"bizrule": 1, "standard": 1, "unknown": 1}

    def test_flag_contradicted(self):
        docs = [
            _doc("a", meta={"relations": [{"type": "contradicts", "target_id": "x"}]}),
        ]
        m = compute_quality_metrics(docs, now=FROZEN_NOW)
        assert m.flagged["contradicted"] == 1
        assert "a" in m.flagged_doc_ids

    def test_flag_tombstoned(self):
        docs = [_doc("a", status=DocumentStatus.TOMBSTONED)]
        m = compute_quality_metrics(docs, now=FROZEN_NOW)
        assert m.flagged["tombstoned"] == 1

    def test_flag_superseded(self):
        docs = [_doc("a", meta={"superseded": True})]
        m = compute_quality_metrics(docs, now=FROZEN_NOW)
        assert m.flagged["superseded"] == 1

    def test_contradicted_short_circuits_freshness(self):
        # contradicted docs don't go into freshness buckets
        docs = [
            _doc("a", meta={"relations": [{"type": "contradicts", "target_id": "x"}]}),
        ]
        m = compute_quality_metrics(docs, now=FROZEN_NOW)
        assert "fresh" not in m.freshness_buckets
        assert "stale" not in m.freshness_buckets

    def test_freshness_buckets(self):
        # 5 days ago -> fresh (<=7)
        # 15 days ago -> aging (<=30)
        # 60 days ago -> stale (>30)
        now_epoch = FROZEN_EPOCH
        d_fresh = _doc(
            "a", updated_at=datetime.fromtimestamp(now_epoch - 5 * 86400, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        d_aging = _doc(
            "b", updated_at=datetime.fromtimestamp(now_epoch - 15 * 86400, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        d_stale = _doc(
            "c", updated_at=datetime.fromtimestamp(now_epoch - 60 * 86400, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        m = compute_quality_metrics([d_fresh, d_aging, d_stale], now=FROZEN_NOW)
        assert m.freshness_buckets == {"fresh": 1, "aging": 1, "stale": 1}

    def test_low_confidence_count(self):
        docs = [
            _doc("a", meta={"confidence": "established"}),  # not low
            _doc("b", meta={"confidence": "once"}),  # low
            _doc("c", meta={}),  # low (None)
            _doc("d", meta={"confidence": ""}),  # low (empty)
        ]
        m = compute_quality_metrics(docs, now=FROZEN_NOW)
        assert m.low_confidence_count == 3

    def test_unknown_updated_at_goes_to_unknown_bucket(self):
        docs = [_doc("a", updated_at="not-a-date")]
        m = compute_quality_metrics(docs, now=FROZEN_NOW)
        assert m.freshness_buckets.get("unknown") == 1

    def test_as_dict_round_trip(self):
        docs = [_doc("a")]
        m = compute_quality_metrics(docs, now=FROZEN_NOW)
        d = m.as_dict()
        assert d["total_documents"] == 1
        assert "by_status" in d
        assert "flagged_doc_ids" in d

    def test_dataclass_is_frozen(self):
        m = compute_quality_metrics([], now=FROZEN_NOW)
        with pytest.raises(Exception):
            m.total_documents = 5  # type: ignore[misc]


# ---------- compute_freshness_metrics ----------


class TestComputeFreshnessMetrics:
    def test_empty_input(self):
        m = compute_freshness_metrics([], now=FROZEN_NOW)
        assert m.total_documents == 0
        assert m.median_days_remaining is None
        assert m.soonest_expiry_days is None

    def test_with_explicit_valid_until(self):
        future = (FROZEN_NOW + timedelta(days=365)).isoformat().replace("+00:00", "Z")
        past = (FROZEN_NOW - timedelta(days=30)).isoformat().replace("+00:00", "Z")
        soon = (FROZEN_NOW + timedelta(days=10)).isoformat().replace("+00:00", "Z")
        # Use WORKFLOW kind for doc "d" so no implicit TTL applies (-> no_expiry).
        docs = [
            _doc("a", meta={"valid_until": future}),
            _doc("b", meta={"valid_until": past}),
            _doc("c", meta={"valid_until": soon}),
            _doc("d", kind=KnowledgeKind.WORKFLOW, meta={}),  # no_expiry
        ]
        m = compute_freshness_metrics(docs, now=FROZEN_NOW)
        assert m.by_expiry_status == {
            "active": 1, "expiring_soon": 1, "expired": 1, "no_expiry": 1,
        }
        assert "b" in m.expired_doc_ids
        assert "c" in m.expiring_doc_ids

    def test_implicit_ttl_for_bizrule_standard(self):
        # No valid_until but kind=bizrule -> applies DEFAULT_BIZRULE_TTL_DAYS
        # from updated_at = 2026-09-15 (3 days before now) -> +180 = 2027-03-14 (active)
        docs = [_doc("a", kind=KnowledgeKind.BIZRULE, meta={})]
        m = compute_freshness_metrics(docs, now=FROZEN_NOW)
        assert m.by_expiry_status.get("active") == 1

    def test_implicit_ttl_for_workflow_no_expiry(self):
        docs = [_doc("a", kind=KnowledgeKind.WORKFLOW, meta={})]
        m = compute_freshness_metrics(docs, now=FROZEN_NOW)
        assert m.by_expiry_status.get("no_expiry") == 1

    def test_median_days_remaining(self):
        # three samples with explicit valid_until
        future1 = (FROZEN_NOW + timedelta(days=10)).isoformat().replace("+00:00", "Z")
        future2 = (FROZEN_NOW + timedelta(days=20)).isoformat().replace("+00:00", "Z")
        future3 = (FROZEN_NOW + timedelta(days=30)).isoformat().replace("+00:00", "Z")
        docs = [
            _doc("a", meta={"valid_until": future1}),
            _doc("b", meta={"valid_until": future2}),
            _doc("c", meta={"valid_until": future3}),
        ]
        m = compute_freshness_metrics(docs, now=FROZEN_NOW)
        assert m.median_days_remaining == pytest.approx(20.0, abs=0.1)
        assert m.soonest_expiry_days == pytest.approx(10.0, abs=0.1)

    def test_unparseable_valid_until_counted_as_expired(self):
        docs = [_doc("a", meta={"valid_until": "not-a-date"})]
        m = compute_freshness_metrics(docs, now=FROZEN_NOW)
        assert m.by_expiry_status.get("expired") == 1
        assert "a" in m.expired_doc_ids

    def test_accepts_epoch_now(self):
        future = (FROZEN_NOW + timedelta(days=100)).isoformat().replace("+00:00", "Z")
        docs = [_doc("a", meta={"valid_until": future})]
        m = compute_freshness_metrics(docs, now=FROZEN_EPOCH)
        assert m.by_expiry_status.get("active") == 1


# ---------- compute_permission_metrics ----------


class TestComputePermissionMetrics:
    def test_empty_input(self):
        m = compute_permission_metrics([])
        assert m.total_operations == 0
        assert m.denied_count == 0
        assert m.unknown_count == 0

    def test_failed_with_permission_error_is_denied(self):
        ops = [
            _op("o1", status=OperationStatus.FAILED, error="Permission denied for kind: BIZRULE"),
        ]
        m = compute_permission_metrics(ops)
        assert m.denied_count == 1
        assert m.denied_by_kind == {"apply": 1}
        assert m.recent_denied_ids == ["o1"]

    def test_failed_with_other_error_not_denied(self):
        ops = [
            _op("o1", status=OperationStatus.FAILED, error="DB connection lost"),
        ]
        m = compute_permission_metrics(ops)
        assert m.denied_count == 0

    def test_unknown_status_counted(self):
        ops = [
            _op("o1", status=OperationStatus.UNKNOWN),
            _op("o2", status=OperationStatus.UNKNOWN),
        ]
        m = compute_permission_metrics(ops)
        assert m.unknown_count == 2

    def test_various_permission_tokens(self):
        for token in ["forbidden", "denied", "unauthorized", "rbac"]:
            ops = [_op("o", status=OperationStatus.FAILED, error=f"oops: {token}")]
            m = compute_permission_metrics(ops)
            assert m.denied_count == 1, f"token {token} should fire"

    def test_case_insensitive_match(self):
        ops = [_op("o", status=OperationStatus.FAILED, error="PERMISSION DENIED")]
        m = compute_permission_metrics(ops)
        assert m.denied_count == 1

    def test_recent_denied_capped(self):
        ops = [
            _op(
                f"o{i}", status=OperationStatus.FAILED,
                error="permission denied",
                created_at=f"2026-09-{17 - i // 24}T00:00:00Z",
            )
            for i in range(50)
        ]
        m = compute_permission_metrics(ops, recent_limit=5)
        assert m.denied_count == 50
        assert len(m.recent_denied_ids) == 5

    def test_denied_by_kind(self):
        ops = [
            _op("o1", status=OperationStatus.FAILED, error="denied", kind=OperationKind.APPLY),
            _op("o2", status=OperationStatus.FAILED, error="denied", kind=OperationKind.INGEST),
            _op("o3", status=OperationStatus.FAILED, error="denied", kind=OperationKind.APPLY),
        ]
        m = compute_permission_metrics(ops)
        assert m.denied_by_kind == {"apply": 2, "ingest": 1}

    def test_no_error_string_not_denied(self):
        ops = [_op("o", status=OperationStatus.FAILED, error=None)]
        m = compute_permission_metrics(ops)
        assert m.denied_count == 0

    def test_custom_error_tokens(self):
        ops = [
            _op("o", status=OperationStatus.FAILED, error="kicked by policy"),
        ]
        m = compute_permission_metrics(ops, error_tokens=("policy",))
        assert m.denied_count == 1

    def test_tokens_constant(self):
        assert "permission" in PERMISSION_ERROR_TOKENS
        assert "denied" in PERMISSION_ERROR_TOKENS

    def test_succeeded_counted_in_by_status(self):
        ops = [_op("o", status=OperationStatus.SUCCEEDED)]
        m = compute_permission_metrics(ops)
        assert m.by_status == {"succeeded": 1}


# ---------- compute_sync_backlog_metrics ----------


class TestComputeSyncBacklogMetrics:
    def test_empty(self):
        m = compute_sync_backlog_metrics([])
        assert m.total_sources == 0
        assert m.stale_sources == 0
        assert m.total_pending_ops == 0
        assert m.total_conflicted_docs == 0

    def test_fresh_source_not_stale(self):
        # last check 30 min ago < 1h SLA
        state = _state(last_remote_check_at="2026-09-18T11:30:00Z")
        m = compute_sync_backlog_metrics([state], now=FROZEN_NOW)
        assert m.stale_sources == 0

    def test_stale_source_flagged(self):
        # last check 2 days ago > 1h SLA
        state = _state(last_remote_check_at="2026-09-16T12:00:00Z")
        m = compute_sync_backlog_metrics([state], now=FROZEN_NOW)
        assert m.stale_sources == 1

    def test_pending_and_conflicted_summed(self):
        states = [
            _state("s1", pending_ops=3, conflicted_docs=1),
            _state("s2", pending_ops=5, conflicted_docs=2),
        ]
        m = compute_sync_backlog_metrics(states, now=FROZEN_NOW)
        assert m.total_pending_ops == 8
        assert m.total_conflicted_docs == 3

    def test_no_remote_check_treated_as_stale(self):
        state = _state(last_remote_check_at=None)
        m = compute_sync_backlog_metrics([state], now=FROZEN_NOW)
        assert m.stale_sources == 1

    def test_last_failed_sources_collected(self):
        states = [
            _state("s1", last_failed_at="2026-09-17T00:00:00Z"),
            _state("s2"),  # no failure
        ]
        m = compute_sync_backlog_metrics(states, now=FROZEN_NOW)
        assert m.last_failed_sources == ["s1"]

    def test_sync_run_status_counted(self):
        runs = [
            SyncRun(
                id=f"r{i}", source_id="s1",
                status=SyncRunStatus.SUCCEEDED,
                started_at="2026-09-17T00:00:00Z",
            )
            for i in range(3)
        ]
        runs.append(
            SyncRun(
                id="rf", source_id="s1",
                status=SyncRunStatus.FAILED,
                started_at="2026-09-17T01:00:00Z",
            )
        )
        m = compute_sync_backlog_metrics([_state()], runs, now=FROZEN_NOW)
        assert m.by_run_status == {"succeeded": 3, "failed": 1}

    def test_custom_sla(self):
        # With SLA=1s, 30-min-old check is stale
        state = _state(last_remote_check_at="2026-09-18T11:30:00Z")
        m = compute_sync_backlog_metrics([state], staleness_sla_seconds=1, now=FROZEN_NOW)
        assert m.stale_sources == 1

    def test_default_sla_is_one_hour(self):
        assert DEFAULT_SYNC_STALENESS_SLA_SECONDS == 3600

    def test_unparseable_check_treated_as_stale(self):
        state = _state(last_remote_check_at="not-a-date")
        m = compute_sync_backlog_metrics([state], now=FROZEN_NOW)
        assert m.stale_sources == 1


# ---------- collect_governance_snapshot ----------


class _FakeRepo:
    def __init__(
        self,
        documents=None,
        operations=None,
        sync_states=None,
        sync_runs=None,
    ):
        self.documents = documents or []
        self.operations = operations or []
        self.sync_states = sync_states or []
        self.sync_runs = sync_runs or []

    def list_documents(self, *a, **kw):
        return list(self.documents)

    def list_operations_by_status(self, status, limit=20):
        return [op for op in self.operations if op.status == status][:limit]

    def list_sync_states(self, *a, **kw):
        return list(self.sync_states)

    def list_sync_runs_by_source(self, source_id, limit=20):
        return [r for r in self.sync_runs if r.source_id == source_id][:limit]


class TestCollectGovernanceSnapshot:
    def test_empty_repo_returns_zeroed_snapshot(self):
        snap = collect_governance_snapshot(_FakeRepo(), now=FROZEN_NOW)
        d = snap.as_dict()
        assert d["quality"]["total_documents"] == 0
        assert d["freshness"]["total_documents"] == 0
        assert d["permissions"]["total_operations"] == 0
        assert d["sync_backlog"]["total_sources"] == 0
        assert "generated_at" in d

    def test_full_pipeline_snapshot(self):
        docs = [
            _doc("a", kind=KnowledgeKind.BIZRULE, meta={"confidence": "established"}),
            _doc("b", kind=KnowledgeKind.STANDARD, status=DocumentStatus.TOMBSTONED),
        ]
        ops = [
            _op("o1", status=OperationStatus.FAILED, error="permission denied"),
            _op("o2", status=OperationStatus.SUCCEEDED),
        ]
        states = [_state("s1", pending_ops=3, conflicted_docs=1)]
        snap = collect_governance_snapshot(
            _FakeRepo(docs, ops, states), now=FROZEN_NOW
        )
        d = snap.as_dict()
        assert d["quality"]["total_documents"] == 2
        assert d["freshness"]["total_documents"] == 2
        assert d["permissions"]["denied_count"] == 1
        assert d["permissions"]["total_operations"] == 2
        assert d["sync_backlog"]["total_pending_ops"] == 3
        assert d["sync_backlog"]["total_conflicted_docs"] == 1

    def test_repository_errors_are_tolerated(self):
        # Sync state missing some methods -> snapshot still produced.
        class BrokenRepo:
            def list_documents(self, *a, **kw):
                return [_doc("a")]

            def list_operations_by_status(self, *a, **kw):
                raise RuntimeError("boom")

            def list_sync_states(self, *a, **kw):
                raise RuntimeError("boom")

            def list_sync_runs_by_source(self, *a, **kw):
                raise RuntimeError("boom")

        snap = collect_governance_snapshot(BrokenRepo(), now=FROZEN_NOW)
        d = snap.as_dict()
        # Documents still counted
        assert d["quality"]["total_documents"] == 1
        # Operations + sync gracefully empty
        assert d["permissions"]["total_operations"] == 0
        assert d["sync_backlog"]["total_sources"] == 0

    def test_default_limits_exposed(self):
        assert DEFAULT_OPERATION_SAMPLE_LIMIT > 0
        assert DEFAULT_SYNC_RUN_SAMPLE_LIMIT > 0

    def test_snapshot_is_dataclass(self):
        snap = collect_governance_snapshot(_FakeRepo(), now=FROZEN_NOW)
        assert isinstance(snap, GovernanceSnapshot)
        with pytest.raises(Exception):
            snap.quality = None  # type: ignore[misc]

    def test_generated_at_uses_now(self):
        snap = collect_governance_snapshot(_FakeRepo(), now=FROZEN_EPOCH)
        assert snap.generated_at.startswith("2026-09-18T12:00:00")

    def test_passes_now_to_freshness_and_quality(self):
        docs = [_doc("a", kind=KnowledgeKind.WORKFLOW, meta={})]
        snap = collect_governance_snapshot(_FakeRepo(documents=docs), now=FROZEN_NOW)
        assert snap.freshness.total_documents == 1


# ---------- constants sanity ----------


class TestConstants:
    def test_quality_fresh_days(self):
        assert QUALITY_FRESH_DAYS == 7

    def test_quality_aging_days(self):
        assert QUALITY_AGING_DAYS == 30

    def test_permission_tokens_iterable(self):
        # All tokens must be lowercased strings
        for tok in PERMISSION_ERROR_TOKENS:
            assert isinstance(tok, str)
            assert tok == tok.lower()