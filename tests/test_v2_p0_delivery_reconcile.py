"""合并复核：P3 v2 白名单 vs P0 递送口径（四批合并时补的缺口）。

背景：P0 唯一实际发射的递送类型是 knowledge.delivered（context.execute），
acceptance 全链路亦只认该类型；但 P3 的 v2 白名单起初只有
retrieval.delivered（无任何发射点），导致 export_events 按
event_type in V2_FIELDS 过滤时会把 P0 的递送事件静默排除在 v2 包外
（甚至不进 rejected），递送证据走 v2 通道时永远到不了汇总库。
修复：v2 白名单复用 P0 的 knowledge.delivered 主线口径。
本文件锁定该复核结论，禁止回退。
"""

import json

from knowbase import index
from knowbase.value import aggregate, events, events_v2, exchange_v2


def _p0_delivered_event(event_id="E-DEL-1"):
    """P0 context.execute 实际发射的口径（见 context.py 递送循环）。"""
    return {"event_id": event_id, "event_type": "knowledge.delivered",
            "occurred_at": "2026-10-24T10:00:00+00:00", "team_id": "default",
            "member_id": "m1", "installation_id": "inst-1",
            "schema_version": "knowbase.value.events.v1",
            "payload": {"delivery_id": "DEL-1", "memory_id": "M1",
                        "content_revision": "r1", "task_id": "T1",
                        "search_id": "S1", "context_event_id": "CTX-1",
                        "scope": "test", "rank": 1}}


def _insert_outbox_row(memory_repo, event):
    conn = index.connect(memory_repo)
    try:
        conn.execute(
            "INSERT INTO value_outbox(event_id,schema_version,event_type,source_key,payload_json,"
            "payload_hash,state,attempt,next_attempt_at,lease_owner,lease_until,last_error_code,"
            "created_at,acknowledged_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (event["event_id"], "knowbase.value.events.v1", event["event_type"], "",
             json.dumps(event, ensure_ascii=False), events.payload_hash(event), "pending", 0, "", "", "",
             "", event["occurred_at"], None))
        conn.commit()
    finally:
        conn.close()


def test_v2_whitelist_matches_p0_delivered_spec():
    """v2 的 knowledge.delivered 口径与 P0 主线口径完全一致，不漂移。"""
    assert events_v2.V2_FIELDS["knowledge.delivered"] == {
        "required": set(events.PAYLOAD_FIELDS["knowledge.delivered"]["required"]),
        "optional": set(events.PAYLOAD_FIELDS["knowledge.delivered"]["optional"]),
    }
    # 与 v2 别名 retrieval.delivered 字段相同：两者任一到达汇总库都可被识别。
    assert (events_v2.V2_FIELDS["knowledge.delivered"]["required"]
            == events_v2.V2_FIELDS["retrieval.delivered"]["required"])
    assert (events_v2.V2_FIELDS["knowledge.delivered"]["optional"]
            == events_v2.V2_FIELDS["retrieval.delivered"]["optional"])


def test_p0_delivered_event_validates_and_strips_without_loss():
    event = _p0_delivered_event()
    assert events_v2.validate_v2_event(event) == {"ok": True}
    stripped = events_v2.strip_v2_event(event)
    assert stripped["payload"] == event["payload"]


def test_p0_delivered_event_is_exported_not_silently_filtered(memory_repo):
    """核心回归：P0 递送事件必须进 v2 包，而非被 export 过滤静默丢弃。"""
    _insert_outbox_row(memory_repo, _p0_delivered_event())
    package = exchange_v2.export_events(memory_repo, states=("pending", "leased"))
    assert package.get("error") is None
    assert package["event_ids"] == ["E-DEL-1"]
    assert package["rejected"] == []
    assert package["events"][0]["event"]["event_type"] == "knowledge.delivered"


def test_p0_delivered_event_imports_into_aggregate(memory_repo, tmp_path):
    """导入汇总库后事件类型仍为 knowledge.delivered，可供 acceptance 全链路查询。"""
    _insert_outbox_row(memory_repo, _p0_delivered_event())
    package = exchange_v2.export_events(memory_repo, states=("pending", "leased"))
    agg_conn = aggregate.connect(str(tmp_path / "agg.db"))
    try:
        result = exchange_v2.import_package(agg_conn, package, team_id="default")
        assert result["counts"]["accepted"] == 1
        row = agg_conn.execute(
            "SELECT event_type, payload_json FROM agg_events WHERE event_id='E-DEL-1'"
        ).fetchone()
        assert row[0] == "knowledge.delivered"
        assert json.loads(row[1])["payload"]["delivery_id"] == "DEL-1"
    finally:
        agg_conn.close()


def test_retrieval_delivered_alias_still_valid():
    """P3 别名保留，不破坏已有 v2 测试与包。"""
    event = _p0_delivered_event("E-RET-1")
    event["event_type"] = "retrieval.delivered"
    assert events_v2.validate_v2_event(event) == {"ok": True}
