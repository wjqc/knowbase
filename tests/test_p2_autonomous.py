"""P2 offline end-to-end controller regressions; no real model or team network."""
import json
import re
import sys

import pytest

from knowbase import config, index
from knowbase.server import capture_impl
from knowbase.automation import receipts, distillation, execution
from knowbase.automation.protocol import build_isolation_material


def setup_executor(monkeypatch, *, script=None, verified=True):
    from test_p1_executor_probe import FAKE_GOOD_SCRIPT
    options = {'executor_id':'fixture-text','kind':'command','command':sys.executable,
               'noninteractive_argv':['-c',script or FAKE_GOOD_SCRIPT], 'timeout_seconds':10}
    cfg=config.load_config()
    cfg['automation'].update(enabled=True,allowed_scopes=['probe'],allow_trusted_commands=True,
                             executors=[options])
    config.CONFIG_PATH.write_text(json.dumps(cfg))
    if verified: receipts.record(options,{'passed':True})
    return cfg, options


def capture(text=None, scope='probe'):
    out=capture_impl(session_id='p2-fixture',scope=scope,
                     messages=[{'role':'user','text':text or build_isolation_material()['text']}])
    return re.search(r'J-[a-f0-9]+',out).group()


def state(repo, job):
    c=index.connect(repo)
    try: return c.execute('SELECT state FROM jobs WHERE job_id=?',(job,)).fetchone()[0]
    finally: c.close()


NO_VALUE = "import sys,json;p=json.load(sys.stdin);print(json.dumps({'candidates':[],'segment_results':[{'segment_id':s['segment_id'],'decision':'no_value','reason':'synthetic transient observation'} for s in p['segments']]}))"


def test_real_controller_claim_page_submit_readback(memory_repo, monkeypatch):
    cfg,_=setup_executor(monkeypatch); job=capture()
    result=distillation.run_due(memory_repo,cfg)
    assert result['processed']==1 and state(memory_repo,job)=='needs_review',result
    c=index.connect(memory_repo)
    assert c.execute('SELECT count(*) FROM meta').fetchone()[0]==1
    assert c.execute('SELECT count(*) FROM feedback_events').fetchone()[0]==0
    assert c.execute('SELECT count(*) FROM executor_runs WHERE state=\'settled\'').fetchone()[0]==1
    c.close()
    assert distillation.run_due(memory_repo,cfg)=={'skipped':'no_due_distillation_jobs'}


def test_no_verified_executor_or_scope_never_claims(memory_repo, monkeypatch):
    cfg,opt=setup_executor(monkeypatch,verified=False); job=capture()
    assert distillation.run_due(memory_repo,cfg)['blocked']=='verified_executor_required'
    assert state(memory_repo,job)=='awaiting_agent'
    receipts.record(opt,{'passed':True});cfg['automation']['allowed_scopes']=['other']
    assert distillation.run_due(memory_repo,cfg)['skipped']=='no_due_distillation_jobs'
    assert state(memory_repo,job)=='awaiting_agent'


def test_no_candidate_is_explicitly_settled(memory_repo,monkeypatch):
    cfg,_=setup_executor(monkeypatch,script=NO_VALUE);job=capture('Synthetic: no reusable conclusion.')
    distillation.run_due(memory_repo,cfg)
    assert state(memory_repo,job)=='no_candidate'
    c=index.connect(memory_repo);assert c.execute('SELECT count(*) FROM meta').fetchone()[0]==0;c.close()


def test_cached_response_survives_crash_before_submit(memory_repo,monkeypatch):
    cfg,_=setup_executor(monkeypatch);job=capture();original=distillation.materialize.submit
    def crash(*a,**k):raise RuntimeError('synthetic controller crash')
    monkeypatch.setattr(distillation.materialize,'submit',crash)
    with pytest.raises(RuntimeError):distillation.run_due(memory_repo,cfg)
    c=index.connect(memory_repo);c.execute("UPDATE jobs SET lease_until='2000-01-01' WHERE job_id=?",(job,));c.commit();c.close()
    monkeypatch.setattr(distillation.materialize,'submit',original)
    monkeypatch.setattr(execution,'execute_text',lambda *a,**k:pytest.fail('must reuse durable response'))
    distillation.run_due(memory_repo,cfg)
    assert state(memory_repo,job)=='needs_review'
    c=index.connect(memory_repo);assert c.execute('SELECT calls FROM automation_budget').fetchone()[0]==1;c.close()


def test_daily_budget_defers_without_burning_attempts(memory_repo,monkeypatch):
    cfg,_=setup_executor(monkeypatch);cfg['automation']['daily_calls']=0;job=capture()
    result=distillation.run_due(memory_repo,cfg)
    assert result['results'][0]['error']=='daily_budget_exhausted'
    c=index.connect(memory_repo);row=c.execute('SELECT state,attempt,next_run_at FROM jobs WHERE job_id=?',(job,)).fetchone();c.close()
    assert row[0]=='retry_wait' and row[1]==0 and row[2]>distillation.now()


def test_incomplete_output_does_not_claim_success(memory_repo,monkeypatch):
    cfg,_=setup_executor(monkeypatch,script="print('{\"candidates\":[],\"segment_results\":[]}')");job=capture()
    result=distillation.run_due(memory_repo,cfg)
    assert result['results'][0]['error']=='incomplete_segment_results'
    assert state(memory_repo,job)=='retry_wait'


def test_configuration_change_invalidates_receipt(memory_repo,monkeypatch):
    cfg,opt=setup_executor(monkeypatch)
    assert receipts.verified(opt)
    assert not receipts.verified({**opt,'model':'different-deployment'})


def test_lease_takeover_rejects_late_model_response(memory_repo,monkeypatch):
    cfg,_=setup_executor(monkeypatch);job=capture();original=execution.execute_text
    def takeover(*args,**kw):
        result=original(*args,**kw)
        c=index.connect(memory_repo);c.execute("UPDATE jobs SET lease_owner='new-owner:new-token',fencing_token=fencing_token+1 WHERE job_id=?",(job,));c.commit();c.close()
        return result
    monkeypatch.setattr(execution,'execute_text',takeover)
    result=distillation.run_due(memory_repo,cfg)
    assert result['results'][0]['error']=='authority_changed'
    c=index.connect(memory_repo);assert c.execute('SELECT count(*) FROM meta').fetchone()[0]==0
    assert c.execute('SELECT lease_owner FROM jobs WHERE job_id=?',(job,)).fetchone()[0]=='new-owner:new-token';c.close()


def test_retry_queued_manifest_is_prepared_without_new_model_call(memory_repo,monkeypatch):
    cfg,_=setup_executor(monkeypatch,script=NO_VALUE);job=capture('synthetic transient material')
    c=index.connect(memory_repo);c.execute("UPDATE jobs SET state='queued' WHERE job_id=?",(job,));c.commit();c.close()
    distillation.run_due(memory_repo,cfg)
    assert state(memory_repo,job)=='no_candidate'


def test_expired_receipt_does_not_probe_while_idle(memory_repo,monkeypatch):
    cfg,opt=setup_executor(monkeypatch)
    path=receipts._path(opt);data=json.loads(path.read_text());data['verified_at']=0;path.write_text(json.dumps(data))
    from knowbase.automation import isolation
    monkeypatch.setattr(isolation,'isolated_probe',lambda *a,**k:pytest.fail('idle worker must not call a model'))
    assert distillation.run_due(memory_repo,cfg)=={'skipped':'no_due_distillation_jobs'}


def test_transient_executor_failure_recovers_after_bounded_probe(memory_repo,monkeypatch):
    from knowbase.automation import isolation
    from knowbase.automation.protocol import ExecutorFailure
    cfg,opt=setup_executor(monkeypatch,script=NO_VALUE);job=capture('synthetic transient observation')
    original=execution.execute_text
    def unavailable(*a,**k):raise ExecutorFailure('timeout')
    monkeypatch.setattr(execution,'execute_text',unavailable)
    c=index.connect(memory_repo);c.execute('UPDATE jobs SET max_attempts=1 WHERE job_id=?',(job,));c.commit();c.close()
    distillation.run_due(memory_repo,cfg);assert state(memory_repo,job)=='failed'
    path=receipts._path(opt);data=json.loads(path.read_text());data['next_probe_at']=0;path.write_text(json.dumps(data))
    monkeypatch.setattr(isolation,'isolated_probe',lambda *a,**k:{'passed':True})
    monkeypatch.setattr(execution,'execute_text',original)
    distillation.run_due(memory_repo,cfg)
    assert state(memory_repo,job)=='no_candidate'


def test_provider_usage_is_recorded_once_and_not_a_bill(memory_repo,monkeypatch):
    cfg,opt=setup_executor(monkeypatch,script=NO_VALUE)
    opt.update(kind='intranet_text',model='synthetic-company-model')
    cfg['value']['events_enabled']=True;config.CONFIG_PATH.write_text(json.dumps(cfg));receipts.record(opt,{'passed':True})
    capture('synthetic observation')
    def response(options,request,text,segments,policy):
        return {'candidates':[],'segment_results':[{'segment_id':s['segment_id'],'decision':'no_value','reason':'synthetic'} for s in segments],
                'usage':{'prompt_tokens':30,'completion_tokens':5}}
    monkeypatch.setattr(execution,'execute_text',response)
    distillation.run_due(memory_repo,cfg)
    c=index.connect(memory_repo);assert c.execute("SELECT count(*) FROM value_outbox WHERE event_type='model.usage'").fetchone()[0]==1;c.close()
    from knowbase.retrieval_metrics import report
    metrics=report(memory_repo)
    assert metrics['executor_invocations_started']==1 and metrics['background_cost_actual'] is None
