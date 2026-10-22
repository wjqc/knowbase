"""V1 价值事件：可靠记录、幂等与同事务原子性、身份缺失不假归属。"""

import json

import pytest

from knowbase import config, index, telemetry, versioned_feedback
from knowbase.value import events as vevents
from knowbase.value import identity as videntity
from knowbase.value import outbox as voutbox

_USE_PAYLOAD = {"memory_id": "P-1", "content_revision": "r1", "context_event_id": "ce1",
                "use_id": "u1", "state": "adopted", "task_id": "t1", "scope": "orders"}


def enable_events():
    cfg = json.loads(config.CONFIG_PATH.read_text())
    cfg["value"] = {"events_enabled": True}
    config.CONFIG_PATH.write_text(json.dumps(cfg))


def seed_card(repo):
    from knowbase import store
    meta = store.new_meta("pitfall", "SSE 重连消息序号核对", "orders", ["SSE"], "agent:fixture")
    meta["id"] = "P-2026-9001"
    body = "## 结论\nSSE 重连要核对消息序号并去重。\n## 适用条件\n订单订阅。\n"
    path = store.mem_path(repo, "pitfall", meta["id"])
    path.write_text(store.render(meta, body))
    conn = index.connect(repo)
    index.upsert(conn, meta, body, path)
    conn.close()
    return meta, body


def delivered_card(repo):
    from knowbase.server import context_impl
    seed_card(repo)
    result = json.loads(context_impl(session_id="task-session", event_id="retrieval", scope="orders",
                                     task_id="fix-reconnect", task_revision="plan-v2",
                                     current_goal="SSE 重连", host_profile="fixture-host"))
    assert result["candidates"]
    return result["candidates"][0]


def adopt(repo, card, **changes):
    from knowbase.server import use_impl
    args = dict(session_id="task-session", event_id="use-1", context_event_id="retrieval",
                id=card["id"], content_revision=card["content_revision"], state="adopted",
                host_profile="fixture-host")
    args.update(changes)
    return json.loads(use_impl(**args))


def outbox_rows(repo):
    conn = index.connect(repo)
    try:
        return voutbox.list_events(conn)
    finally:
        conn.close()


def test_migration_v19_creates_value_tables(memory_repo):
    conn = index.connect(memory_repo)
    versions = [r[0] for r in conn.execute("SELECT version FROM schema_migrations")]
    assert max(versions) == 20 and 19 in versions
    conn.execute("SELECT event_id,payload_hash,state FROM value_outbox LIMIT 1")
    conn.execute("SELECT key,value FROM value_identity LIMIT 1")
    conn.execute("SELECT task_outcome_id,result,verification_source FROM task_outcomes LIMIT 1")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(command_calls)")]
    assert "installation_id" in cols
    conn.close()


def test_build_event_validates_type_and_payload():
    event = vevents.build_event("knowledge.used", dict(_USE_PAYLOAD), host="zcode", session_id="s1")
    assert event["event_id"].startswith("VE-")
    assert event["schema_version"] == vevents.SCHEMA_VERSION
    assert "member_binding_id" not in event
    assert vevents.payload_hash(event) == vevents.payload_hash(
        json.loads(vevents.canonical_payload(event)))
    with pytest.raises(ValueError):
        vevents.build_event("no.such.type", dict(_USE_PAYLOAD), host="zcode")
    with pytest.raises(ValueError):  # 协议内类型但本构建未启用
        vevents.build_event("model.usage", {"tokens": 1}, host="zcode")
    with pytest.raises(ValueError):
        vevents.build_event("knowledge.used", {**_USE_PAYLOAD, "extra": 1}, host="zcode")
    with pytest.raises(ValueError):
        vevents.build_event("knowledge.used",
                            {k: v for k, v in _USE_PAYLOAD.items() if k != "scope"}, host="zcode")
    with pytest.raises(ValueError):
        vevents.build_event("feedback.recorded",
                            {"memory_id": "P-1", "content_revision": "r1", "outcome": "helpful",
                             "binding": "version_task", "receipt_id": "FB-1",
                             "feedback_source": "human"}, host="zcode")


def test_outbox_append_idempotency_source_key_and_states(memory_repo):
    conn = index.connect(memory_repo)
    event = vevents.build_event("knowledge.used", dict(_USE_PAYLOAD), host="zcode")
    first = voutbox.append(conn, event, source_key="knowledge_uses:u1")
    conn.commit()
    assert first["state"] == "pending"
    assert voutbox.append(conn, event, source_key="knowledge_uses:u1")["idempotent"] is True
    other = vevents.build_event("knowledge.used", dict(_USE_PAYLOAD), host="zcode")
    deduped = voutbox.append(conn, other, source_key="knowledge_uses:u1")
    assert deduped["idempotent"] is True and deduped["deduped_by"] == "source_key"
    conflict = dict(event)
    conflict["occurred_at"] = "1999-01-01T00:00:00+00:00"
    assert voutbox.append(conn, conflict)["error"] == "IDEMPOTENCY_CONFLICT"
    assert voutbox.append(conn, vevents.build_event("knowledge.used", dict(_USE_PAYLOAD),
                                                    host="zcode"))["state"] == "pending"
    assert voutbox.stats(conn)["total"] == 2
    assert voutbox.mark_acknowledged(conn, [event["event_id"]]) == 1
    assert voutbox.mark_acknowledged(conn, [event["event_id"]]) == 0
    free = vevents.build_event("knowledge.used", dict(_USE_PAYLOAD), host="zcode")
    voutbox.append(conn, free)
    assert voutbox.quarantine(conn, free["event_id"], "REJECTED_SCHEMA") is True
    states = {e["event_id"]: e["state"] for e in voutbox.list_events(conn)}
    assert states[event["event_id"]] == "acknowledged"
    assert states[free["event_id"]] == "quarantined"
    assert all(e["state"] for e in voutbox.list_events(conn, states=("acknowledged",)))
    conn.close()


def test_use_emits_event_same_transaction_disabled_by_default(memory_repo):
    from knowbase.server import read_impl
    card = delivered_card(memory_repo)
    result = adopt(memory_repo, card)
    assert "value_event" not in result
    assert outbox_rows(memory_repo) == []
    enable_events()
    result = adopt(memory_repo, card, event_id="use-2")
    assert result["value_event"]["event_id"]
    replay = adopt(memory_repo, card, event_id="use-2")
    assert replay["idempotent"] is True and "value_event" not in replay
    rows = outbox_rows(memory_repo)
    assert len(rows) == 1
    used = json.loads(rows[0]["payload"])
    assert rows[0]["event_type"] == "knowledge.used"
    assert rows[0]["source_key"] == f"knowledge_uses:{result['use_id']}"
    assert used["payload"]["use_id"] == result["use_id"]
    assert used["installation_id"].startswith("INST-")
    assert "member_binding_id" not in used  # 成员未确认：不冒充归属
    text = read_impl(card["id"], session_id="task-session", event_id="read-1",
                     context_event_id="retrieval", host_profile="fixture-host")
    assert "read_use_id=USE-" in text
    types = [e["event_type"] for e in outbox_rows(memory_repo)]
    assert types.count("knowledge.used") == 1 and types.count("knowledge.read") == 1


def test_outbox_failure_rolls_back_business_receipt(memory_repo, monkeypatch):
    enable_events()
    card = delivered_card(memory_repo)

    def crash_after_insert(conn, event, *, source_key=""):
        conn.execute(
            "INSERT INTO value_outbox(event_id,schema_version,event_type,source_key,payload_json,"
            "payload_hash,state,attempt,next_attempt_at,lease_owner,lease_until,last_error_code,"
            "created_at,acknowledged_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("VE-crash", vevents.SCHEMA_VERSION, "knowledge.used", "knowledge_uses:crash", "{}", "h",
             "pending", 0, "", "", "", "", "2026-01-01T00:00:00+00:00", None))
        raise RuntimeError("crash before commit")

    monkeypatch.setattr(voutbox, "append", crash_after_insert)
    with pytest.raises(RuntimeError):
        adopt(memory_repo, card, event_id="use-crash")
    assert outbox_rows(memory_repo) == []
    conn = index.connect(memory_repo)
    assert conn.execute("SELECT COUNT(*) FROM knowledge_uses").fetchone()[0] == 0
    conn.close()


def test_identity_member_binding_evidence_and_clearing(memory_repo):
    conn = index.connect(memory_repo)
    first = videntity.install(conn)
    conn.commit()
    assert videntity.install(conn) == first and first.startswith("INST-")
    assert videntity.get(conn)["member_binding_id"] == ""
    ident = videntity.set_member(conn, "qc", evidence="manual_confirmed")
    conn.commit()
    assert ident["member_binding_id"].startswith("MBR-")
    assert ident["member_evidence"] == "manual_confirmed" and ident["member_confirmed_at"]
    with pytest.raises(ValueError):
        videntity.set_member(conn, "qc", evidence="certified")  # 中心认证等级不可自封
    cleared = videntity.set_member(conn, "")
    conn.commit()
    assert cleared["member_binding_id"] == "" and cleared["member_label"] == ""
    videntity.set_member(conn, "qc")
    conn.commit()
    conn.close()
    enable_events()
    card = delivered_card(memory_repo)
    adopt(memory_repo, card, event_id="use-m")
    payload = json.loads(outbox_rows(memory_repo)[0]["payload"])
    assert payload["member_binding_id"].startswith("MBR-")
    assert payload["member_binding_id"] not in ("fixture-host", "pytest-inflight", "unknown")


def test_feedback_records_event_once_across_replay(memory_repo):
    enable_events()
    card = delivered_card(memory_repo)
    use = adopt(memory_repo, card)
    conn = index.connect(memory_repo)
    result = versioned_feedback.apply(memory_repo, conn, card["id"], "helpful", "pytest-agent",
                                      expected_content_revision=card["content_revision"],
                                      use_id=use["use_id"], task_outcome_id="OUT-1",
                                      request_id="req-1")
    conn.close()
    assert result["binding"] == "version_task" and not result.get("error")
    rows = [e for e in outbox_rows(memory_repo) if e["event_type"] == "feedback.recorded"]
    assert len(rows) == 1
    assert rows[0]["source_key"] == f"feedback_events:{result['receipt_id']}"
    payload = json.loads(rows[0]["payload"])["payload"]
    assert payload["outcome"] == "helpful" and payload["feedback_source"] == "agent"
    conn = index.connect(memory_repo)
    replay = versioned_feedback.apply(memory_repo, conn, card["id"], "helpful", "pytest-agent",
                                      expected_content_revision=card["content_revision"],
                                      use_id=use["use_id"], task_outcome_id="OUT-1",
                                      request_id="req-1")
    conn.close()
    assert replay["idempotent"] is True
    rows = [e for e in outbox_rows(memory_repo) if e["event_type"] == "feedback.recorded"]
    assert len(rows) == 1


def test_telemetry_carries_installation_identity(memory_repo):
    conn = index.connect(memory_repo)
    inst = videntity.install(conn)
    conn.commit()
    conn.close()
    telemetry._INSTALLATION_CACHE.clear()
    telemetry.record("mcp", "memory_stats", "ok", 1.2)
    telemetry.record("cli", "stats", "ok", 0.5)
    conn = index.connect(memory_repo)
    got = dict(conn.execute("SELECT command,installation_id FROM command_calls").fetchall())
    conn.close()
    assert got.get("memory_stats") == inst and got.get("stats") == inst


def test_member_binding_stable_across_installations(memory_repo, tmp_path):
    """同一成员标签在不同安装生成同一绑定：跨机器不再拆成两个人。"""
    import sqlite3
    conn = index.connect(memory_repo)
    first = videntity.set_member(conn, "qc")
    conn.commit()
    conn.close()
    other = sqlite3.connect(tmp_path / "other-device.db")
    other.execute("CREATE TABLE value_identity(key TEXT PRIMARY KEY, value TEXT NOT NULL "
                  "DEFAULT '', updated_at TEXT NOT NULL DEFAULT '')")
    other.commit()
    videntity.install(other)
    second = videntity.set_member(other, "qc")
    other.commit()
    other.close()
    assert first["member_binding_id"] == second["member_binding_id"]
    # 不同标签仍是不同绑定
    conn = index.connect(memory_repo)
    other_label = videntity.set_member(conn, "someone-else")
    conn.commit()
    conn.close()
    assert other_label["member_binding_id"] != first["member_binding_id"]
