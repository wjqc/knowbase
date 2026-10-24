"""Independent regressions for the merged P0/P1/P3/P4 boundaries."""
import json
import sys
import threading

from knowbase import config, context, consumption, index
from knowbase.__main__ import cmd_executor_probe
from knowbase.automation import executor, worker
from knowbase.acceptance import check_full_chain
from knowbase.value import aggregate, events, exchange_v2


def test_cli_isolation_does_not_write_configured_repository(memory_repo, monkeypatch, tmp_path, capsys):
    from test_p1_executor_probe import FAKE_GOOD_SCRIPT
    cfg = config.load_config()
    cfg['automation'] = {'executors': [{
        'executor_id': 'review-fixture', 'kind': 'command', 'command': sys.executable,
        'noninteractive_argv': ['-c', FAKE_GOOD_SCRIPT],
        'auth_check': {'env_var': 'KNOWBASE_SYNTHETIC_PROBE_AUTH'},
    }]}
    config.CONFIG_PATH.write_text(json.dumps(cfg))
    monkeypatch.setenv('KNOWBASE_SYNTHETIC_PROBE_AUTH', 'fixture-only')
    monkeypatch.setattr(executor, 'discover_executors', lambda: [])
    before_config = config.CONFIG_PATH.read_bytes()
    conn = index.connect(memory_repo)
    before = {t: conn.execute(f'SELECT count(*) FROM {t}').fetchone()[0]
              for t in ['meta', 'jobs', 'candidates', 'usage_log', 'value_outbox']}
    conn.close()
    capsys.readouterr()
    assert cmd_executor_probe(isolation=True, scope='probe') == 0
    report = json.loads(capsys.readouterr().out)
    assert report['isolation']['passed']
    conn = index.connect(memory_repo)
    after = {t: conn.execute(f'SELECT count(*) FROM {t}').fetchone()[0] for t in before}
    conn.close()
    assert after == before
    assert config.CONFIG_PATH.read_bytes() == before_config


def test_v2_roundtrip_preserves_complete_chain_identity(tmp_path):
    agg = aggregate.connect(tmp_path/'aggregate.db')
    for n in range(3):
        inst, binding = 'inst-'+str(n % 2), 'member-'+str(n % 2)
        task, session, mid, use = [prefix+str(n) for prefix in ['task-', 'session-', 'card-', 'use-']]
        common = dict(host='synthetic', session_id=session, task_id=task,
                      installation_id=inst, member_binding_id=binding)
        raw = [events.build_event(kind, payload, **common) for kind, payload in [
            ('knowledge.delivered', {'delivery_id': 'del-'+str(n), 'memory_id': mid,
                'content_revision': 'r1', 'task_id': task}),
            ('knowledge.used', {'memory_id': mid, 'content_revision': 'r1',
                'context_event_id': 'ctx-'+str(n), 'use_id': use, 'state': 'adopted',
                'task_id': task, 'scope': 'test'}),
            ('task.completed', {'task_id': task, 'task_outcome_id': 'outcome-'+str(n),
                'result': 'completed', 'verification_source': 'independent', 'use_id': use}),
        ]]
        package = exchange_v2.build_package(raw, source={'installation_id': inst,
            'member_binding_id': binding}, target_team='test')
        imported = exchange_v2.import_package(agg, package, team_id='test')
        assert imported['counts']['accepted'] == 3
    assert check_full_chain(agg, 'test')['passed']
    assert agg.execute("SELECT count(*) FROM agg_events WHERE member_binding_id='' OR session_id=''").fetchone()[0] == 0
    agg.close()


def test_adoption_links_only_to_its_host_and_session(memory_repo):
    from test_p0_evidence_repair import seed_card
    mid = seed_card(memory_repo)
    for host, sid, query in [('a', 'session-a', '已结算 订单 撤回'),
                             ('b', 'session-b', '订单 撤回 校验')]:
        result = context.execute(memory_repo, session_id=sid, event_id='same-local-event',
            host_profile=host, scope='orders', task_id='task-'+sid, current_goal=query,
            topics=[{'topic_id': 'topic', 'queries': [query], 'status': 'active'}])
        assert result['candidates']
        use = consumption.record(memory_repo, session_id=sid, event_id='use',
            context_event_id='same-local-event', memory_id=mid,
            content_revision=result['candidates'][0]['content_revision'], state='adopted', host_profile=host)
        conn = index.connect(memory_repo)
        linked = conn.execute('SELECT d.host_profile,d.session_id FROM use_retrieval_links l '
            'JOIN retrieval_deliveries d ON d.delivery_id=l.delivery_id WHERE l.use_id=?', (use['use_id'],)).fetchone()
        conn.close()
        assert linked == (host, sid)


def _handlers(monkeypatch, **overrides):
    handlers = {kind: (lambda *_: {'skipped': 'fixture'}) for kind in worker.TASK_INTERVALS}
    handlers.update(overrides)
    monkeypatch.setattr(worker, '_HANDLERS', handlers)


def test_worker_runs_team_collector_after_replacing_legacy_entry(memory_repo, monkeypatch):
    from knowbase.team import service
    cfg = config.load_config(); cfg['team']['enabled'] = True
    config.CONFIG_PATH.write_text(json.dumps(cfg))
    calls = []
    monkeypatch.setattr(service, 'run', lambda **kw: calls.append(kw) or {'errors': []})
    handlers = {k: v for k, v in worker._HANDLERS.items() if k == 'team_sync'}
    assert handlers, 'replacement worker must schedule the retired team sync entry'
    monkeypatch.setattr(worker, '_HANDLERS', handlers)
    worker.tick(memory_repo)
    assert calls == [{'network': True}]


def test_worker_records_returned_errors_and_persistent_backoff(memory_repo, monkeypatch):
    _handlers(monkeypatch, knowledge_sync=lambda *_: {'error': 'synthetic_network_failure'})
    worker.tick(memory_repo)
    conn = index.connect(memory_repo)
    first = conn.execute("SELECT last_success_at,last_error FROM automation_schedule WHERE kind='knowledge_sync'").fetchone()
    assert first[0] is None and first[1]
    conn.execute("UPDATE automation_schedule SET next_run_at='2000-01-01' WHERE kind='knowledge_sync'"); conn.commit()
    worker.tick(memory_repo)
    assert conn.execute("SELECT failure_count FROM automation_schedule WHERE kind='knowledge_sync'").fetchone()[0] == 2
    conn.close()


def test_expired_owner_cannot_continue_after_takeover(memory_repo, monkeypatch):
    seen, takeover = [], [False]
    def long_handler(repo, conn, cfg):
        seen.append('sync')
        if not takeover[0]:
            takeover[0] = True
            conn.execute("UPDATE automation_lease SET lease_until='2000-01-01'"); conn.commit()
            worker.tick(repo, owner='new-owner')
        return {'ok': True}
    _handlers(monkeypatch, knowledge_sync=long_handler,
              evidence_exchange=lambda *_: seen.append('evidence') or {'ok': True})
    result = worker.tick(memory_repo, owner='old-owner')
    assert seen.count('evidence') == 1
    assert result['lease'] == 'lost'


def test_worker_heartbeat_renews_during_handler_and_fences_old_release(memory_repo, monkeypatch):
    renewed = threading.Event()
    original = worker.renew_lease
    def observed_renew(conn, owner, fencing):
        result = original(conn, owner, fencing)
        if threading.current_thread() is not threading.main_thread(): renewed.set()
        return result
    monkeypatch.setattr(worker, 'LEASE_SECONDS', .15)
    monkeypatch.setattr(worker, 'renew_lease', observed_renew)
    _handlers(monkeypatch, knowledge_sync=lambda *_: {'heartbeat': renewed.wait(3)})
    result = worker.tick(memory_repo)
    assert renewed.is_set() and result['lease'] == 'acquired'
    conn = index.connect(memory_repo)
    first = worker.acquire_lease(conn, 'reused-owner')
    conn.execute("UPDATE automation_lease SET lease_until='2000-01-01'"); conn.commit()
    second = worker.acquire_lease(conn, 'reused-owner')
    assert second[1] > first[1]
    assert worker.release_lease(conn, *first) is False
    assert worker.renew_lease(conn, *first) is False
    assert worker.lease_status(conn)['held']
    worker.release_lease(conn, *second); conn.close()


def test_worker_nested_failure_does_not_advance_previous_success(memory_repo, monkeypatch):
    _handlers(monkeypatch, evidence_exchange=lambda *_: {'ok': True})
    worker.tick(memory_repo)
    conn = index.connect(memory_repo)
    before = conn.execute("SELECT last_success_at FROM automation_schedule WHERE kind='evidence_exchange'").fetchone()[0]
    conn.execute("UPDATE automation_schedule SET next_run_at='2000-01-01' WHERE kind='evidence_exchange'"); conn.commit()
    _handlers(monkeypatch, evidence_exchange=lambda *_: {'sent': {'error': 'EVIDENCE_SEND_FAILED'}})
    worker.tick(memory_repo)
    after = conn.execute("SELECT last_success_at,last_status,failure_count FROM automation_schedule WHERE kind='evidence_exchange'").fetchone()
    assert after == (before, 'error', 1)
    conn.close()


def test_worker_once_cli_reports_handler_failure(memory_repo, monkeypatch, capsys):
    from knowbase.__main__ import main
    _handlers(monkeypatch, knowledge_sync=lambda *_: {'warning': 'synthetic_sync_failure'})
    capsys.readouterr()
    assert main(['worker', 'once']) == 1
    report = json.loads(capsys.readouterr().out)
    sync = next(row for row in report['ran'] if row['kind'] == 'knowledge_sync')
    assert sync['status'] == 'error'
