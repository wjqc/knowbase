"""T4 隔离库全量验收：一张样例卡贯穿全生命周期（统一生命周期最终门）。

链路：提炼生成（capture→claim→submit）→ 待审 → 权威确认 → 发布 → 搜索
→ 使用/反馈 → 修订（旧证据不转嫁）→ 3 独立任务×2 独立组晋升 verified
→ 可信反证隔离（quarantine，退出默认检索、证据可追溯）→ 受控时钟归档。
回执可追到卡 ID、内容版本与证据；不以"入队成功"代替成卡。
"""

import io
import json
import re
import contextlib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from knowbase import __main__ as cli, config, index, jobs, materials, store, writer
from knowbase.knowledge_lifecycle import admission, attribution, changes, governance
from knowbase.knowledge_lifecycle import observation, publication, reconcile, validation
from knowbase.migrations import apply_all
from knowbase.mutations import _compute_revision
from knowbase.server import capture_impl, job_claim_impl, job_material_impl, job_submit_impl
from knowbase.server import read_impl, search_impl

BODY_OBJ = {
    "scope": "global",
    "conclusion": "T4 样例：结算状态变更前必须先读当前状态。",
    "problem": "直连 RPC 绕过前端校验时会出现非法撤回。",
    "applicable_when": "收到已结算订单的撤回请求。",
    "not_applicable_when": "草稿订单走单独流程。",
    "actionable_steps": "写状态前读取结算状态，已结算即拒绝。",
    "key_evidence": "T4-CONTROL §1 受控样本明确要求服务层校验。",
    "verification": "隔离库协议验收，业务回归另行安排。",
    "unknown_items": "真实审批约束未确认。",
}


def _cap(fn):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn()
    return buf.getvalue()


def _distill_sample(memory_repo):
    """显式批量整理生成样例卡（global bizrule + provenance → staging）。"""
    out = capture_impl(session_id="t4", scope="global",
                       messages=[{"role": "user",
                                  "text": "T4 受控样本：已结算订单的撤回必须在服务层拒绝并返回业务错误码。"}])
    job = re.search(r"J-[a-f0-9]+", out).group()
    claim = json.loads(job_claim_impl(job_id=job, host_session_id="t4-host",
                                      protocol_version=2))
    seg = claim["materials"][0]
    candidate = {"candidate_key": "t4-sample", "type": "bizrule",
                 "title": "已结算订单撤回服务层校验", "scope": "global",
                 "action": "create", "provenance": "PRD-T4 §1",
                 "rule_context": {"business_object": "订单", "trigger": "撤回请求",
                                  "required_or_forbidden": "已结算时禁止撤回",
                                  "exceptions": "草稿订单另行处理",
                                  "applicable_version": "T4 隔离样本 v1",
                                  "business_confirmation": "explicit_source",
                                  "implementation_status": "not_implemented"},
                 "segment_ids": [seg["segment_id"]],
                 "evidence_refs": [{"segment_id": seg["segment_id"],
                                    "text_hash": seg["text_hash"]}],
                 "body": BODY_OBJ}
    submitted = json.loads(job_submit_impl(
        job_id=job, batch_id=claim["batch_id"], lease_token=claim["lease_token"],
        fencing_token=claim["fencing_token"], request_id="t4-submit",
        protocol_version=2, candidates=[candidate],
        segment_results=[{"segment_id": seg["segment_id"], "decision": "distilled"}]))
    assert submitted["state"] == "needs_review", submitted  # 待审，不是入队即成卡
    mid = submitted["results"][0]["memory_id"]
    return job, mid, seg


def _trusted_l3(conn, repo, mid, revision, *, task, group, outcome="pass"):
    """注册验证器 + 任务身份 + 执行戳/执行记录 → trusted L3（统一绑定）。"""
    run = observation.ingest(conn, "zcode", f"s-{task}",
                             [{"native_key": f"n-{task}", "kind": "note", "payload": {}}],
                             project_key="repo:t4", native_task_key=task,
                             native_run_key=task)
    from lifecycle_verifier_fixture import run_verifier
    result, _ = run_verifier(conn, repo, mid, revision, run_id=run["run_id"],
                             validator_id="tv", group=group, case=task, outcome=outcome, project="repo:t4")
    assert result["outcome"] == outcome, result
    return result


def test_t4_sample_card_full_lifecycle(memory_repo):
    # 配置：注册验证器（CLI verify --format=pytest-text 与直接归因 tv）。
    cfg_path = Path(config.CONFIG_PATH)
    cfg = json.loads(cfg_path.read_text())
    cfg["knowledge_lifecycle"] = {**(cfg.get("knowledge_lifecycle") or {}),
                                  "validators": [{"validator_id": "pytest-text", "kind": "pytest"},
                                                 {"validator_id": "tv", "kind": "pytest"}]}
    cfg_path.write_text(json.dumps(cfg))
    conn = index.connect(memory_repo)
    apply_all(conn)
    conn.close()

    job, mid, seg = _distill_sample(memory_repo)
    # 回执可追溯：job_submissions → memory_id + 版本；候选 → 证据片段。
    conn = index.connect(memory_repo)
    apply_all(conn)
    receipt = json.loads(conn.execute(
        "SELECT result_json FROM job_submissions WHERE job_id=?", (job,)).fetchone()[0])
    assert receipt["results"][0]["memory_id"] == mid
    cand = conn.execute(
        "SELECT memory_id, memory_revision, payload_hash FROM candidates WHERE job_id=?",
        (job,)).fetchone()
    assert cand[0] == mid and cand[1] and cand[2]
    meta, body, path = store.load(memory_repo, mid)
    revision = _compute_revision(meta, body)
    assert path.parent.name == "staging"
    assert conn.execute("SELECT staging FROM meta WHERE id=?", (mid,)).fetchone()[0] == 1
    assert "默认检索" not in search_impl("已结算订单撤回服务层校验", scope="global")

    # 发布：global/R3 → 无权威确认 fail；确认后 validate pass → publish。
    change = changes.propose_new_card(conn, memory_repo, mid, proposer="t4")
    change_id = change["change_id"]
    report = validation.validate_change(conn, memory_repo, change_id)
    assert report["result"] == "fail", report
    assert change["risk"] == "R3"
    assert any(c["check"] == "risk_authority" and c["result"] == "fail"
               for c in report["checks"]), report
    confirmed = admission.authority_confirm(conn, memory_repo, change_id, mid,
                                            revision, "PRD-T4 §1 负责人确认记录")
    assert confirmed["result"] == "pass", confirmed
    report = validation.validate_change(conn, memory_repo, change_id)
    assert report["result"] == "pass", report
    published = publication.publish(memory_repo, conn, change_id)
    assert not published.get("error"), published
    meta, body, path = store.load(memory_repo, mid)
    assert meta["confidence"] == "once" and meta["status"] == "active"
    assert path.parent.name != "staging"
    revision = _compute_revision(meta, body)
    # 独立读回 + 搜索命中（发布 ≠ 只入队）。
    read_text = read_impl(mid)  # 普通阅读：只传 id
    assert "已结算" in read_text
    assert mid in search_impl("已结算订单撤回服务层校验", scope="global")
    conn.close()

    # 修订：受信 CLI revise；旧验证清零、降回 once（不转嫁旧证据）。
    conn = index.connect(memory_repo)
    apply_all(conn)
    _trusted_l3(conn, memory_repo, mid, revision, task="t4-pre", group="g0")
    conn.close()
    new_body = body + "\n补充：撤回错误码返回 ORDER_SETTLED。"
    body_file = Path(memory_repo).parent / "t4-revise.md"
    body_file.write_text(new_body, encoding="utf-8")
    assert cli.main(["revise", mid, "--body-file", str(body_file)]) == 0
    meta2, body2, _ = store.load(memory_repo, mid)
    assert meta2["confidence"] == "once", meta2  # 修订即降权
    assert meta2.get("verified_content_revision", "") != _compute_revision(meta2, body2)
    revision2 = _compute_revision(meta2, body2)
    assert revision2 != revision

    # 晋升：3 个独立任务 × 2 个独立组（g1: t4-a/t4-b，g2: t4-c）→ verified。
    conn = index.connect(memory_repo)
    apply_all(conn)
    for task, group in (("t4-a", "g1"), ("t4-b", "g1"), ("t4-c", "g2")):
        result = _trusted_l3(conn, memory_repo, mid, revision2, task=task, group=group)
        assert result["outcome"] == "pass"
    reconcile.materialize_and_recompute(memory_repo, conn)
    meta3, _, _ = store.load(memory_repo, mid)
    assert meta3["confidence"] == "verified", meta3
    assert meta3["verified_content_revision"] == revision2
    conn.close()

    # 反证隔离：可信 fail → quarantine，退出默认检索，证据保留可追溯。
    conn = index.connect(memory_repo)
    apply_all(conn)
    _trusted_l3(conn, memory_repo, mid, revision2, task="t4-neg", group="g1",
                outcome="fail")
    decision = governance.apply_decision(memory_repo, conn, mid)
    assert decision["decision"] == "quarantine", decision
    meta4, _, _ = store.load(memory_repo, mid)
    assert meta4["status"] == "stale" and meta4.get("quarantine_reason")
    assert mid not in search_impl("已结算订单撤回服务层校验", scope="global")
    n_events = conn.execute(
        "SELECT COUNT(*) FROM evidence_events WHERE subject_revision=?",
        (f"{mid}@content-v1:{revision2}",)).fetchone()[0]
    assert n_events >= 4  # 晋升 3 + 反证 1，证据账本完整（含隔离后保留）
    conn.close()

    # 归档时钟：受控时钟 stale 满 90 天且无成功复验证据 → 归档，仍可追溯。
    conn = index.connect(memory_repo)
    apply_all(conn)
    stale_since = (date.today() - timedelta(days=91)).isoformat()
    meta4["stale_since"] = stale_since
    meta4["updated"] = stale_since
    path4 = store.find_file(memory_repo, mid)
    store.write_shared_text(path4, store.render(meta4, body2))
    index.upsert(conn, meta4, body2, path4)
    conn.commit()
    from knowbase import lifecycle
    archived = lifecycle.auto_archive_sweep(memory_repo, conn, today=date.today())
    assert any(item.get("id") == mid for item in archived), archived
    meta5, body5, path5 = store.load(memory_repo, mid)
    assert meta5["status"] == "archived" and meta5.get("archived_at")
    assert "已结算" in body5  # 正文保留
    assert mid not in search_impl("已结算订单撤回服务层校验", scope="global")
    conn.close()
