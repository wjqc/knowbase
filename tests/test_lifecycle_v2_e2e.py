"""V2-16 六场景汇总端到端（隔离回放层）。

S1～S6 的机制路径分别落在：
- S1 自动补充：test_v2_06_07_08（ENRICH 同 ID 发布）——此处补完整链尾：
  发布后另一"宿主"经正式 search 读到新版本，旧版本证据仍可读。
- S2 已验证错误纠正：test_v2_12_13_15（可信反证隔离）+ 此处纠正发布链。
- S3 多 Agent 重复整合：test_v2_06_07_08（三卡 MERGE 原子）。
- S4 曝光/引用/采用分离：test_v2_10_11。
- S5 双机证据交换：test_v2_12_13_15（签名/去重/撤销/伪签发）。
- S6 失效与复活：test_v2_12_13_15（mismatch 只限制）+ 此处完整复活链
  （依赖变化→待复验→修订→准入→发布→旧环境证据保留）。
真实宿主/真实设备/真实模型对照：external_pending（见交付记录）。
"""
import json

from knowbase import config, index, store
from knowbase.knowledge_lifecycle import (applicability, attribution, changes,
                                          governance, observation, publication,
                                          reconcile, validation)
from knowbase.migrations import apply_all
from knowbase.server import search_impl

BODY_V1 = ("## 结论\nlib 1.x 下用方案 A。\n## 解决的问题\nx。\n## 适用条件\nlib>=1,<2。\n"
           "## 不适用条件\nlib 2.x。\n## 可执行动作\n运行 scripts/a.py。\n"
           "## 关键证据\nPR。\n## 验证情况\nv。\n## 未知与待确认\n无。\n")
BODY_V2 = ("## 结论\nlib 1.x 用方案 A；lib 2.x 须改用方案 B（分支并存）。\n"
           "## 解决的问题\nx。\n## 适用条件\nlib>=1,<3。\n## 不适用条件\n无。\n"
           "## 可执行动作\n2.x 运行 scripts/b.py，1.x 仍用 scripts/a.py。\n"
           "## 关键证据\nPR-9。\n## 验证情况\n双环境回放。\n## 未知与待确认\n无。\n")


def seed_card(repo, title, body, **extra):
    meta = store.new_meta("pitfall", title, "orders", ["fixture"], "agent:fixture:s")
    meta["id"] = store.alloc_id(repo, "pitfall")
    meta.update(extra)
    path = store.mem_path(repo, "pitfall", meta["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    store.write_shared_text(path, store.render(meta, body))
    conn = index.connect(repo)
    index.upsert(conn, meta, body, path)
    conn.close()
    return meta["id"]


def head_of(conn, mid):
    return conn.execute("SELECT content_revision FROM entity_heads WHERE entity_id=?",
                        (mid,)).fetchone()[0]


def seed_validator():
    cfg = json.loads(config.CONFIG_PATH.read_text())
    cfg["knowledge_lifecycle"] = {**(cfg.get("knowledge_lifecycle") or {}),
                                  "validators": [{"validator_id": "tv", "kind": "pytest"}]}
    config.CONFIG_PATH.write_text(json.dumps(cfg))


def test_s1_full_chain_new_version_visible_via_formal_search(memory_repo):
    """S1 完整链尾：发布后另一宿主经正式 search 读到 r2；r1 证据保留。"""
    seed_validator()
    mid = seed_card(memory_repo, "方案A 排障", BODY_V1)
    conn = index.connect(memory_repo)
    apply_all(conn)
    base = head_of(conn, mid)
    # 旧版本证据 + verified。
    conn.execute("INSERT INTO feedback_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 ("rc-old", "", "x", mid, base, "u1", "tk1", "o1", "seed", "helpful",
                  "version_task", "applied", "", "", "{}", "{}", "2026-01-01T00:00:00"))
    conn.commit()
    meta, body, _ = store.load(memory_repo, mid)
    meta2 = dict(meta)
    proposal = changes.propose_change(
        conn, memory_repo, relation="ENRICH", risk="R1", scope="orders",
        base_heads=[{"memory_id": mid, "revision_algo": "content-v1",
                     "content_revision": base}],
        outputs=[{"memory_id": mid, "disposition": "active", "meta": meta2,
                  "body": BODY_V2}],
        source_dispositions=[{"memory_id": mid, "action": "revise", "redirect_ids": []}],
        claim_map=[{"old_claim_id": "C-whole", "new_claim_ids": ["C-whole"],
                    "mapping": "preserved", "justification_ref": "source:b"}])
    assert not proposal.get("error"), proposal
    check = validation.validate_change(conn, memory_repo, proposal["change_id"])
    assert check["result"] == "pass", check
    published = publication.publish(memory_repo, conn, proposal["change_id"])
    assert published["local_published"] and published["readback"]["ok"]
    # 另一宿主：正式 search 读到新版本（镜像 + 索引已切）。
    hits = search_impl("方案A 排障", scope="orders")
    assert mid in hits
    new_meta, new_body, _ = store.load(memory_repo, mid)
    assert "方案 B" in new_body
    # 旧版本快照与证据可读回。
    from knowbase.knowledge_lifecycle import revisions
    old = revisions.load_revision(memory_repo, conn, mid, "content-v1", base)
    assert old["hash_ok"]
    assert conn.execute("SELECT COUNT(*) FROM feedback_events WHERE memory_id=? AND "
                        "content_revision=?", (mid, base)).fetchone()[0] == 1
    # 并发头检测为空（单线演进）。
    assert reconcile.detect_divergent_heads(memory_repo, conn) == []
    conn.close()


def test_s6_dependency_change_restricts_then_revives_via_revision(memory_repo):
    """S6 完整链：依赖 2.x 观测 → 待复验（不判错）→ 修订 ENRICH → 准入发布 →
    旧 1.x 证据保留、新版 once。"""
    seed_validator()
    mid = seed_card(memory_repo, "lib 1.x 方案卡", BODY_V1,
                    applicability={"components": [{"name": "example-lib",
                                                   "version_spec": ">=1,<2"}]})
    conn = index.connect(memory_repo)
    apply_all(conn)
    base = head_of(conn, mid)
    # 1.x 环境旧证据（回归样本）。
    for i in range(2):
        conn.execute("INSERT INTO feedback_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (f"rc-1x-{i}", "", "x", mid, base, f"u{i}", f"tk{i}", f"o{i}",
                      "seed", "helpful", "version_task", "applied", "", "", "{}", "{}",
                      "2026-01-01T00:00:00"))
    conn.commit()
    # 依赖升级 2.x：只生成待复验，卡不被判错、不被归档。
    observed = applicability.observe_environment(
        conn, memory_repo, "repo:orders", {"example-lib": "2.1.0"})
    assert any(r["memory_id"] == mid for r in observed["revalidation_needed"])
    meta, _, _ = store.load(memory_repo, mid)
    assert meta.get("status") == "active" and "quarantine_reason" not in meta
    # 修订：ENRICH 条件分支（保留 1.x 结论，加 2.x 分支）。
    meta_v2 = dict(meta)
    meta_v2["applicability"] = {"components": [{"name": "example-lib",
                                                "version_spec": ">=1,<3"}]}
    proposal = changes.propose_change(
        conn, memory_repo, relation="ENRICH", risk="R2", scope="orders",
        base_heads=[{"memory_id": mid, "revision_algo": "content-v1",
                     "content_revision": base}],
        outputs=[{"memory_id": mid, "disposition": "active", "meta": meta_v2,
                  "body": BODY_V2}],
        source_dispositions=[{"memory_id": mid, "action": "revise", "redirect_ids": []}],
        claim_map=[{"old_claim_id": "C-whole", "new_claim_ids": ["C-whole"],
                    "mapping": "preserved",
                    "justification_ref": "env:example-lib-2.1.0"}])
    assert not proposal.get("error"), proposal
    check = validation.validate_change(conn, memory_repo, proposal["change_id"])
    assert check["result"] == "pass", check  # 旧证据作为回归样本在位
    published = publication.publish(memory_repo, conn, proposal["change_id"])
    assert published["local_published"]
    new_meta, _, _ = store.load(memory_repo, mid)
    assert new_meta["confidence"] == "once"  # 旧票不转嫁
    # 复活后的版本需重新积累证据才 verified（治理只认当前版本 L3）。
    decision = governance.decide(conn, mid, head_of(conn, mid))
    assert decision["decision"] == "unverified"
    # 旧环境证据保留可读。
    assert conn.execute("SELECT COUNT(*) FROM feedback_events WHERE memory_id=? AND "
                        "content_revision=?", (mid, base)).fetchone()[0] == 2
    conn.close()


def test_s2_self_report_does_not_overwrite_verified_knowledge(memory_repo):
    """S2 双支：自报 incorrect 只留观察；可信反证才隔离且走 CORRECT 候选。"""
    from knowbase import versioned_feedback
    seed_validator()
    mid = seed_card(memory_repo, "已验证方法卡", BODY_V1)
    conn = index.connect(memory_repo)
    apply_all(conn)
    base = head_of(conn, mid)
    # 置 verified（直写治理字段模拟历史状态）。
    meta, body, path = store.load(memory_repo, mid)
    meta["confidence"] = "verified"
    meta["verified_content_revision"] = base
    path.write_text(store.render(meta, body), encoding="utf-8")
    index.upsert(conn, *store.load(memory_repo, mid), commit=False)
    conn.commit()
    # 支 A：Agent 声称 incorrect（未绑定回执）→ 只观察，不覆盖。
    observation_feedback = versioned_feedback.apply(memory_repo, conn, mid,
                                                     "incorrect", "tool-x")
    assert not observation_feedback.get("error")
    meta_after, _, _ = store.load(memory_repo, mid)
    assert meta_after["status"] == "active"  # V2-01 门：自报不置 stale
    # 支 B：可信同版本 L3 fail → 治理隔离 + CORRECT 候选可立案。
    run = observation.ingest(conn, "zcode", "s-s2", [
        {"native_key": "n1", "kind": "tool_call", "tool_call_id": "t1",
         "tool_name": "Bash", "payload": {"command": "python scripts/a.py"}},
        {"native_key": "n2", "kind": "tool_result", "tool_call_id": "t1",
         "payload": {"exit_code": 1}}], project_key="repo:orders",
        native_task_key="s2-task", native_run_key="s2-run")
    attribution.record_execution(conn, memory_id=mid, content_revision=base,
                                 artifact_hash="b" * 64, run_id=run["run_id"],
                                 validator_id="tv")
    attribution.verify_execution(
        conn, memory_repo, run_id=run["run_id"], validator_id="tv",
        report={"target": {"memory_id": mid, "content_revision": base,
                           "claim_id": "C-whole"},
                "artifact_hash": "b" * 64,
                "execution_stamp": {"memory_id": mid, "content_revision": base,
                                    "run_id": run["run_id"], "validator_id": "tv"},
                "checks": [{"check_id": "regression-1x", "claim_id": "C-whole",
                            "observed_result": "fail"}]})
    decision = governance.apply_decision(memory_repo, conn, mid)
    assert decision["decision"] == "quarantine"
    meta_q, _, _ = store.load(memory_repo, mid)
    assert meta_q["status"] == "stale" and meta_q.get("quarantine_reason")
    # 错误文本没被覆盖：正文仍是原文，等 CORRECT 候选修订。
    _, body_q, _ = store.load(memory_repo, mid)
    assert "方案 A" in body_q
    conn.close()
