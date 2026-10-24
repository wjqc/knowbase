"""Phase 2 regression tests: execution-derived evaluation gates + tiered injection.

No human annotation anywhere in the enabling path: derived samples come
from search/use/task/feedback evidence, the frozen report carries
annotation_status 'execution_derived', and dense activation is proven
end-to-end with zero human fields.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from knowbase import config, execution_eval, hooks, index, retrieval_metrics
from knowbase import semantic_index as si
from knowbase.embedding_provider import digest
from knowbase.retrieval_evaluation import export_cache, freeze_report, run, validate_labels
from test_local_retrieval_upgrade import seed


@pytest.fixture
def gateway():
    state = {'status': 200, 'response': None, 'calls': [], 'delay': 0, 'dim': 3}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def do_GET(self):
            raw = json.dumps({'data': [{'id': 'company-test'}]}).encode()
            self.send_response(200); self.end_headers(); self.wfile.write(raw)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            state['calls'].append(body)
            time.sleep(state['delay'])
            self.send_response(state['status']); self.send_header('Retry-After', '15'); self.end_headers()
            response = state['response'] or {'model': 'company-test', 'data': [
                {'index': i, 'embedding': ([1., 0., 0.] if 'unrelated' not in text else [0., 1., 0.])[:state['dim']]}
                for i, text in enumerate(body['input'])],
                'usage': {'prompt_tokens': 7, 'total_tokens': 7}}
            try:
                self.wfile.write(json.dumps(response).encode())
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True); t.start()
    state['settings'] = {'enabled': True, 'api_root': f'http://127.0.0.1:{server.server_port}/v1',
                         'model': 'company-test', 'allowed_hosts': ['127.0.0.1'],
                         'allowed_scopes': ['global'], 'allow_insecure_http': True, 'auth_kind': 'none',
                         'deployment_id': 'synthetic-test-v1', 'auto_index': False, 'timeout_seconds': .5}
    yield state
    server.shutdown(); server.server_close()


def configure(gateway):
    cfg = config.load_config(); cfg['embedding'] = gateway['settings']
    config.CONFIG_PATH.write_text(json.dumps(cfg))


def seed_use_evidence(repo, mid, query, *, event_id=None, outcome="completed",
                      feedback="helpful", task_key="task-1", state="adopted"):
    """One execution chain: search event -> adopted use -> task outcome -> feedback."""
    conn = index.connect(repo)
    rev = conn.execute("SELECT content_revision FROM entity_heads WHERE entity_id = ?", (mid,)).fetchone()[0]
    hit = {"id": mid, "title": "seeded", "score": 1.0, "confidence": "verified", "status": "active"}
    search_id = event_id or index.record_search(conn, query, "global", [hit])
    use_id = "use-" + search_id
    conn.execute("INSERT INTO knowledge_uses VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (use_id, "pytest", "sess", search_id, "hash", "", "task-1", "rev-1",
                  "global", mid, rev, state, 1, "2026-01-01T00:00:00"))
    outcome_id = "out-" + search_id
    conn.execute("INSERT INTO task_outcomes VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                 (outcome_id, "task-1", "run-1", outcome, "agent_report",
                  "pytest", "sess", use_id, mid, "2026-01-01T00:00:00", "2026-01-01T00:00:00"))
    if feedback is not None:
        conn.execute("INSERT INTO feedback_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     ("rc-" + search_id, "", "hash", mid, rev, use_id, task_key,
                      outcome_id, "pytest", feedback, "version_task", "applied",
                      "b", "a", "{}", "{}", "2026-01-01T00:00:00"))
    conn.commit(); conn.close()
    return search_id, rev


def seed_once_card(repo, title):
    from knowbase import store
    meta = store.new_meta("pitfall", title, "global", ["HAR"], "agent:test:fixture")
    meta["id"] = "P-" + digest(title)[:12]
    body = "只看 HTTP 200 的 Content-Encoding，不把 304 当成压缩证据。HAR 排查。"
    path = store.mem_path(repo, "pitfall", meta["id"])
    path.write_text(store.render(meta, body))
    conn = index.connect(repo); index.upsert(conn, meta, body, path); conn.close()
    return meta["id"]


def derived_report(**over):
    base = {'schema': 'knowbase.retrieval-evaluation.v1', 'verdict': 'PASS',
            'annotation_status': 'execution_derived', 'derived_sample_count': 60,
            'confidence_interval': {'metric': 'Recall@5', 'delta': 0.0, 'CI95': [0.0, 0.2], 'positive_n': 60},
            'dataset_sha256': digest('synthetic dataset'), 'vectors_sha256': digest('synthetic vectors'),
            'retrieval_code': si.code_profile(), 'covered_scopes': ['global'],
            'provider_fp': 'fp', 'space_id': 'space', 'deployment_id': 'synthetic-test-v1',
            'dense_min': .8, 'hybrid_recall_at_5': 1.0,
            'gates': {k: True for k in ['quality', 'rejection', 'regression', 'latency']}}
    base.update(over)
    return base


# ---------- 2-A derivation ----------

def seed_search_event(repo, mid, query):
    conn = index.connect(repo)
    sid = index.record_search(conn, query, "global",
                              [{"id": mid, "title": "seeded", "score": 1.0, "confidence": "verified", "status": "active"}])
    conn.close()
    return sid


def test_derivation_positive_negative_irrelevant(memory_repo):
    mid = seed(memory_repo)
    sid_pos = seed_search_event(memory_repo, mid, "HAR")
    seed_use_evidence(memory_repo, mid, "HAR", event_id=sid_pos, outcome="completed", feedback="helpful", task_key="t-pos")
    sid_neg = seed_search_event(memory_repo, mid, "HAR")
    seed_use_evidence(memory_repo, mid, "HAR", event_id=sid_neg, outcome="failed", feedback="not_helpful", task_key="t-neg")
    seed_search_event(memory_repo, mid, "HAR")
    conn = index.connect(memory_repo)
    rows, stats = execution_eval.derive_dataset(conn)
    conn.close()
    by_id = {r["id"]: r for r in rows}
    pos = by_id["exec-" + sid_pos]
    assert pos["labels"]["relevant_ids"] == [mid] and pos["labels"]["unanswerable"] is False
    assert pos["labels"]["status"] == "execution_derived" and pos["labels"]["sample_class"] == "positive"
    assert pos["evidence"]["positive"][0]["task_keys"] == ["t-pos"]
    assert pos["evidence"]["search_id"] == sid_pos and pos["labels"]["corpus_revision"]
    neg = by_id["exec-" + sid_neg]
    assert neg["labels"]["relevant_ids"] == [] and neg["labels"]["unanswerable"] is True
    assert neg["labels"]["sample_class"] == "negative"
    assert stats["derived"] == 3 and stats["positive"] == 1 and stats["negative"] == 1 and stats["irrelevant"] == 1
    assert not validate_labels(rows)


def test_derivation_unresolved_adoption_is_skipped_not_counted(memory_repo):
    mid = seed(memory_repo)
    sid = seed_search_event(memory_repo, mid, "HAR")
    seed_use_evidence(memory_repo, mid, "HAR", event_id=sid, outcome="completed", feedback=None)
    conn = index.connect(memory_repo)
    rows, stats = execution_eval.derive_dataset(conn)
    conn.close()
    assert rows == [] and stats["skipped_unresolved"] == 1 and stats["derived"] == 0


def test_derivation_scrub_failure_excluded_and_counted(memory_repo):
    mid = seed(memory_repo)
    conn = index.connect(memory_repo)
    index.record_search(conn, 'api_key = "sk-abcdefghijklmnop1234"', "global",
                        [{"id": mid, "title": "seeded", "score": 1.0, "confidence": "verified", "status": "active"}])
    conn.close()
    conn = index.connect(memory_repo)
    rows, stats = execution_eval.derive_dataset(conn)
    conn.close()
    assert rows == [] and stats["excluded_for_privacy"] == 1


def test_validate_labels_irrelevant_needs_sample_class(memory_repo):
    good = [{'id': 'r1', 'labels': {'status': 'execution_derived', 'labeler': 'execution_derived',
                                    'corpus_revision': 'abc', 'relevant_ids': [], 'unanswerable': False,
                                    'sample_class': 'irrelevant'}}]
    bad = [{'id': 'r2', 'labels': {'status': 'execution_derived', 'labeler': 'execution_derived',
                                   'corpus_revision': 'abc', 'relevant_ids': [], 'unanswerable': False}}]
    assert validate_labels(good) == []
    assert validate_labels(bad) == ['r2']


# ---------- 2-A freeze + gate ----------

def comparison_stub(**over):
    base = {'metrics': {
        'B': {'Recall@5': 0.8, 'correct_rejection': 0.5, 'negative_queries': 5},
        'D': {'Recall@5': 1.0, 'correct_rejection': 0.6, 'negative_queries': 5}},
        'paired_intervals': {'D-B': {'delta': 0.2, 'CI95': [0.0, 0.4], 'positive_n': 60}},
        'p95_lexical_compute_ms': 42.0}
    for k, v in over.items():
        if isinstance(v, dict) and k in base:
            base[k].update(v)
        else:
            base[k] = v
    return base


def freeze_kwargs():
    return {'dataset_sha256': digest('d'), 'vectors_sha256': digest('v'), 'provider_fp': 'fp',
            'space_id': 'space', 'covered_scopes': ['global'], 'deployment_id': 'synthetic-test-v1',
            'dense_min': .5, 'derived_stats': {'derived': 65, 'positive': 60, 'negative': 5, 'irrelevant': 0}}


def test_freeze_gates_and_verdict():
    report = freeze_report(comparison_stub(), **freeze_kwargs())
    assert report['verdict'] == 'PASS' and report['annotation_status'] == 'execution_derived'
    assert all(report['gates'].values()) and report['derived_sample_count'] == 65
    assert report['confidence_interval']['CI95'] == [0.0, 0.4]
    worse = comparison_stub(metrics={'D': {'Recall@5': 0.5}})
    assert freeze_report(worse, **freeze_kwargs())['gates']['quality'] is False
    regressed = comparison_stub(paired_intervals={'D-B': {'delta': -0.1, 'CI95': [-0.3, 0.1], 'positive_n': 60}})
    assert freeze_report(regressed, **freeze_kwargs())['gates']['regression'] is False
    no_negative = comparison_stub(metrics={'D': {'negative_queries': 0}})
    assert freeze_report(no_negative, **freeze_kwargs())['gates']['rejection'] is False
    slow = comparison_stub(p95_lexical_compute_ms=5000.0)
    assert freeze_report(slow, **freeze_kwargs())['gates']['latency'] is False


def test_freeze_quality_absolute_recall_floor():
    # Relative win alone is not enough: D beats B but sits below the
    # absolute floor, so the quality gate (and the verdict) must fail.
    weak_but_better = comparison_stub(metrics={'B': {'Recall@5': 0.3}, 'D': {'Recall@5': 0.4}})
    report = freeze_report(weak_but_better, **freeze_kwargs())
    assert report['gates']['quality'] is False and report['verdict'] == 'FAIL'
    assert report['hybrid_recall_at_5'] == 0.4
    # The floor is inclusive: exactly 0.5 passes when it also beats lexical.
    boundary = comparison_stub(metrics={'B': {'Recall@5': 0.3}, 'D': {'Recall@5': 0.5}})
    assert freeze_report(boundary, **freeze_kwargs())['gates']['quality'] is True
    assert freeze_report(boundary, **freeze_kwargs())['hybrid_recall_at_5'] == 0.5


def test_quality_accepts_derived_and_rejects_human_and_thin_reports(memory_repo):
    cfg = config.load_config(); options = dict(cfg.get('embedding') or {})
    options.update({'allowed_scopes': ['global'], 'deployment_id': 'synthetic-test-v1'})
    identity = {'provider_fp': 'fp', 'space_id': 'space'}
    good = derived_report()
    p = memory_repo.parent / 'good-report.json'; p.write_text(json.dumps(good))
    assert si.quality({**options, 'quality_report': str(p)}, identity) is not None
    human = derived_report(annotation_status='reviewed', human_query_count=60, reviewer='someone')
    human.pop('derived_sample_count'); human.pop('confidence_interval')
    p.write_text(json.dumps(human))
    assert si.quality({**options, 'quality_report': str(p)}, identity) is None
    thin = derived_report(derived_sample_count=59)
    p.write_text(json.dumps(thin))
    assert si.quality({**options, 'quality_report': str(p)}, identity) is None
    regressed = derived_report(confidence_interval={'CI95': [-0.2, 0.1]})
    p.write_text(json.dumps(regressed))
    assert si.quality({**options, 'quality_report': str(p)}, identity) is None


def test_quality_rejects_below_absolute_recall_floor(memory_repo):
    cfg = config.load_config(); options = dict(cfg.get('embedding') or {})
    options.update({'allowed_scopes': ['global'], 'deployment_id': 'synthetic-test-v1'})
    identity = {'provider_fp': 'fp', 'space_id': 'space'}
    p = memory_repo.parent / 'floor-report.json'
    # A foreign report claiming quality=True must still be rejected when
    # its hybrid Recall@5 sits below the absolute floor: activation
    # re-validates the value instead of trusting the claimed gate.
    weak = derived_report(hybrid_recall_at_5=0.4)
    p.write_text(json.dumps(weak))
    assert si.quality({**options, 'quality_report': str(p)}, identity) is None
    missing = derived_report(); missing.pop('hybrid_recall_at_5')
    p.write_text(json.dumps(missing))
    assert si.quality({**options, 'quality_report': str(p)}, identity) is None
    # The floor is inclusive.
    boundary = derived_report(hybrid_recall_at_5=0.5)
    p.write_text(json.dumps(boundary))
    assert si.quality({**options, 'quality_report': str(p)}, identity) is not None


def test_dense_activates_with_zero_human_annotations(memory_repo, gateway, tmp_path, monkeypatch):
    for var in ("CLAUDE_PROJECT_DIR", "ZCODE_PROJECT_DIR"):
        monkeypatch.delenv(var, raising=False)
    mid = seed(memory_repo)
    configure(gateway)
    built = si.build(memory_repo)
    assert built['state'] == 'ready'
    for i in range(60):
        seed_use_evidence(memory_repo, mid, "HAR", outcome="completed", feedback="helpful", task_key=f"pos-{i}")
    for i in range(5):
        seed_use_evidence(memory_repo, mid, "HAR", outcome="failed", feedback="not_helpful", task_key=f"neg-{i}")
    conn = index.connect(memory_repo)
    rows, stats = execution_eval.derive_dataset(conn)
    conn.close()
    assert stats['derived'] == 65 and stats['positive'] == 60 and stats['negative'] == 5
    assert 'human' not in json.dumps(rows)
    data = tmp_path / 'derived.jsonl'
    info = execution_eval.write_dataset(rows, data)
    cache = tmp_path / 'vectors.json'
    assert export_cache(memory_repo, data, cache)['state'] == 'cached'
    comparison = run(memory_repo, data, cache, Path(__file__).resolve().parents[1], dense_min=.5, gate_dense_min=.8)
    assert comparison['state'] == 'COMPLETED_OFFLINE'
    cache_json = json.loads(cache.read_text())
    conn = index.connect(memory_repo)
    g = si.generation(conn, built['generation'])
    conn.close()
    report = freeze_report(comparison, dataset_sha256=info['sha256'],
                           vectors_sha256=digest(cache_json['documents']),
                           provider_fp=g['identity']['provider_fp'], space_id=g['identity']['space_id'],
                           covered_scopes=['global'], deployment_id='synthetic-test-v1',
                           dense_min=.5, derived_stats=stats)
    assert report['verdict'] == 'PASS'
    assert 'human_query_count' not in report and 'reviewer' not in report
    rp = tmp_path / 'frozen-report.json'; rp.write_text(json.dumps(report))
    cfg = config.load_config(); cfg['embedding']['quality_report'] = str(rp)
    config.CONFIG_PATH.write_text(json.dumps(cfg))
    conn = index.connect(memory_repo)
    assert si.activate(conn, built['generation']) == built['generation']
    assert si.generation(conn)['state'] == 'active'
    diag = {}
    assert index.search(conn, 'HAR', origin='mcp_search', diagnostics=diag)
    assert diag['level'] == 'L3'
    conn.close()


# ---------- 2-B tiered injection ----------

def test_hook_tier_helper_staging_never_injects(memory_repo):
    conn = index.connect(memory_repo)
    hits = [
        {"id": "P-staging00001", "title": "staging card", "confidence": "verified", "status": "active", "staging": 1, "scope": "global"},
        {"id": "P-archived0001", "title": "archived card", "confidence": "verified", "status": "archived", "staging": 0, "scope": "global"},
        {"id": "P-verified0001", "title": "verified card", "confidence": "verified", "status": "active", "staging": 0, "scope": "global"},
    ]
    kept, dropped = hooks._tier_hits(conn, hits, "verified")
    assert [h["id"] for h, _ in kept] == ["P-verified0001"]
    conn.close()


def test_hook_once_summary_only_with_positive_evidence(memory_repo, monkeypatch):
    for var in ("CLAUDE_PROJECT_DIR", "ZCODE_PROJECT_DIR"):
        monkeypatch.delenv(var, raising=False)
    cfg = config.load_config(); cfg['hooks'] = {"enabled": True, "inject_min_confidence": "verified"}
    config.CONFIG_PATH.write_text(json.dumps(cfg))
    once_id = seed_once_card(memory_repo, "HAR 代理排查")
    conn = index.connect(memory_repo)
    rev = conn.execute("SELECT content_revision FROM entity_heads WHERE entity_id = ?", (once_id,)).fetchone()[0]
    conn.close()
    # No evidence yet: once card must not inject.
    assert once_id not in hooks.user_prompt("HAR 代理排查")
    # Add adopted helpful + completed evidence on the current revision.
    seed_use_evidence(memory_repo, once_id, "HAR 代理排查", event_id="hook-ev",
                      outcome="completed", feedback="helpful", task_key="hook-task")
    out = hooks.user_prompt("HAR 代理排查")
    assert once_id in out and "仅指针" in out


def test_hook_verified_full_line_unchanged(memory_repo, monkeypatch):
    for var in ("CLAUDE_PROJECT_DIR", "ZCODE_PROJECT_DIR"):
        monkeypatch.delenv(var, raising=False)
    cfg = config.load_config(); cfg['hooks'] = {"enabled": True, "inject_min_confidence": "verified"}
    config.CONFIG_PATH.write_text(json.dumps(cfg))
    mid = seed(memory_repo)
    out = hooks.user_prompt("HAR 压缩排查")
    assert mid in out and "verified" in out and "仅指针" not in out


def test_hook_tier_adoption_visible_in_report(memory_repo):
    mid = seed(memory_repo)
    conn = index.connect(memory_repo)
    search_id = index.record_search(conn, "HAR", "global",
                                    [{"id": mid, "title": "seeded", "score": 1.0, "confidence": "verified", "status": "active"}])
    conn.close()
    retrieval_metrics.delivery(memory_repo, search_id, f"- [{mid}] title",
                               tiers={"verified_full": [mid], "once_summary": []}, dropped_once=2)
    seed_use_evidence(memory_repo, mid, "HAR", event_id=search_id, outcome="completed",
                      feedback="helpful", task_key="rep-task")
    report = retrieval_metrics.report(memory_repo)
    assert report['injection_tiers']['verified_full'] == 1
    assert report['dropped_once_injections'] == 2
    assert report['tier_adoption']['verified_full'] == {'delivered': 1, 'adopted': 1, 'rate': 1.0}
