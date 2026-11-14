"""Formal entry and independent counterexamples, using only synthetic isolated knowledge."""
import io
import hashlib
import json
import sqlite3

import pytest
from knowbase import config, hooks, index, store, context


def seed(repo, title="HAR 压缩排查", scope="global", body="只看 HTTP 200 的 Content-Encoding，不把 304 当成压缩证据。", tags=None):
    meta = store.new_meta("pitfall", title, scope, ["HAR"] if tags is None else tags, "agent:test:fixture")
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
    """2026-11-04 裁决后语义：未知 cwd 自动绑定派生 scope 并与 global 共享（global 卡可召回）；
    但其他项目的非 global 卡绝不能经未知项目泄漏进来。"""
    other = seed(memory_repo, "HAR 压缩排查（orders 项目）", scope="orders")
    monkeypatch.setattr(hooks.sys,'stdin',io.StringIO(json.dumps({'prompt':'HAR','cwd':str(tmp_path/'unknown')})))
    hooks.cmd_user_prompt('trae');assert other not in capsys.readouterr().out


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
                # W1-1：search_impl 新增可选 session_id（与 read/context/use 同款模式），
                # 用于把检索记入任务观测；不传时行为不变。
                assert set(schema['properties'])=={'query','type','scope','tag','limit','include_inactive','session_id'}
                result=await client.call_tool('memory_search',{'query':'HAR','scope':'global'})
                assert not result.isError
                assert mid in result.content[0].text and 'L2 本地词法检索' in result.content[0].text
    asyncio.run(asyncio.wait_for(run(),timeout=20))


def test_memory_search_requires_cmux_and_prioritizes_title_and_tags(memory_repo):
    from knowbase.server import search_impl

    noise = seed(memory_repo, "Feishu Agent Bridge", body="终端 agent 管理器和 cmuxd", tags=[])
    query = "cmux 终端 agent 管理器"
    assert "未命中" in search_impl(query, scope="global", limit=5)

    body_match = seed(memory_repo, "终端 agent 管理器对比", scope="tools",
                      body="备选方案包括 cmux。", tags=[])
    title_match = seed(memory_repo, "cmux 终端工作区配置", tags=[])
    tag_match = seed(memory_repo, "快捷键配置", body="配置工作区切换。", tags=["cmux"])
    assert title_match in search_impl(query, scope="tools", limit=1)
    top_two = search_impl(query, scope="tools", limit=2)
    assert title_match in top_two and tag_match in top_two and body_match not in top_two
    all_hits = search_impl(query, scope="tools", limit=5)
    assert body_match in all_hits and noise not in all_hits


def test_memory_search_accepts_registered_alias_and_keeps_chinese_expansion(memory_repo, tmp_path, monkeypatch):
    from knowbase.server import search_impl

    synonyms = tmp_path / "synonyms.json"
    synonyms.write_text(json.dumps({"CMUX": ["终端复用工作台"]}))
    monkeypatch.setenv("KNOWBASE_SYNONYMS", str(synonyms))
    alias = seed(memory_repo, "终端复用工作台配置", body="配置工作区切换。", tags=[])
    connection = seed(memory_repo, "企业内网连接失败的排查", body="检查客户端路由。", tags=[])
    seed(memory_repo, "终端配置", scope="tools", body="详见连接失败和启动失败文档。", tags=[])
    report = seed(memory_repo, "项目日报模板", body="写进展、风险和下一步。", tags=[])
    assert alias in search_impl("cmux 终端 agent 管理器", scope="global")
    assert connection in search_impl("内网连不上怎么办", scope="tools", limit=1)
    assert report in search_impl("日报怎么写", scope="global", limit=1)


def test_memory_search_keeps_exact_error_code(memory_repo):
    from knowbase.server import search_impl

    unauthorized = seed(memory_repo, "HTTP401 认证错误", body="检查访问令牌。", tags=[])
    forbidden = seed(memory_repo, "HTTP 403 认证错误", body="检查访问权限。", tags=[])
    hits = search_impl("HTTP 401 认证错误", scope="global")
    assert unauthorized in hits and forbidden not in hits


def test_question_retrieves_existing_card_without_matching_generic_product_name(memory_repo):
    from knowbase.server import search_impl

    relevant = seed(memory_repo, "经营指标记录", scope="ops",
                    body="本月签约转化率 25%，全国均值 30%。", tags=[])
    unrelated = seed(memory_repo, "平台部署手册", scope="ops",
                     body="平台支持审批、账号和查询。", tags=[])
    hits = search_impl("本月签约转化率是多少，跟全国差多少？", scope="ops")
    assert relevant in hits
    assert unrelated not in hits
    assert "未命中" in search_impl("平台的量子点阵额度在哪儿开通？", scope="ops")


def test_project_question_rejects_incidental_fragment_in_shared_card(memory_repo):
    from knowbase.server import search_impl

    checklist = seed(memory_repo, "华东 42 项功能清单", scope="ops",
                     body="42 项中，标准有 31 项，个性化有 11 项。", tags=[])
    unrelated = seed(memory_repo, "工具接入规范",
                     body="标准能力变更需要先提交设计提案。", tags=[])
    shared = seed(memory_repo, "华东功能清单核对记录",
                  body="华东功能清单分标准与个性化两类，共 42 项。", tags=[])

    hits = search_impl("华东功能清单一共多少项，标准能力和个性化分别多少？", scope="ops", limit=3)
    assert checklist in hits and shared in hits
    assert unrelated not in hits
