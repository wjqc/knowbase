"""V2 governance lifecycle (P5-B1).

Responsibilities:
1. Expiry detection: evaluate `valid_until` for bizrule / standard
2. Conflict detection: identify supersede / contradict relations
3. Role gate: decide whether a role may approve a given KnowledgeKind

Design notes:
- Pure functions for easy unit-test and deterministic replay.
- `now` parameter is injectable for deterministic tests.
- Complements retrieval/governance.py: retrieval computes ranking factors;
  this module handles lifecycle / workflow decisions.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Literal

# ---------- Expiry ----------

# Default TTL (days) for bizrule. Source: V2 plan section 8.2 recommends 180d (~6 months).
DEFAULT_BIZRULE_TTL_DAYS = 180

# Warning window (days): if expiry is within this many days, status becomes 'expiring_soon'.
EXPIRY_WARNING_DAYS = 30


def _parse_iso(value: str | None) -> float | None:
    """Parse ISO 8601 string to epoch seconds (UTC). Returns None for empty input."""
    if not value:
        return None
    try:
        # Accept '2026-09-18T10:30:00Z' / '2026-09-18T10:30:00+00:00' / '2026-09-18'
        s = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def _now_epoch(now: datetime | float | None) -> float:
    """Accept datetime / epoch seconds / None; return epoch seconds."""
    if now is None:
        return datetime.now(timezone.utc).timestamp()
    if isinstance(now, (int, float)):
        return float(now)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.timestamp()


ExpiryStatus = Literal["active", "expiring_soon", "expired", "no_expiry"]


@dataclass(frozen=True)
class ExpiryInfo:
    """Expiry evaluation result.

    Attributes:
        status: one of 'active' / 'expiring_soon' / 'expired' / 'no_expiry'.
        days_remaining: positive when active/expiring_soon, negative when expired;
            None when status is 'no_expiry' or value unparseable.
        valid_until: raw ISO string (echo), None if absent.
    """

    status: ExpiryStatus
    days_remaining: float | None
    valid_until: str | None

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "days_remaining": (
                round(self.days_remaining, 1) if self.days_remaining is not None else None
            ),
            "valid_until": self.valid_until,
        }


def evaluate_expiry(
    meta: dict,
    *,
    now: datetime | float | None = None,
    warning_days: int = EXPIRY_WARNING_DAYS,
) -> ExpiryInfo:
    """Evaluate expiry status for a single memory meta.

    - Missing/empty `valid_until` -> 'no_expiry' (no mandatory expiry).
    - Unparseable `valid_until` -> 'expired' (conservative; flag for manual review).
    - `valid_until` < now -> 'expired'.
    - `valid_until - now` < warning_days -> 'expiring_soon'.
    - otherwise -> 'active'.
    """
    valid_until_raw = meta.get("valid_until") if isinstance(meta, dict) else None
    if not valid_until_raw:
        return ExpiryInfo(status="no_expiry", days_remaining=None, valid_until=None)

    until = _parse_iso(valid_until_raw)
    if until is None:
        # Field present but unparseable - return expired to flag for manual review.
        return ExpiryInfo(
            status="expired", days_remaining=None, valid_until=str(valid_until_raw)
        )

    cur = _now_epoch(now)
    delta_seconds = until - cur
    days_remaining = delta_seconds / 86400.0

    if days_remaining < 0:
        status: ExpiryStatus = "expired"
    elif days_remaining < warning_days:
        status = "expiring_soon"
    else:
        status = "active"

    return ExpiryInfo(
        status=status,
        days_remaining=days_remaining,
        valid_until=str(valid_until_raw),
    )


# ---------- Conflict detection ----------


@dataclass(frozen=True)
class ConflictPair:
    """A single supersede / contradict / extends / derived_from relation.

    Attributes:
        source_id: id of the document that initiates the relation.
        target_id: id of the related target document.
        relation: one of 'supersedes' / 'contradicts' / 'extends' / 'derived_from'.
        bidirectional: True when target's relations also reference source with same type.
    """

    source_id: str
    target_id: str
    relation: str
    bidirectional: bool = False

    def as_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "target_id": self.target_id,
            "relation": self.relation,
            "bidirectional": self.bidirectional,
        }


def detect_conflicts(
    documents: Iterable[dict],
    *,
    relation_types: tuple[str, ...] = ("supersedes", "contradicts"),
) -> list[ConflictPair]:
    """Detect conflict pairs from a collection of document dicts.

    Each document dict should contain:
    - id: str
    - relations: list[dict], each with 'target_id' and 'type'

    Returns ConflictPair entries for every relation whose type is in `relation_types`.
    If the target document also references source with the same type, mark bidirectional=True.
    """
    by_id: dict[str, dict] = {}
    for d in documents:
        if d.get("id"):
            by_id[d["id"]] = d

    pairs: list[ConflictPair] = []
    seen: set[tuple[str, str, str]] = set()
    for doc in documents:
        source_id = doc.get("id")
        if not source_id:
            continue
        for rel in doc.get("relations") or []:
            rel_type = rel.get("type")
            target_id = rel.get("target_id") or rel.get("id")
            if not target_id or rel_type not in relation_types:
                continue
            key = (source_id, target_id, rel_type)
            if key in seen:
                continue
            seen.add(key)
            target = by_id.get(target_id, {})
            back_rels = target.get("relations") or []
            bidirectional = any(
                br.get("type") == rel_type
                and (br.get("target_id") or br.get("id")) == source_id
                for br in back_rels
            )
            pairs.append(
                ConflictPair(
                    source_id=source_id,
                    target_id=target_id,
                    relation=rel_type,
                    bidirectional=bidirectional,
                )
            )
    return pairs


def suggest_status_after_supersede(target_meta: dict) -> str:
    """Suggest target document status after a supersede relation.

    - If already archived / tombstoned / stale -> keep current status (no rewrite).
    - Otherwise -> return 'stale' (V1 server convention: supersedes auto-marks stale).
    """
    status = (target_meta.get("status") or "").lower()
    if status in {"archived", "tombstoned", "stale"}:
        return status
    return "stale"


# ---------- Role gate ----------


# Which kinds require REVIEWER (or higher) for staging proposals.
APPROVAL_REQUIRED_KINDS: frozenset[str] = frozenset({"standard", "bizrule"})

# Role hierarchy. Higher number = higher privilege.
_ROLE_LEVEL = {
    "viewer": 1,
    "contributor": 2,
    "reviewer": 3,
    "project_admin": 4,
    "platform_admin": 5,
}


def can_approve(role: str, kind: str) -> bool:
    """Decide whether the given role may approve a staging proposal of the given kind.

    - standard / bizrule -> requires reviewer or higher.
    - other kinds -> contributor is sufficient (no special approval needed).
    """
    level = _ROLE_LEVEL.get((role or "").lower(), 0)
    if kind in APPROVAL_REQUIRED_KINDS:
        return level >= _ROLE_LEVEL["reviewer"]
    return level >= _ROLE_LEVEL["contributor"]


__all__ = [
    "DEFAULT_BIZRULE_TTL_DAYS",
    "EXPIRY_WARNING_DAYS",
    "ExpiryInfo",
    "ExpiryStatus",
    "evaluate_expiry",
    "detect_conflicts",
    "ConflictPair",
    "suggest_status_after_supersede",
    "APPROVAL_REQUIRED_KINDS",
    "can_approve",
]