"""W1-6：staging 卡自动提案（lifecycle propose / propose-admitted）。

覆盖：NEW 立案 + 确定性 change_id 幂等；准入通过的 staging 卡 --publish 全链发布
（正式文件落盘、staging 源删除、search 可见）；统一生命周期下权威门只看共享
范围——global 卡无权威确认时停在 proposed，确认后可发布；立案后改 staging 源
→ 发布 SOURCE_MOVED 且源文件保留。
staging 路由按统一规则制造：项目内新增去掉 provenance（来源不足）或 scope="global"。
"""
from pathlib import Path

from knowbase import __main__ as cli
from knowbase import index, store, writer
from knowbase.knowledge_lifecycle import admission, changes, publication, validation
from knowbase.migrations import apply_all
from knowbase.server import search_impl

BODY = ("## 结论\nstaging 卡首次发布走 NEW 提案。\n## 解决的问题\nx。\n"
        "## 适用条件\ny。\n## 不适用条件\nz。\n## 可执行动作\n运行 scripts/a.py。\n"
        "## 关键证据\nPR。\n## 验证情况\nv。\n## 未知与待确认\n无。\n")


def head_of(conn, mid):
    return conn.execute("SELECT content_revision FROM entity_heads WHERE entity_id=?",
                        (mid,)).fetchone()[0]


def test_propose_new_card_idempotent(memory_repo):
    std = writer.create(memory_repo, "standard", "结算标准", BODY, scope="orders",
                        provenance="PRD-4")
    assert not std.get("error"), std
    mid = std["entity_id"]
    conn = index.connect(memory_repo)
    apply_all(conn)
    p1 = changes.propose_new_card(conn, memory_repo, mid)
    assert not p1.get("error"), p1
    assert p1["relation"] == "NEW" and p1["state"] == "proposed"
    assert p1["change_id"].startswith("CHG-NEW-")
    got = changes.get_change(conn, p1["change_id"])
    assert got["base_heads"] == [] and len(got["outputs"]) == 1
    assert got["outputs"][0]["disposition"] == "active"
    p2 = changes.propose_new_card(conn, memory_repo, mid)
    assert p2.get("idempotent") is True and p2["change_id"] == p1["change_id"]
    assert changes.propose_new_card(
        conn, memory_repo, "P-000000000000").get("error") == "NOT_FOUND"
    conn.close()


def test_propose_admitted_publishes_staging_bizrule(memory_repo):
    # 统一生命周期：bizrule 不再按类型进 staging；项目内新增去掉 provenance
    # （来源不足）按统一规则落 staging，走提案管道发布。
    biz = writer.create(memory_repo, "bizrule", "折扣审批规则", BODY, scope="orders")
    assert not biz.get("error"), biz
    assert biz["staging"] is True, biz
    mid = biz["entity_id"]
    assert writer.update(memory_repo, mid, provenance="测试依据 PRD-4")["staging"]
    conn = index.connect(memory_repo)
    apply_all(conn)
    staging_path = Path(memory_repo) / "staging" / f"{mid}.md"
    assert staging_path.exists()
    report = changes.propose_admitted(conn, memory_repo, publish=True)
    assert report["published"] and report["published"][0]["memory_id"] == mid, report
    assert not report["failed"] and not report["skipped"]
    # 正式文件落盘、staging 源删除、search 可见、confidence once。
    formal = Path(memory_repo) / "bizrules" / f"{mid}.md"
    assert formal.exists() and not staging_path.exists()
    meta, _body, _ = store.load(memory_repo, mid)
    assert meta["confidence"] == "once"
    assert mid in search_impl("折扣审批规则", scope="orders")
    # 待准入清单不再列出。
    assert mid not in {p["memory_id"]
                       for p in admission.pending_admissions(conn, memory_repo)}
    # 重跑：已发布，幂等（不重复发布、不报错）。
    again = changes.propose_admitted(conn, memory_repo, publish=True)
    assert again["published"] == [] and not again["failed"]
    conn.close()


def test_propose_admitted_standard_needs_authority_then_publishes(memory_repo):
    # 统一生命周期：权威门只看共享范围——global 卡（不按类型）无权威确认时
    # 准入 fail，停在 proposed 等待 authority_confirm。
    std = writer.create(memory_repo, "standard", "结算标准", BODY, scope="global",
                        provenance="PRD-4")
    assert not std.get("error"), std
    assert std["staging"] is True, std
    mid = std["entity_id"]
    conn = index.connect(memory_repo)
    apply_all(conn)
    report = changes.propose_admitted(conn, memory_repo, publish=True)
    assert len(report["skipped"]) == 1
    assert report["skipped"][0]["reason"] == "admission_not_passed"
    change_id = report["skipped"][0]["change_id"]
    assert changes.get_change(conn, change_id)["state"] == "proposed"
    assert (Path(memory_repo) / "staging" / f"{mid}.md").exists()
    # 权威确认后重跑 → 发布。
    confirmed = admission.authority_confirm(
        conn, memory_repo, change_id, mid, head_of(conn, mid),
        authority_ref="需求 PRD-4 §2 负责人确认 2026-11-05")
    assert confirmed["result"] == "pass", confirmed
    report2 = changes.propose_admitted(conn, memory_repo, publish=True)
    assert len(report2["published"]) == 1, report2
    assert not (Path(memory_repo) / "staging" / f"{mid}.md").exists()
    conn.close()


def test_publish_new_rejects_moved_staging_source(memory_repo):
    # 统一生命周期：项目内新增去掉 provenance（来源不足）按统一规则落 staging。
    biz = writer.create(memory_repo, "bizrule", "折扣审批规则", BODY, scope="orders")
    assert not biz.get("error"), biz
    assert biz["staging"] is True, biz
    mid = biz["entity_id"]
    assert writer.update(memory_repo, mid, provenance="测试依据 PRD-4")["staging"]
    conn = index.connect(memory_repo)
    apply_all(conn)
    proposal = changes.propose_new_card(conn, memory_repo, mid)
    assert not proposal.get("error"), proposal
    # 立案后改 staging 源。
    staging_path = Path(memory_repo) / "staging" / f"{mid}.md"
    meta, body, _ = store.load(memory_repo, mid)
    edited = body.replace("staging 卡首次发布走 NEW 提案。",
                          "staging 卡首次发布走 NEW 提案（已改）。")
    assert edited != body
    staging_path.write_text(store.render(meta, edited), encoding="utf-8")
    # 验证仍通过（冻结输出未动），发布被 SOURCE_MOVED 拦截。
    check = validation.validate_change(conn, memory_repo, proposal["change_id"])
    assert check["result"] == "pass", check
    result = publication.publish(memory_repo, conn, proposal["change_id"])
    assert result.get("error") == "SOURCE_MOVED", result
    assert changes.get_change(conn, proposal["change_id"])["state"] == "needs_rebase"
    # 源文件保留新编辑，正式文件未产生。
    assert "已改" in store.load(memory_repo, mid)[1]
    assert not (Path(memory_repo) / "bizrules" / f"{mid}.md").exists()
    conn.close()


def test_cli_lifecycle_propose_and_propose_admitted(memory_repo):
    # 统一生命周期：项目内新增去 provenance 落 staging，CLI 管道全链发布。
    biz = writer.create(memory_repo, "bizrule", "折扣审批规则", BODY, scope="orders")
    assert not biz.get("error"), biz
    assert biz["staging"] is True, biz
    mid = biz["entity_id"]
    assert writer.update(memory_repo, mid, provenance="测试依据 PRD-4")["staging"]
    assert cli.main(["lifecycle", "propose", mid]) == 0
    assert cli.main(["lifecycle", "propose-admitted", "--publish"]) == 0
    assert not (Path(memory_repo) / "staging" / f"{mid}.md").exists()
    assert (Path(memory_repo) / "bizrules" / f"{mid}.md").exists()
