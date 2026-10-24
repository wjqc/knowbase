"""Local retrieval policy. No network or provider import is permitted in this batch."""
import hashlib
import json
import re

from . import config

POLICY_VERSION = "local-v1"
_GENERIC = {"git", "agent", "knowbase", "如何", "怎么", "帮我", "看看", "一下", "问题", "出问题了", "怎么办", "怎么解决", "帮我看看"}


def local_policy(aliases=None):
    from .index import _load_aliases
    cfg = config.load_config().get("retrieval", {}) or {}
    cfg = cfg if isinstance(cfg, dict) else {"mode": "invalid"}
    mode = str(cfg.get("mode", "auto")).lower()
    level = "L1" if mode in {"raw_fts", "l1"} else "L2"
    reason = None if mode in {"auto", "lexical_only", "raw_fts", "l1", "l2"} else "invalid_mode"
    if reason:
        level = "L1"
    aliases = (_load_aliases() if aliases is None else aliases) if level == "L2" else {}
    if not aliases:
        level = "L1"
    revision = hashlib.sha256(json.dumps(aliases, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
    return {"level": level, "channels": ["lexical"], "reason": reason,
            "dictionary_revision": revision, "policy_revision": POLICY_VERSION,
            "semantic_status": "local_only", "remote_calls": 0}, aliases


def automatic_match(conn, query, mid):
    """Conservative lexical evidence, not a calibrated semantic relevance score."""
    from .query_hygiene import _identifiers
    row = conn.execute("SELECT title,tags,body FROM mem_fts WHERE id=?", (mid,)).fetchone()
    if not row:
        return False
    blob = " ".join(str(v or "") for v in row).lower()
    identifiers = _identifiers(query) or [t for t in re.findall(r"[A-Za-z][A-Za-z0-9_.-]{2,}", query) if t.lower() not in _GENERIC]
    if identifiers:
        # Keep error codes/classes intact, and do not confuse HTTP 401 with 403.
        return all(re.search(r"(?<![A-Za-z0-9_.-])" + re.escape(t.lower()) + r"(?![A-Za-z0-9_.-])", blob)
                   for t in identifiers)
    terms = [t for t in re.findall(r"[A-Za-z][\w.:-]*|[\u4e00-\u9fff]{2,}", query.lower())
             if t not in _GENERIC]
    return bool(terms) and all(t in blob for t in terms)
