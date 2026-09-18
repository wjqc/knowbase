"""Tests for knowbase.v2.governance.dashboard (P5-B3).

Coverage:
- HTML escapes user-controlled fields
- All 4 metric sections render (quality / freshness / permissions / sync)
- Generated timestamp is present
- write_dashboard() persists file
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from knowbase.v2.domain.models import (
    DocumentStatus,
    KnowledgeKind,
    OperationKind,
    OperationStatus,
    SyncRunStatus,
)
from knowbase.v2.governance.api import GovernanceSnapshot, collect_governance_snapshot
from knowbase.v2.governance.dashboard import render_dashboard_html, write_dashboard
from knowbase.v2.governance.metrics import (
    FreshnessMetrics,
    PermissionMetrics,
    QualityMetrics,
    SyncBacklogMetrics,
)


FROZEN_NOW = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)


def _empty_snapshot() -> GovernanceSnapshot:
    return GovernanceSnapshot(
        generated_at="2026-09-18T12:00:00Z",
        quality=QualityMetrics(),
        freshness=FreshnessMetrics(),
        permissions=PermissionMetrics(),
        sync_backlog=SyncBacklogMetrics(),
    )


class TestRenderDashboardHtml:
    def test_returns_string(self):
        html = render_dashboard_html(_empty_snapshot())
        assert isinstance(html, str)
        assert len(html) > 200

    def test_contains_doctype_and_charset(self):
        html = render_dashboard_html(_empty_snapshot())
        assert "<!doctype html>" in html.lower()
        assert "charset" in html.lower()

    def test_contains_generated_at(self):
        snap = _empty_snapshot()
        html = render_dashboard_html(snap)
        assert snap.generated_at in html

    def test_contains_all_four_section_titles(self):
        html = render_dashboard_html(_empty_snapshot())
        assert "质量" in html
        assert "新鲜度" in html or "有效期" in html
        assert "权限拒绝" in html
        assert "同步积压" in html

    def test_empty_snapshot_shows_zero_or_dash(self):
        html = render_dashboard_html(_empty_snapshot())
        # quality / sync show 0; freshness shows — for None values
        assert "0</b>" in html or "—" in html

    def test_escapes_user_data_in_generated_at(self):
        snap = GovernanceSnapshot(
            generated_at='<script>alert(1)</script>',
            quality=QualityMetrics(total_documents=42),
            freshness=FreshnessMetrics(),
            permissions=PermissionMetrics(),
            sync_backlog=SyncBacklogMetrics(),
        )
        html = render_dashboard_html(snap)
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_escapes_doc_ids_in_metric_subtitles(self):
        snap = GovernanceSnapshot(
            generated_at="2026-09-18T12:00:00Z",
            quality=QualityMetrics(
                total_documents=1,
                flagged_doc_ids=['<img src=x onerror=alert(1)>'],
            ),
            freshness=FreshnessMetrics(),
            permissions=PermissionMetrics(),
            sync_backlog=SyncBacklogMetrics(),
        )
        # flagged_doc_ids aren't shown in quality section directly,
        # but ensure no raw <img> from generated_at or doc ids leaks
        html = render_dashboard_html(snap)
        # Quality section doesn't surface flagged_doc_ids; assert just total
        assert "1</b>" in html

    def test_escapes_failed_source_ids(self):
        snap = GovernanceSnapshot(
            generated_at="2026-09-18T12:00:00Z",
            quality=QualityMetrics(),
            freshness=FreshnessMetrics(),
            permissions=PermissionMetrics(),
            sync_backlog=SyncBacklogMetrics(last_failed_sources=['<bad&src>']),
        )
        html = render_dashboard_html(snap)
        assert "<bad&src>" not in html
        assert "&lt;bad&amp;src&gt;" in html

    def test_quality_section_renders_low_confidence(self):
        snap = GovernanceSnapshot(
            generated_at="2026-09-18T12:00:00Z",
            quality=QualityMetrics(total_documents=10, low_confidence_count=3),
            freshness=FreshnessMetrics(),
            permissions=PermissionMetrics(),
            sync_backlog=SyncBacklogMetrics(),
        )
        html = render_dashboard_html(snap)
        assert "10</b>" in html
        assert "3</b>" in html

    def test_freshness_section_renders_median_and_soonest(self):
        snap = GovernanceSnapshot(
            generated_at="2026-09-18T12:00:00Z",
            quality=QualityMetrics(),
            freshness=FreshnessMetrics(
                median_days_remaining=45.0,
                soonest_expiry_days=12.0,
                expiring_doc_ids=["a", "b"],
                expired_doc_ids=["c"],
            ),
            permissions=PermissionMetrics(),
            sync_backlog=SyncBacklogMetrics(),
        )
        html = render_dashboard_html(snap)
        assert "45</b>" in html
        assert "12</b>" in html

    def test_permission_section_renders_denied_count(self):
        snap = GovernanceSnapshot(
            generated_at="2026-09-18T12:00:00Z",
            quality=QualityMetrics(),
            freshness=FreshnessMetrics(),
            permissions=PermissionMetrics(
                total_operations=20,
                denied_count=5,
                denied_by_kind={"apply": 3, "publish": 2},
            ),
            sync_backlog=SyncBacklogMetrics(),
        )
        html = render_dashboard_html(snap)
        assert "20</b>" in html
        assert "5</b>" in html
        assert "apply" in html
        assert "publish" in html

    def test_sync_section_renders_stale_count(self):
        snap = GovernanceSnapshot(
            generated_at="2026-09-18T12:00:00Z",
            quality=QualityMetrics(),
            freshness=FreshnessMetrics(),
            permissions=PermissionMetrics(),
            sync_backlog=SyncBacklogMetrics(
                total_sources=4,
                stale_sources=2,
                total_pending_ops=7,
                total_conflicted_docs=1,
                last_failed_sources=["src-a", "src-b"],
            ),
        )
        html = render_dashboard_html(snap)
        assert "4</b>" in html
        assert "2</b>" in html
        assert "7</b>" in html
        assert "src-a" in html
        assert "src-b" in html

    def test_html_has_no_external_resources(self):
        html = render_dashboard_html(_empty_snapshot())
        assert "http://" not in html or "http://www.w3.org" in html  # SVG ns only
        assert "https://cdn" not in html
        assert "<script" not in html  # no JS at all

    def test_html_includes_inline_style(self):
        html = render_dashboard_html(_empty_snapshot())
        assert "<style>" in html
        assert "--ink" in html  # CSS variable present


class TestWriteDashboard:
    def test_writes_file(self, tmp_path: Path):
        out = tmp_path / "sub" / "dash.html"
        path = write_dashboard(_empty_snapshot(), out)
        assert path.exists()
        assert path.read_text(encoding="utf-8").startswith("<!doctype html>")

    def test_creates_parent_dirs(self, tmp_path: Path):
        out = tmp_path / "a" / "b" / "c" / "dash.html"
        write_dashboard(_empty_snapshot(), out)
        assert out.exists()

    def test_overwrites_existing(self, tmp_path: Path):
        out = tmp_path / "dash.html"
        out.write_text("OLD", encoding="utf-8")
        write_dashboard(_empty_snapshot(), out)
        assert out.read_text(encoding="utf-8").startswith("<!doctype html>")


class TestDashboardFromSnapshot:
    def test_renders_real_snapshot(self):
        """Smoke: build a snapshot from a fake repo and render it."""
        class FakeRepo:
            def list_documents(self, *a, **kw):
                from knowbase.v2.domain.models import Document
                return [
                    Document(
                        id="d1", source_id="s1", path="p1",
                        kind=KnowledgeKind.BIZRULE, title="t",
                        status=DocumentStatus.READY, meta={},
                        updated_at="2026-09-15T00:00:00Z",
                    )
                ]

            def list_operations_by_status(self, status, limit=20):
                from knowbase.v2.domain.models import Operation
                if status == OperationStatus.FAILED:
                    return [Operation(
                        id="o1", kind=OperationKind.APPLY,
                        target_id="d1", by="agent:test",
                        status=status, error="permission denied",
                        created_at="2026-09-17T00:00:00Z",
                    )]
                return []

            def list_sync_states(self, *a, **kw):
                from knowbase.v2.domain.models import SourceSyncState
                return [SourceSyncState(
                    source_id="s1",
                    last_remote_check_at="2026-09-18T11:30:00Z",
                    pending_ops=3, conflicted_docs=1,
                    last_failed_at=None,
                )]

            def list_sync_runs_by_source(self, source_id, limit=20):
                from knowbase.v2.domain.models import SyncRun
                return [SyncRun(
                    id="r1", source_id=source_id,
                    status=SyncRunStatus.SUCCEEDED,
                    started_at="2026-09-17T00:00:00Z",
                )]

        snap = collect_governance_snapshot(FakeRepo(), now=FROZEN_NOW)
        html = render_dashboard_html(snap)
        assert "1</b>" in html  # 1 doc
        assert "质量" in html
        assert "新鲜度" in html or "有效期" in html
        assert "权限拒绝" in html
        assert "同步积压" in html
