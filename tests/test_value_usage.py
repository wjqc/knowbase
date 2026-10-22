"""V4 请求计量：claude transcripts 提取、model.usage 事件、汇总 cost 口径。"""

import json

from knowbase import config, index
from knowbase.__main__ import main
from knowbase.value import aggregate, events as vevents, exchange, host_usage, outbox as voutbox

USAGE_LINE = {
    "type": "assistant", "sessionId": "sess-1", "timestamp": "2026-10-22T02:00:00.000Z",
    "uuid": "u-1", "cwd": "/private/x",
    "message": {"id": "msg_20261022_0001", "model": "claude-sonnet-4",
                "role": "assistant", "type": "message",
                "content": [{"type": "text", "text": "PRIVATE_CONVERSATION_BODY"}],
                "usage": {"input_tokens": 100, "output_tokens": 40,
                          "cache_read_input_tokens": 500,
                          "cache_creation_input_tokens": 20}},
}


def enable_events():
    cfg = json.loads(config.CONFIG_PATH.read_text())
    cfg["value"] = {"events_enabled": True}
    config.CONFIG_PATH.write_text(json.dumps(cfg))


def make_transcript(root, name="sess-1.jsonl", lines=None):
    d = root / "proj"
    d.mkdir(parents=True)
    path = d / name
    path.write_text("\n".join(json.dumps(line) for line in (lines or [USAGE_LINE])), encoding="utf-8")
    return root


def outbox_events(repo, event_type=None):
    conn = index.connect(repo)
    try:
        return [e for e in voutbox.list_events(conn)
                if event_type is None or e["event_type"] == event_type]
    finally:
        conn.close()


def test_claude_usage_extract_emits_idempotent_and_content_free(memory_repo, tmp_path, capsys):
    enable_events()
    make_transcript(tmp_path)
    assert main(["value", "usage", "--source", "claude", "--path", str(tmp_path)]) == 0
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary["emitted"] == 1
    events = outbox_events(memory_repo, "model.usage")
    assert len(events) == 1
    payload = json.loads(events[0]["payload"])
    assert payload["payload"]["input_tokens"] == 100
    assert payload["payload"]["cache_read_tokens"] == 500  # 分列，不并入输入
    assert payload["payload"]["task_mapping"] == "session"  # 会话级归属，不冒充任务实测
    assert payload["occurred_at"].startswith("2026-10-22T02:00")
    assert "PRIVATE_CONVERSATION_BODY" not in json.dumps(payload)  # 不采正文
    # 幂等：重跑只报 duplicate
    assert main(["value", "usage", "--source", "claude", "--path", str(tmp_path)]) == 0
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary["emitted"] == 0 and summary["duplicate"] == 1
    assert len(outbox_events(memory_repo, "model.usage")) == 1


def test_usage_payload_whitelist_and_numeric_guard(memory_repo, tmp_path):
    enable_events()
    line = dict(USAGE_LINE, message=dict(USAGE_LINE["message"], usage={
        "input_tokens": "not-a-number"}))
    make_transcript(tmp_path, lines=[line])
    from knowbase.__main__ import main as m
    assert m(["value", "usage", "--source", "claude", "--path", str(tmp_path)]) == 0
    # 数值非法 → skipped，不入库不崩
    conn = index.connect(memory_repo)
    assert conn.execute("SELECT COUNT(*) FROM value_outbox").fetchone()[0] == 0
    conn.close()
    with_wrap = {"usage_source": "claude_transcripts", "session_id": "s",
                 "extra_field": "x"}
    try:
        vevents.build_event("model.usage", with_wrap, host="claude-code")
        raised = False
    except ValueError:
        raised = True
    assert raised


def test_zcode_trae_usage_explicitly_unsupported(memory_repo, capsys):
    for source in ("zcode", "trae"):
        assert main(["value", "usage", "--source", source]) == 0
        out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert out["status"] == "not_supported" and out["source"] == source


def test_repeated_request_merges_partial_usage_without_double_count(tmp_path):
    from knowbase.value.host_usage.claude import extract_records
    first=json.loads(json.dumps(USAGE_LINE));last=json.loads(json.dumps(USAGE_LINE))
    first['message']['usage']['output_tokens']=0
    last['message']['usage']['output_tokens']=240
    make_transcript(tmp_path,lines=[first,last,last])
    records=list(extract_records(str(tmp_path)))
    assert len(records)==1 and records[0]['output_tokens']==240
    assert records[0]['input_tokens']==100


def test_token_values_reject_boolean_negative_and_nonfinite():
    import pytest
    for value in [True,-1,float('nan'),float('inf')]:
        with pytest.raises(ValueError):
            vevents.build_event('model.usage',{'usage_source':'fixture','session_id':'s',
                                            'input_tokens':value},host='fixture')


def test_aggregate_cost_and_reconciliation(memory_repo, tmp_path):
    enable_events()
    second = dict(USAGE_LINE)
    second = json.loads(json.dumps(USAGE_LINE))
    second["message"] = dict(USAGE_LINE["message"], id="msg_2", usage={
        "input_tokens": 10, "output_tokens": 5,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0})
    second["uuid"] = "u-2"
    make_transcript(tmp_path, lines=[USAGE_LINE, second])
    assert main(["value", "usage", "--source", "claude", "--path", str(tmp_path)]) == 0
    pkg = tmp_path / "pkg.json"
    exchange.export_events(memory_repo, pkg)
    agg = aggregate.connect(tmp_path / "agg.db")
    exchange.import_package(agg, pkg, team_id="t")
    snap = aggregate.snapshot(agg, team_id="t")
    cost = snap["cost"]
    assert cost["available"] is True
    model = cost["by_model"]["claude-sonnet-4"]
    # 对账：快照汇总 = transcript 原始值之和
    assert model["requests"] == 2
    assert model["input_tokens"] == 110 and model["output_tokens"] == 45
    assert model["cache_read_tokens"] == 500 and model["cache_creation_tokens"] == 20
    assert cost["usage_sources"] == ["claude_transcripts"]
    assert cost["attribution"] == "session"
    agg.close()


def test_cost_missing_fields_are_not_zero(memory_repo, tmp_path):
    enable_events()
    partial = json.loads(json.dumps(USAGE_LINE))
    partial["message"] = dict(USAGE_LINE["message"], id="msg_partial",
                              usage={"input_tokens": 7})
    make_transcript(tmp_path, lines=[partial])  # 只有部分字段的记录
    assert main(["value", "usage", "--source", "claude", "--path", str(tmp_path)]) == 0
    pkg = tmp_path / "pkg.json"
    exchange.export_events(memory_repo, pkg)
    agg = aggregate.connect(tmp_path / "agg.db")
    exchange.import_package(agg, pkg, team_id="t")
    snap = aggregate.snapshot(agg, team_id="t")
    slot = snap["cost"]["by_model"]["claude-sonnet-4"]
    assert slot["input_tokens"] == 7 and slot["requests"] == 1
    assert slot["output_tokens"] is None  # 未采集 ≠ 0
    assert slot["fields_missing_on_requests"] == 1
    assert set(slot["missing_fields"]) == {"output_tokens", "cache_read_tokens",
                                           "cache_creation_tokens"}
    # cost 进入看板数据（不再被白名单裁剪）
    from knowbase.value.report import load_team_snapshot
    snap_path = tmp_path / "snap.json"
    snap_path.write_text(json.dumps(snap))
    loaded = load_team_snapshot(snap_path)
    assert "cost" in loaded and loaded["cost"]["available"] is True
    agg.close()
