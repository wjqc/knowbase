import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from knowbase import integration, config, index
from knowbase.__main__ import _main, main


def test_install_merge_is_idempotent_and_preserves_other_servers(tmp_path):
    p=tmp_path/'.claude.json';p.write_text(json.dumps({'mcpServers':{'other':{'command':'keep'}},'ui':'keep'}))
    changes=integration.plan(['claude-code','zcode','trae'],tmp_path)
    result=integration.apply(changes,tmp_path)
    assert result['protocol_verified'] is False and result['session_effective']=='reconnect_required'
    assert json.loads(p.read_text())['mcpServers']['other']=={'command':'keep'}
    before=p.read_bytes();assert integration.plan(['claude-code','zcode','trae'],tmp_path)==[]
    assert p.read_bytes()==before


def test_merge_rejects_invalid_config_and_existing_conflict(tmp_path):
    p=tmp_path/'.claude.json';p.write_text('bad')
    with pytest.raises(integration.IntegrationError,match='INVALID_HOST_JSON'):integration.plan(['claude-code'],tmp_path)
    assert p.read_text()=='bad'
    p.write_text('{"mcpServers":{"knowbase":{"command":"different"}}}')
    with pytest.raises(integration.IntegrationError,match='CONFLICT'):integration.plan(['claude-code'],tmp_path)


def test_transaction_rolls_back_first_file_when_second_write_fails(tmp_path,monkeypatch):
    p=tmp_path/'.claude.json';p.write_text('{"original":true}')
    changes=integration.plan(['claude-code'],tmp_path)
    original=integration._atomic
    def fail(path,raw):
        if path==tmp_path/'.claude/settings.json':raise OSError('controlled')
        return original(path,raw)
    monkeypatch.setattr(integration,'_atomic',fail)
    with pytest.raises(OSError):integration.apply(changes,tmp_path)
    assert p.read_text()=='{"original":true}'
    assert not (tmp_path/'.claude/settings.json').exists()


def test_apply_cas_does_not_overwrite_concurrent_change(tmp_path):
    changes=integration.plan(['zcode'],tmp_path)
    p=tmp_path/'.zcode/cli/config.json';p.parent.mkdir(parents=True);p.write_text('{"user":"new"}')
    with pytest.raises(integration.IntegrationError,match='CONCURRENT'):integration.apply(changes,tmp_path)
    assert json.loads(p.read_text())=={'user':'new'}


def test_symlink_host_file_is_never_followed(tmp_path):
    target=tmp_path/'user';target.write_text('{}');(tmp_path/'.claude.json').symlink_to(target)
    with pytest.raises(integration.IntegrationError,match='SYMLINK'):integration.plan(['claude-code'],tmp_path)


def test_cli_plan_does_not_initialize_repo_or_config(tmp_path,monkeypatch,capsys):
    monkeypatch.setenv('HOME',str(tmp_path));monkeypatch.setattr(config,'CONFIG_PATH',tmp_path/'cfg.json')
    assert main(['init','--plan','--agents','claude-code','--non-interactive'])==0
    assert not config.CONFIG_PATH.exists() and not (tmp_path/'knowbase').exists()


def test_doctor_cannot_call_connect_migrations_or_telemetry(memory_repo,monkeypatch,capsys):
    from knowbase import telemetry
    conn=index.connect(memory_repo);n=conn.execute('SELECT count(*) FROM command_calls').fetchone()[0];conn.close()
    monkeypatch.setattr(index,'connect',lambda *a:pytest.fail('doctor must be read only'))
    monkeypatch.setattr(telemetry,'record',lambda *a:pytest.fail('doctor must not write telemetry'))
    assert main(['doctor','--json'])==0
    output=capsys.readouterr().out
    assert '"retrieval"' in output and '"fail"' not in output[output.rfind('[\n'):]
    with sqlite3.connect(index.db_path(memory_repo)) as c:assert c.execute('SELECT count(*) FROM command_calls').fetchone()[0]==n


def test_doctor_reports_missing_schema_without_creating_it(tmp_path,monkeypatch,capsys):
    monkeypatch.setenv('KNOWBASE_REPO_PATH',str(tmp_path))
    with sqlite3.connect(tmp_path/'memory.db') as c:c.execute('CREATE TABLE unrelated(x)')
    before=(tmp_path/'memory.db').read_bytes()
    assert main(['doctor','--json'])==2
    assert (tmp_path/'memory.db').read_bytes()==before


def test_resume_after_crash_restores_original_and_preserves_user_edit(tmp_path,monkeypatch):
    p=tmp_path/'.claude.json';p.write_text('{"original":true}')
    original=integration._atomic
    def crash(path,raw):
        if path==tmp_path/'.claude/settings.json':raise SystemExit('simulated crash')
        original(path,raw)
    monkeypatch.setattr(integration,'_atomic',crash)
    with pytest.raises(SystemExit):integration.apply(integration.plan(['claude-code'],tmp_path),tmp_path)
    monkeypatch.setattr(integration,'_atomic',original)
    integration.resume(tmp_path)
    assert p.read_text()=='{"original":true}'
    assert integration.apply(integration.plan(['claude-code'],tmp_path),tmp_path)['configured']


def test_recovery_never_overwrites_a_post_crash_user_change(tmp_path,monkeypatch):
    p=tmp_path/'.claude.json';p.write_text('{"original":true}')
    original=integration._atomic
    def crash(path,raw):
        if path==tmp_path/'.claude/settings.json':raise SystemExit('simulated crash')
        original(path,raw)
    monkeypatch.setattr(integration,'_atomic',crash)
    with pytest.raises(SystemExit):integration.apply(integration.plan(['claude-code'],tmp_path),tmp_path)
    monkeypatch.setattr(integration,'_atomic',original)
    p.write_text('{"user":"later"}')
    with pytest.raises(integration.IntegrationError,match='ROLLBACK_CONFLICT'):integration.resume(tmp_path)
    assert json.loads(p.read_text())=={'user':'later'}


def test_existing_fixed_runtime_env_and_hook_timeout_preserved(tmp_path):
    integration.apply(integration.plan(['claude-code'],tmp_path),tmp_path)
    p=tmp_path/'.claude/settings.json';d=json.loads(p.read_text());d['hooks']['UserPromptSubmit'][0]['hooks'][0]['timeout']=5;p.write_text(json.dumps(d))
    assert integration.plan(['claude-code'],tmp_path)==[]


def test_formal_cli_initializes_and_configures_selected_host(tmp_path,monkeypatch,capsys):
    monkeypatch.setenv('HOME',str(tmp_path));monkeypatch.setenv('KNOWBASE_REPO_PATH',str(tmp_path/'memory'))
    monkeypatch.setattr(config,'CONFIG_PATH',tmp_path/'cfg.json')
    cfg=config._deep_merge(config.DEFAULTS,{'repo_path':str(tmp_path/'memory'),'git':{'auto_commit':False,'auto_pull':False,'auto_push':False}})
    config.CONFIG_PATH.write_text(json.dumps(cfg))
    assert main(['init','--non-interactive','--agents','claude-code'])==0
    output=capsys.readouterr().out
    assert 'reconnect_required' in output and '"protocol_verified": false' in output
    assert (tmp_path/'memory/memory.db').exists()
    assert json.loads((tmp_path/'.claude.json').read_text())['mcpServers']['knowbase']['args']==['-I','-m','knowbase','serve']


def test_doctor_checks_content_beyond_equal_row_counts(memory_repo,capsys):
    from test_local_retrieval_upgrade import seed
    seed(memory_repo)
    conn=index.connect(memory_repo);conn.execute("UPDATE mem_fts SET body='tampered'");conn.commit();conn.close()
    assert main(['doctor','--json'])==2
    output=capsys.readouterr().out
    assert 'content_parity' in output and 'mismatched_cards=1' in output
