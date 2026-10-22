"""V3 dashboard 增强：本机价值分区、采集健康、团队快照分区、确定性报告。"""

import json
import pathlib

from knowbase import config, index
from knowbase.__main__ import main
from knowbase.value import aggregate, report as value_report


def enable_events():
    cfg = json.loads(config.CONFIG_PATH.read_text())
    cfg["value"] = {"events_enabled": True}
    config.CONFIG_PATH.write_text(json.dumps(cfg))


def full_card():
    return ("## 结论\n已结算订单不能撤回。\n## 解决的问题\n防重复撤回。\n## 适用条件\n订单已结算。\n"
            "## 不适用条件\n草稿订单。\n## 可执行动作\n保存前拒绝撤回。\n## 关键证据\nPRD §4。\n"
            "## 验证情况\n测试复现拒绝。\n## 未知与待确认\n无。\n")


def produce_events(repo):
    import re
    from knowbase.server import context_impl, save_impl, use_impl
    saved = save_impl("pitfall", "订单撤回规则<script>", full_card(), scope="orders")
    mid = re.search(r"\b([PB]-[0-9a-f]+)\b", saved).group(1)
    conn = index.connect(repo)
    revision = conn.execute("SELECT content_revision FROM entity_heads WHERE entity_id=?",
                            (mid,)).fetchone()[0]
    conn.close()
    card = json.loads(context_impl(session_id="s1", event_id="retrieval", scope="orders",
                                   task_id="t1", task_revision="v1", current_goal="订单",
                                   host_profile="h1"))["candidates"][0]
    use = json.loads(use_impl(session_id="s1", event_id="use-1", context_event_id="retrieval",
                              id=card["id"], content_revision=card["content_revision"],
                              state="adopted", host_profile="h1"))
    return mid, revision, use


def test_dashboard_local_sections_distinguish_zero_unknown_disabled(memory_repo, tmp_path, monkeypatch):
    from knowbase import dashboard
    monkeypatch.setattr(config, "load_config", lambda: {"dashboard": {"host_usage_source": "none"}})
    out=tmp_path / "dash.html"
    dashboard.generate(memory_repo,out)
    html=out.read_text()
    data=json.loads(html.split('<script type="application/json" id="data">')[1].split('</script>')[0])
    local=data["overview"]["local"]
    assert local["knowledge_total"]==0 and local["calls"]==0
    assert local["usage"]["total"] is None and local["usage"]["saved_tokens"] is None
    assert "Token 净节省" in html and "知识状态" in html and "自动演进" in html
    assert '<table' not in html and 'data["items"]' not in html


def _telemetry_rows(repo):
    conn = index.connect(repo)
    try:
        return conn.execute("SELECT COUNT(*) FROM command_calls").fetchone()[0]
    finally:
        conn.close()


def test_team_snapshot_validation_and_rendering(memory_repo, tmp_path):
    enable_events(); produce_events(memory_repo)
    from knowbase.value import exchange
    pkg=tmp_path / "pkg.json"; exchange.export_events(memory_repo,pkg)
    conn=aggregate.connect(tmp_path / "agg.db"); exchange.import_package(conn,pkg,team_id="team-1")
    snap=aggregate.snapshot(conn,team_id="team-1"); conn.close()
    sp=tmp_path / "snap.json"; sp.write_text(json.dumps(snap));out=tmp_path / "dash.html"
    assert main(["dashboard","--output",str(out),"--team-data",str(sp)])==0
    data=json.loads(out.read_text().split('<script type="application/json" id="data">')[1].split('</script>')[0])
    team=data["overview"]["team"]
    assert team["scope"]=="团队" and team["calls"] is None and team["knowledge_total"] is None
    assert team["events"]==snap["counts"]["events_total"]
    sp.write_text('{"schema":"unsupported"}')
    assert main(["dashboard","--output",str(tmp_path / "bad.html"),"--team-data",str(sp)])==2
    assert not (tmp_path / "bad.html").exists()


def test_report_deterministic_markdown(memory_repo, tmp_path):
    enable_events()
    produce_events(memory_repo)
    out = tmp_path / "report.md"
    assert main(["value", "report", "--output", str(out), "--personal"]) == 0
    text1 = out.read_text()
    assert "本机（事件窗口口径）" in text1 and "已启用" in text1
    assert "knowledge.created 1" in text1
    assert "不相加" in text1
    # 同一数据二次生成：除生成时间行外逐行一致（确定性）
    assert main(["value", "report", "--output", str(out), "--personal"]) == 0
    text2 = out.read_text()
    strip = lambda t: [line for line in t.splitlines() if not line.startswith("生成时间")]
    assert strip(text1) == strip(text2)
    # 无内容时拒绝
    assert main(["value", "report", "--output", str(tmp_path / "empty.md")]) == 2


def test_team_output_excludes_local_search_history(memory_repo, tmp_path):
    from knowbase import dashboard
    conn=index.connect(memory_repo)
    index.record_search(conn,"PRIVATE_QUERY_CUSTOMER", "orders", [], tool="auto_search")
    conn.commit();conn.close()
    for snap in [None,{"counts":{},"members":{},"cost":{}}]:
        out=tmp_path / ("local.html" if snap is None else "team.html")
        dashboard.generate(memory_repo,out,team_snapshot=snap)
        assert "PRIVATE_QUERY_CUSTOMER" not in out.read_text()


def test_card_overview_retains_counts_without_task_detail(memory_repo, tmp_path):
    from knowbase import dashboard
    enable_events();produce_events(memory_repo)
    out=tmp_path / "cards.html";dashboard.generate(memory_repo,out)
    html=out.read_text();data=json.loads(html.split('<script type="application/json" id="data">')[1].split('</script>')[0])
    assert data["overview"]["local"]["knowledge_total"]==1
    assert 'id="scope"' in html and 'id="metrics"' in html
    assert "recent_events" not in data and "value" not in data and "history" not in data
    assert "<table" not in html


def test_snapshot_deep_sanitize_strips_nested_smuggling():
    """嵌套位置夹带的正文（task_details[].raw_query 等）不进看板数据。"""
    from knowbase.value.report import load_team_snapshot
    import tempfile
    dirty = {"schema": "knowbase.value.snapshot.v1", "team_id": "t",
             "window": {"since": "", "until": ""},
             "data_watermark": {"max_event_time": "", "max_import_time": ""},
             "counts": {"events_total": 1, "events_by_type": {"knowledge.used": 1}},
             "members": {"observed": 1}, "tasks": {}, "feedback": {},
             "cross_member_reuse": {"available": False, "note": "x"},
             "cost": {"available": False,
                      "by_model": {"m": {"requests": 1, "input_tokens": None}}},
             "weekly": [], "sources": [],
             "task_details": [{"task_id": "t1", "raw_query": "PRIVATE_NESTED",
                               "prompt": {"text": "PRIVATE"}}],
             "reuse_evidence": [{"task_id": "t1", "consumer": "B",
                                 "secret": "PRIVATE",
                                 "cards": [{"memory_id": "P", "raw": "PRIVATE"}],
                                 "timeline": [{"type": "used", "at": "x", "actor": "B",
                                               "note": "PRIVATE"}]}],
             "limitations": ["ok", {"nested": "PRIVATE"}],
             "generated_at": "2026-10-22T00:00:00+00:00"}
    import json as _json
    import pathlib
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "snap.json"
        p.write_text(_json.dumps(dirty))
        loaded = load_team_snapshot(p)
    assert "PRIVATE" not in _json.dumps(loaded)
    assert loaded["task_details"] == [{"task_id": "t1"}]
    assert loaded["reuse_evidence"][0]["cards"] == [{"memory_id": "P"}]
    assert loaded["reuse_evidence"][0]["timeline"] == [{"type": "used", "at": "x", "actor": "B"}]
    assert loaded["limitations"] == ["ok"]
    assert loaded["cost"]["by_model"]["m"]["input_tokens"] is None


def test_team_weekly_by_host_in_snapshot_and_sanitized():
    from knowbase.value import aggregate as agg_mod
    import tempfile
    # 汇总库直接造两宿主的周数据，验证 weekly_by_host 生成与深层脱敏
    with tempfile.TemporaryDirectory() as d:
        db = pathlib.Path(d) / "agg.db"
        conn = agg_mod.connect(db)
        conn.execute("INSERT INTO members(member_id,display_label,confirmed_at,note) "
                     "VALUES('M1','A','','')")
        conn.commit()
        for host, week_date in (("zcode", "2026-10-20T00:00:00+00:00"),
                                ("claude-code", "2026-09-15T00:00:00+00:00")):
            event = {"event_id": f"VE-{host}", "schema_version": "knowbase.value.events.v1",
                     "event_type": "knowledge.used", "host": host,
                     "occurred_at": week_date, "session_id": "s",
                     "payload": {"memory_id": "P", "content_revision": "r",
                                 "context_event_id": "c", "use_id": "u",
                                 "state": "adopted", "task_id": "t", "scope": "o"}}
            import hashlib
            payload_json = json.dumps(event, sort_keys=True, ensure_ascii=False,
                                       separators=(",", ":"))
            conn.execute(
                "INSERT INTO agg_events(team_id,event_id,event_type,occurred_at,session_id,"
                "host,payload_json,payload_hash,imported_at) VALUES('t',?,?,?,?,?,?,?,?)",
                (event["event_id"], "knowledge.used", week_date, "s", host,
                 json.dumps(event), hashlib.sha256(payload_json.encode()).hexdigest(),
                 week_date))
        conn.commit()
        snap = agg_mod.snapshot(conn, team_id="t")
        conn.close()
        assert set(snap["weekly_by_host"]) == {"zcode", "claude-code"}
        assert snap["hosts"] == ["claude-code", "zcode"]
        # 深层脱敏：weekly_by_host 只保留周标量字段
        snap_path = pathlib.Path(d) / "snap.json"
        snap["weekly_by_host"]["zcode"][0]["raw_query"] = "PRIVATE"
        snap_path.write_text(json.dumps(snap))
        from knowbase.value.report import load_team_snapshot
        loaded = load_team_snapshot(snap_path)
        assert "PRIVATE" not in json.dumps(loaded)
        assert loaded["weekly_by_host"]["zcode"][0].get("events") == 1


def test_team_cards_do_not_embed_host_or_task_details(memory_repo, tmp_path):
    from knowbase import dashboard
    snap={"counts":{"events_total":2,"events_by_type":{"knowledge.revised":1}},
          "hosts":["PRIVATE_HOST"],"task_details":[{"task_id":"PRIVATE_TASK"}],
          "cost":{"available":False},"members":{},"cross_member_reuse":{}}
    out=tmp_path / "team.html";dashboard.generate(memory_repo,out,team_snapshot=snap)
    html=out.read_text();assert "PRIVATE_HOST" not in html and "PRIVATE_TASK" not in html
    data=json.loads(html.split('<script type="application/json" id="data">')[1].split('</script>')[0])
    assert data["overview"]["team"]["updates"]==1
