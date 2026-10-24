"""Formal entry and independent counterexamples, using only synthetic isolated knowledge."""
import io
import hashlib
import json
import sqlite3

import pytest
from knowbase import config, hooks, index, store, context


def seed(repo, title="HAR 压缩排查", scope="global", body="只看 HTTP 200 的 Content-Encoding，不把 304 当成压缩证据。"):
    meta = store.new_meta("pitfall", title, scope, ["HAR"], "agent:test:fixture")
    meta["id"] = "P-" + hashlib.sha256((title+scope).encode()).hexdigest()[:12]
    meta["confidence"] = "verified"
    meta.pop("schema", None)
    path = store.mem_path(repo, "pitfall", meta["id"])
    path.write_text(store.render(meta, body))
    conn = index.connect(repo); index.upsert(conn, meta, body, path);conn.close()
    return meta["id"]


def test_local_search_never_reads_legacy_dense_even_if_enabled(memory_repo, monkeypatch):
    mid = seed(memory_repo)
    monkeypatch.setattr(index, "_dense_ids", lambda *a, **k: pytest.fail("dense must never run"))
    conn = index.connect(memory_repo)
    conn.execute("UPDATE embedding_index SET model_version='fake-real',vector='[999999]'");conn.commit()
    diag = {};hits = index.search(conn, "HAR", diagnostics=diag)
    assert hits[0]["id"] == mid and hits[0]["channels"] == ["lexical"]
    assert diag["level"] == "L2" and diag["remote_calls"] == 0
    assert index.search(conn, "unanswerableXYZ") == []
    conn.close()


def test_l1_disables_alias_recall_and_l2_enables_it(memory_repo, monkeypatch):
    seed(memory_repo, "自定义连接失败方法", body="企业VPN连接失败的隔离经验。")
    monkeypatch.setattr(index, "_load_aliases", lambda: {"虚构同义词": ["连接失败"]})
    cfg = config.load_config();cfg["retrieval"]["mode"] = "raw_fts";config.CONFIG_PATH.write_text(json.dumps(cfg))
    conn = index.connect(memory_repo);diag={}
    assert index.search(conn, "虚构同义词", diagnostics=diag) == [] and diag["level"] == "L1"
    cfg["retrieval"]["mode"] = "auto";config.CONFIG_PATH.write_text(json.dumps(cfg))
    assert index.search(conn, "虚构同义词")
    conn.close()


def test_formal_hook_keeps_har_and_respects_exact_total_budget(memory_repo, monkeypatch, capsys):
    mid = seed(memory_repo)
    monkeypatch.setattr(hooks.sys, "stdin", io.StringIO('{"prompt":"HAR"}'))
    hooks.cmd_user_prompt("claude")
    text = capsys.readouterr().out.strip();assert mid in text and len(text) <= 900
    cfg=config.load_config();cfg['hooks']['inject_max_chars']=1;config.CONFIG_PATH.write_text(json.dumps(cfg))
    assert hooks.user_prompt('HAR') == ''
    conn=index.connect(memory_repo)
    last=json.loads(conn.execute("SELECT detail FROM usage_log WHERE tool='auto_search' ORDER BY rowid DESC LIMIT 1").fetchone()[0])
    assert last['hits']==[] and last['retrieval']['delivered_chars']==0
    conn.close()


def test_formal_hook_uses_stdin_project_instead_of_global_fallback(memory_repo, tmp_path, monkeypatch, capsys):
    mid = seed(memory_repo)
    monkeypatch.setattr(hooks.sys,'stdin',io.StringIO(json.dumps({'prompt':'HAR','cwd':str(tmp_path/'unknown')})))
    hooks.cmd_user_prompt('trae');assert mid not in capsys.readouterr().out


def test_error_code_gate_rejects_similar_code_and_long_bypass(memory_repo):
    seed(memory_repo, "HTTP 403 处理", body="此卡只处理 HTTP 403，HAR示例。")
    assert hooks.user_prompt("HTTP 401") == ""
    cfg=config.load_config();cfg['hooks']['query_hygiene']=False;config.CONFIG_PATH.write_text(json.dumps(cfg))
    assert hooks.user_prompt('HAR ' * 200)==''
    assert hooks.user_prompt('翻译这段话 HAR')==''


def test_dictionary_invalid_shape_cannot_break_base_search(memory_repo, tmp_path, monkeypatch):
    seed(memory_repo);p=tmp_path/'synonyms.json';p.write_text('[]')
    monkeypatch.setenv('KNOWBASE_SYNONYMS',str(p))
    conn=index.connect(memory_repo);assert index.search(conn,'HAR');conn.close()


def test_context_policy_change_invalidates_coverage(memory_repo):
    seed(memory_repo, '合同审核约束', scope='orders',body='合同审批前需先检查订单状态。')
    def run(event):return context.execute(memory_repo,session_id='s',event_id=event,scope='orders',topics=[{'topic_id':'t','queries':['合同']}])
    assert run('a')['budget_used']['queries']==1
    assert run('b')['retrieval_status']=='skipped_cache'
    cfg=config.load_config();cfg['retrieval']['mode']='raw_fts';config.CONFIG_PATH.write_text(json.dumps(cfg))
    assert run('c')['budget_used']['queries']==1


def test_user_prompt_observation_does_not_replay_inherited_topics(memory_repo):
    seed(memory_repo)
    context.execute(memory_repo,session_id='s',event_id='a',scope='global',topics=[{'topic_id':'old','queries':['HAR']}])
    out=context.execute(memory_repo,session_id='s',event_id='b',scope='global',automatic=True,
        observation={'hook_event':'user-prompt'},stage='observe')
    assert not out['candidates'] and out['budget_used']['queries']==0


def test_real_stdio_mcp_search_keeps_signature_and_reports_local_mode(memory_repo):
    import asyncio,os,sys
    from pathlib import Path
    from mcp import ClientSession,StdioServerParameters
    from mcp.client.stdio import stdio_client
    mid=seed(memory_repo)
    async def run():
        params=StdioServerParameters(command=sys.executable,args=['-B','-m','knowbase','serve'],
            cwd=str(Path(__file__).resolve().parents[1]),env={
                'KNOWBASE_CONFIG':str(config.CONFIG_PATH),'KNOWBASE_REPO_PATH':str(memory_repo),'KNOWBASE_AGENT_NAME':'test'})
        async with stdio_client(params) as (read,write):
            async with ClientSession(read,write) as client:
                await client.initialize()
                schema=next(t.inputSchema for t in (await client.list_tools()).tools if t.name=='memory_search')
                assert set(schema['properties'])=={'query','type','scope','tag','limit','include_inactive'}
                result=await client.call_tool('memory_search',{'query':'HAR','scope':'global'})
                assert not result.isError
                assert mid in result.content[0].text and 'L2 本地词法检索' in result.content[0].text
    asyncio.run(asyncio.wait_for(run(),timeout=20))
