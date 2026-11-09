"""V2-06/08/07 内容演进闭环：候选→验证→准入→发布→读回/恢复/回滚。

覆盖：S1 前半程（ENRICH 同 ID 新版本、claim 保真、不继承 verified）、
S3 核心路径（三卡 MERGE 整批发布与整体恢复）、断点恢复、回滚不复活旧票。
"""
import json

import pytest

from knowbase import index, store, writer
from knowbase.knowledge_lifecycle import admission, changes, publication, validation
from knowbase.migrations import apply_all
from knowbase.mutations import _compute_revision

BODY = ("## 结论\nCRLF 读视图不一致需入口归一。\n## 解决的问题\n跨平台协作。\n"
        "## 适用条件\nWindows 编辑器。\n## 不适用条件\n纯 LF 仓库。\n"
        "## 可执行动作\nread_text 统一 LF。\n## 关键证据\nPR-7 复现。\n"
        "## 验证情况\n双机验证。\n## 未知与待确认\n无。\n")
ENRICHED = BODY.replace("## 未知与待确认\n无。\n",
                        "## 未知与待确认\n无。\n## 补充\nCRLF 场景下 source_current 也要归一。\n")


def make_card(repo, title, body=BODY, **kw):
    result = writer.create(repo, "pitfall", title, body, scope="orders", **kw)
    assert not result.get("error"), result
    return result["entity_id"]


def head_of(conn, mid):
    return conn.execute("SELECT content_revision FROM entity_heads WHERE entity_id=?",
                        (mid,)).fetchone()[0]


def seed_evidence(conn, mid, revision, count=1):
    """绑定旧版本的成功证据（回归样本）。"""
    for i in range(count):
        conn.execute(
            "INSERT INTO feedback_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"rc-{mid}-{i}", "", "x", mid, revision, f"use-{i}", f"tk-{i}",
             f"out-{i}", "seed", "helpful", "version_task", "applied",
             "", "", "{}", "{}", "2026-01-01T00:00:00"))
    conn.commit()


def test_s1_enrich_same_id_new_version_no_inherited_verified(memory_repo):
    """S1 前半程：ENRICH 同 ID 新版本，旧 claim 全保留，新版本不继承 verified。"""
    mid = make_card(memory_repo, "CRLF 读视图不一致")
    conn = index.connect(memory_repo)
    apply_all(conn)
    base_rev = head_of(conn, mid)
    # 基线卡有旧证据（回归样本）且人工置 verified（模拟历史已验证卡）。
    seed_evidence(conn, mid, base_rev, count=3)
    meta, body, _ = store.load(memory_repo, mid)
    meta["confidence"] = "verified"
    meta["last_verified"] = "2026-10-01"
    meta["verified_content_revision"] = base_rev
    path = store.load(memory_repo, mid)[2]
    path.write_text(store.render(meta, body), encoding="utf-8")
    index.upsert(conn, *store.load(memory_repo, mid), commit=False)
    conn.commit()
    base_rev = head_of(conn, mid)

    proposal = changes.propose_change(
        conn, memory_repo, relation="ENRICH", risk="R1", scope="orders",
        base_heads=[{"memory_id": mid, "revision_algo": "content-v1",
                     "content_revision": base_rev}],
        outputs=[{"memory_id": mid, "disposition": "active",
                  "meta": {**meta, "updated": "2026-11-05"}, "body": ENRICHED}],
        source_dispositions=[{"memory_id": mid, "action": "revise", "redirect_ids": []}],
        claim_map=[{"old_claim_id": "C-whole", "new_claim_ids": ["C-whole"],
                    "mapping": "preserved", "justification_ref": "source:h"}],
        proposer="agent-b")
    assert not proposal.get("error"), proposal
    # 候选创建后已发布内容零变化。
    assert store.load(memory_repo, mid)[1] == body
    assert head_of(conn, mid) == base_rev

    check = validation.validate_change(conn, memory_repo, proposal["change_id"])
    assert check["result"] == "pass", check
    published = publication.publish(memory_repo, conn, proposal["change_id"])
    assert published.get("local_published"), published
    assert published["readback"]["ok"]

    # 同 ID 新版本：镜像与 head 切换，旧证据仍指旧版本。
    new_meta, new_body, new_path = store.load(memory_repo, mid)
    assert "## 补充" in new_body
    assert head_of(conn, mid) != base_rev
    assert new_meta["confidence"] == "once"  # 不继承 verified
    assert not new_meta.get("verified_content_revision")
    # 旧版本证据仍可读回（快照保留）。
    from knowbase.knowledge_lifecycle import revisions
    old = revisions.load_revision(memory_repo, conn, mid, "content-v1", base_rev)
    assert old["hash_ok"] and old["meta"]["id"] == mid
    # 再发同 change：ALREADY_PUBLISHED，不产生第二个 publication。
    again = publication.publish(memory_repo, conn, proposal["change_id"])
    assert again["error"] == "ALREADY_PUBLISHED"
    assert conn.execute("SELECT COUNT(*) FROM publication_batches").fetchone()[0] == 1
    conn.close()


def test_claim_coverage_and_base_conflict_gates(memory_repo):
    mid = make_card(memory_repo, "claim 保真卡")
    conn = index.connect(memory_repo)
    apply_all(conn)
    base_rev = head_of(conn, mid)
    meta, body, _ = store.load(memory_repo, mid)
    # 漏映射旧 claim → 拒绝立案。
    bad = changes.propose_change(
        conn, memory_repo, relation="CORRECT", risk="R2", scope="orders",
        base_heads=[{"memory_id": mid, "revision_algo": "content-v1",
                     "content_revision": base_rev}],
        outputs=[{"memory_id": mid, "disposition": "active", "meta": meta, "body": body}],
        source_dispositions=[{"memory_id": mid, "action": "revise", "redirect_ids": []}],
        claim_map=[])
    assert bad["error"] == "CLAIM_COVERAGE_INCOMPLETE"
    # removed 无理由 → 拒绝。
    bad2 = changes.propose_change(
        conn, memory_repo, relation="CORRECT", risk="R2", scope="orders",
        base_heads=[{"memory_id": mid, "revision_algo": "content-v1",
                     "content_revision": base_rev}],
        outputs=[{"memory_id": mid, "disposition": "active", "meta": meta, "body": body}],
        source_dispositions=[{"memory_id": mid, "action": "revise", "redirect_ids": []}],
        claim_map=[{"old_claim_id": "C-whole", "new_claim_ids": [],
                    "mapping": "removed"}])
    assert bad2["error"] == "CLAIM_COVERAGE_INCOMPLETE"
    # base head 已移动 → REVISION_CONFLICT。
    bad3 = changes.propose_change(
        conn, memory_repo, relation="ENRICH", risk="R1", scope="orders",
        base_heads=[{"memory_id": mid, "revision_algo": "content-v1",
                     "content_revision": "deadbeefdeadbeef"}],
        outputs=[], source_dispositions=[],
        claim_map=[{"old_claim_id": "C-whole", "new_claim_ids": ["C-whole"],
                    "mapping": "preserved"}])
    assert bad3["error"] == "REVISION_CONFLICT"
    # 幂等：同内容同 id 返回既有；改内容同 id 冲突。
    good = changes.propose_change(
        conn, memory_repo, relation="ENRICH", risk="R1", scope="orders",
        base_heads=[{"memory_id": mid, "revision_algo": "content-v1",
                     "content_revision": base_rev}],
        outputs=[{"memory_id": mid, "disposition": "active", "meta": meta, "body": body}],
        source_dispositions=[{"memory_id": mid, "action": "revise", "redirect_ids": []}],
        claim_map=[{"old_claim_id": "C-whole", "new_claim_ids": ["C-whole"],
                    "mapping": "preserved"}],
        change_id="CH-fixed0000000001")
    assert not good.get("error")
    same = changes.propose_change(
        conn, memory_repo, relation="ENRICH", risk="R1", scope="orders",
        base_heads=[{"memory_id": mid, "revision_algo": "content-v1",
                     "content_revision": base_rev}],
        outputs=[{"memory_id": mid, "disposition": "active", "meta": meta, "body": body}],
        source_dispositions=[{"memory_id": mid, "action": "revise", "redirect_ids": []}],
        claim_map=[{"old_claim_id": "C-whole", "new_claim_ids": ["C-whole"],
                    "mapping": "preserved"}],
        change_id="CH-fixed0000000001")
    assert same["idempotent"] is True
    changed = changes.propose_change(
        conn, memory_repo, relation="ENRICH", risk="R1", scope="orders",
        base_heads=[{"memory_id": mid, "revision_algo": "content-v1",
                     "content_revision": base_rev}],
        outputs=[{"memory_id": mid, "disposition": "active", "meta": meta,
                  "body": body + "改"}],
        source_dispositions=[{"memory_id": mid, "action": "revise", "redirect_ids": []}],
        claim_map=[{"old_claim_id": "C-whole", "new_claim_ids": ["C-whole"],
                    "mapping": "preserved"}],
        change_id="CH-fixed0000000001")
    assert changed["error"] == "IDEMPOTENCY_CONFLICT"
    # NO_CHANGE：无输出、不可发布。
    nc = changes.propose_change(
        conn, memory_repo, relation="NO_CHANGE", risk="R0", scope="orders",
        base_heads=[], outputs=[], source_dispositions=[],
        claim_map=[], proposer="dedup")
    assert not nc.get("error") and nc["state"] == "proposed"
    assert publication.publish(memory_repo, conn, nc["change_id"])["error"] == \
        "NO_CHANGE_NOT_PUBLISHABLE"
    # CONFLICT：不自动选边，state=needs_review 且无输出。
    cf = changes.propose_change(
        conn, memory_repo, relation="CONFLICT", risk="R2", scope="orders",
        base_heads=[{"memory_id": mid, "revision_algo": "content-v1",
                     "content_revision": base_rev}],
        outputs=[{"memory_id": mid, "disposition": "active", "meta": meta,
                  "body": body + "冲突版"}],
        source_dispositions=[], claim_map=[],
        proposer="detector")
    assert not cf.get("error") and cf["state"] == "needs_review"
    detail = changes.get_change(conn, cf["change_id"])
    assert detail["outputs"] == []
    conn.close()


def test_s3_merge_three_cards_atomic_publish_and_recovery(memory_repo):
    """S3：三卡 MERGE 整批发布——canonical 1、两旧卡归档带重定向、
    半发布崩溃恢复后只见全旧或全新。"""
    # 三份跨会话产生的相似卡（标题相近但躲过 0.82 查重——真实场景中三卡
    # 由不同时间/会话写入，且查重只看标题）。
    a = make_card(memory_repo, "Git 邮箱门禁提交被拒排查")
    b = make_card(memory_repo, "push 因邮箱域校验失败处理", body=BODY + "乙来源。\n")
    c = make_card(memory_repo, "企业 Git 白名单不通过的绕行", body=BODY + "丙例外：内部域放行。\n")
    conn = index.connect(memory_repo)
    apply_all(conn)
    heads = {m: head_of(conn, m) for m in (a, b, c)}
    for m in (a, b, c):
        seed_evidence(conn, m, heads[m], count=2)
    meta_a, body_a, _ = store.load(memory_repo, a)
    merged_body = BODY + "乙来源。\n## 例外\n内部域放行（丙）。\n"
    outputs = [
        {"memory_id": a, "disposition": "active",
         "meta": {**meta_a, "updated": "2026-11-05"}, "body": merged_body},
        {"memory_id": b, "disposition": "alias", "meta": store.load(memory_repo, b)[0],
         "body": store.load(memory_repo, b)[1]},
        {"memory_id": c, "disposition": "alias", "meta": store.load(memory_repo, c)[0],
         "body": store.load(memory_repo, c)[1]},
    ]
    base_heads = [{"memory_id": m, "revision_algo": "content-v1",
                   "content_revision": heads[m]} for m in (a, b, c)]
    proposal = changes.propose_change(
        conn, memory_repo, relation="MERGE", risk="R2", scope="orders",
        base_heads=base_heads, outputs=outputs,
        source_dispositions=[
            {"memory_id": a, "action": "revise", "redirect_ids": []},
            {"memory_id": b, "action": "redirect", "redirect_ids": [a]},
            {"memory_id": c, "action": "redirect", "redirect_ids": [a]}],
        claim_map=[{"old_claim_id": "C-whole", "new_claim_ids": ["C-whole"],
                    "mapping": "preserved", "justification_ref": "source:merge"}],
        proposer="s3")
    assert not proposal.get("error"), proposal
    check = validation.validate_change(conn, memory_repo, proposal["change_id"])
    assert check["result"] == "pass", check
    # 手工模拟"published manifest 已落、SQLite 未物化"的半发布崩溃：
    # 先正常发布，再把 published_heads/镜像拨回，用 recover 恢复。
    published = publication.publish(memory_repo, conn, proposal["change_id"])
    assert published.get("local_published")
    pub_id = published["publication_id"]
    conn.execute("DELETE FROM published_heads")
    conn.execute("UPDATE publication_batches SET state='published' WHERE publication_id=?",
                 (pub_id,))
    conn.commit()
    # 读者此时看不到新 head（半发布不可见）。
    assert conn.execute("SELECT COUNT(*) FROM published_heads").fetchone()[0] == 0
    healed = publication.recover(memory_repo, conn)
    assert pub_id in healed["rolled_forward"]
    back = publication.readback(memory_repo, conn, pub_id)
    assert back["ok"], back
    # canonical 新内容 + 两别名卡 archived + 重定向。
    vis = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT memory_id,visibility,content_revision FROM published_heads")}
    assert vis[a][0] == "active" and vis[a][1] != heads[a]
    assert vis[b][0] == "alias" and vis[c][0] == "alias"
    meta_b, _, _ = store.load(memory_repo, b)
    assert meta_b["status"] == "archived" and meta_b["redirect_ids"] == [a]
    # 丙的例外内容进入 canonical 正文（claim 无丢失）。
    merged_meta, merged_body_now, _ = store.load(memory_repo, a)
    assert "内部域放行" in merged_body_now and "乙来源" in merged_body_now
    conn.close()


def test_prepared_without_manifest_is_discarded(memory_repo):
    """prepared 无 manifest：从未服务，恢复直接作废，不留半批。"""
    mid = make_card(memory_repo, "恢复作废卡")
    conn = index.connect(memory_repo)
    apply_all(conn)
    conn.execute(
        "INSERT INTO publication_batches VALUES(?,?,?,?,?,?,?,?,?)",
        ("PUB-orphan0000000001", "CH-none", "[]", "", "lifecycle/publications/x.json",
         "prepared", "{}", "", "2026-11-05T00:00:00"))
    conn.commit()
    result = publication.recover(memory_repo, conn)
    assert "PUB-orphan0000000001" in result["discarded_prepared"]
    state = conn.execute("SELECT state FROM publication_batches WHERE "
                         "publication_id='PUB-orphan0000000001'").fetchone()[0]
    assert state == "rejected"
    conn.close()


def test_rollback_creates_new_publication_without_resurrecting_verified(memory_repo):
    mid = make_card(memory_repo, "回滚验证卡")
    conn = index.connect(memory_repo)
    apply_all(conn)
    base_rev = head_of(conn, mid)
    seed_evidence(conn, mid, base_rev, count=2)
    meta, body, _ = store.load(memory_repo, mid)
    proposal = changes.propose_change(
        conn, memory_repo, relation="ENRICH", risk="R1", scope="orders",
        base_heads=[{"memory_id": mid, "revision_algo": "content-v1",
                     "content_revision": base_rev}],
        outputs=[{"memory_id": mid, "disposition": "active", "meta": meta,
                  "body": body + "## 补充\n坏内容。\n"}],
        source_dispositions=[{"memory_id": mid, "action": "revise", "redirect_ids": []}],
        claim_map=[{"old_claim_id": "C-whole", "new_claim_ids": ["C-whole"],
                    "mapping": "preserved"}])
    validation.validate_change(conn, memory_repo, proposal["change_id"])
    first = publication.publish(memory_repo, conn, proposal["change_id"])
    assert first["local_published"]
    bad_rev = head_of(conn, mid)
    rolled = publication.rollback(memory_repo, conn, first["publication_id"],
                                  reason="反例：坏内容在目标环境回归失败")
    assert rolled.get("local_published"), rolled
    # head 指回旧版本语义；confidence=once（不复活旧 verified）。
    final_meta, final_body, _ = store.load(memory_repo, mid)
    assert "坏内容" not in final_body
    assert head_of(conn, mid) != bad_rev
    assert final_meta["confidence"] == "once"
    # 坏版本与两份 manifest 都保留（历史不抹）。
    assert conn.execute("SELECT COUNT(*) FROM knowledge_revisions WHERE memory_id=?",
                        (mid,)).fetchone()[0] >= 2
    conn.close()


def test_admission_authority_gate(memory_repo):
    """首次准入：staging 无需被采用即可评估；统一权威门只看共享范围——
    global 卡无权威确认 fail，项目内不再按类型要求。"""
    # 统一生命周期：global（共享范围）→ staging，准入需权威确认；类型不参与。
    std = writer.create(memory_repo, "standard", "结算标准", BODY, scope="global",
                        provenance="PRD-4")
    assert std["staging"] is True, std
    # 项目内新增带 provenance（有依据）→ 直接发布 active。
    pit = make_card(memory_repo, "pitfall 准入卡", provenance="PRD-PIT-1")
    conn = index.connect(memory_repo)
    apply_all(conn)
    # global 卡无 authority_ref → fail（多个 Agent 赞成也不行）。
    result = admission.evaluate_admission(
        conn, memory_repo, std["entity_id"], head_of(conn, std["entity_id"]))
    assert result["result"] == "fail"
    assert any(c["check"] == "authority_confirmation" and c["result"] == "fail"
               for c in result["checks"])
    # 带权威出处 → pass，且只代表 validated/once。
    confirmed = admission.authority_confirm(
        conn, memory_repo, "CH-x", std["entity_id"], head_of(conn, std["entity_id"]),
        authority_ref="需求 PRD-4 §2 负责人确认 2026-11-05")
    assert confirmed["result"] == "pass"
    # 项目内卡：结构与来源通过即 pass（低风险），无权威检查项、provenance
    # 缺失只记 unknown 不阻断。
    pitfall = admission.evaluate_admission(conn, memory_repo, pit,
                                           head_of(conn, pit))
    assert pitfall["result"] in ("pass", "unknown")
    assert all(c["check"] != "authority_confirmation" or c["result"] == "pass"
               for c in pitfall["checks"])
    # 待准入清单含 staging 卡（无需先被检索命中）；已激活 pitfall 不在列。
    pending = admission.pending_admissions(conn, memory_repo)
    ids = {p["memory_id"] for p in pending}
    assert std["entity_id"] in ids
    assert pit not in ids
    conn.close()
