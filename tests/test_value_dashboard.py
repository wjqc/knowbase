"""V3 dashboard 增强：本机价值分区、采集健康、团队快照分区、确定性报告。"""

import json

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


def test_dashboard_local_sections_distinguish_zero_unknown_disabled(memory_repo, tmp_path):
    out = tmp_path / "dash.html"
    assert main(["dashboard", "--output", str(out)]) == 0  # 原命令兼容
    html = out.read_text()
    assert "价值与使用" in html and "采集健康（本机）" in html
    assert 'id="v-time"' in html and 'id="v-host"' in html and 'id="v-scope"' in html
    assert "团队快照" in html and "未提供团队快照：仅展示本机数据" in html
    data = json.loads(html.split('<script type="application/json" id="data">')[1]
                      .split("</script>")[0].replace("\\u003c", "<").replace("\\u003e", ">")
                      .replace("\\u0026", "&"))
    personal = data["value"]["personal"]
    assert personal["events_enabled"] is False and personal["outbox"]["total"] == 0
    assert personal["identity"]["member_label"] == ""  # 未确认成员：不冒充

    enable_events()
    produce_events(memory_repo)
    assert main(["dashboard", "--output", str(out)]) == 0
    html = out.read_text()
    data = json.loads(html.split('<script type="application/json" id="data">')[1]
                      .split("</script>")[0].replace("\\u003c", "<").replace("\\u003e", ">")
                      .replace("\\u0026", "&"))
    personal = data["value"]["personal"]
    assert personal["events_enabled"] is True
    assert personal["outbox"]["by_type"].get("knowledge.created") == 1
    assert personal["outbox"]["by_type"].get("knowledge.used") == 1
    assert personal["uses"]["adopted_tasks"] == 1
    assert personal["identity"]["installation_id"].startswith("INST-")


def _telemetry_rows(repo):
    conn = index.connect(repo)
    try:
        return conn.execute("SELECT COUNT(*) FROM command_calls").fetchone()[0]
    finally:
        conn.close()


def test_team_snapshot_validation_and_rendering(memory_repo, tmp_path):
    enable_events()
    produce_events(memory_repo)
    # 生成真实团队快照
    from knowbase.value import exchange
    pkg = tmp_path / "pkg.json"
    exchange.export_events(memory_repo, pkg)
    agg_db = tmp_path / "agg.db"
    agg = aggregate.connect(agg_db)
    exchange.import_package(agg, pkg, team_id="team-1")
    snap = aggregate.snapshot(agg, team_id="team-1")
    agg.close()
    snap_path = tmp_path / "snap.json"
    snap_path.write_text(json.dumps(snap, ensure_ascii=False))

    out = tmp_path / "dash-team.html"
    assert main(["dashboard", "--output", str(out), "--team-data", str(snap_path)]) == 0
    data = json.loads(out.read_text().split('<script type="application/json" id="data">')[1]
                      .split("</script>")[0].replace("\\u003c", "<").replace("\\u003e", ">")
                      .replace("\\u0026", "&"))
    team = data["value"]["team"]
    assert team["team_id"] == "team-1" and team["counts"]["events_total"] >= 2
    # 团队与个人分区并存且范围分开标注
    assert data["value"]["personal"]["schema"] == value_report.SUMMARY_SCHEMA
    # 未知顶层键被裁剪，不进入页面数据
    dirty = json.loads(snap_path.read_text())
    dirty["evil_key"] = "<b>inject</b>"
    dirty_path = tmp_path / "snap-dirty.json"
    dirty_path.write_text(json.dumps(dirty))
    loaded = value_report.load_team_snapshot(dirty_path)
    assert "evil_key" not in loaded

    # schema 不符拒绝，rc=2，不生成
    bad = tmp_path / "snap-bad.json"
    bad.write_text(json.dumps({"schema": "knowbase.value.snapshot.v999"}))
    assert main(["dashboard", "--output", str(tmp_path / "no.html"), "--team-data", str(bad)]) == 2
    assert not (tmp_path / "no.html").exists()


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
    enable_events()
    produce_events(memory_repo)
    # 制造含私密查询的本机搜索历史
    conn = index.connect(memory_repo)
    index.record_search(conn, "PRIVATE_QUERY_7C2A 客户名单", "orders", [], tool="auto_search")
    conn.commit()
    conn.close()
    snap = {"schema": "knowbase.value.snapshot.v1", "team_id": "t", "window": {"since": "", "until": ""},
            "counts": {"events_total": 1, "events_by_type": {"knowledge.used": 1}},
            "members": {"observed": 1, "effective_consumers": 1, "roster_size": None,
                        "coverage": None, "coverage_known": False},
            "tasks": {"delivered": 1, "adopted": 1, "adoption_rate": 1.0},
            "feedback": {}, "cross_member_reuse": {"available": False, "reason": "x"},
            "sources": [], "task_details": [], "task_details_truncated": False,
            "limitations": [], "generated_at": "2026-10-22T00:00:00+00:00",
            "data_watermark": {"max_event_time": "", "max_import_time": ""}}
    snap_path = tmp_path / "snap.json"
    snap_path.write_text(json.dumps(snap))
    team_out = tmp_path / "team.html"
    assert main(["dashboard", "--output", str(team_out), "--team-data", str(snap_path)]) == 0
    team_html = team_out.read_text()
    assert "PRIVATE_QUERY_7C2A" not in team_html
    assert "团队分享输出不嵌入本机搜索原文" in team_html
    # 本地模式（不带 --team-data）保留搜索原文
    local_out = tmp_path / "local.html"
    assert main(["dashboard", "--output", str(local_out)]) == 0
    assert "PRIVATE_QUERY_7C2A" in local_out.read_text()


def test_v3_value_controls_trend_and_evidence_timeline(memory_repo, tmp_path):
    enable_events()
    produce_events(memory_repo)
    # 团队快照（含复用证据链）
    from knowbase.value import exchange
    pkg = tmp_path / "pkg.json"
    exchange.export_events(memory_repo, pkg)
    agg = aggregate.connect(tmp_path / "agg.db")
    exchange.import_package(agg, pkg, team_id="t")
    snap = aggregate.snapshot(agg, team_id="t")
    agg.close()
    snap_path = tmp_path / "snap.json"
    snap_path.write_text(json.dumps(snap))
    out = tmp_path / "dash.html"
    assert main(["dashboard", "--output", str(out), "--team-data", str(snap_path)]) == 0
    html = out.read_text()
    # 联动筛选控件存在
    for control in ('id="v-time"', 'id="v-host"', 'id="v-scope"'):
        assert control in html
    data = json.loads(html.split('<script type="application/json" id="data">')[1]
                      .split("</script>")[0].replace("\\u003c", "<").replace("\\u003e", ">")
                      .replace("\\u0026", "&"))
    personal = data["value"]["personal"]
    # 周趋势序列（内核计算）+ 最近事件明细（有界）
    assert personal["weekly"] and personal["weekly"][0]["events"] >= 1
    assert personal["recent_events"]["events"] and not personal["recent_events"]["truncated"]
    kinds = {e["event_type"] for e in personal["recent_events"]["events"]}
    assert "knowledge.used" in kinds
    # 团队快照带周序列与证据链结构
    team = data["value"]["team"]
    assert team["weekly"] and "week" in team["weekly"][0]
    assert isinstance(team["reuse_evidence"], list)
    assert team["reuse_evidence_truncated"] is False
    # 单成员本机自采自用：无跨成员任务 → 证据链为空但不缺失字段
    assert team["reuse_evidence"] == []


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
