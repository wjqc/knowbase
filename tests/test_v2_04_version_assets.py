"""V2-04 版本资产与迁移内核：契约纯函数、不可变快照、legacy 导入幂等、
CRLF 读视图一致。"""
import json
import sqlite3

import pytest

from knowbase import index, migrations, store, writer
from knowbase.knowledge_lifecycle import contracts, revisions
from knowbase.mutations import _compute_revision

BODY = ("## 结论\n已结算订单不能撤回。\n## 解决的问题\n避免结算状态被修改。\n"
        "## 适用条件\n订单完成结算。\n## 不适用条件\n草稿订单。\n"
        "## 可执行动作\n拒绝撤回请求。\n## 关键证据\n需求 PRD-4。\n"
        "## 验证情况\n使用场景测试验证。\n## 未知与待确认\n无。\n")


# ---------- 契约 ----------

def test_content_v1_hash_matches_legacy_revision(memory_repo):
    meta = store.new_meta("pitfall", "CRLF 排查", "orders", ["t"], "agent:t:s")
    meta["id"] = "P-abcdef123456"
    assert contracts.content_v1_hash(meta, BODY) == _compute_revision(meta, BODY)


def test_content_v2_hash_includes_applicability_and_claims():
    meta = store.new_meta("pitfall", "CRLF 排查", "orders", ["t"], "agent:t:s")
    meta["id"] = "P-abcdef123456"
    base = contracts.content_v2_hash(meta, BODY)
    # 语义字段缺失以空结构参与 hash：补字段必然产生新版本身份。
    meta["applicability"] = {"platforms": ["windows"]}
    assert contracts.content_v2_hash(meta, BODY) != base
    meta2 = dict(meta)
    meta2["claims"] = [{"claim_id": "C-1", "subject": "git"}]
    assert contracts.content_v2_hash(meta2, BODY) != contracts.content_v2_hash(meta, BODY)
    # 完整 64 位；v1 为 16 位。
    assert len(base) == 64
    assert len(contracts.content_v1_hash(meta, BODY)) == 16


def test_revision_identity_dispatch_by_marker():
    meta = store.new_meta("pitfall", "t", "orders", [], "agent:t:s")
    meta["id"] = "P-abcdef123456"
    plain = contracts.revision_identity(meta, BODY)
    assert plain["revision_algo"] == "content-v1"
    meta["revision_algo"] = "content-v2"
    v2 = contracts.revision_identity(meta, BODY)
    assert v2["revision_algo"] == "content-v2" and len(v2["content_revision"]) == 64


def test_canonical_json_rejects_nan_and_is_stable():
    with pytest.raises(ValueError):
        contracts.canonical_json({"x": float("nan")})
    assert contracts.canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'


def test_event_envelope_roundtrip_and_tamper_detection():
    env = contracts.build_envelope(
        event_type="validation.completed", team_id="team-a",
        issuer_installation_id="INST-a", project_key="repo:knowbase", scope="knowbase",
        occurred_at="2026-11-05T02:00:00Z", native_event_key="native-42",
        operation_key="check:crlf-roundtrip",
        subject={"memory_id": "P-abcdef123456", "revision_algo": "content-v2",
                 "content_revision": "ab" * 32, "claim_id": "C-1"},
        payload={"observed_result": "pass", "trust_basis": "registered-adapter"})
    assert contracts.validate_envelope(env) == []
    # 同输入稳定派生；改 payload 同 ID 是冲突（payload_hash 变化被检出）。
    again = contracts.build_envelope(
        event_type="validation.completed", team_id="team-a",
        issuer_installation_id="INST-a", project_key="repo:knowbase", scope="knowbase",
        occurred_at="2026-11-05T02:00:00Z", native_event_key="native-42",
        operation_key="check:crlf-roundtrip",
        subject={"memory_id": "P-abcdef123456", "revision_algo": "content-v2",
                 "content_revision": "ab" * 32, "claim_id": "C-1"},
        payload={"observed_result": "pass", "trust_basis": "registered-adapter"})
    assert env["event_id"] == again["event_id"]
    tampered = dict(env, payload={**env["payload"], "observed_result": "fail"})
    assert "payload_hash_mismatch" in contracts.validate_envelope(tampered)
    assert "unknown_event_type" in contracts.validate_envelope(
        dict(env, event_type="knowledge.made_up"))
    # 信封哈希覆盖 issuer/subject/payload（非仅业务 payload）。
    changed_subject = dict(env, memory_id="P-other0000000")
    assert contracts.envelope_hash(changed_subject) != contracts.envelope_hash(env)


def test_task_identity_unknown_is_explicit():
    assert contracts.logical_task_key("repo:x", "").startswith("unknown:")
    assert contracts.origin_run_key("repo:x", "").startswith("run-unknown:")
    assert contracts.project_key_from_repo("") == "repo:unknown"
    # 同输入同组；不同输入不同组。
    assert (contracts.independence_group("repo:x", "a", "b")
            == contracts.independence_group("repo:x", "a", "b"))
    assert (contracts.independence_group("repo:x", "a")
            != contracts.independence_group("repo:y", "a"))


def test_change_manifest_validation_matrix():
    def manifest(**over):
        base = {
            "change_id": "CH-1", "proposal_version": 1, "relation": "ENRICH",
            "risk": "R1", "scope": "knowbase", "source_material_refs": ["segment:s@h"],
            "recall_snapshot_hash": "r" * 64,
            "base_heads": [{"memory_id": "P-abcdef123456", "revision_algo": "content-v1",
                            "content_revision": "a" * 16, "governance_revision": "b" * 16}],
            "outputs": [{"memory_id": "P-abcdef123456", "snapshot_hash": "c" * 64,
                         "disposition": "active", "applicability_hash": "d" * 64}],
            "source_dispositions": [{"memory_id": "P-abcdef123456", "action": "revise",
                                     "redirect_ids": []}],
            "claim_map": [{"old_claim_id": "C-1", "new_claim_ids": ["C-1"],
                           "mapping": "preserved", "justification_ref": "source:h"}],
            "diff_hash": "e" * 64, "validation_plan_hash": "f" * 64,
            "policy_revision": contracts.CHANGE_POLICY_REVISION, "state": "proposed"}
        base.update(over)
        return base
    assert contracts.validate_change_manifest(manifest()) == []
    assert "relation_invalid:X" in contracts.validate_change_manifest(manifest(relation="X"))
    assert "risk_invalid:R9" in contracts.validate_change_manifest(manifest(risk="R9"))
    assert "no_change_with_outputs" in contracts.validate_change_manifest(
        manifest(relation="NO_CHANGE"))
    assert "new_with_base_heads" in contracts.validate_change_manifest(manifest(relation="NEW"))
    assert "redirect_without_targets:0" in contracts.validate_change_manifest(
        manifest(source_dispositions=[{"memory_id": "P-abcdef123456", "action": "redirect"}]))
    assert "claim_map_invalid:0" in contracts.validate_change_manifest(
        manifest(claim_map=[{"old_claim_id": "C-1", "new_claim_ids": ["C-1"],
                             "mapping": "guessed"}]))
    assert "output_disposition_invalid:0" in contracts.validate_change_manifest(
        manifest(outputs=[{"memory_id": "P-abcdef123456", "snapshot_hash": "c" * 64,
                           "disposition": "maybe"}]))


def test_applicability_validation():
    assert contracts.validate_applicability(
        {"components": [{"name": "lib", "version_spec": ">=2,<3"}],
         "platforms": ["windows"], "unknown_fields": ["locale"]}) == []
    assert contracts.validate_applicability(
        {"components": [{"name": "lib", "version_spec": "v2.3-custom"}]}) == \
        ["component_version_spec_unparseable:0"]
    assert contracts.validate_applicability({"platforms": "windows"}) == \
        ["platforms_not_list"]


# ---------- 迁移与快照 ----------

def test_migration_v31_tables(memory_repo):
    conn = index.connect(memory_repo)
    try:
        migrations.apply_all(conn)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"knowledge_revisions", "publication_batches", "published_heads",
                "task_runs", "evidence_events"} <= tables
    finally:
        conn.close()


def test_record_revision_is_write_once_and_idempotent(memory_repo):
    result = writer.create(memory_repo, "pitfall", "快照幂等卡", BODY, scope="orders")
    mid = result["entity_id"]
    meta, body, _ = store.load(memory_repo, mid)
    conn = index.connect(memory_repo)
    try:
        first = revisions.record_revision(memory_repo, conn, meta, body,
                                          parent_revisions=[])
        assert not first["idempotent"]
        second = revisions.record_revision(memory_repo, conn, meta, body,
                                           parent_revisions=[])
        assert second["idempotent"]
        assert second["identity"]["snapshot_hash"] == first["identity"]["snapshot_hash"]
        # 篡改快照文件 → 校验失败（不可变被破坏，显式报错不静默）。
        path = memory_repo / first["identity"]["snapshot_path"]
        path.write_bytes(b"tampered")
        broken = revisions.record_revision(memory_repo, conn, meta, body)
        assert broken["error"] == "SNAPSHOT_HASH_CONFLICT"
        # 读回（修复文件后）。
        path.write_bytes(store.render(meta, body).encode())
        back = revisions.load_revision(memory_repo, conn, mid,
                                       first["identity"]["revision_algo"],
                                       first["identity"]["content_revision"])
        assert back["hash_ok"] and back["meta"]["id"] == mid
        conn.commit()
    finally:
        conn.close()


def test_legacy_import_idempotent_and_shadow(memory_repo):
    a = writer.create(memory_repo, "pitfall", "导入卡A", BODY, scope="orders")
    b = writer.create(memory_repo, "standard", "导入卡B", BODY, scope="orders",
                      provenance="PRD-4")
    conn = index.connect(memory_repo)
    try:
        first = revisions.legacy_import(memory_repo, conn, dry_run=False)
        assert first["snapshots"] == 2 and first["existing"] == 0
        assert first["errors"] == []
        # 影子导入：不写 published_heads、不动 entity_heads。
        assert conn.execute("SELECT COUNT(*) FROM published_heads").fetchone()[0] == 0
        # 重复导入：零新增。
        second = revisions.legacy_import(memory_repo, conn, dry_run=False)
        assert second["snapshots"] == 0 and second["existing"] == 2
        # 幂等 manifest：同 digest 只写一次（路径一致）。
        assert second["manifest"] == first["manifest"]
        # dry-run 不写。
        before = conn.execute("SELECT COUNT(*) FROM knowledge_revisions").fetchone()[0]
        plan = revisions.legacy_import(memory_repo, conn, dry_run=True)
        assert plan["candidates"] == 2
        after = conn.execute("SELECT COUNT(*) FROM knowledge_revisions").fetchone()[0]
        assert before == after
        # drift 检测：人为改 entity_heads 后重算出现 drift 单列。
        conn.execute("UPDATE entity_heads SET content_revision='deadbeefdead' "
                     "WHERE entity_id=?", (a["entity_id"],))
        conn.commit()
        drifted = revisions.legacy_import(memory_repo, conn, dry_run=True)
        assert any(d["memory_id"] == a["entity_id"] for d in drifted["drift"])
        assert any(d["memory_id"] == b["entity_id"] for d in drifted["drift"]) is False
    finally:
        conn.close()
    # 跨连接幂等（真实库首跑暴露的回归：导入缺 commit 时连接关闭即回滚，
    # 磁盘快照与 DB 账本脱节）。重开连接复跑必须 existing=N、snapshots=0，
    # 且行数在全新只读连接可见。
    conn2 = index.connect(memory_repo)
    try:
        third = revisions.legacy_import(memory_repo, conn2, dry_run=False)
        assert third["snapshots"] == 0 and third["existing"] == 2
    finally:
        conn2.close()
    ro = sqlite3.connect((memory_repo / "memory.db").resolve().as_uri() + "?mode=ro",
                         uri=True)
    assert ro.execute("SELECT COUNT(*) FROM knowledge_revisions").fetchone()[0] == 2
    ro.close()


def test_legacy_import_classifies_bad_cards_without_blocking(memory_repo):
    writer.create(memory_repo, "pitfall", "正常卡", BODY, scope="orders")
    # 畸形卡：无 frontmatter。
    bad = memory_repo / "pitfalls" / "P-badbadbadbad.md"
    bad.write_text("没有 frontmatter 的坏卡", encoding="utf-8")
    # 重复 ID 卡：同 ID 两份（staging 一份、正式一份）。
    # 统一生命周期：带 provenance 让这张卡直接发布到正式目录，再复制进
    # staging 构造同 ID 两份；不带依据时卡本身已落 staging，会覆盖同一路径。
    dup = writer.create(memory_repo, "pitfall", "重复ID卡", BODY, scope="orders",
                        provenance="PRD-DUP-1")
    from pathlib import Path
    src = store.load(memory_repo, dup["entity_id"])[2]
    target = memory_repo / "staging" / src.name
    target.parent.mkdir(exist_ok=True)
    target.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    conn = index.connect(memory_repo)
    try:
        result = revisions.legacy_import(memory_repo, conn, dry_run=False)
        assert result["snapshots"] >= 1  # 正常卡照常导入
        reasons = {item["reason"].split(":")[0] for item in result["needs_review"]}
        assert "parse" in reasons and "duplicate_id" in reasons
    finally:
        conn.close()


def test_crlf_semantic_equivalence_and_source_current(memory_repo):
    """F-12 回归：磁盘 CRLF 与 LF 同语义；source_current 读视图一致。"""
    result = writer.create(memory_repo, "pitfall", "CRLF 等价卡", BODY, scope="orders")
    mid = result["entity_id"]
    meta, body, path = store.load(memory_repo, mid)
    conn = index.connect(memory_repo)
    try:
        assert index.source_current(conn, mid) is True
        # 磁盘改写为 CRLF（外部编辑模拟）：语义不变，source_current 必须仍 True。
        raw = path.read_bytes().replace(b"\n", b"\r\n")
        path.write_bytes(raw)
        assert index.source_current(conn, mid) is True
        # 同卡 CRLF/LF 两种磁盘形态产生同一 content revision（不产生两版）。
        meta2, body2, _ = store.load(memory_repo, mid)
        assert _compute_revision(meta2, body2) == result["content_revision"]
        # 快照保留原字节（审计），语义身份不变。
        snap = revisions.record_revision(memory_repo, conn, meta2, body2, raw_bytes=raw)
        assert snap["identity"]["content_revision"] == result["content_revision"]
        conn.commit()
    finally:
        conn.close()


def test_shadow_heads_compare_empty_after_import(memory_repo):
    writer.create(memory_repo, "pitfall", "影子对账卡", BODY, scope="orders")
    conn = index.connect(memory_repo)
    try:
        revisions.legacy_import(memory_repo, conn, dry_run=False)
        diff = revisions.shadow_heads_compare(memory_repo, conn)
        assert diff["published_only"] == []  # 未发布：published_heads 空
        assert diff["heads_only"]  # 现行索引视图存在
        assert diff["both_mismatch"] == []
    finally:
        conn.close()
