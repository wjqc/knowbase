"""A1 文件汇总：导出/幂等导入/回执闭环/独立汇总库/最小快照口径。"""

import json

import pytest

from knowbase import config, index, versioned_feedback
from knowbase.value import aggregate, exchange, outbox as voutbox


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


def produce_events(repo):
    """递送→采纳→反馈，产生 delivered + used + feedback 三类事件。"""
    from knowbase.server import context_impl, use_impl
    seed_card(repo)
    card = json.loads(context_impl(session_id="s1", event_id="retrieval", scope="orders",
                                   task_id="t1", task_revision="v1", current_goal="SSE",
                                   host_profile="h1"))["candidates"][0]
    use = json.loads(use_impl(session_id="s1", event_id="use-1", context_event_id="retrieval",
                              id=card["id"], content_revision=card["content_revision"],
                              state="adopted", host_profile="h1"))
    conn = index.connect(repo)
    feedback = versioned_feedback.apply(repo, conn, card["id"], "helpful", "pytest",
                                        expected_content_revision=card["content_revision"],
                                        use_id=use["use_id"], task_outcome_id="OUT-1",
                                        request_id="req-1")
    conn.close()
    assert not feedback.get("error")
    return card, use, feedback


def outbox_states(repo):
    conn = index.connect(repo)
    try:
        return {e["event_id"]: e["state"] for e in voutbox.list_events(conn)}
    finally:
        conn.close()


def test_export_import_receipt_closed_loop(memory_repo, tmp_path):
    enable_events()
    produce_events(memory_repo)
    pkg = tmp_path / "pkg.json"
    result = exchange.export_events(memory_repo, pkg)
    assert not result.get("error") and result["events"] == 3
    package = json.loads(pkg.read_text())
    assert package["source"]["installation_id"].startswith("INST-")
    assert package["source"]["member_evidence"] in ("", "none")  # 未确认成员不冒充
    # 导出不改变事件状态（导出成功≠已接收）
    assert set(outbox_states(memory_repo).values()) == {"pending"}

    agg_db = tmp_path / "agg" / "team.db"
    agg = aggregate.connect(agg_db)
    imported = exchange.import_package(agg, pkg, team_id="team-x",
                                       receipt_out=str(tmp_path / "pkg.receipt.json"))
    assert imported["counts"] == {"accepted": 3, "duplicate": 0, "conflict": 0, "rejected": 0}
    # 重复导入不累计
    again = exchange.import_package(agg, pkg, team_id="team-x")
    assert again["counts"]["duplicate"] == 3 and again["counts"]["accepted"] == 0
    n = agg.execute("SELECT COUNT(*) FROM agg_events WHERE team_id='team-x'").fetchone()[0]
    assert n == 3
    agg.close()

    # 源端核对回执 → acknowledged；伪回执（错误哈希）不确认
    receipt = json.loads((tmp_path / "pkg.receipt.json").read_text())
    acked = exchange.acknowledge_receipt(memory_repo, receipt)
    assert acked["acknowledged"] == 3 and not acked["mismatched"]
    assert set(outbox_states(memory_repo).values()) == {"acknowledged"}
    tampered = json.loads(json.dumps(receipt))
    tampered["events"][0]["payload_hash"] = "deadbeef"
    bad = exchange.acknowledge_receipt(memory_repo, tampered)
    assert bad["acknowledged"] == 0 and len(bad["mismatched"]) == 1


def test_import_rejects_bad_schema_conflicts_and_personal_db(memory_repo, tmp_path):
    enable_events()
    produce_events(memory_repo)
    pkg = tmp_path / "pkg.json"
    exchange.export_events(memory_repo, pkg)
    package = json.loads(pkg.read_text())
    agg = aggregate.connect(tmp_path / "agg.db")
    # 未知 schema 拒绝
    bad_schema = dict(package, schema="knowbase.value.events.v999")
    bad_path = tmp_path / "bad_schema.json"
    bad_path.write_text(json.dumps(bad_schema))
    assert exchange.import_package(agg, bad_path)["error"] == "UNSUPPORTED_PACKAGE_SCHEMA"
    # 同 event_id 异内容 → conflict 隔离：先导入原包，再导被改包
    assert exchange.import_package(agg, pkg, team_id="t")["counts"]["accepted"] == 3
    conflict = json.loads(json.dumps(package))
    conflict["events"][0]["event"]["occurred_at"] = "1999-01-01T00:00:00+00:00"
    conflict["events"][0]["payload_hash"] = exchange.value_events.payload_hash(
        conflict["events"][0]["event"])
    conflict_path = tmp_path / "conflict.json"
    conflict_path.write_text(json.dumps(conflict))
    result = exchange.import_package(agg, conflict_path, team_id="t")
    assert result["counts"]["conflict"] == 1 and result["counts"]["duplicate"] == 2
    assert result["counts"]["accepted"] == 0
    stored = agg.execute("SELECT COUNT(*) FROM agg_events WHERE team_id='t' AND state='accepted'").fetchone()[0]
    assert stored == 3  # 冲突不覆盖已存事件
    # 哈希不匹配（篡改 payload 不重算哈希）→ rejected
    tampered = json.loads(json.dumps(package))
    tampered["events"][0]["event"]["occurred_at"] = "2000-01-01T00:00:00+00:00"
    tampered_path = tmp_path / "tampered.json"
    tampered_path.write_text(json.dumps(tampered))
    result = exchange.import_package(agg, tampered_path, team_id="t2")
    assert result["counts"]["rejected"] >= 1
    agg.close()
    # 个人 memory.db 不能作为汇总目标
    with pytest.raises(ValueError):
        aggregate.connect(index.db_path(memory_repo))


def test_same_person_multi_installation_counts_once(memory_repo, tmp_path):
    enable_events()
    from knowbase.value import identity as videntity
    conn = index.connect(memory_repo)
    videntity.set_member(conn, "qc")  # 同一成员先在设备一确认
    conn.commit()
    conn.close()
    card, use, feedback = produce_events(memory_repo)
    pkg = tmp_path / "pkg.json"
    exchange.export_events(memory_repo, pkg)
    package = json.loads(pkg.read_text())
    agg = aggregate.connect(tmp_path / "agg.db")
    assert exchange.import_package(agg, pkg, team_id="t")["counts"]["accepted"] == 3
    # 第二台安装（同成员绑定、不同 installation/event id）导出的包
    second = json.loads(json.dumps(package))
    second["package_id"] = "PKG-second"
    binding = package["source"]["member_binding_id"]
    assert binding.startswith("MBR-")
    for item in second["events"]:
        item["event"]["installation_id"] = "INST-second0000000000000000"
        item["event"]["event_id"] = item["event"]["event_id"] + "-b"
        item["payload_hash"] = exchange.value_events.payload_hash(item["event"])
    second_path = tmp_path / "pkg2.json"
    second_path.write_text(json.dumps(second))
    assert exchange.import_package(agg, second_path, team_id="t")["counts"]["accepted"] == 3
    # 同一人两台安装：observed=1（按 member_binding 归并），有效消费不重复
    snap = aggregate.snapshot(agg, team_id="t")
    assert snap["members"]["observed"] == 1
    assert snap["members"]["effective_consumers"] == 1
    assert snap["tasks"]["adopted"] == 1  # 任务按 task_id 去重，不因两台机器翻倍
    # Phase 3-A：覆盖率不再以人工登记名单为门——两台安装均有绑定归属，
    # 分母为有归属实例数 2，分子为有效消费实例数 2；名单为空不影响口径
    assert snap["members"]["roster_size"] is None
    assert snap["members"]["coverage"] == 1.0
    assert snap["members"]["instances_attributed"] == 2
    assert snap["members"]["unknown_instances"] == []
    agg.close()


def test_snapshot_coverage_requires_roster_and_confirmed_attribution(memory_repo, tmp_path):
    enable_events()
    produce_events(memory_repo)
    pkg = tmp_path / "pkg.json"
    exchange.export_events(memory_repo, pkg)
    package = json.loads(pkg.read_text())
    agg = aggregate.connect(tmp_path / "agg.db")
    exchange.import_package(agg, pkg, team_id="t")
    installation = package["source"]["installation_id"]
    aggregate.register_member(agg, "M-001", "成员A")
    snap = aggregate.snapshot(agg, team_id="t")
    # 有名单但安装未人工确认归属 → 覆盖率口径不完整，仍 unknown
    assert snap["members"]["coverage_known"] is False and snap["members"]["coverage"] is None
    aggregate.confirm_member(agg, installation, "M-001")
    snap = aggregate.snapshot(agg, team_id="t")
    assert snap["members"]["coverage_known"] is True
    assert snap["members"]["coverage"] == 1.0 and snap["members"]["roster_size"] == 1
    # 快照口径：反馈按来源拆分、跨成员复用明确 not_available
    assert snap["feedback"]["by_outcome"].get("helpful") == 1
    assert snap["feedback"]["by_source"].get("agent") == 1
    assert snap["cross_member_reuse"]["available"] is False
    # 可由事件重建：新库重导同包得到相同事件总数与指标
    fresh = aggregate.connect(tmp_path / "fresh.db")
    exchange.import_package(fresh, pkg, team_id="t")
    fresh_snap = aggregate.snapshot(fresh, team_id="t")
    assert fresh_snap["counts"] == snap["counts"]
    assert fresh_snap["tasks"] == snap["tasks"]
    fresh.close()
    agg.close()


def test_cli_value_roundtrip(memory_repo, tmp_path, monkeypatch, capsys):
    from knowbase.__main__ import main
    enable_events()
    produce_events(memory_repo)
    agg_db = tmp_path / "agg" / "team.db"
    pkg = tmp_path / "pkg.json"
    assert main(["value", "export", "--output", str(pkg)]) == 0
    assert main(["value", "import", str(pkg), "--agg-db", str(agg_db),
                 "--team-id", "team-1"]) == 0
    assert main(["value", "register-member", "--agg-db", str(agg_db),
                 "--member-id", "M-001"]) == 0
    assert main(["value", "confirm-member", "--agg-db", str(agg_db),
                 "--installation", "INST-any", "--member-id", "M-001"]) == 0
    assert main(["value", "receipt", str(pkg) + ".receipt.json"]) == 0
    snap_path = tmp_path / "snap.json"
    assert main(["value", "snapshot", "--output", str(snap_path),
                 "--agg-db", str(agg_db), "--team-id", "team-1"]) == 0
    snap = json.loads(snap_path.read_text())
    assert snap["schema"] == "knowbase.value.snapshot.v1"
    assert snap["counts"]["events_total"] == 3
    assert set(outbox_states(memory_repo).values()) == {"acknowledged"}


def test_import_rejects_extra_payload_fields_with_recomputed_hash(memory_repo, tmp_path):
    enable_events()
    produce_events(memory_repo)
    pkg = tmp_path / "pkg.json"
    exchange.export_events(memory_repo, pkg)
    package = json.loads(pkg.read_text())
    # 攻击面：额外加入 raw_query/prompt 并重算哈希——导入端 payload 白名单必须拒绝
    poisoned = json.loads(json.dumps(package))
    poisoned["events"][0]["event"]["payload"]["raw_query"] = "PRIVATE_QUERY_MEMBER_9C2A"
    poisoned["events"][0]["event"]["payload"]["prompt"] = "PRIVATE_PROMPT"
    poisoned["events"][0]["payload_hash"] = exchange.value_events.payload_hash(
        poisoned["events"][0]["event"])
    poisoned_path = tmp_path / "poisoned.json"
    poisoned_path.write_text(json.dumps(poisoned))
    agg = aggregate.connect(tmp_path / "agg.db")
    result = exchange.import_package(agg, poisoned_path, team_id="t")
    assert result["counts"]["rejected"] == 1 and result["counts"]["accepted"] == 2
    leaked = agg.execute("SELECT COUNT(*) FROM agg_events WHERE team_id='t' "
                         "AND payload_json LIKE '%PRIVATE_QUERY_MEMBER_9C2A%'").fetchone()[0]
    assert leaked == 0
    agg.close()


def test_import_rejects_top_level_and_nested_smuggling(memory_repo, tmp_path):
    """顶层额外键与白名单字段的嵌套对象即使重算哈希也被拒绝。"""
    enable_events()
    produce_events(memory_repo)
    pkg = tmp_path / "pkg.json"
    exchange.export_events(memory_repo, pkg)
    package = json.loads(pkg.read_text())
    base = package["events"][0]["event"]

    def try_smuggle(mutate):
        poisoned = json.loads(json.dumps(package))
        mutate(poisoned["events"][0]["event"])
        poisoned["events"][0]["payload_hash"] = exchange.value_events.payload_hash(
            poisoned["events"][0]["event"])
        path = tmp_path / "poisoned.json"
        path.write_text(json.dumps(poisoned))
        agg = aggregate.connect(tmp_path / "agg.db")
        result = exchange.import_package(agg, path, team_id="t")
        leaked = agg.execute("SELECT COUNT(*) FROM agg_events WHERE payload_json "
                             "LIKE '%PRIVATE%'").fetchone()[0]
        agg.close()
        return result["counts"]["rejected"] == 1 and leaked == 0

    # 顶层夹带原文
    assert try_smuggle(lambda e: e.update(raw_query="PRIVATE_TOP_LEVEL"))
    # 白名单字段塞嵌套对象
    assert try_smuggle(lambda e: e["payload"].update(context_event_id={"text": "PRIVATE"}))
    agg2 = aggregate.connect(tmp_path / "agg2.db")
    agg2.close()
