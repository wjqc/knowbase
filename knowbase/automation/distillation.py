"""P2 controller: bounded jobs, full material pages, durable responses and shared writer."""
import json
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from datetime import timedelta

from .. import config, index, jobs, materials, store
from ..embedding_provider import digest
from ..extraction import materialize
from . import receipts
from .protocol import DistillationRequest, ExecutorFailure


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def prepare_due(repo, conn):
    """Repair a crash between durable enqueue and manifest preparation, without inference."""
    rows=conn.execute("SELECT job_id,state FROM jobs WHERE kind IN ('capture','extract') "
                      "AND state IN ('queued','preparing') ORDER BY created_at LIMIT 10").fetchall()
    prepared=0
    for job_id,state in rows:
        try:
            payload=materials.payload_for_job(conn,job_id)
            if payload.get('protocol_version')!=2 or not payload.get('segments'):
                raise ValueError('material_manifest_required')
            if state=='queued':jobs.set_preparing(conn,job_id)
            materials.register_manifest(conn,job_id,payload)
            if jobs.set_awaiting(conn,job_id,len(payload['segments'])):prepared+=1
        except (ValueError,TypeError,KeyError):
            conn.execute("UPDATE jobs SET state='blocked',error='material_manifest_required' WHERE job_id=? "
                         "AND state IN ('queued','preparing')",(job_id,));conn.commit()
    return prepared


def ready_executor(cfg):
    policy = cfg.get('automation', {})
    if not policy.get('enabled'):
        return None, 'automation_disabled'
    if not policy.get('allowed_scopes'):
        return None, 'scope_authorization_required'
    if not isinstance(policy['allowed_scopes'],list) or not all(isinstance(s,str) and s and s!='*' for s in policy['allowed_scopes']):
        return None,'invalid_scope_authorization'
    for options in policy.get('executors', []):
        if policy.get('selected_executor') and options.get('executor_id')!=policy['selected_executor']:continue
        if options.get('kind', 'command') == 'command' and not policy.get('allow_trusted_commands'):
            continue
        if receipts.verified(options):
            return options, None
    return None, 'verified_executor_required'


def _owned(conn, job_id, claim):
    row = conn.execute('SELECT lease_owner,fencing_token,lease_until FROM jobs WHERE job_id=?', (job_id,)).fetchone()
    return bool(row and row[0] and row[0].endswith(':'+claim['lease_token'])
                and row[1] == claim['fencing_token'] and row[2] and row[2] > now())


def _renew(repo, job_id, claim, stop, lost):
    while not stop.wait(10):
        conn = index.connect(repo)
        try:
            if not _owned(conn, job_id, claim) or jobs.renew(conn, job_id, claim['lease_token']).get('error'):
                lost.set(); return
        except sqlite3.Error:
            lost.set(); return
        finally:
            conn.close()


def _reserve(conn, policy, chars):
    day = now()[:10]
    conn.execute('BEGIN IMMEDIATE')
    try:
        conn.execute('INSERT OR IGNORE INTO automation_budget(day) VALUES(?)', (day,))
        row = conn.execute('SELECT calls,input_chars FROM automation_budget WHERE day=?', (day,)).fetchone()
        if row[0] >= max(0, int(policy.get('daily_calls', 20))) or row[1]+chars > max(0, int(policy.get('daily_input_chars', 512000))):
            conn.rollback(); return False
        conn.execute('UPDATE automation_budget SET calls=calls+1,input_chars=input_chars+? WHERE day=?', (chars, day))
        conn.commit(); return True
    except Exception:
        conn.rollback(); raise


def _pause(conn, job_id, claim, reason, *, retry=True):
    conn.execute('BEGIN IMMEDIATE')
    if not _owned(conn, job_id, claim):
        conn.rollback(); return
    # Never rewrite another claimant's job; a registered submission is recovered first.
    pending = conn.execute("SELECT 1 FROM job_submissions WHERE job_id=? AND state!='done'", (job_id,)).fetchone()
    if pending:
        conn.rollback(); return
    if reason == 'daily_budget_exhausted':
        tomorrow = (datetime.now(timezone.utc)+timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0).isoformat(timespec='seconds')
        conn.execute("UPDATE jobs SET state='retry_wait',next_run_at=?,error=?,attempt=MAX(0,attempt-1),"
                     "lease_owner=NULL,lease_until=NULL WHERE job_id=?",(tomorrow,reason,job_id))
        conn.commit(); return
    conn.commit()
    # CAS includes the ownership again inside the transaction used by jobs.fail.
    jobs.fail(conn, job_id, reason, retryable=retry,
              expected_owner=claim['lease_token'], expected_fencing=claim['fencing_token'])


def _one(repo, conn, job_id, options, policy, guard):
    host = 'knowbase-background-'+uuid.uuid4().hex
    claim = jobs.claim(conn, job_id, host, kinds=jobs.PUBLIC_CLAIM_KINDS)
    if claim.get('error'): return {'job_id': job_id, 'skipped': 'claim_unavailable'}
    stop, lost = threading.Event(), threading.Event()
    heartbeat = threading.Thread(target=_renew, args=(repo,job_id,claim,stop,lost), daemon=True)
    heartbeat.start()
    run_id = None
    try:
        manifest = materials.create_batch(conn, claim, job_id, host,
                         max_chars=max(1,int(policy.get('max_input_chars',128000))//2))
        if not manifest['batch_segment_ids']:raise ExecutorFailure('material_manifest_required')
        text, segments, cursor = {}, {}, ''
        while True:
            page = materials.page(conn, job_id, claim['batch_id'], claim['lease_token'],
                cursor=cursor, max_chars=8000, fencing_token=claim['fencing_token'])
            if page.get('error'): raise ExecutorFailure('material_unavailable')
            for item in page['materials']:
                sid = item['segment_id']; text[sid] = text.get(sid, '')+item['text']
                segments[sid] = {'segment_id': sid, 'text_hash': item['text_hash']}
            if sum(map(len, text.values())) > int(policy.get('max_input_chars',128000)):
                raise ExecutorFailure('material_budget_exceeded')
            if not page['has_more']: break
            if cursor == page['next_cursor']: raise ExecutorFailure('material_cursor_stalled')
            cursor = page['next_cursor']
        related = []
        for card in manifest['similar_cards']:
            meta, body, _ = store.load(repo, card['id'])
            if meta and meta.get('scope') in {claim['scope'], 'global'}:
                from ..mutations import _compute_governance_revision
                related.append({**card, 'body': body,'governance_revision':_compute_governance_revision(meta)})
        full_text = '\n\n'.join(text.values())
        if related:
            full_text += '\n\n[RELATED_KNOWLEDGE_DATA]\n'+json.dumps({
                'similarity_snapshot':manifest['similarity_snapshot'],'similar_cards':related},ensure_ascii=False)
        if len(full_text) > int(policy.get('max_input_chars',128000)):
            raise ExecutorFailure('material_budget_exceeded')
        config_hash = receipts.fingerprint(options)
        input_hash = digest([job_id, manifest['input_version'], list(segments.values()), related, config_hash])
        cached = conn.execute("SELECT response_json FROM executor_runs WHERE input_hash=? AND config_hash=? "
                              "AND response_json IS NOT NULL ORDER BY created_at DESC LIMIT 1", (input_hash,config_hash)).fetchone()
        run_id = 'RUN-'+uuid.uuid4().hex; request_id = 'auto:'+claim['batch_id']
        conn.execute('INSERT INTO executor_runs(run_id,job_id,batch_id,executor_id,config_hash,input_hash,request_id,'
            'fencing_token,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (run_id,job_id,claim['batch_id'],str(options.get('executor_id','configured')),config_hash,input_hash,
             request_id,claim['fencing_token'],'preparing',now(),now()));conn.commit()
        if cached:
            result = json.loads(cached[0])
        else:
            if not _reserve(conn,policy,len(full_text)): raise ExecutorFailure('daily_budget_exhausted')
            conn.execute("UPDATE executor_runs SET state='calling',invoked=1,updated_at=? WHERE run_id=?",(now(),run_id));conn.commit()
            from .execution import execute_text
            result = execute_text(options, DistillationRequest(job_id,claim['scope'],host),
                                  full_text,list(segments.values()),policy)
        if not isinstance(result,dict) or not isinstance(result.get('candidates'),list) or not isinstance(result.get('segment_results'),list):
            raise ExecutorFailure('structured_output')
        from ..credentials import contains_credentials
        if contains_credentials(result): raise ExecutorFailure('credential_material_rejected')
        if len(json.dumps(result,ensure_ascii=False)) > int(policy.get('max_output_chars',64000)):
            raise ExecutorFailure('output_budget_exceeded')
        # A complete batch must explicitly settle every segment; no silent partial success.
        if {r.get('segment_id') for r in result['segment_results'] if isinstance(r,dict)} != set(segments):
            raise ExecutorFailure('incomplete_segment_results')
        # Persist before submitting. After a crash, reuse the response rather than calling again.
        usage=result.get('usage')
        usage={k:v for k,v in usage.items() if k in {'input_tokens','output_tokens','prompt_tokens','completion_tokens','total_tokens'} and type(v) is int and v>=0} if isinstance(usage,dict) else {}
        usage_source=('observed' if options.get('kind')=='intranet_text' else 'executor_report') if usage else 'unavailable'
        if cached:usage={};usage_source='cache'
        conn.execute("UPDATE executor_runs SET state='received',response_json=?,usage_json=?,usage_source=?,updated_at=? WHERE run_id=?",
            (json.dumps(result,ensure_ascii=False),json.dumps(usage),usage_source,now(),run_id))
        if usage_source=='observed':
            from ..value.events import record
            observed={'usage_source':'model_usage','session_id':host,'request_id':run_id,
                      'model':options.get('model','unknown'),'provider':'intranet_text'}
            if 'prompt_tokens' in usage:observed['input_tokens']=usage['prompt_tokens']
            if 'completion_tokens' in usage:observed['output_tokens']=usage['completion_tokens']
            record(conn,'model.usage',observed,source_key='executor-usage:'+run_id,host='knowbase-worker',session_id=host)
        conn.commit()
        if lost.is_set() or not guard() or not receipts.verified(options) or not _owned(conn,job_id,claim):
            raise ExecutorFailure('authority_changed')
        candidates = json.loads(json.dumps(result['candidates']))
        for candidate in candidates:
            candidate['candidate_key'] = 'auto-'+digest([manifest['batch_segment_ids'],candidate.get('candidate_key')])[:32]
        submitted = materialize.submit(repo,conn,job_id,claim['batch_id'],claim['lease_token'],request_id,
                                        candidates,result['segment_results'],claim['fencing_token'])
        if submitted.get('error') or submitted.get('failed'):
            conn.execute('UPDATE executor_runs SET response_json=NULL WHERE run_id=?',(run_id,));conn.commit()
            raise ExecutorFailure('candidate_validation_failed')
        for item in submitted.get('results',[]):
            if item.get('memory_id'):
                from ..mutations import _compute_revision
                meta,body,_=store.load(repo,item['memory_id'])
                if not meta or _compute_revision(meta,body)!=item.get('content_revision'):
                    raise ExecutorFailure('readback_failed')
        conn.execute("UPDATE executor_runs SET state='settled',result_json=?,updated_at=? WHERE run_id=?",
                     (json.dumps(submitted,ensure_ascii=False),now(),run_id));conn.commit()
        return {'job_id':job_id,'state':submitted['state'],'saved':submitted.get('saved_active',0)+submitted.get('saved_staging',0)}
    except ExecutorFailure as exc:
        reason = exc.category
        if reason in receipts.TRANSIENT_ERRORS or reason=='model_mismatch':
            receipts.record(options, {'passed': False})
        _pause(conn,job_id,claim,reason,retry=reason not in {'material_budget_exceeded'})
        if run_id:
            conn.execute("UPDATE executor_runs SET state='failed',error_code=?,updated_at=? WHERE run_id=?",(reason,now(),run_id));conn.commit()
        return {'job_id':job_id,'error':reason}
    finally:
        stop.set();heartbeat.join(timeout=2)


def run_due(repo, cfg=None, *, guard=lambda: True):
    cfg = cfg or config.load_config(); options, reason = ready_executor(cfg)
    if options is None and reason=='verified_executor_required':
        policy=cfg['automation']
        check=index.connect(repo)
        try:
            due=check.execute("SELECT scope,state,error FROM jobs WHERE kind IN ('capture','extract') AND "
                "(state IN ('awaiting_agent','failed') OR (state='retry_wait' AND next_run_at<=?) OR (state='claimed' AND lease_until<=?))",(now(),now()))
            has_work=any(row[0] in policy['allowed_scopes'] and (row[1]!='failed' or row[2] in receipts.TRANSIENT_ERRORS) for row in due)
        finally:check.close()
        if not has_work:return {'skipped':'no_due_distillation_jobs'}
        for candidate in policy.get('executors',[]):
            if policy.get('selected_executor') and candidate.get('executor_id')!=policy['selected_executor']:continue
            if candidate.get('kind','command')=='command' and not policy.get('allow_trusted_commands'):continue
            if receipts.needs_refresh(candidate) and guard():
                check=index.connect(repo)
                try: permitted=_reserve(check,policy,50000)
                finally:check.close()
                if permitted:
                    from .isolation import isolated_probe
                    proof=receipts.record(candidate,isolated_probe(candidate,scope='probe'))
                    if proof['passed']:
                        repair=index.connect(repo)
                        try:
                            errors=list(receipts.TRANSIENT_ERRORS);scopes=policy['allowed_scopes']
                            repair.execute("UPDATE jobs SET state='awaiting_agent',attempt=0,error=NULL,next_run_at=? WHERE "
                                "kind IN ('capture','extract') AND state IN ('failed','retry_wait') AND error IN ("+','.join('?' for _ in errors)+") AND scope IN ("+','.join('?' for _ in scopes)+")",[now(),*errors,*scopes]);repair.commit()
                        finally:repair.close()
                break
        options,reason=ready_executor(cfg)
    if options is None:return {'blocked':reason}
    conn = index.connect(repo)
    policy = cfg['automation']; started = time.monotonic(); results = []
    try:
        prepare_due(repo,conn)
        # Complete registered submissions before requesting any new model inference.
        pending = conn.execute("SELECT DISTINCT job_id FROM job_submissions WHERE state!='done' LIMIT 10").fetchall()
        for (job_id,) in pending:
            if not guard(): return {'blocked':'supervisor_lease_lost'}
            materialize.recover(repo,conn,job_id)
        allowed = list(policy.get('allowed_scopes', []))
        if not all(isinstance(s,str) and s for s in allowed): return {'blocked':'invalid_scope_authorization'}
        sql = "SELECT job_id FROM jobs WHERE kind IN ('capture','extract') AND scope IN ("+','.join('?' for _ in allowed)+") "
        sql += "AND (state='awaiting_agent' OR (state='retry_wait' AND next_run_at<=?) OR (state='claimed' AND lease_until<=?)) ORDER BY updated_at,scope,job_id LIMIT ?"
        rows = conn.execute(sql, [*allowed,now(),now(),max(1,min(10,int(policy.get('max_batches',2))))]).fetchall()
        for (job_id,) in rows:
            if not guard() or time.monotonic()-started > min(600,float(policy.get('job_seconds',600))): break
            remaining=max(.05,min(600,float(policy.get('job_seconds',600)))-(time.monotonic()-started))
            results.append(_one(repo,conn,job_id,options,{**policy,'batch_seconds':min(remaining,float(policy.get('batch_seconds',120)))},guard))
        return {'processed':len(results),'results':results} if results else {'skipped':'no_due_distillation_jobs'}
    finally:
        conn.close()
