"""Local retrieval policy. No network or provider import is permitted in this batch."""
import hashlib
import json
import re

from . import config

POLICY_VERSION = "local-v1"
EXPLICIT_POLICY_VERSION = "explicit-lexical-v1"
_GENERIC = {"git", "agent", "knowbase", "如何", "怎么", "帮我", "看看", "一下", "问题", "出问题了", "怎么办", "怎么解决", "帮我看看"}
_SEARCH_FILLER = _GENERIC | {"please", "how", "find", "search", "show", "use", "using", "install", "configure", "setup", "help"}
_TECHNICAL_TERM = re.compile(r"(?<![A-Za-z0-9_.-])[A-Za-z][A-Za-z0-9_.-]{2,}")


def chinese_phrases(query):
    for run in re.findall(r"[\u4e00-\u9fff]{2,}", query):
        cleaned = re.sub(r"(怎么写|怎么办|如何|一下吗|一下)$", "", run)
        if len(cleaned) >= 2:
            yield cleaned


def lexical_expansion_terms(query, aliases):
    terms = [alias for phrase, values in aliases.items() if phrase in query for alias in values]
    terms.extend(chinese_phrases(query))
    return list(dict.fromkeys(terms))[:6]


def search_core_terms(query, aliases):
    """Prefer explicit identifiers, otherwise use the first specific technical term."""
    from .query_hygiene import _identifiers

    identifiers = [term.lower() for term in _identifiers(query) if term.lower() not in _SEARCH_FILLER]
    if not identifiers:
        terms = (term.rstrip(".").lower() for term in _TECHNICAL_TERM.findall(query))
        identifiers = [next((term for term in terms if len(term) >= 3 and term not in _SEARCH_FILLER), "")]

    by_name = {name.lower(): values for name, values in aliases.items()}
    groups = []
    for term in dict.fromkeys(filter(None, identifiers)):
        http = re.fullmatch(r"http\s*(\d{3})", term)
        variants = (f"http {http[1]}", f"http{http[1]}") if http else (term,)
        groups.append((*variants, *by_name.get(term, ())))
    return groups


def _whole_term(term):
    pattern = re.escape(term)
    if re.match(r"[A-Za-z0-9_]", term):
        pattern = r"(?<![A-Za-z0-9_.-])" + pattern
    if re.search(r"[A-Za-z0-9_]$", term):
        pattern += r"(?![A-Za-z0-9_-]|\.[A-Za-z0-9_])"
    return pattern


def explicit_lexical_evidence(conn, candidates, core_terms, terms, expansions):
    """Eligible lexical candidates and their title/tag and query-term evidence."""
    core = [re.compile("|".join(map(_whole_term, group)), re.IGNORECASE) for group in core_terms]
    support = [re.compile(_whole_term(term), re.IGNORECASE) for term in terms]
    expanded = [re.compile(_whole_term(term), re.IGNORECASE) for term in expansions]
    evidence = {}
    ids = list(candidates)
    for start in range(0, len(ids), 900):
        chunk = ids[start:start + 900]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"SELECT id,title,tags,body FROM mem_fts WHERE id IN ({placeholders})", chunk
        )
        for mid, title, tags, body in rows:
            heading = f"{title}\n{tags}"
            content = f"{heading}\n{body}"
            if not all(pattern.search(content) for pattern in core):
                continue
            evidence[mid] = (
                sum(bool(pattern.search(heading)) for pattern in core),
                sum(bool(pattern.search(heading)) for pattern in support),
                sum(bool(pattern.search(heading)) for pattern in expanded),
                sum(bool(pattern.search(content)) for pattern in support),
            )
    return evidence


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
