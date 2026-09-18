"""V2 governance metrics (P5-B2).

Four aggregation categories powering the dashboard:

1. Quality: document status / kind distribution; flagged items (contradict,
   tombstoned, low confidence).
2. Freshness: expiry distribution (active / expiring_soon / expired / no_expiry);
   stale-by-update-age buckets.
3. Permission denials: failed operations + permission-related errors.
4. Sync backlog: per-source pending ops + conflicted docs + stale sources.

All functions are pure aggregation: they take pre-fetched dataclasses and return
plain dicts. The V2 repository wiring lives in `governance.api`.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Mapping

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
from knowbase.v2.governance.lifecycle import (
    DEFAULT_BIZRULE_TTL_DAYS,
    EXPIRY_WARNING_DAYS,
    evaluate_expiry,
)


# ---------- 1. Quality ----------


# Stale-by-update-age thresholds (days). Buckets are mutually exclusive.
QUALITY_FRESH_DAYS = 7
QUALITY_AGING_DAYS = 30


@dataclass(frozen=True)
class QualityMetrics:
    total_documents: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    by_kind: dict[str, int] = field(default_factory=dict)
    flagged: dict[str, int] = field(default_factory=dict)
    flagged_doc_ids: list[str] = field(default_factory=list)
    freshness_buckets: dict[str, int] = field(default_factory=dict)
    low_confidence_count: int = 0

    def as_dict(self) -> dict:
        return {
            "total_documents": self.total_documents,
            "by_status": dict(self.by_status),
            "by_kind": dict(self.by_kind),
            "flagged": dict(self.flagged),
            "flagged_doc_ids": list(self.flagged_doc_ids),
            "freshness_buckets": dict(self.freshness_buckets),
            "low_confidence_count": self.low_confidence_count,
        }


def compute_quality_metrics(
    documents: Iterable[Document],
    *,
    now: datetime | float | None = None,
    fresh_days: int = QUALITY_FRESH_DAYS,
    aging_days: int = QUALITY_AGING_DAYS,
) -> QualityMetrics:
    """Aggregate document status / kind / flags + freshness buckets.

    Flags (counter categories):
    - 'contradicted': meta.relations contains type='contradicts'
    - 'tombstoned': DocumentStatus.TOMBSTONED
    - 'error': DocumentStatus.ERROR
    - 'superseded': status='stale' (V1 convention) or meta.superseded set
    - 'low_confidence': meta.confidence == 'once' or missing
    """
    by_status: Counter[str] = Counter()
    by_kind: Counter[str] = Counter()
    flagged: Counter[str] = Counter()
    flagged_ids: list[str] = []
    freshness: Counter[str] = Counter()
    low_confidence_count = 0

    cur_epoch = _to_epoch(now)
    for doc in documents:
        by_status[doc.status.value] += 1
        kind_key = doc.kind.value if isinstance(doc.kind, KnowledgeKind) else "unknown"
        by_kind[kind_key] += 1

        meta = doc.meta or {}
        # flag: contradicted relation
        if any(
            (r.get("type") == "contradicts") for r in (meta.get("relations") or [])
        ):
            flagged["contradicted"] += 1
            flagged_ids.append(doc.id)
            continue  # superseded/contradicted; don't double-flag

        # flag: tombstoned / error / superseded
        if doc.status == DocumentStatus.TOMBSTONED:
            flagged["tombstoned"] += 1
            flagged_ids.append(doc.id)
            continue
        if doc.status == DocumentStatus.ERROR:
            flagged["error"] += 1
            flagged_ids.append(doc.id)
            continue
        if meta.get("superseded"):
            flagged["superseded"] += 1
            flagged_ids.append(doc.id)
            continue

        # freshness buckets (only for non-flagged docs)
        days = _days_since(doc.updated_at, cur_epoch)
        if days is None:
            freshness["unknown"] += 1
        elif days <= fresh_days:
            freshness["fresh"] += 1
        elif days <= aging_days:
            freshness["aging"] += 1
        else:
            freshness["stale"] += 1

        # low confidence (called 'once' or missing)
        conf = meta.get("confidence")
        if conf in (None, "once", ""):
            low_confidence_count += 1

    return QualityMetrics(
        total_documents=sum(by_status.values()),
        by_status=dict(by_status),
        by_kind=dict(by_kind),
        flagged=dict(flagged),
        flagged_doc_ids=flagged_ids,
        freshness_buckets=dict(freshness),
        low_confidence_count=low_confidence_count,
    )


# ---------- 2. Freshness ----------


@dataclass(frozen=True)
class FreshnessMetrics:
    total_documents: int = 0
    by_expiry_status: dict[str, int] = field(default_factory=dict)
    expiring_doc_ids: list[str] = field(default_factory=list)
    expired_doc_ids: list[str] = field(default_factory=list)
    median_days_remaining: float | None = None
    soonest_expiry_days: float | None = None

    def as_dict(self) -> dict:
        return {
            "total_documents": self.total_documents,
            "by_expiry_status": dict(self.by_expiry_status),
            "expiring_doc_ids": list(self.expiring_doc_ids),
            "expired_doc_ids": list(self.expired_doc_ids),
            "median_days_remaining": self.median_days_remaining,
            "soonest_expiry_days": self.soonest_expiry_days,
        }


def compute_freshness_metrics(
    documents: Iterable[Document],
    *,
    now: datetime | float | None = None,
    warning_days: int = EXPIRY_WARNING_DAYS,
    ttl_days: int = DEFAULT_BIZRULE_TTL_DAYS,
) -> FreshnessMetrics:
    """Aggregate expiry distribution across documents.

    Docs whose meta contains `valid_until` are evaluated; missing fields yield
    'no_expiry'. Documents whose kind is bizrule / standard and which lack
    `valid_until` get an implicit TTL applied for the median calculation.
    """
    by_status: Counter[str] = Counter()
    expiring_ids: list[str] = []
    expired_ids: list[str] = []
    days_remaining_samples: list[float] = []

    for doc in documents:
        meta = doc.meta or {}
        # If kind is bizrule / standard without explicit valid_until,
        # apply implicit TTL measured from updated_at.
        effective_meta = dict(meta)
        if not effective_meta.get("valid_until") and doc.kind in (
            KnowledgeKind.BIZRULE,
            KnowledgeKind.STANDARD,
        ):
            effective_meta["valid_until"] = _implicit_valid_until(
                doc.updated_at, ttl_days
            )

        info = evaluate_expiry(effective_meta, now=now, warning_days=warning_days)
        by_status[info.status] += 1
        if info.status == "expiring_soon":
            expiring_ids.append(doc.id)
        elif info.status == "expired":
            expired_ids.append(doc.id)
        if info.days_remaining is not None:
            days_remaining_samples.append(info.days_remaining)

    median = _median(days_remaining_samples)
    soonest = min(days_remaining_samples) if days_remaining_samples else None

    return FreshnessMetrics(
        total_documents=sum(by_status.values()),
        by_expiry_status=dict(by_status),
        expiring_doc_ids=expiring_ids,
        expired_doc_ids=expired_ids,
        median_days_remaining=median,
        soonest_expiry_days=soonest,
    )


# ---------- 3. Permission denials ----------


# Substrings (any one fires) that suggest a permission / authorization failure.
PERMISSION_ERROR_TOKENS: tuple[str, ...] = (
    "permission",
    "denied",
    "deny",
    "forbidden",
    "unauthorized",
    "not allowed",
    "access denied",
    "rbac",
    "role required",
)


@dataclass(frozen=True)
class PermissionMetrics:
    total_operations: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    denied_count: int = 0
    denied_by_kind: dict[str, int] = field(default_factory=dict)
    recent_denied_ids: list[str] = field(default_factory=list)
    unknown_count: int = 0

    def as_dict(self) -> dict:
        return {
            "total_operations": self.total_operations,
            "by_status": dict(self.by_status),
            "denied_count": self.denied_count,
            "denied_by_kind": dict(self.denied_by_kind),
            "recent_denied_ids": list(self.recent_denied_ids),
            "unknown_count": self.unknown_count,
        }


def compute_permission_metrics(
    operations: Iterable[Operation],
    *,
    recent_limit: int = 20,
    error_tokens: tuple[str, ...] = PERMISSION_ERROR_TOKENS,
) -> PermissionMetrics:
    """Aggregate operation status + identify permission denials.

    A denial is defined as an operation in FAILED status whose `error` string
    contains any of `error_tokens` (case-insensitive). 'unknown' operations
    also surface separately as a reliability signal.
    """
    by_status: Counter[str] = Counter()
    denied_by_kind: Counter[str] = Counter()
    denied_count = 0
    unknown_count = 0
    denied_records: list[tuple[str, str]] = []  # (created_at, op_id)

    for op in operations:
        by_status[op.status.value] += 1
        if op.status == OperationStatus.UNKNOWN:
            unknown_count += 1
        if op.status == OperationStatus.FAILED and _looks_like_permission_error(
            op.error, error_tokens
        ):
            denied_count += 1
            kind_key = op.kind.value if isinstance(op.kind, OperationKind) else "unknown"
            denied_by_kind[kind_key] += 1
            denied_records.append((op.created_at or "", op.id))

    # Most recent denials first; truncate.
    denied_records.sort(reverse=True)
    recent_ids = [op_id for _, op_id in denied_records[:recent_limit]]

    return PermissionMetrics(
        total_operations=sum(by_status.values()),
        by_status=dict(by_status),
        denied_count=denied_count,
        denied_by_kind=dict(denied_by_kind),
        recent_denied_ids=recent_ids,
        unknown_count=unknown_count,
    )


# ---------- 4. Sync backlog ----------


@dataclass(frozen=True)
class SyncBacklogMetrics:
    total_sources: int = 0
    stale_sources: int = 0
    total_pending_ops: int = 0
    total_conflicted_docs: int = 0
    last_failed_sources: list[str] = field(default_factory=list)
    by_run_status: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "total_sources": self.total_sources,
            "stale_sources": self.stale_sources,
            "total_pending_ops": self.total_pending_ops,
            "total_conflicted_docs": self.total_conflicted_docs,
            "last_failed_sources": list(self.last_failed_sources),
            "by_run_status": dict(self.by_run_status),
        }


# Default staleness SLA for dashboard aggregation. 1 hour is more appropriate
# than the per-source 60s heartbeat SLA used by sync_status; the latter is for
# real-time alerts, while the dashboard wants "no successful check in the last
# hour" to count as stale.
DEFAULT_SYNC_STALENESS_SLA_SECONDS = 3600


def compute_sync_backlog_metrics(
    sync_states: Iterable[SourceSyncState],
    sync_runs: Iterable[SyncRun] | None = None,
    *,
    staleness_sla_seconds: int = DEFAULT_SYNC_STALENESS_SLA_SECONDS,
    now: datetime | float | None = None,
) -> SyncBacklogMetrics:
    """Aggregate per-source sync health into dashboard metrics.

    A source is 'stale' when its last_remote_check_at is older than the SLA.
    Pending ops and conflicted docs are summed across the per-source state.
    """
    cur_epoch = _to_epoch(now)
    stale_sources = 0
    total_pending = 0
    total_conflicted = 0
    last_failed_sources: list[str] = []
    total_sources = 0

    for state in sync_states:
        total_sources += 1
        total_pending += state.pending_ops or 0
        total_conflicted += state.conflicted_docs or 0

        if _is_stale(state.last_remote_check_at, cur_epoch, staleness_sla_seconds):
            stale_sources += 1
        if state.last_failed_at:
            last_failed_sources.append(state.source_id)

    by_run_status: Counter[str] = Counter()
    if sync_runs is not None:
        for run in sync_runs:
            by_run_status[run.status.value if isinstance(run.status, SyncRunStatus) else "unknown"] += 1

    return SyncBacklogMetrics(
        total_sources=total_sources,
        stale_sources=stale_sources,
        total_pending_ops=total_pending,
        total_conflicted_docs=total_conflicted,
        last_failed_sources=last_failed_sources,
        by_run_status=dict(by_run_status),
    )


# ---------- helpers ----------


def _to_epoch(now: datetime | float | None) -> float:
    if now is None:
        return datetime.now(timezone.utc).timestamp()
    if isinstance(now, (int, float)):
        return float(now)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.timestamp()


def _parse_iso_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        s = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _days_since(iso: str | None, now_epoch: float) -> float | None:
    dt = _parse_iso_dt(iso)
    if dt is None:
        return None
    return (now_epoch - dt.timestamp()) / 86400.0


def _implicit_valid_until(updated_at: str | None, ttl_days: int) -> str | None:
    """Compute an implicit valid_until = updated_at + ttl_days (ISO)."""
    dt = _parse_iso_dt(updated_at)
    if dt is None:
        return None
    from datetime import timedelta
    return (dt + timedelta(days=ttl_days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    mid = n // 2
    if n % 2 == 1:
        return float(sorted_vals[mid])
    return (sorted_vals[mid - 1] + sorted_vals[mid]) / 2.0


def _looks_like_permission_error(error: str | None, tokens: tuple[str, ...]) -> bool:
    if not error:
        return False
    haystack = error.lower()
    return any(tok.lower() in haystack for tok in tokens)


def _is_stale(last_check: str | None, now_epoch: float, sla_seconds: int) -> bool:
    dt = _parse_iso_dt(last_check)
    if dt is None:
        return True
    return (now_epoch - dt.timestamp()) > sla_seconds


__all__ = [
    "QualityMetrics", "compute_quality_metrics",
    "FreshnessMetrics", "compute_freshness_metrics",
    "PermissionMetrics", "compute_permission_metrics",
    "SyncBacklogMetrics", "compute_sync_backlog_metrics",
    "PERMISSION_ERROR_TOKENS",
    "QUALITY_FRESH_DAYS", "QUALITY_AGING_DAYS",
    "DEFAULT_SYNC_STALENESS_SLA_SECONDS",
]