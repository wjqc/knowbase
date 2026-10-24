"""Execution-derived evaluation datasets: no human labels required.

Derivation rule (per query-card pair; every sample is version-bound):
- adopted + helpful feedback + completed task  -> positive sample
- adopted + negative feedback, or a failed/abandoned task -> negative sample
- delivered but never adopted -> irrelevant sample
- adopted but with no feedback/task outcome at all -> unresolved: the query
  is skipped and counted, never padded into any sample class. Unbound
  evidence must not inflate the dataset.

Privacy is a deterministic gate, not a silent filter: a candidate row whose
query or hit titles match the credential scrub patterns is excluded AND
counted in excluded_for_privacy.

The derived rows reuse the retrieval_evaluation dataset shape so the
existing offline replay (export_cache + run) and the frozen-report gate can
consume them unchanged: labels.status is 'execution_derived', the labeler
is 'execution_derived', and each row carries an evidence binding (search
event id, use ids, task keys, card revisions) plus a corpus_revision that
is the digest of that binding.
"""

import json
import os
import sqlite3

from .credentials import transform
from .embedding_provider import digest

MAX_QUERY_CHARS = 512  # same bound the dense path enforces at query time
NEGATIVE_OUTCOMES = {"not_helpful", "incorrect", "outdated"}
FAILED_RESULTS = {"failed", "abandoned"}


def _scrubbed(text):
    """True when the deterministic credential scrub changes the text."""
    return transform(text) != text


def _card_evidence(conn, use_id, mid, revision):
    """Feedback + task outcomes bound to one adopted use (current version)."""
    feedback = conn.execute(
        "SELECT f.task_key, f.outcome, t.result FROM feedback_events f "
        "LEFT JOIN task_outcomes t ON t.task_outcome_id = f.task_outcome_id "
        "WHERE f.use_id = ? AND f.memory_id = ? AND f.content_revision = ? "
        "AND f.binding = 'version_task' AND f.state = 'applied'",
        (use_id, mid, revision)).fetchall()
    tasks = {r[0] for r in conn.execute(
        "SELECT result FROM task_outcomes WHERE use_id = ?", (use_id,)).fetchall()}
    task_keys = sorted({row[0] for row in feedback})
    failed = bool(tasks & FAILED_RESULTS)
    negative = failed or any(row[1] in NEGATIVE_OUTCOMES for row in feedback)
    positive = (not failed and any(
        row[1] == "helpful" and row[2] == "completed" for row in feedback))
    return {"task_keys": task_keys, "positive": positive, "negative": negative}


def derive_dataset(conn, *, limit=None):
    """Derive evaluation rows from execution evidence in the index database.

    Reads search events from usage_log (any tool that logged a search_id +
    hits payload), joins them to knowledge_uses by retrieval event id, then
    to task_outcomes and feedback_events. Returns (rows, stats).
    """
    stats = {"derived": 0, "positive": 0, "negative": 0, "irrelevant": 0,
             "excluded_for_privacy": 0, "skipped_query": 0, "skipped_no_hits": 0,
             "skipped_unresolved": 0}
    rows = []
    try:
        details = [r[0] for r in conn.execute("SELECT detail FROM usage_log ORDER BY rowid")]
    except sqlite3.Error:
        return rows, stats
    for raw in details:
        if limit is not None and len(rows) >= limit:
            break
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict) or not payload.get("search_id"):
            continue
        hits = payload.get("hits")
        if not isinstance(hits, list):
            continue
        query = payload.get("query") or ""
        if not query or len(query) > MAX_QUERY_CHARS:
            stats["skipped_query"] += 1
            continue
        if not hits:
            stats["skipped_no_hits"] += 1
            continue
        uses = conn.execute(
            "SELECT use_id, memory_id, content_revision, state FROM knowledge_uses "
            "WHERE event_id = ? AND current_version = 1",
            (payload["search_id"],)).fetchall()
        adopted = [u for u in uses if u[3] == "adopted"]
        positives, negatives, unresolved = [], [], []
        used_cards = []
        for use_id, mid, revision, _state in adopted:
            ev = _card_evidence(conn, use_id, mid, revision)
            entry = {"memory_id": mid, "content_revision": revision,
                     "use_id": use_id, "task_keys": ev["task_keys"]}
            used_cards.append(entry)
            if ev["negative"]:
                negatives.append(entry)
            elif ev["positive"]:
                positives.append(entry)
            else:
                unresolved.append(entry)
        if positives:
            sample_class, relevant, unanswerable = "positive", sorted(
                p["memory_id"] for p in positives), False
        elif negatives:
            sample_class, relevant, unanswerable = "negative", [], True
        elif unresolved:
            stats["skipped_unresolved"] += 1
            continue
        else:
            sample_class, relevant, unanswerable = "irrelevant", [], False
        titles = [h.get("title", "") for h in hits if isinstance(h, dict)]
        if _scrubbed(query) or any(_scrubbed(t) for t in titles):
            stats["excluded_for_privacy"] += 1
            continue
        delivered_ids = {h["id"] for h in hits if isinstance(h, dict) and h.get("id")}
        used_ids = {u["memory_id"] for u in used_cards}
        binding = {"cards": sorted(
            (u["memory_id"], u["content_revision"]) for u in used_cards),
            "tasks": sorted({k for u in used_cards for k in u["task_keys"]})}
        evidence = {"search_id": payload["search_id"], "scope": payload.get("scope"),
                    "positive": positives, "negative": negatives,
                    "unresolved": unresolved,
                    "delivered_not_used": sorted(delivered_ids - used_ids)}
        rows.append({
            "id": "exec-" + str(payload["search_id"]),
            "query": query,
            "scope": payload.get("scope") or "global",
            "labels": {"status": "execution_derived", "labeler": "execution_derived",
                       "corpus_revision": digest(binding),
                       "relevant_ids": relevant, "unanswerable": unanswerable,
                       "sample_class": sample_class},
            "privacy_review": "approved",
            "evidence": evidence,
        })
        stats["derived"] += 1
        stats[sample_class] += 1
    return rows, stats


def write_dataset(rows, path):
    """Write derived rows as JSONL (0600) and return their digest."""
    from pathlib import Path
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("x") as f:
        os.chmod(out, 0o600)
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return {"rows": len(rows), "sha256": digest(rows)}
