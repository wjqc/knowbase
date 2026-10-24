"""Foreground assistance must not turn a completed task into queue maintenance."""
import io
import json
from datetime import datetime, timezone

import pytest

from knowbase import config, hooks, index, materials
from knowbase.adapters import host_events
from knowbase.adapters.normalizer import NormalizedEvent


def enable():
    cfg=config.load_config();cfg['inflight']['enabled']=True
    cfg['git'].update(auto_pull=False,auto_push=False,auto_commit=False)
    config.CONFIG_PATH.write_text(json.dumps(cfg))


@pytest.mark.parametrize('style',['zcode','claude','trae'])
def test_stop_never_blocks_for_new_job_or_pending_feedback(memory_repo,monkeypatch,capsys,style):
    enable()
    results=iter([{'job_ids':['J-one']},{'job_ids':['J-two']},{'error':'MATERIAL_PARSE_FAILED'}])
    monkeypatch.setattr(host_events,'handle',lambda *a,**k:next(results))
    for _ in range(3):
        monkeypatch.setattr(hooks.sys,'stdin',io.StringIO('{"session_id":"s"}'))
        hooks.cmd_stop(style)
        assert capsys.readouterr().out==''


def test_legacy_stop_silently_queues_and_does_not_demand_feedback(memory_repo,tmp_path,monkeypatch):
    transcript=tmp_path/'legacy.jsonl'
    transcript.write_text(json.dumps({'type':'tool_use','name':'Bash','input':{'command':'test'}})+'\n')
    calls=[]
    monkeypatch.setattr(hooks,'_try_enqueue_capture',lambda *a:(calls.append(1) or 'J-new'))
    assert hooks.stop_event('legacy',str(transcript))==''
    assert calls==[1]


def test_stop_keeps_business_rule_queue_without_continuing_conversation(memory_repo,tmp_path,monkeypatch,capsys):
    enable();transcript=tmp_path/'rule.jsonl'
    transcript.write_text(json.dumps({'role':'user','content':'业务规则：已结算订单不得撤回，出处PRD第3条。'},ensure_ascii=False)+'\n')
    monkeypatch.setattr(hooks.sys,'stdin',io.StringIO(json.dumps({'session_id':'business','transcript_path':str(transcript)})))
    hooks.cmd_stop('zcode');assert capsys.readouterr().out==''
    conn=index.connect(memory_repo)
    job=conn.execute('SELECT job_id FROM jobs').fetchone()
    assert job and '已结算订单' in materials.payload_for_job(conn,job[0])['segments'][0]['text']
    conn.close()


def test_auto_capture_filters_evaluation_and_operational_read_but_keeps_rule(memory_repo,tmp_path):
    enable();transcript=tmp_path/'eval.jsonl'
    rows=[{'role':'assistant','content':'# knowbase 产品评估\n本周938次调用，这是一份一次性产品评估。'},
          {'role':'tool','text':json.dumps({'hook_event':'tool-before','payload':{'tool_name':'Read','tool_input':{'file_path':'README.md'}}})},
          {'role':'tool','text':json.dumps({'hook_event':'tool-after','payload':{'tool_name':'Read','tool_input':{'file_path':'progress.md'},'tool_response':{'stdout':'当前执行进度'}}})},
          {'role':'user','content':'业务规则：冻结合同不能修改，出处合同PRD第5条。'},
          {'role':'assistant','content':'修复经验：重试必须沿用幂等键；已用重复请求验证不会再次扣款。'}]
    transcript.write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in rows))
    result=host_events.handle({'session_id':'filter','transcript_path':str(transcript)},event='stop',style='zcode')
    conn=index.connect(memory_repo);payload=materials.payload_for_job(conn,result['job_ids'][0]);conn.close()
    texts='\n'.join(s['text'] for s in payload['segments'])
    assert '冻结合同' in texts and '幂等键' in texts
    assert '938次调用' not in texts and '当前执行进度' not in texts
    assert len(payload['segments'])==2


def test_pending_jobs_never_render_as_foreground_orders():
    assert host_events.render({'job_ids':['J-one'],'notify_job_ids':['J-one']})==''


def test_foreground_notice_budget_deduplicates_and_resets_on_new_prompt(memory_repo):
    enable();conn=index.connect(memory_repo)
    conn.execute("INSERT INTO context_sessions(host_profile,session_id,scope,coverage_json,updated_at) VALUES('zcode','notice','global','{}',?)",(datetime.now(timezone.utc).isoformat(),));conn.commit();conn.close()
    data={'session_id':'notice','prompt':'开发订单功能'}
    plan={'needs_query_plan':True,'planning_context':{'session_id':'notice','host_profile':'zcode','scope':'global','event_id':'plan','stage':'task_plan'}}
    first=host_events.render_foreground(plan,data,event='user-prompt',style='zcode')
    assert 'memory_context' in first
    assert host_events.render_foreground(plan,data,event='tool-before',style='zcode')==''
    assert host_events.render_foreground({'error':'SCOPE_MISMATCH'},data,event='tool-after',style='zcode')
    assert host_events.render_foreground({'error':'OTHER'},data,event='tool-after',style='zcode')==''
    data['prompt']='现在开发退款规则'
    assert host_events.render_foreground(plan,data,event='user-prompt',style='zcode')
    assert host_events.render_foreground(plan,data,event='stop',style='zcode')==''


def test_initial_policy_is_short_and_does_not_order_queue_maintenance():
    text=hooks.session_start()
    assert len(text)<800
    assert '不阻断' in text and 'memory_context' in text and '业务规则' in text


def test_readme_with_explicit_business_rule_and_source_is_retained():
    from knowbase.hook_policy import filter_capture
    event=NormalizedEvent(session_id='s',message_id='m',role='tool',text=json.dumps({
        'hook_event':'tool-after','payload':{'tool_name':'Read','tool_input':{'file_path':'README.md'},
        'tool_response':{'stdout':'业务规则：付款后禁止修改金额。出处：支付PRD第3条。'}}}))
    kept,counts=filter_capture([event])
    assert kept==[event] and counts=={}


def test_explicit_capture_still_accepts_report_material(memory_repo):
    from knowbase.server import capture_impl
    result=capture_impl(session_id='explicit',scope='global',messages=[{
        'role':'assistant','text':'# 项目评估\n用户明确要整理这份原始评估。'}])
    conn=index.connect(memory_repo)
    import re
    payload=materials.payload_for_job(conn,re.search(r'J-[a-f0-9]+',result).group());conn.close()
    assert '项目评估' in payload['segments'][0]['text']


def test_notice_never_truncates_identity_or_grows_above_budget(memory_repo):
    enable()
    data={'session_id':'long','prompt':'开发订单规则'}
    assert host_events.render_foreground({'needs_query_plan':True,'planning_context':{'session_id':'a'*1500}},data,event='user-prompt',style='zcode')==''


def test_filtered_only_capture_advances_cursor_without_creating_job(memory_repo,tmp_path):
    enable();transcript=tmp_path/'report.jsonl'
    transcript.write_text(json.dumps({'role':'assistant','text':'# knowbase 产品评估\n一次性指标。'},ensure_ascii=False)+'\n')
    result=host_events.handle({'session_id':'report','transcript_path':str(transcript)},event='stop',style='zcode')
    assert result['job_ids']==[] and result['capture_status']=='no_delta'
    conn=index.connect(memory_repo)
    assert conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==0
    cov=json.loads(conn.execute("SELECT coverage_json FROM context_sessions WHERE session_id='report'").fetchone()[0]);conn.close()
    assert cov['_hook_capture']['cursor']==1
    assert cov['_hook_capture_filter']['one_off_report']==1


@pytest.mark.parametrize('style',['zcode','claude','trae'])
def test_formal_cli_two_stops_do_not_resume_model_but_persist_rule(memory_repo,tmp_path,style):
    import os,subprocess,sys
    enable()
    transcript=tmp_path/'formal.jsonl'
    transcript.write_text(json.dumps({'role':'user','text':'业务规则：已支付订单不可撤回；出处PRD第4条。'},ensure_ascii=False)+'\n')
    payload={'session_id':'formal-'+style,'transcript_path':str(transcript)}
    interpreter=os.environ.get('KNOWBASE_HOOK_INTERPRETER',sys.executable)
    args=[interpreter,*(['-I'] if os.environ.get('KNOWBASE_HOOK_INTERPRETER') else []),'-m','knowbase','hook','stop','--style',style]
    env={**os.environ,'KNOWBASE_CONFIG':str(config.CONFIG_PATH),'KNOWBASE_REPO_PATH':str(memory_repo),'KNOWBASE_AGENT_NAME':style}
    for _ in range(2):
        result=subprocess.run(args,input=json.dumps(payload),text=True,capture_output=True,env=env,timeout=15)
        assert result.returncode==0 and result.stdout=='' and result.stderr==''
    conn=index.connect(memory_repo)
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE kind='capture'").fetchone()[0]==1
    conn.close()


def test_explicit_observation_capture_can_revisit_auto_filtered_document(memory_repo):
    enable()
    conn=index.connect(memory_repo);now=datetime.now(timezone.utc).isoformat()
    conn.execute("INSERT INTO context_sessions(host_profile,session_id,scope,coverage_json,updated_at) VALUES('zcode','revisit','global','{}',?)",(now,))
    obs={'hook_event':'tool-after','payload':{'tool_name':'Read','tool_input':{'file_path':'progress.md'},'tool_response':{'stdout':'REVIEW_MARKER: 一次性进度'}}}
    conn.execute("INSERT INTO context_events(host_profile,session_id,event_id,trigger,payload_hash,status,created_at,observation_json) VALUES('zcode','revisit','r1','observe','h','executed',?,?)",(now,json.dumps(obs)))
    conn.commit();conn.close()
    auto=host_events.capture_increment(memory_repo,'revisit','','global','zcode')
    assert auto['job_ids']==[]
    from knowbase.server import capture_impl
    import re
    result=capture_impl(session_id='revisit',scope='global',host_profile='zcode')
    job=re.search(r'J-[a-f0-9]+',result).group()
    conn=index.connect(memory_repo);payload=materials.payload_for_job(conn,job);conn.close()
    assert 'REVIEW_MARKER' in payload['segments'][0]['text']
    repeated=capture_impl(session_id='revisit',scope='global',host_profile='zcode')
    assert '已入队' not in repeated
