"""Path B 第一批（Hook 实时成卡）回归测试；不调真实模型、不碰真实库。

覆盖：P0-1 检查点后短时成卡（不走提炼）、P0-2 重复链接/补充更新/
标题查重改道/verified 目标改走 review、超预算进 retry_wait、无执行器
blocked、hook_capture 标记选择与 worker 接线、quarantine/verified 未触动。
模型调用一律用 monkeypatch 的 execute_text 桩代替；桩从提示词的
RELATED_KNOWLEDGE_DATA 块里读相似卡快照，与真实模型看到的输入一致。
"""
import json
import re

import pytest

from knowbase import config, index, materials, store
from knowbase.automation import distillation, execution, hook_capture, worker
from knowbase.automation.hook_capture import normalize_hook_candidates, run_hook_capture
from knowbase.mutations import _compute_revision
from knowbase.adapters.host_events import capture_increment
from knowbase.server import capture_impl
from test_p2_autonomous import setup_executor


def _capture(repo, text, scope="probe"):
    from uuid import uuid4

    sid = uuid4().hex
    transcript = repo.parent / f"{sid}.jsonl"
    transcript.write_text(json.dumps({"role": "user", "text": text}), encoding="utf-8")
    conn = index.connect(repo)
    try:
        conn.execute("INSERT INTO context_sessions(host_profile,session_id,scope,updated_at) VALUES(?,?,?,?)",
                     ("zcode", sid, scope, distillation.now()))
        conn.commit()
    finally:
        conn.close()
    return capture_increment(repo, sid, str(transcript), scope, "zcode")["job_ids"][0]


def _job_state(repo, job_id):
    conn = index.connect(repo)
    try:
        return conn.execute("SELECT state FROM jobs WHERE job_id=?", (job_id,)).fetchone()[0]
    finally:
        conn.close()


def _card_count(repo):
    conn = index.connect(repo)
    try:
        return conn.execute("SELECT COUNT(*) FROM meta").fetchone()[0]
    finally:
        conn.close()


def _related_from_text(text):
    marker = "[RELATED_KNOWLEDGE_DATA]\n"
    if marker not in text:
        return {"similarity_snapshot": "", "similar_cards": []}
    return json.loads(text.split(marker, 1)[1])


def _body(conclusion="合成结论：下单时用订单号加幂等键去重。"):
    return {"conclusion": conclusion,
            "problem": "问题：重复提交会产生重复订单。",
            "applicable_when": "下单接口重复调用时",
            "not_applicable_when": "订单号本身不唯一时",
            "actionable_steps": ["第一步：下单时传订单号加幂等键", "第二步：重复提交直接返回首次结果"],
            "key_evidence": ["最终规则：下单时用订单号加幂等键去重。"],
            "verification": "重复提交两次，只产生一单。",
            "unknown_items": "暂无未知项。"}


def _candidate(*, action, segments, scope, target=None, snapshot="",
               evaluated=(), title="订单去重用幂等键", key="cand-1",
               conclusion=None):
    cand = {"candidate_key": key, "type": "pitfall", "title": title, "scope": scope,
            "action": action,
            "segment_ids": [s["segment_id"] for s in segments],
            "evidence_refs": [{"segment_id": s["segment_id"], "text_hash": s["text_hash"]}
                              for s in segments],
            "body": _body(conclusion) if conclusion else _body(),
            "duplicate_evaluation": {
                "snapshot_hash": snapshot,
                "evaluated_cards": [{"id": c["id"], "content_revision": c["content_revision"]}
                                    for c in evaluated],
                "rationale": "桩判定"}}
    if target is not None:
        cand["target_id"] = target["id"]
        cand["expected_content_revision"] = target.get("content_revision", "")
        if target.get("governance_revision"):
            cand["expected_governance_revision"] = target["governance_revision"]
    return cand


def _segment_results(segments, decision="distilled", reason=""):
    out = []
    for s in segments:
        item = {"segment_id": s["segment_id"], "decision": decision}
        if reason:
            item["reason"] = reason
        out.append(item)
    return out


def _stub_response(build):
    """build(options, request, text, segments) -> (candidates, segment_results)。"""
    def stub(options, request, text, segments, policy, instructions=None):
        related = _related_from_text(text)
        candidates, results = build(options, request, text, segments, related)
        return {"candidates": candidates, "segment_results": results}
    return stub


def _seed_card(repo, title, scope="probe"):
    from knowbase import writer
    from knowbase.extraction.validator import build_card_body
    body = build_card_body({"type": "pitfall", "title": title, "body": _body()})
    created = writer.create(repo, "pitfall", title, body, scope=scope,
                            request_id="seed-" + title[:8], push=False,
                            source="agent:knowbase:test-seed")
    assert not created.get("error"), created
    return created["entity_id"]


def _mark_verified(repo, mid):
    meta, body, path = store.load(repo, mid)
    meta["confidence"] = "verified"
    meta["verified_content_revision"] = _compute_revision(meta, body)
    path.write_text(store.render(meta, body), encoding="utf-8")
    conn = index.connect(repo)
    try:
        index.upsert(conn, *store.load(repo, mid), commit=False)
        conn.commit()
    finally:
        conn.close()


CREATE_TEXT = ("订单去重用幂等键：最终规则：下单时用订单号加幂等键去重，"
               "重复提交直接返回首次结果，不产生重复订单。")


def test_hook_capture_creates_card_without_distillation(memory_repo, monkeypatch):
    """P0-1：检查点入队后，Hook 路径直接成卡，不调用 run_due（提炼）。"""
    cfg, _ = setup_executor(monkeypatch)
    job = _capture(memory_repo, CREATE_TEXT)

    def build(options, request, text, segments, related):
        cand = _candidate(action="create", segments=segments, scope="probe",
                          snapshot=related["similarity_snapshot"],
                          evaluated=related["similar_cards"])
        return [cand], _segment_results(segments)
    monkeypatch.setattr(execution, "execute_text", _stub_response(build))

    result = run_hook_capture(memory_repo, cfg)
    assert result.get("processed") == 1, result
    assert _job_state(memory_repo, job) == "succeeded"
    assert _card_count(memory_repo) == 1
    conn = index.connect(memory_repo)
    try:
        meta = conn.execute("SELECT title,confidence FROM meta").fetchone()
        assert meta[0] == "订单去重用幂等键" and meta[1] == "once"
    finally:
        conn.close()


def test_hook_duplicate_links_existing_no_new_card(memory_repo, monkeypatch):
    """P0-2 重复：模型判 link_existing，旧卡现读版本号修复后链接成功，不产生新卡。"""
    cfg, _ = setup_executor(monkeypatch)
    mid = _seed_card(memory_repo, "订单去重用幂等键")
    job = _capture(memory_repo, CREATE_TEXT)

    def build(options, request, text, segments, related):
        assert related["similar_cards"], "相似卡应含已建卡"
        target = dict(related["similar_cards"][0])
        target["content_revision"] = "stale-revision-value"  # 桩故意给错版本
        cand = _candidate(action="link_existing", segments=segments, scope="probe",
                          target=target, snapshot=related["similarity_snapshot"],
                          evaluated=related["similar_cards"])
        return [cand], _segment_results(segments)
    monkeypatch.setattr(execution, "execute_text", _stub_response(build))

    result = run_hook_capture(memory_repo, cfg)
    assert result.get("processed") == 1, result
    assert _job_state(memory_repo, job) == "succeeded"
    assert _card_count(memory_repo) == 1  # 无新卡
    conn = index.connect(memory_repo)
    try:
        row = conn.execute("SELECT materialization_state,memory_id FROM candidates "
                           "WHERE job_id=?", (job,)).fetchone()
        assert row[0] == "linked_existing" and row[1] == mid
    finally:
        conn.close()


def test_conflicting_titles_go_to_review(memory_repo, monkeypatch):
    cfg, _ = setup_executor(monkeypatch)
    _seed_card(memory_repo, "审批单可在提交后撤回")
    job = _capture(memory_repo, "业务规则：审批单不可在提交后撤回。")

    def build(options, request, text, segments, related):
        cand = _candidate(action="create", segments=segments, scope="probe",
                          title="审批单不可在提交后撤回",
                          conclusion="审批单提交后禁止撤回。",
                          snapshot=related["similarity_snapshot"],
                          evaluated=related["similar_cards"])
        return [cand], _segment_results(segments)
    monkeypatch.setattr(execution, "execute_text", _stub_response(build))

    assert run_hook_capture(memory_repo, cfg).get("processed") == 1
    assert _job_state(memory_repo, job) == "needs_review"
    assert _card_count(memory_repo) == 1
    conn = index.connect(memory_repo)
    try:
        row = conn.execute("SELECT materialization_state,memory_id FROM candidates "
                           "WHERE job_id=?", (job,)).fetchone()
        assert tuple(row) == ("needs_review", "")
    finally:
        conn.close()


def test_hook_supplement_proposes_review_without_overwriting_card(memory_repo, monkeypatch):
    """P0-2 补充：模型判 update，旧卡内容被增量更新，仍只有一张卡。"""
    cfg, _ = setup_executor(monkeypatch)
    mid = _seed_card(memory_repo, "订单去重用幂等键")
    job = _capture(memory_repo, "订单去重用幂等键补充：最终规则：幂等键有效期 24 小时，"
                   "过期后同一订单号可重新下单。")

    def build(options, request, text, segments, related):
        assert related["similar_cards"]
        target = related["similar_cards"][0]
        cand = _candidate(action="update", segments=segments, scope="probe",
                          target=target, snapshot=related["similarity_snapshot"],
                          evaluated=related["similar_cards"],
                          conclusion="合成结论：下单时用订单号加幂等键去重；幂等键有效期 24 小时。")
        return [cand], _segment_results(segments)
    monkeypatch.setattr(execution, "execute_text", _stub_response(build))

    assert run_hook_capture(memory_repo, cfg).get("processed") == 1
    assert _job_state(memory_repo, job) == "needs_review"
    assert _card_count(memory_repo) == 1
    meta, body, _ = store.load(memory_repo, mid)
    assert "24 小时" not in body
    assert meta.get("confidence") == "once"
    conn = index.connect(memory_repo)
    try:
        row = conn.execute("SELECT materialization_state FROM candidates WHERE job_id=?",
                           (job,)).fetchone()
        assert row[0] == "needs_review"
    finally:
        conn.close()


def test_hook_supplement_to_verified_card_goes_to_review(memory_repo, monkeypatch):
    """verified 红线：补充 verified 卡时自动路径不改版，改走 review，卡原样不动。"""
    cfg, _ = setup_executor(monkeypatch)
    mid = _seed_card(memory_repo, "订单去重用幂等键")
    _mark_verified(memory_repo, mid)
    before = store.load(memory_repo, mid)[1]
    job = _capture(memory_repo, "订单去重用幂等键补充：最终规则：幂等键有效期 24 小时。")

    def build(options, request, text, segments, related):
        assert related["similar_cards"]
        target = related["similar_cards"][0]
        cand = _candidate(action="update", segments=segments, scope="probe",
                          target=target, snapshot=related["similarity_snapshot"],
                          evaluated=related["similar_cards"],
                          conclusion="合成结论：下单时用订单号加幂等键去重；幂等键有效期 24 小时。")
        return [cand], _segment_results(segments)
    monkeypatch.setattr(execution, "execute_text", _stub_response(build))

    assert run_hook_capture(memory_repo, cfg).get("processed") == 1
    assert _job_state(memory_repo, job) == "needs_review"
    assert _card_count(memory_repo) == 1
    meta, body, _ = store.load(memory_repo, mid)
    assert meta.get("confidence") == "verified"
    assert body == before  # 内容未被改动
    conn = index.connect(memory_repo)
    try:
        row = conn.execute("SELECT materialization_state FROM candidates WHERE job_id=?",
                           (job,)).fetchone()
        assert row[0] == "needs_review"
    finally:
        conn.close()


def test_hook_budget_exhausted_goes_retry_wait(memory_repo, monkeypatch):
    """超预算进 retry_wait（不扣 attempt、不调模型），不静默丢弃。"""
    cfg, _ = setup_executor(monkeypatch)
    cfg["automation"]["daily_calls"] = 0
    job = _capture(memory_repo, CREATE_TEXT)
    monkeypatch.setattr(execution, "execute_text",
                        lambda *a, **k: pytest.fail("超预算时不得调模型"))

    result = run_hook_capture(memory_repo, cfg)
    assert result["results"][0]["error"] == "daily_budget_exhausted", result
    conn = index.connect(memory_repo)
    try:
        row = conn.execute("SELECT state,attempt,next_run_at FROM jobs WHERE job_id=?",
                           (job,)).fetchone()
        assert row[0] == "retry_wait" and row[1] == 0 and row[2] > distillation.now()
    finally:
        conn.close()


def test_hook_no_executor_blocked(memory_repo):
    """无已验证执行器时 blocked，任务保持 awaiting_agent（不探针、不认领）。"""
    job = _capture(memory_repo, CREATE_TEXT)
    assert run_hook_capture(memory_repo)["blocked"] == "automation_disabled"
    assert _job_state(memory_repo, job) == "awaiting_agent"


def test_hook_flag_selection_and_worker_wiring(memory_repo, monkeypatch):
    """标记选择：普通提炼只取显式作业，Hook 只取检查点作业与到期重试。"""
    cfg, _ = setup_executor(monkeypatch)
    flagged = _capture(memory_repo, CREATE_TEXT)
    conn = index.connect(memory_repo)
    try:
        row = conn.execute("SELECT json_extract(input_json,'$.hook_capture') FROM job_spool "
                           "WHERE job_id=?", (flagged,)).fetchone()
        assert row[0] == 1
    finally:
        conn.close()
    unflagged = re.search(r"J-[a-f0-9]+", capture_impl(
        session_id="explicit-fixture", scope="probe",
        messages=[{"role": "user", "text": "显式材料：最终规则：X。"}])).group()

    assert "hook_capture" in worker.TASK_INTERVALS
    assert worker.TASK_INTERVALS["hook_capture"] == 10
    assert worker._HANDLERS["hook_capture"] is worker._h_hook_capture
    def no_value(options, request, text, segments, related):
        assert request.job_id == unflagged
        return [], _segment_results(segments, decision="no_value", reason="测试材料")
    monkeypatch.setattr(execution, "execute_text", _stub_response(no_value))
    assert distillation.run_due(memory_repo, cfg)["processed"] == 1
    assert _job_state(memory_repo, unflagged) == "no_candidate"
    assert _job_state(memory_repo, flagged) == "awaiting_agent"

    # 只处理带标记任务：未标记任务保持 awaiting_agent；标记任务因预算走 retry_wait。
    monkeypatch.setattr(execution, "execute_text",
                        lambda *a, **k: pytest.fail("超预算时不得调模型"))
    cfg["automation"]["daily_calls"] = 0  # 标记任务也因预算跳过，不调模型
    out = run_hook_capture(memory_repo, cfg)
    assert out["processed"] == 1 and out["results"][0]["error"] == "daily_budget_exhausted", out
    assert _job_state(memory_repo, unflagged) == "no_candidate"
    assert _job_state(memory_repo, flagged) == "retry_wait"

    # worker handler 直调：有执行器且放行租约时处理标记任务。
    cfg["automation"]["daily_calls"] = 20
    conn = index.connect(memory_repo)
    try:
        conn.execute("UPDATE jobs SET next_run_at=? WHERE job_id=?", (distillation.now(), flagged))
        conn.commit()
    finally:
        conn.close()

    def build(options, request, text, segments, related):
        cand = _candidate(action="create", segments=segments, scope="probe",
                          snapshot=related["similarity_snapshot"],
                          evaluated=related["similar_cards"])
        return [cand], _segment_results(segments)
    monkeypatch.setattr(execution, "execute_text", _stub_response(build))
    conn = index.connect(memory_repo)
    try:
        out = worker._h_hook_capture(memory_repo, conn, {**cfg, "_lease_valid": lambda: True})
    finally:
        conn.close()
    assert out.get("processed") == 1, out
    assert _job_state(memory_repo, flagged) == "succeeded"


def test_hook_capture_leaves_quarantine_and_verified_untouched(memory_repo, monkeypatch):
    """治理隔离：Hook 成卡/链接/更新不产生证据事件、不动 quarantine、不晋升 verified。"""
    cfg, _ = setup_executor(monkeypatch)
    mid = _seed_card(memory_repo, "订单去重用幂等键")
    create_job = _capture(memory_repo, "退款原路返回：最终规则：退款必须原路返回，不可改道。")

    calls = {"n": 0}

    def build_create(options, request, text, segments, related):
        calls["n"] += 1
        cand = _candidate(action="create", segments=segments, scope="probe",
                          title="退款原路返回",
                          snapshot=related["similarity_snapshot"],
                          evaluated=related["similar_cards"])
        return [cand], _segment_results(segments)
    monkeypatch.setattr(execution, "execute_text", _stub_response(build_create))
    assert run_hook_capture(memory_repo, cfg).get("processed") == 1
    assert _job_state(memory_repo, create_job) == "succeeded"

    link_job = _capture(memory_repo, CREATE_TEXT)

    def build_link(options, request, text, segments, related):
        calls["n"] += 1
        target = related["similar_cards"][0]
        cand = _candidate(action="link_existing", segments=segments, scope="probe",
                          target=target, snapshot=related["similarity_snapshot"],
                          evaluated=related["similar_cards"])
        return [cand], _segment_results(segments)
    monkeypatch.setattr(execution, "execute_text", _stub_response(build_link))
    assert run_hook_capture(memory_repo, cfg).get("processed") == 1
    assert _job_state(memory_repo, link_job) == "succeeded"

    conn = index.connect(memory_repo)
    try:
        assert conn.execute("SELECT COUNT(*) FROM evidence_events").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM card_conflicts").fetchone()[0] == 0
        confidences = {r[0] for r in conn.execute("SELECT confidence FROM meta").fetchall()}
        assert "verified" not in confidences, confidences
    finally:
        conn.close()
    for card_id in (mid,):
        meta, _, _ = store.load(memory_repo, card_id)
        assert meta.get("quality_state") is None  # 治理投影未被触动
    created = [r for r in store.iter_all(memory_repo) if r[0].get("title") == "退款原路返回"]
    assert created and created[0][0].get("confidence") == "once"
    assert calls["n"] == 2
