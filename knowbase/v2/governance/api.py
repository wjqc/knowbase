"""V2 governance API facade (P5-B2).

`collect_governance_snapshot(repo)` pulls the four metric categories from a
V2 repository and returns a single dict suitable for the dashboard.

Design:
- Snapshot is point-in-time; caller may schedule periodically.
- The repository wiring lives here; metrics functions stay aggregation-only so
  they remain pure-testable (see tests/v2/test_governance_metrics.py).
- Operation sampling is bounded (last N) to keep snapshots cheap on large repos.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

from knowbase.v2.governance.metrics import (
    FreshnessMetrics,
    PermissionMetrics,
    QualityMetrics,
    SyncBacklogMetrics,
    compute_freshness_metrics,
    compute_permission_metrics,
    compute_quality_metrics,
    compute_sync_backlog_metrics,
)
from knowbase.v2.repositories.sqlite_repo import V2Repository


# Operation sampling bound. Snapshots are point-in-time; we don't need full
# history for dashboard trends. P5-B2 default = last 1000.
DEFAULT_OPERATION_SAMPLE_LIMIT = 1000

# Sync-run sample bound.
DEFAULT_SYNC_RUN_SAMPLE_LIMIT = 200


class _RepoLike(Protocol):
    """Minimal repo surface required by collect_governance_snapshot."""

    def list_documents(self, *args, **kwargs): ...
    def list_operations_by_status(self, *args, **kwargs): ...
    def list_sync_states(self, *args, **kwargs): ...
    def list_sync_runs_by_source(self, *args, **kwargs): ...


@dataclass(frozen=True)
class GovernanceSnapshot:
    generated_at: str
    quality: QualityMetrics
    freshness: FreshnessMetrics
    permissions: PermissionMetrics
    sync_backlog: SyncBacklogMetrics

    def as_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "quality": self.quality.as_dict(),
            "freshness": self.freshness.as_dict(),
            "permissions": self.permissions.as_dict(),
            "sync_backlog": self.sync_backlog.as_dict(),
        }


def collect_governance_snapshot(
    repo: _RepoLike,
    *,
    operation_sample_limit: int = DEFAULT_OPERATION_SAMPLE_LIMIT,
    sync_run_sample_limit: int = DEFAULT_SYNC_RUN_SAMPLE_LIMIT,
    now: datetime | float | None = None,
) -> GovernanceSnapshot:
    """Collect a dashboard-ready governance snapshot from a V2 repository.

    Args:
        repo: anything with `list_documents`, `list_operations_by_status`,
            `list_sync_states`, `list_sync_runs_by_source`.
        operation_sample_limit: per-status cap when scanning operations.
        sync_run_sample_limit: per-source cap when scanning sync_runs.
        now: time anchor for `now` computations (datetime / epoch / None).

    The function tolerates partial repositories (e.g. empty V1-era projects
    without sync_state rows, or DB schema drift) by falling back to empty
    collections when a top-level method raises. Per-row failures during scan
    are handled inside the metrics helpers.
    """
    documents = _safe_call(repo.list_documents) or []
    operations = _collect_operations(repo, operation_sample_limit)
    sync_states = _safe_call(repo.list_sync_states) or []
    sync_runs = _collect_sync_runs(sync_states, repo, sync_run_sample_limit)

    quality = compute_quality_metrics(documents, now=now)
    freshness = compute_freshness_metrics(documents, now=now)
    permissions = compute_permission_metrics(operations)
    sync_backlog = compute_sync_backlog_metrics(sync_states, sync_runs, now=now)

    return GovernanceSnapshot(
        generated_at=_now_iso(now),
        quality=quality,
        freshness=freshness,
        permissions=permissions,
        sync_backlog=sync_backlog,
    )


def _safe_call(fn) -> list:
    """Run a repo accessor and return [] on any exception."""
    try:
        return list(fn())
    except Exception:
        return []


def _collect_operations(repo: _RepoLike, limit: int) -> list:
    """Pull a bounded sample of operations across statuses.

    `list_operations_by_status` accepts an enum + limit; we walk all statuses
    so the dashboard sees pending + failed + unknown + running + succeeded.
    """
    from knowbase.v2.domain.models import OperationStatus

    out: list = []
    for status in OperationStatus:
        try:
            rows = repo.list_operations_by_status(status, limit=limit)
        except Exception:
            rows = []
        out.extend(rows)
    return out


def _collect_sync_runs(sync_states: list, repo: _RepoLike, limit: int) -> list:
    """Pull sync_runs from each known source (bounded per source).

    Sync states are passed in (not re-fetched) so the caller's already-tolerated
    `_safe_call(repo.list_sync_states)` result is reused; per-source failures
    on `list_sync_runs_by_source` are still swallowed here.
    """
    out: list = []
    for state in sync_states:
        try:
            rows = repo.list_sync_runs_by_source(state.source_id, limit=limit)
        except Exception:
            rows = []
        out.extend(rows)
    return out


def _now_iso(now: datetime | float | None) -> str:
    if now is None:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(now, (int, float)):
        return datetime.fromtimestamp(float(now), tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "GovernanceSnapshot",
    "collect_governance_snapshot",
    "DEFAULT_OPERATION_SAMPLE_LIMIT",
    "DEFAULT_SYNC_RUN_SAMPLE_LIMIT",
]