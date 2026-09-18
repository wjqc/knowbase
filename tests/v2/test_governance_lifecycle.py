"""Tests for knowbase.v2.governance.lifecycle (P5-B1).

Coverage:
- evaluate_expiry: no_expiry / active / expiring_soon / expired / unparseable / None inputs
- detect_conflicts: single supersede / bidirectional / non-conflict filter / dedup
- suggest_status_after_supersede: archived / stale / fresh / tombstoned
- can_approve: role hierarchy + kind gate
- dataclass as_dict: round-trip
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from knowbase.v2.governance.lifecycle import (
    APPROVAL_REQUIRED_KINDS,
    ConflictPair,
    DEFAULT_BIZRULE_TTL_DAYS,
    EXPIRY_WARNING_DAYS,
    ExpiryInfo,
    ExpiryStatus,
    can_approve,
    detect_conflicts,
    evaluate_expiry,
    suggest_status_after_supersede,
)


# A fixed reference instant for deterministic expiry tests.
FROZEN_NOW = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
FROZEN_EPOCH = FROZEN_NOW.timestamp()


# ---------- evaluate_expiry ----------

class TestEvaluateExpiryNoExpiry:
    def test_missing_field_returns_no_expiry(self):
        info = evaluate_expiry({}, now=FROZEN_EPOCH)
        assert info.status == "no_expiry"
        assert info.days_remaining is None
        assert info.valid_until is None

    def test_empty_string_returns_no_expiry(self):
        info = evaluate_expiry({"valid_until": ""}, now=FROZEN_EPOCH)
        assert info.status == "no_expiry"
        assert info.valid_until is None

    def test_none_value_returns_no_expiry(self):
        info = evaluate_expiry({"valid_until": None}, now=FROZEN_EPOCH)
        assert info.status == "no_expiry"


class TestEvaluateExpiryActive:
    def test_far_future_is_active(self):
        # 365 days from now -> well outside warning window
        future = (FROZEN_NOW + timedelta(days=365)).isoformat().replace("+00:00", "Z")
        info = evaluate_expiry({"valid_until": future}, now=FROZEN_EPOCH)
        assert info.status == "active"
        assert info.days_remaining is not None
        assert info.days_remaining > 360

    def test_active_uses_warning_days_default(self):
        # 60 days out -> active (since default warning is 30d)
        future = (FROZEN_NOW + timedelta(days=60)).isoformat().replace("+00:00", "Z")
        info = evaluate_expiry({"valid_until": future}, now=FROZEN_EPOCH)
        assert info.status == "active"


class TestEvaluateExpiryExpiringSoon:
    def test_within_default_warning_window_is_expiring_soon(self):
        # 10 days out -> within 30d warning -> expiring_soon
        future = (FROZEN_NOW + timedelta(days=10)).isoformat().replace("+00:00", "Z")
        info = evaluate_expiry({"valid_until": future}, now=FROZEN_EPOCH)
        assert info.status == "expiring_soon"
        assert 0 <= info.days_remaining < EXPIRY_WARNING_DAYS

    def test_one_day_remaining_is_expiring_soon(self):
        future = (FROZEN_NOW + timedelta(days=1)).isoformat().replace("+00:00", "Z")
        info = evaluate_expiry({"valid_until": future}, now=FROZEN_EPOCH)
        assert info.status == "expiring_soon"

    def test_custom_warning_days(self):
        # 50 days out, but warning=60 -> expiring_soon
        future = (FROZEN_NOW + timedelta(days=50)).isoformat().replace("+00:00", "Z")
        info = evaluate_expiry({"valid_until": future}, now=FROZEN_EPOCH, warning_days=60)
        assert info.status == "expiring_soon"


class TestEvaluateExpiryExpired:
    def test_past_time_is_expired(self):
        past = (FROZEN_NOW - timedelta(days=1)).isoformat().replace("+00:00", "Z")
        info = evaluate_expiry({"valid_until": past}, now=FROZEN_EPOCH)
        assert info.status == "expired"
        assert info.days_remaining < 0

    def test_far_past_is_expired(self):
        past = (FROZEN_NOW - timedelta(days=365)).isoformat().replace("+00:00", "Z")
        info = evaluate_expiry({"valid_until": past}, now=FROZEN_EPOCH)
        assert info.status == "expired"
        assert info.days_remaining < -300

    def test_unparseable_value_is_expired_conservative(self):
        info = evaluate_expiry(
            {"valid_until": "not-a-date"}, now=FROZEN_EPOCH
        )
        assert info.status == "expired"
        assert info.days_remaining is None
        assert info.valid_until == "not-a-date"


class TestEvaluateExpiryAcceptsVariousNow:
    def test_accepts_epoch_float(self):
        future = (FROZEN_NOW + timedelta(days=100)).isoformat().replace("+00:00", "Z")
        info = evaluate_expiry({"valid_until": future}, now=FROZEN_EPOCH)
        assert info.status == "active"

    def test_accepts_datetime_naive_normalized_to_utc(self):
        naive_future = (FROZEN_NOW + timedelta(days=100)).replace(tzinfo=None)
        future = naive_future.isoformat()
        info = evaluate_expiry({"valid_until": future}, now=FROZEN_NOW)
        assert info.status == "active"

    def test_now_none_uses_wall_clock_without_throw(self):
        # Should not raise; result is one of the valid statuses.
        future = (datetime.now(timezone.utc) + timedelta(days=100)).isoformat().replace("+00:00", "Z")
        info = evaluate_expiry({"valid_until": future}, now=None)
        assert info.status in {"active", "expiring_soon"}


class TestEvaluateExpiryAcceptsIsoFormats:
    def test_accepts_z_suffix(self):
        future = (FROZEN_NOW + timedelta(days=100)).strftime("%Y-%m-%dT%H:%M:%SZ")
        info = evaluate_expiry({"valid_until": future}, now=FROZEN_EPOCH)
        assert info.status == "active"

    def test_accepts_offset_suffix(self):
        future = (FROZEN_NOW + timedelta(days=100)).isoformat()
        info = evaluate_expiry({"valid_until": future}, now=FROZEN_EPOCH)
        assert info.status == "active"


class TestExpiryInfoDataclass:
    def test_as_dict_rounds_days_remaining(self):
        info = ExpiryInfo(
            status="expiring_soon",
            days_remaining=3.4567,
            valid_until="2099-01-01T00:00:00Z",
        )
        d = info.as_dict()
        assert d["status"] == "expiring_soon"
        assert d["days_remaining"] == 3.5
        assert d["valid_until"] == "2099-01-01T00:00:00Z"

    def test_as_dict_keeps_none(self):
        info = ExpiryInfo(status="no_expiry", days_remaining=None, valid_until=None)
        d = info.as_dict()
        assert d["days_remaining"] is None
        assert d["valid_until"] is None

    def test_is_frozen(self):
        info = ExpiryInfo(status="active", days_remaining=10.0, valid_until="x")
        with pytest.raises(Exception):
            info.status = "expired"  # type: ignore[misc]


# ---------- detect_conflicts ----------

class TestDetectConflicts:
    def test_single_supersede(self):
        docs = [
            {"id": "A", "relations": [{"type": "supersedes", "target_id": "B"}]},
            {"id": "B", "relations": []},
        ]
        pairs = detect_conflicts(docs)
        assert len(pairs) == 1
        assert pairs[0].source_id == "A"
        assert pairs[0].target_id == "B"
        assert pairs[0].relation == "supersedes"
        assert pairs[0].bidirectional is False

    def test_bidirectional_when_target_reflects(self):
        docs = [
            {"id": "A", "relations": [{"type": "supersedes", "target_id": "B"}]},
            {"id": "B", "relations": [{"type": "supersedes", "target_id": "A"}]},
        ]
        pairs = detect_conflicts(docs)
        # Both A->B and B->A are emitted; both should be bidirectional.
        assert len(pairs) == 2
        assert all(p.bidirectional for p in pairs)

    def test_contradict_bidirectional(self):
        docs = [
            {"id": "X", "relations": [{"type": "contradicts", "target_id": "Y"}]},
            {"id": "Y", "relations": [{"type": "contradicts", "target_id": "X"}]},
        ]
        pairs = detect_conflicts(docs, relation_types=("contradicts",))
        assert len(pairs) == 2
        assert all(p.relation == "contradicts" for p in pairs)
        assert all(p.bidirectional for p in pairs)

    def test_relation_types_filter_excludes_extends(self):
        docs = [
            {"id": "A", "relations": [{"type": "extends", "target_id": "B"}]},
            {"id": "B", "relations": []},
        ]
        pairs = detect_conflicts(docs)  # default excludes 'extends'
        assert pairs == []

    def test_relation_types_includes_extends_when_requested(self):
        docs = [
            {"id": "A", "relations": [{"type": "extends", "target_id": "B"}]},
            {"id": "B", "relations": []},
        ]
        pairs = detect_conflicts(docs, relation_types=("extends",))
        assert len(pairs) == 1
        assert pairs[0].relation == "extends"

    def test_missing_id_skipped(self):
        docs = [
            {"id": None, "relations": [{"type": "supersedes", "target_id": "B"}]},
            {"id": "B", "relations": []},
        ]
        assert detect_conflicts(docs) == []

    def test_dedup_within_source(self):
        docs = [
            {
                "id": "A",
                "relations": [
                    {"type": "supersedes", "target_id": "B"},
                    {"type": "supersedes", "target_id": "B"},
                ],
            },
            {"id": "B", "relations": []},
        ]
        pairs = detect_conflicts(docs)
        assert len(pairs) == 1

    def test_relation_without_target_id_skipped(self):
        docs = [
            {"id": "A", "relations": [{"type": "supersedes"}]},
            {"id": "B", "relations": []},
        ]
        assert detect_conflicts(docs) == []

    def test_target_id_falls_back_to_id_key(self):
        docs = [
            {"id": "A", "relations": [{"type": "supersedes", "id": "B"}]},
            {"id": "B", "relations": []},
        ]
        pairs = detect_conflicts(docs)
        assert len(pairs) == 1
        assert pairs[0].target_id == "B"

    def test_empty_documents_returns_empty(self):
        assert detect_conflicts([]) == []


class TestConflictPairAsDict:
    def test_round_trip(self):
        pair = ConflictPair(
            source_id="A", target_id="B", relation="supersedes", bidirectional=True
        )
        d = pair.as_dict()
        assert d == {
            "source_id": "A",
            "target_id": "B",
            "relation": "supersedes",
            "bidirectional": True,
        }


# ---------- suggest_status_after_supersede ----------

class TestSuggestStatusAfterSupersede:
    @pytest.mark.parametrize(
        "current,expected",
        [
            ("active", "stale"),
            ("", "stale"),
            (None, "stale"),
            ("stale", "stale"),
            ("archived", "archived"),
            ("tombstoned", "tombstoned"),
            ("ACTIVE", "stale"),  # case-insensitive input
        ],
    )
    def test_status_mapping(self, current, expected):
        meta = {"status": current} if current is not None else {}
        assert suggest_status_after_supersede(meta) == expected

    def test_keeps_unknown_status(self):
        # 'unknown' isn't in keep-set -> falls through to 'stale'
        assert suggest_status_after_supersede({"status": "unknown"}) == "stale"


# ---------- can_approve ----------

class TestCanApprove:
    def test_reviewer_can_approve_bizrule(self):
        assert can_approve("reviewer", "bizrule") is True

    def test_reviewer_can_approve_standard(self):
        assert can_approve("reviewer", "standard") is True

    def test_contributor_cannot_approve_bizrule(self):
        assert can_approve("contributor", "bizrule") is False

    def test_contributor_cannot_approve_standard(self):
        assert can_approve("contributor", "standard") is False

    def test_contributor_can_approve_other_kinds(self):
        for kind in ("workflow", "note", "snippet", "code"):
            assert can_approve("contributor", kind) is True, kind

    def test_viewer_cannot_approve_anything(self):
        assert can_approve("viewer", "workflow") is False
        assert can_approve("viewer", "bizrule") is False

    def test_project_admin_can_approve_all(self):
        for kind in ("bizrule", "standard", "workflow"):
            assert can_approve("project_admin", kind) is True

    def test_platform_admin_can_approve_all(self):
        for kind in ("bizrule", "standard", "workflow"):
            assert can_approve("platform_admin", kind) is True

    def test_unknown_role_denied(self):
        assert can_approve("nonexistent", "bizrule") is False
        assert can_approve("nonexistent", "workflow") is False

    def test_empty_role_denied(self):
        assert can_approve("", "bizrule") is False

    def test_case_insensitive_role(self):
        assert can_approve("Reviewer", "bizrule") is True
        assert can_approve("REVIEWER", "bizrule") is True

    def test_approval_required_kinds_constant(self):
        assert APPROVAL_REQUIRED_KINDS == frozenset({"standard", "bizrule"})

    def test_default_ttl_constant(self):
        assert DEFAULT_BIZRULE_TTL_DAYS == 180

    def test_default_warning_days_constant(self):
        assert EXPIRY_WARNING_DAYS == 30


# Note: `ExpiryStatus` is a typing.Literal alias used only as a static type hint.
# In Python 3.13 Literal cannot be instantiated at runtime; runtime membership
# is enforced by code paths in evaluate_expiry() instead.