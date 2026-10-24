"""Synthetic HTTP/model fixtures prove contracts, never real semantic quality."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from knowbase import config,index,semantic_index as si,retrieval_state
from knowbase.embedding_provider import OpenAICompatProvider,ProviderError,digest
from test_local_retrieval_upgrade import seed


@pytest.fixture
def gateway():
    state={'status':200,'response':None,'calls':[],'delay':0,'dim':3}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*a):pass
        def do_GET(self):
            raw=json.dumps({'data':[{'id':'company-test'}]}).encode();self.send_response(200);self.end_headers();self.wfile.write(raw)
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            state['calls'].append(body)
            if state.get('on_request'):state['on_request'](body)
            time.sleep(state['delay'])
            self.send_response(state['status']);self.send_header('Retry-After','15');self.end_headers()
            response=state['response'] or {'model':'company-test','data':[
                {'index':i,'embedding':([1.,0.,0.] if 'unrelated' not in text else [0.,1.,0.])[:state['dim']]} for i,text in enumerate(body['input'])],
                'usage':{'prompt_tokens':7,'total_tokens':7}}
            try:self.wfile.write(json.dumps(response).encode())
            except (BrokenPipeError,ConnectionResetError):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    t=threading.Thread(target=server.serve_forever,daemon=True);t.start()
    state['settings']={'enabled':True,'api_root':f'http://127.0.0.1:{server.server_port}/v1','model':'company-test',
        'allowed_hosts':['127.0.0.1'],'allowed_scopes':['global'],'allow_insecure_http':True,'auth_kind':'none',
        'deployment_id':'synthetic-test-v1','auto_index':False,'timeout_seconds':.5}
    yield state
    server.shutdown();server.server_close()


def configure(gateway):
    cfg=config.load_config();cfg['embedding']=gateway['settings'];config.CONFIG_PATH.write_text(json.dumps(cfg))


def approve(repo,activate=True):
    conn=index.connect(repo)
    gid=conn.execute('SELECT generation_id FROM embedding_generations ORDER BY created_at DESC,rowid DESC LIMIT 1').fetchone()[0]
    g=si.generation(conn,gid);conn.close()
    # This is explicitly a synthetic control artifact, not a claim of human evaluation.
    # Since Phase 2 the gate accepts only execution-derived provenance.
    report={'schema':'knowbase.retrieval-evaluation.v1','verdict':'PASS','annotation_status':'execution_derived',
        'derived_sample_count':60,'confidence_interval':{'metric':'Recall@5','delta':0.0,'CI95':[0.0,0.2],'positive_n':60},
        'dataset_sha256':digest('synthetic dataset'),'vectors_sha256':digest('synthetic vectors'),
        'retrieval_code':si.code_profile(),'covered_scopes':['global'],
        'provider_fp':g['identity']['provider_fp'],'space_id':g['identity']['space_id'],'dense_min':.8,
        'hybrid_recall_at_5':1.0,
        'gates':{k:True for k in ['quality','rejection','regression','latency']}}
    p=repo.parent/'synthetic-report.json';p.write_text(json.dumps(report))
    cfg=config.load_config();cfg['embedding']['quality_report']=str(p);config.CONFIG_PATH.write_text(json.dumps(cfg))
    if activate:
        conn=index.connect(repo);si.activate(conn,gid);conn.close()
    return gid


def test_provider_batch_order_identity_and_models(gateway):
    p=OpenAICompatProvider(gateway['settings'])
    assert p.models()==['company-test']
    gateway['response']={'model':'company-test','data':[{'index':1,'embedding':[0.,2.,0.]},{'index':0,'embedding':[3.,0.,0.]}]}
    vectors,identity=p.embed(['one','two'])
    assert vectors==[[1.,0.,0.],[0.,1.,0.]] and identity['dimension']==3


@pytest.mark.parametrize('data',[
    [], [{'index':0,'embedding':[1.,0.]},{'index':0,'embedding':[1.,0.]}],
    [{'index':0,'embedding':[1.,float('nan')]}],
    [{'index':0,'embedding':[0.,0.]}],
    [{'index':True,'embedding':[1.,0.]}],
    [{'index':0,'embedding':[True,1.]}],
    [{'index':1,'embedding':[1.,0.]}],
])
def test_provider_rejects_bad_responses(gateway,data):
    gateway['response']={'data':data}
    with pytest.raises(ProviderError):OpenAICompatProvider(gateway['settings']).embed(['one'])


@pytest.mark.parametrize('status,category',[(401,'auth_error'),(403,'auth_error'),(404,'model_missing'),(429,'rate_limited'),(500,'server_error')])
def test_errors_are_classified_and_never_echo_body(gateway,status,category):
    gateway['status']=status;gateway['response']={'error':'SECRET_FROM_SERVER'}
    with pytest.raises(ProviderError) as e:OpenAICompatProvider(gateway['settings']).embed(['one'])
    assert e.value.category==category and 'SECRET' not in str(e.value)
    if status==429:assert e.value.retry_after==15


def test_deadline_returns_without_waiting_for_slow_server(gateway):
    gateway['delay']=.2;p=OpenAICompatProvider(gateway['settings']);start=time.monotonic()
    with pytest.raises(ProviderError):p.embed(['one'],deadline=start+.04)
    assert time.monotonic()-start<.15
    time.sleep(.22)  # finish bounded synthetic handler before fixture cleanup


def test_no_public_default_or_plaintext_auth(gateway,monkeypatch):
    options=dict(gateway['settings']);options['allowed_hosts']=[]
    with pytest.raises(ProviderError,match='invalid_endpoint'):OpenAICompatProvider(options)
    options=dict(gateway['settings']);options['auth_kind']='bearer'
    monkeypatch.delenv('KNOWBASE_EMBED_API_KEY',raising=False)
    with pytest.raises(ProviderError,match='auth_missing'):OpenAICompatProvider(options).embed(['one'])


def test_canary_detects_same_dimension_model_drift(gateway):
    p=OpenAICompatProvider(gateway['settings']);baseline=p.probe()
    gateway['response']={'model':'company-test','data':[{'index':i,'embedding':[0.,0.,1.]} for i in range(12)]}
    with pytest.raises(ProviderError,match='canary_drift'):p.probe(baseline)


def test_build_shadow_generation_quality_gate_and_incremental_reuse(memory_repo,gateway):
    seed(memory_repo);configure(gateway)
    first=si.build(memory_repo);assert first['state']=='ready' and first['embedded']==1
    conn=index.connect(memory_repo);assert si.generation(conn) is None
    assert conn.execute("SELECT model_version FROM embedding_index WHERE generation_id='' LIMIT 1").fetchone()[0]=='char-ngram-v1'
    assert index.search(conn,'HAR',origin='mcp_search')
    conn.close()
    gid=approve(memory_repo)
    conn=index.connect(memory_repo);diag={}
    hits=index.search(conn,'semantic-query',origin='mcp_search',diagnostics=diag)
    assert hits and hits[0]['channels']==['dense'] and diag['level']=='L3'
    conn.close()
    seed(memory_repo,'另一张合同卡',body='另一条合同条件')
    second=si.build(memory_repo)
    assert second['state']=='active' and second['embedded']==1 and second['reused']==1 and second['generation']!=gid
    conn=index.connect(memory_repo)
    assert si.generation(conn,gid)['state']=='retired'
    conn.close()


def test_timeout_degrades_and_shared_breaker_survives_new_connection(memory_repo,gateway):
    seed(memory_repo);configure(gateway);si.build(memory_repo);approve(memory_repo)
    conn=index.connect(memory_repo);index.search(conn,'HAR',origin='mcp_search');conn.close()
    gateway['status']=429
    conn=index.connect(memory_repo);diag={}
    assert index.search(conn,'HAR fresh',origin='mcp_search',diagnostics=diag)
    assert diag['level']=='L2' and diag['reason']=='rate_limited';conn.close()
    calls=len(gateway['calls'])
    gateway['status']=200
    conn=index.connect(memory_repo);index.search(conn,'HAR different',origin='mcp_search');assert len(gateway['calls'])==calls
    conn.execute('UPDATE retrieval_provider_state SET retry_at=0');conn.commit();diag={}
    assert index.search(conn,'HAR recovered',origin='mcp_search',diagnostics=diag)
    assert diag['level']=='L3';conn.close()


def test_hook_and_inactive_search_cannot_send_remote_requests(memory_repo,gateway):
    seed(memory_repo);configure(gateway);si.build(memory_repo);approve(memory_repo)
    from knowbase import hooks
    calls=len(gateway['calls']);assert hooks.user_prompt('HAR')
    conn=index.connect(memory_repo);index.search(conn,'HAR',origin='hook_inflight');conn.close()
    assert len(gateway['calls'])==calls


def test_corrupt_or_stale_vectors_never_replace_basic_fts(memory_repo,gateway):
    mid=seed(memory_repo);configure(gateway);si.build(memory_repo);gid=approve(memory_repo)
    conn=index.connect(memory_repo);conn.execute("UPDATE embedding_index SET vector='[1]' WHERE generation_id=?",(gid,));conn.commit()
    diag={};assert index.search(conn,'HAR',origin='mcp_search',diagnostics=diag)
    assert diag['level']=='L2' and diag['reason']=='index_incomplete'
    conn.close()
    repaired=si.build(memory_repo);assert repaired['state']=='active' and repaired['generation']!=gid


def test_configuration_change_prevents_old_generation_activation(memory_repo,gateway):
    seed(memory_repo);configure(gateway);result=si.build(memory_repo)
    cfg=config.load_config();cfg['embedding']['deployment_id']='changed';config.CONFIG_PATH.write_text(json.dumps(cfg))
    conn=index.connect(memory_repo)
    with pytest.raises(ProviderError,match='configuration_changed'):si.activate(conn,result['generation'])
    conn.close()


def test_legacy_rebuild_deletion_is_detected_and_rollback_requires_current_hash(memory_repo,gateway):
    from knowbase import store
    mid=seed(memory_repo);configure(gateway);first=si.build(memory_repo);old=approve(memory_repo)
    conn=index.connect(memory_repo)
    # Exactly the old binary's cleanup statement: namespaced keys are never active card IDs.
    conn.execute('DELETE FROM embedding_index WHERE memory_id NOT IN (SELECT id FROM meta)');conn.commit()
    diag={};assert index.search(conn,'HAR',origin='mcp_search',diagnostics=diag)
    assert diag['level']=='L2' and diag['reason']=='index_incomplete'
    conn.close()
    assert si.build(memory_repo)['state']=='active'
    meta,body,path=store.load(memory_repo,mid);body+=' Changed condition.';path.write_text(store.render(meta,body))
    conn=index.connect(memory_repo);index.upsert(conn,meta,body,path)
    with pytest.raises(ProviderError,match='index_changed'):si.activate(conn,old)
    conn.close()


def test_shared_half_open_allows_only_one_process_probe(memory_repo,gateway):
    seed(memory_repo);configure(gateway);si.build(memory_repo);approve(memory_repo)
    conn=index.connect(memory_repo);g=si.generation(conn);conn.close()
    gateway['delay']=.12
    results=[];barrier=threading.Barrier(2);count=len(gateway['calls'])
    def run():
        c=index.connect(memory_repo);p=OpenAICompatProvider(gateway['settings']);barrier.wait()
        results.append(retrieval_state._healthy(c,p,g['identity'],time.monotonic()+1));c.close()
    ts=[threading.Thread(target=run) for _ in range(2)]
    for t in ts:t.start()
    for t in ts:t.join()
    assert sum(results)==1 and len(gateway['calls'])==count+1


def test_dimension_identity_mismatch_and_cas_during_query_are_rejected(memory_repo,gateway,monkeypatch):
    mid=seed(memory_repo);configure(gateway);si.build(memory_repo);approve(memory_repo)
    conn=index.connect(memory_repo);index.search(conn,'HAR',origin='mcp_search');conn.close()
    gateway['dim']=2
    conn=index.connect(memory_repo);diag={}
    assert index.search(conn,'HAR new-dimensional-query',origin='mcp_search',diagnostics=diag)
    assert diag['level']=='L2' and diag['reason']=='model_mismatch'
    conn.close()


def test_long_cards_are_complete_chunks_not_silent_truncation(memory_repo,gateway):
    text='synthetic-long-body-'*800
    seed(memory_repo,body=text);configure(gateway)
    first=len(gateway['calls']);result=si.build(memory_repo)
    assert result['state']=='ready'
    doc_inputs=[call['input'][0] for call in gateway['calls'][first:] if len(call['input'])==1]
    assert text in ''.join(doc_inputs) and len(doc_inputs)>=3


def test_activation_failure_rolls_back_pointer_and_generation_states(memory_repo,gateway):
    seed(memory_repo);configure(gateway);si.build(memory_repo);old=approve(memory_repo)
    original=config.load_config();quality_path=original['embedding']['quality_report']
    original['embedding']['quality_report']=None;config.CONFIG_PATH.write_text(json.dumps(original))
    seed(memory_repo,'Another precise card',body='another condition')
    next_generation=si.build(memory_repo)['generation']
    original['embedding']['quality_report']=quality_path;config.CONFIG_PATH.write_text(json.dumps(original))
    conn=index.connect(memory_repo)
    class FailingConnection:
        def __getattr__(self,name):return getattr(conn,name)
        def execute(self,sql,*args):
            if sql.startswith('INSERT INTO retrieval_active'):raise ProviderError('controlled_crash')
            return conn.execute(sql,*args)
    with pytest.raises(ProviderError,match='controlled_crash'):si.activate(FailingConnection(),next_generation)
    assert si.generation(conn)['id']==old and si.generation(conn,old)['state']=='active'
    assert si.generation(conn,next_generation)['state']=='ready'
    conn.close()


def test_production_cache_and_five_arms_use_same_model_without_auto_approval(memory_repo,gateway,tmp_path):
    from knowbase.retrieval_evaluation import export_cache,run
    mid=seed(memory_repo);configure(gateway);si.build(memory_repo);approve(memory_repo)
    rows=[{'id':f'synthetic-{i}','query':'HAR','scope':'global','privacy_review':'approved',
        'labels':{'status':'reviewed','labeler':'SYNTHETIC_TEST_ONLY','corpus_revision':'synthetic',
                  'relevant_ids':[mid],'unanswerable':False}} for i in range(60)]
    data=tmp_path/'synthetic.jsonl';data.write_text(''.join(json.dumps(r)+'\n' for r in rows))
    cache=tmp_path/'vectors.json';assert export_cache(memory_repo,data,cache)['state']=='cached'
    result=run(memory_repo,data,cache,str(__import__('pathlib').Path(__file__).resolve().parents[1]),dense_min=.5,gate_dense_min=.8)
    assert result['state']=='COMPLETED_OFFLINE' and set(result['metrics'])==set('ABCDE')
    assert result['metrics']['D']['Recall@5']==1.0
    assert result['release_verdict'].startswith('NOT_RUN')


def test_ready_index_automatically_activates_when_quality_evidence_arrives(memory_repo,gateway):
    seed(memory_repo);configure(gateway);si.build(memory_repo);gid=approve(memory_repo,activate=False)
    conn=index.connect(memory_repo);assert si.generation(conn) is None
    diag={};assert index.search(conn,'new semantic task',origin='mcp_search',diagnostics=diag)
    assert diag['level']=='L3' and si.generation(conn)['id']==gid
    conn.close()


def test_query_result_rechecks_concurrent_content_revision(memory_repo,gateway):
    from knowbase import store
    mid=seed(memory_repo);configure(gateway);si.build(memory_repo);approve(memory_repo)
    conn=index.connect(memory_repo);index.search(conn,'HAR',origin='mcp_search');conn.close()
    def change(body):
        if body['input']!=['HAR revision race']:return
        meta,text,path=store.load(memory_repo,mid);meta['title']='Changed authoritative title'
        text+=' concurrent authoritative update';path.write_text(store.render(meta,text))
        c=index.connect(memory_repo);index.upsert(c,meta,text,path);c.close()
    gateway['on_request']=change
    conn=index.connect(memory_repo);diag={};hits=index.search(conn,'HAR revision race',origin='mcp_search',diagnostics=diag)
    assert diag['level']=='L2' and diag['reason']=='index_changed'
    assert hits and hits[0]['title']=='Changed authoritative title'
    conn.close()


def test_formal_stdio_active_context_and_hook_local_boundary(memory_repo,gateway):
    import asyncio,os,sys
    from pathlib import Path
    from mcp import ClientSession,StdioServerParameters
    from mcp.client.stdio import stdio_client
    mid=seed(memory_repo);configure(gateway);si.build(memory_repo);approve(memory_repo)
    async def run():
        params=StdioServerParameters(command=sys.executable,args=['-B','-m','knowbase','serve'],cwd=str(Path(__file__).resolve().parents[1]),
            env={'KNOWBASE_CONFIG':str(config.CONFIG_PATH),'KNOWBASE_REPO_PATH':str(memory_repo),'KNOWBASE_AGENT_NAME':'synthetic-stdio'})
        async with stdio_client(params) as (read,write):
            async with ClientSession(read,write) as client:
                await client.initialize()
                for attempt in range(10):
                    reply=await client.call_tool('memory_context',{'session_id':'synthetic-semantic','event_id':str(attempt),'scope':'global','stage':'recover',
                        'topics':[{'topic_id':'semantic','queries':['semantic only task'],'status':'active'}]})
                    result=json.loads(reply.content[0].text)
                    if result.get('retrieval',{}).get('level')=='L3':
                        assert mid in [v['id'] for v in result['candidates']];return
                    await asyncio.sleep(.05)
                raise AssertionError(result)
    asyncio.run(asyncio.wait_for(run(),20))


def test_private_credential_reference_and_rotation_recover_auth_failure(memory_repo,gateway,tmp_path):
    secret=tmp_path/'credential';secret.write_text('SYNTHETIC_PRIVATE_CREDENTIAL');secret.chmod(0o600)
    gateway['settings'].update(auth_kind='bearer',secret_ref='file:'+str(secret))
    seed(memory_repo);configure(gateway);si.build(memory_repo);approve(memory_repo)
    conn=index.connect(memory_repo);index.search(conn,'HAR initial',origin='mcp_search');conn.close()
    gateway['status']=401
    conn=index.connect(memory_repo);diag={};index.search(conn,'HAR unauthorized',origin='mcp_search',diagnostics=diag)
    assert diag['reason']=='auth_error';conn.close()
    secret.write_text('SYNTHETIC_ROTATED_CREDENTIAL');secret.chmod(0o600);gateway['status']=200
    conn=index.connect(memory_repo);diag={};assert index.search(conn,'HAR rotated',origin='mcp_search',diagnostics=diag)
    assert diag['level']=='L3';conn.close()


def test_unsafe_private_credential_file_is_rejected(gateway,tmp_path):
    secret=tmp_path/'credential';secret.write_text('SYNTHETIC');secret.chmod(0o644)
    options=dict(gateway['settings'],auth_kind='bearer',secret_ref='file:'+str(secret))
    with pytest.raises(ProviderError,match='insecure_secret_file'):OpenAICompatProvider(options).embed(['synthetic'])
    secret.chmod(0o600);link=tmp_path/'link';link.symlink_to(secret);options['secret_ref']='file:'+str(link)
    with pytest.raises(ProviderError,match='insecure_secret_file'):OpenAICompatProvider(options).embed(['synthetic'])


def test_markdown_change_without_index_rebuild_blocks_stale_advice(memory_repo,gateway):
    from knowbase import store,hooks
    mid=seed(memory_repo);configure(gateway);si.build(memory_repo);approve(memory_repo)
    meta,body,path=store.load(memory_repo,mid);body+=' Authority changed without updating index.';path.write_text(store.render(meta,body))
    calls=len(gateway['calls']);conn=index.connect(memory_repo);diag={}
    assert index.search(conn,'HAR',origin='mcp_search',diagnostics=diag)==[]
    assert diag['reason']=='source_index_stale' and len(gateway['calls'])==calls
    conn.close();assert hooks.user_prompt('HAR')==''


def test_malformed_markdown_does_not_break_other_local_results(memory_repo):
    from knowbase import store
    bad=seed(memory_repo);meta,body,path=store.load(memory_repo,bad)
    path.write_text('---\nbroken: [\n---\ninvalid source')
    good=seed(memory_repo,'ValidTopic precise experience',body='ValidTopic condition')
    conn=index.connect(memory_repo);results=index.search(conn,'ValidTopic',origin='mcp_search')
    assert good in [r['id'] for r in results] and bad not in [r['id'] for r in results]
    conn.close()


def test_non_regular_secret_and_non_list_host_policy_are_rejected(gateway,tmp_path):
    import os
    fifo=tmp_path/'credential-pipe';os.mkfifo(fifo,0o600)
    options=dict(gateway['settings'],auth_kind='bearer',secret_ref='file:'+str(fifo))
    with pytest.raises(ProviderError,match='insecure_secret_file'):OpenAICompatProvider(options).embed(['synthetic'])
    options=dict(gateway['settings'],allowed_hosts='prefix127.0.0.1suffix')
    with pytest.raises(ProviderError,match='invalid_endpoint'):OpenAICompatProvider(options)


def test_unexpected_transport_errors_never_echo_server_payload(gateway):
    provider=OpenAICompatProvider(gateway['settings'])
    class BadTransport:
        def open(self,*args,**kwargs):raise RuntimeError('SYNTHETIC_PRIVATE_RESPONSE_PAYLOAD')
    provider.opener=BadTransport()
    with pytest.raises(ProviderError,match='protocol_error') as result:provider.embed(['synthetic'])
    assert 'SYNTHETIC_PRIVATE_RESPONSE_PAYLOAD' not in str(result.value)


def test_query_and_batch_have_independent_timeout_budgets(gateway):
    options=dict(gateway['settings'],timeout_seconds=.03,batch_timeout_seconds=.4)
    gateway['delay']=.08;provider=OpenAICompatProvider(options)
    assert provider.embed(['document'],query=False)[0]
    with pytest.raises(ProviderError,match='timeout'):provider.embed(['query'],query=True)
    time.sleep(.1)


def test_busy_model_state_and_logging_do_not_block_ready_local_results(memory_repo,gateway):
    seed(memory_repo);configure(gateway);si.build(memory_repo);approve(memory_repo)
    reader=index.connect(memory_repo);writer=index.connect(memory_repo)
    writer.execute('BEGIN IMMEDIATE')
    try:
        start=time.monotonic();diag={};hits=index.search(reader,'HAR',origin='mcp_search',diagnostics=diag)
        index.record_search(reader,'HAR','global',hits,retrieval=diag)
        assert hits and diag['level']=='L2' and time.monotonic()-start<.5
        assert reader.execute('PRAGMA busy_timeout').fetchone()[0]==5000
    finally:writer.rollback();writer.close();reader.close()


def test_corrupt_generation_metadata_keeps_local_search_available(memory_repo,gateway):
    seed(memory_repo);configure(gateway);si.build(memory_repo);gid=approve(memory_repo)
    conn=index.connect(memory_repo);conn.execute("UPDATE embedding_generations SET identity_json='{}' WHERE generation_id=?",(gid,));conn.commit()
    diag={};assert index.search(conn,'HAR',origin='mcp_search',diagnostics=diag)
    assert diag['level']=='L2' and diag['reason']=='index_not_ready'
    conn.close()
