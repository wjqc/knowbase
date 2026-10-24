import asyncio
import json
import sys
from pathlib import Path

from knowbase import config,integration,index
from knowbase.__main__ import main
from knowbase.retrieval_evaluation import run


def test_six_host_plans_preserve_native_comments_and_are_idempotent(tmp_path):
    codex=tmp_path/'.codex/config.toml';codex.parent.mkdir();codex.write_text('# keep this comment\nmodel = "keep-model"\n')
    oc=tmp_path/'.config/opencode/opencode.jsonc';oc.parent.mkdir(parents=True);oc.write_text('''// prefix comment
{ "mcp": {
  // third party stays
  "other": {"type":"local","command":["echo","https://example.invalid//literal"],},
}, "ui":"keep" }
''')
    agents=['claude-code','zcode','trae','codex','cursor','opencode']
    changes=integration.plan(agents,tmp_path);integration.apply(changes,tmp_path)
    assert codex.read_text().startswith('# keep this comment')
    assert '// third party stays' in oc.read_text() and 'https://example.invalid//literal' in oc.read_text()
    assert integration.plan(agents,tmp_path)==[]
    c=integration._toml(codex.read_bytes())
    assert c['model']=='keep-model' and c['mcp_servers']['knowbase']['args']==['-I','-m','knowbase','serve']
    hooks=json.loads((tmp_path/'.codex/hooks.json').read_text())['hooks']
    assert set(hooks)=={'SessionStart','UserPromptSubmit','PreToolUse','PostToolUse','Stop','PreCompact'}
    z=json.loads((tmp_path/'.zcode/cli/config.json').read_text())['hooks']['events']
    assert 'PostToolUseFailure' in z and 'PreCompact' not in z


def test_model_json_rejects_secret_fields_without_writing(memory_repo,tmp_path,monkeypatch,capsys):
    p=tmp_path/'model.json';p.write_text(json.dumps({'api_root':'https://internal.invalid/v1','model':'test','api_key':'SYNTHETIC-KEY-DO-NOT-ECHO-9d6a'}))
    old=config.CONFIG_PATH.read_bytes()
    assert main(['init','--non-interactive','--embedding-config',str(p)])==3
    assert config.CONFIG_PATH.read_bytes()==old
    assert 'SYNTHETIC-KEY-DO-NOT-ECHO-9d6a' not in capsys.readouterr().out


def test_model_config_backed_up_without_replacing_other_settings(memory_repo,tmp_path,monkeypatch):
    monkeypatch.setenv('HOME',str(tmp_path))
    cfg=config.load_config();cfg['scope_map']={'/project':'orders'};config.CONFIG_PATH.write_text(json.dumps(cfg))
    p=tmp_path/'model.json';p.write_text(json.dumps({'api_root':'https://internal.invalid/v1','model':'company',
        'allowed_hosts':['internal.invalid'],'allowed_scopes':['orders'],'api_key_env':'COMPANY_EMBED_KEY'}))
    assert main(['init','--non-interactive','--embedding-config',str(p)])==0
    updated=config.load_config()
    assert updated['scope_map']=={'/project':'orders'} and updated['embedding']['api_key_env']=='COMPANY_EMBED_KEY'
    assert list((tmp_path/'.knowbase/backups').glob('init-*/journal.json'))


def test_stdio_probe_checks_source_build_and_reports_session_boundary(tmp_path):
    result=asyncio.run(integration.probe_protocol(executable=sys.executable,args=['-B','-m','knowbase','serve'],cwd=str(Path(__file__).resolve().parents[1])))
    assert result['protocol_verified'] is True and result['session_effective']=='reconnect_required'


def test_incomplete_human_qrels_stop_formal_evaluation_before_cache_access(memory_repo,tmp_path):
    p=tmp_path/'dataset.jsonl';p.write_text(json.dumps({'id':'q1','query':'HAR','labels':{'status':'pending_human'}})+'\n')
    result=run(memory_repo,p,tmp_path/'missing-cache.json',tmp_path,dense_min=.5,gate_dense_min=.6)
    assert result['state']=='NOT_RUN' and result['reason']=='human_qrels_incomplete'


def test_retrieval_report_is_read_only_and_does_not_invent_savings(memory_repo,capsys):
    conn=index.connect(memory_repo)
    index.record_search(conn,'nothing','global',[],retrieval={'level':'L2','origin':'mcp_search','reason':'not_configured','remote_calls':0})
    conn.close()
    from knowbase.retrieval_metrics import report
    before=(memory_repo/'memory.db').read_bytes();result=report(memory_repo)
    assert result['levels']=={'L2':1} and result['net_token_savings'] is None and result['currency_cost_actual'] is None
    assert (memory_repo/'memory.db').read_bytes()==before


def test_interactive_init_private_secret_uses_same_config_journal(memory_repo,tmp_path,monkeypatch,capsys):
    import getpass
    monkeypatch.setenv('HOME',str(tmp_path));monkeypatch.setattr(sys.stdin,'isatty',lambda:True)
    values=iter(['https://internal.invalid/v1','company-model','COMPANY_KEY','deployment-1','global','y'])
    monkeypatch.setattr('builtins.input',lambda prompt:next(values))
    monkeypatch.setattr(getpass,'getpass',lambda prompt:'SYNTHETIC_PRIVATE_CREDENTIAL_VALUE')
    assert main(['init','--agents','claude-code','--embedding','configure'])==0
    options=config.load_config()['embedding'];assert options['secret_ref'].startswith('file:')
    p=Path(options['secret_ref'][5:]);assert p.read_text()=='SYNTHETIC_PRIVATE_CREDENTIAL_VALUE' and p.stat().st_mode&0o077==0
    assert 'SYNTHETIC_PRIVATE_CREDENTIAL_VALUE' not in capsys.readouterr().out
    for journal in (tmp_path/'.knowbase/backups').glob('init-*/journal.json'):
        assert 'SYNTHETIC_PRIVATE_CREDENTIAL_VALUE' not in journal.read_text()


def test_effective_adoption_uses_completed_version_bound_outcome_not_reads(memory_repo):
    from test_local_retrieval_upgrade import seed
    from knowbase.server import context_impl,use_impl,feedback_impl
    from knowbase.retrieval_metrics import report
    mid=seed(memory_repo)
    delivered=json.loads(context_impl(session_id='synthetic-adoption',event_id='context-1',scope='global',task_id='synthetic-task',
        topics=[{'topic_id':'har','queries':['HAR'],'status':'active'}]))
    card=next(v for v in delivered['candidates'] if v['id']==mid)
    used=json.loads(use_impl(session_id='synthetic-adoption',event_id='use-1',context_event_id='context-1',id=mid,
        content_revision=card['content_revision'],state='adopted'))
    assert report(memory_repo)['effective_adoption_rate'] is None
    response=feedback_impl(mid,'helpful',expected_content_revision=card['content_revision'],use_id=used['use_id'],
        task_outcome_id='synthetic-completed-outcome',task_result='completed',request_id='synthetic-feedback')
    assert '已记录反馈' in response
    result=report(memory_repo)
    assert result['observed_used_task_outcomes']==1 and result['completed_helpful_outcomes']==1
    assert result['effective_adoption_rate']==1 and result['net_token_savings'] is None


def test_hook_shell_commands_quote_special_home_paths_without_expansion(tmp_path):
    import shlex
    home=tmp_path/'space $(should-not-execute)'
    changes=integration.plan(['claude-code','codex'],home)
    for change in changes:
        if change['path'].endswith(('settings.json','hooks.json')):
            hooks=json.loads(change['after'])['hooks']
            for entries in hooks.values():
                for entry in entries:
                    for handler in entry['hooks']:
                        assert shlex.split(handler['command'])[0]==integration.runtime_command(home.resolve())


def test_trae_merges_native_mcpservers_without_shadow_root(tmp_path):
    path=tmp_path/'Library/Application Support/Trae CN/User/mcp.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'mcpServers':{'other':{'command':'keep'}}}))
    integration.apply(integration.plan(['trae'],tmp_path),tmp_path)
    doc=json.loads(path.read_text())
    assert set(doc)=={'mcpServers'} and doc['mcpServers']['other']=={'command':'keep'}
    assert next(h for h in integration.discover(tmp_path) if h['id']=='trae')['configured']
    assert integration.plan(['trae'],tmp_path)==[]
