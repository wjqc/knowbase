"""Operational readiness must not confuse missing setup with healthy operation."""
import importlib.util
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from knowbase import config, doctor, index, telemetry
from knowbase.team import publish, report, state
from knowbase.value import overview


@pytest.fixture
def operational_home(memory_repo, monkeypatch, tmp_path):
    monkeypatch.setenv('HOME', str(tmp_path))
    cfg = json.loads(config.CONFIG_PATH.read_text())
    cfg['team']['data_dir'] = str(tmp_path/'team')
    config.CONFIG_PATH.write_text(json.dumps(cfg))
    return tmp_path


def test_disabled_automation_and_unenrolled_updates_are_not_pass(memory_repo, operational_home):
    checks = {c['name']:c for c in doctor.inspect(memory_repo)}
    assert checks['automation']['level'] == 'warn'
    assert json.loads(checks['automation']['message'])['state'] == 'automation_disabled'
    assert checks['program_updates']['level'] == 'warn'
    assert json.loads(checks['program_updates']['message'])['readiness'] == 'not_enrolled'
    assert checks['team_statistics']['level'] == 'warn'
    assert not (operational_home/'.knowbase').exists()


@pytest.mark.parametrize(('enabled','last_run','draining','expected'), [
    (False, {}, False, 'disabled'),
    (True, {}, False, 'awaiting_first_run'),
    (True, {'state':'up_to_date','checked_at':time.time()}, False, 'up_to_date'),
    (True, {'state':'updated','checked_at':time.time()}, False, 'updated'),
    (True, {'state':'up_to_date','checked_at':time.time()-86400}, False, 'stale'),
    (True, {'state':'deferred_or_failed','reason':'network'}, False, 'deferred_or_failed'),
    (True, {'state':'updated','checked_at':time.time()}, True, 'recovery_required'),
])
def test_updater_health_requires_fresh_execution(memory_repo, operational_home, enabled, last_run, draining, expected):
    home = operational_home
    directory = home/'.knowbase/updates'
    directory.mkdir(parents=True)
    (home/'.knowbase/updates.json').write_text(json.dumps({'enabled':enabled,'interval_seconds':900}))
    (directory/'state.json').write_text(json.dumps(last_run))
    if draining:
        root=home/'.knowbase/runtime';root.mkdir()
        (root/'update-drain.json').write_text('{}')
    check = next(c for c in doctor.inspect(memory_repo) if c['name']=='program_updates')
    assert json.loads(check['message'])['readiness'] == expected
    assert check['level'] == ('pass' if expected in {'up_to_date','updated'} else 'warn')


def test_team_health_reports_missing_members_and_stale_snapshot(memory_repo, operational_home):
    cfg=json.loads(config.CONFIG_PATH.read_text());cfg['team']['enabled']=True
    config.CONFIG_PATH.write_text(json.dumps(cfg))
    directory=operational_home/'team';directory.mkdir()
    snapshot={'generated_at':datetime.now(timezone.utc).isoformat(),
              'coverage':{'registered':2,'received':1},
              'members':[{'member_id':'bob','current':True,'coverage':'pending','last_report':None}],
              'pending_files':0,'sync_errors':[],'last_sync':{'ok':True}}
    path=directory/'snapshot.json';path.write_text(json.dumps(snapshot))
    check=next(c for c in doctor.inspect(memory_repo) if c['name']=='team_statistics')
    assert check['level']=='warn'
    assert json.loads(check['message'])['members'][0]['coverage']=='pending'
    snapshot['coverage']['received']=2;snapshot['members'][0]['coverage']='received'
    path.write_text(json.dumps(snapshot))
    assert next(c for c in doctor.inspect(memory_repo) if c['name']=='team_statistics')['level']=='pass'
    snapshot['generated_at']=(datetime.now(timezone.utc)-timedelta(days=2)).isoformat()
    path.write_text(json.dumps(snapshot))
    assert next(c for c in doctor.inspect(memory_repo) if c['name']=='team_statistics')['level']=='warn'


def test_local_and_team_call_categories_exclude_hooks_from_knowledge(memory_repo, tmp_path):
    from test_team_stats import NOW, accept, daily, member
    commands={'memory_search':3,'memory_read':2,'hook.tool-before':90,
              'memory_job_material':4,'sync-once':2,'list':1}
    conn=index.connect(memory_repo)
    for command,count in commands.items():
        for i in range(count):
            conn.execute('INSERT INTO command_calls(call_id,ts,transport,command,status,duration_ms) VALUES(?,?,?,?,?,?)',
                         (f'{command}-{i}','2026-10-23T09:00:00Z','mcp',command,'ok',0))
    local=overview.build(conn);conn.close()
    team=state.connect(tmp_path/'team');accept(team,member('alice'));row=daily(calls=sum(commands.values()))
    row['metrics']['commands']=[{'command':k,'count':v,'host':'trae','transport':'mcp','status':'ok'} for k,v in commands.items()]
    accept(team,row);snapshot=report.build(team,now=NOW);team.close()
    assert local['calls']==snapshot['calls']==102
    assert local['call_breakdown']==snapshot['call_breakdown']=={'knowledge':5,'hooks':90,'maintenance':6,'other':1}
    rendered=publish.markdown(snapshot)
    assert '知识工具调用 **5**' in rendered and 'Hook 调用 **90**' in rendered
    assert '不代表知识复用' in rendered and '不能据此判定 MCP 断连' in rendered
    assert overview.call_breakdown({})['knowledge'] is None


def test_bootstrap_degraded_health_output_is_preserved():
    path=Path(__file__).parents[1]/'scripts/bootstrap_auto_update.py'
    spec=importlib.util.spec_from_file_location('bootstrap_readiness',path)
    bootstrap=importlib.util.module_from_spec(spec);spec.loader.exec_module(bootstrap)
    result=bootstrap.command([sys.executable,'-c',"print('[{\"level\":\"fail\"}]');raise SystemExit(2)"],check=False)
    assert json.loads(result)==[{'level':'fail'}]
    invalid=subprocess.run([sys.executable,str(path),'--source','https://example.invalid/program.git',
                            '--enable-automation'],capture_output=True,text=True)
    assert invalid.returncode==2 and '--executor-scopes' in invalid.stderr


@pytest.mark.parametrize(('agent','expected'), [('codex','codex'),('codex-wenjian3','codex'),
    ('zcode-lisi','zcode'),('claude-code-alice','claude-code'),('trae-bob','trae'),
    ('cursor','cursor'),('opencode-alice','opencode'),('unrelated','unknown')])
def test_configured_agent_labels_preserve_host_attribution(agent,expected):
    from jsonschema import validate
    from test_team_stats import daily
    from knowbase.team.contracts import validate_summary
    assert telemetry.host_name(agent)==expected
    row=daily();row['metrics']['commands'][0]['host']=expected
    validate_summary(row)
    schema=json.loads((Path(__file__).parents[1]/'docs/schemas/team-summary.v1.schema.json').read_text())
    validate(row,schema)
