import json
import sqlite3

from knowbase import config, dashboard, index
from knowbase.value import overview


def test_calls_do_not_add_legacy_and_knowledge_states_are_exclusive(memory_repo):
    conn=index.connect(memory_repo)
    for n in range(2):
        conn.execute("INSERT INTO command_calls(call_id,ts,transport,command,status,duration_ms) VALUES(?,?,?,?,?,?)",
                     (str(n),"2026-10-22","mcp","memory_update","ok",0))
    for _ in range(3):
        conn.execute("INSERT INTO usage_log(tool,detail) VALUES('update','legacy')")
    for mid,confidence,status,staging in [('1','verified','active',0),('2','once','active',0),
                                          ('3','staging','active',1),('4','staging','stale',1)]:
        conn.execute("INSERT INTO meta(id,confidence,status,staging) VALUES(?,?,?,?)",(mid,confidence,status,staging))
    index.record_search(conn,"PRIVATE_QUERY", "orders", [{'id':'1','title':'PRIVATE_TITLE',
        'score':1,'confidence':'verified','status':'active'}],tool="search")
    index.record_search(conn,"PRIVATE_QUERY_2", "orders", [],tool="auto_search")
    conn.execute("INSERT INTO usage_log(tool,detail) VALUES('search','old missing detail')")
    result=overview.build(conn)
    assert result['calls']==2 and result['legacy_calls']==6 and result['updates']==3
    assert '历史 usage_log' in result['updates_note']
    assert result['knowledge_total']==4 and sum(result['knowledge'].values())==4
    assert result['knowledge']['stale']==1 and result['knowledge']['staging']==1
    assert result['hit_searches']==1 and result['known_searches']==2 and result['missing_searches']==1
    assert result['hit_rate']==50
    assert 'PRIVATE_QUERY' not in json.dumps(result)
    conn.close()


def test_missing_tokens_and_savings_are_unknown_not_zero():
    c=sqlite3.connect(':memory:')
    missing=overview.build(c)
    assert missing['usage']['total'] is None and missing['usage']['saved_tokens'] is None
    measured=overview.build(c,local_usage=[{'input_tokens':100,'output_tokens':20,
        'cache_read_tokens':1000,'cache_creation_tokens':0}])
    assert measured['usage']['total']==120 and measured['usage']['fields']['cache_read_tokens']==1000
    assert measured['usage']['saved_tokens'] is None
    partial=overview.build(c,local_usage=[{'input_tokens':100,'output_tokens':20},{'input_tokens':10}])
    assert partial['usage']['total'] is None
    c.close()


def test_dashboard_generation_does_not_migrate_or_write_database(tmp_path,monkeypatch):
    repo=tmp_path/'old-memory';repo.mkdir();p=repo/'memory.db'
    c=sqlite3.connect(p);c.execute('CREATE TABLE old_marker(value TEXT)');c.commit();c.close()
    original=p.read_bytes()
    monkeypatch.setattr(config,'load_config',lambda:{'dashboard':{'host_usage_source':'none'}})
    out=tmp_path/'cards.html';dashboard.generate(repo,out)
    assert p.read_bytes()==original
    html=out.read_text();assert 'Token 净节省' in html and '<table' not in html


def test_partial_team_cost_does_not_report_complete_total():
    result=overview.team({'cost':{'by_model':{'m':{'requests':2,'input_tokens':10,
        'output_tokens':5,'fields_missing_on_requests':1}}}})
    assert result['usage']['total'] is None
    assert result['calls'] is None and result['knowledge_total'] is None
