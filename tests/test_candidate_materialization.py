"""从正式 capture/claim/submit 到文件、索引及默认检索的独立读回。"""

import copy
import json
import re
from datetime import datetime, timedelta, timezone

from knowbase import index, jobs, store, writer
from knowbase.server import capture_impl, job_claim_impl, job_submit_impl, job_status_impl


def prepare(text="用户明确：订单已结算后禁止撤回，草稿订单走其他流程。", scope="orders"):
    out = capture_impl(session_id="s1", scope=scope, messages=[{"role": "user", "text": text}])
    job = re.search(r"J-[a-f0-9]+", out).group()
    claim = json.loads(job_claim_impl(job_id=job, host_session_id="host1", protocol_version=2))
    return job, claim


def candidate(claim, dtype="bizrule", key="order-rule"):
    return {"candidate_key": key, "type": dtype, "title": "已结算订单禁止撤回的业务约束",
            "scope": "orders", "action": "create", "provenance": "用户需求确认 2026-10-21，订单状态条款",
            "body": {
                "scope": "orders", "conclusion": "已结算订单禁止撤回。",
                "problem": "避免结算后通过撤回改变财务相关的订单状态。",
                "applicable_when": "订单状态为已结算且用户请求撤回订单。",
                "not_applicable_when": "草稿订单使用单独的状态处理流程。",
                "actionable_steps": "读取订单状态，已结算时拒绝撤回并返回业务错误。",
                "key_evidence": "用户明确给出结算后的撤回限制，并说明草稿使用其他流程。",
                "verification": "本次需求澄清确认了结算状态限制；实现与集成回归尚未进行。",
                "unknown_items": "跨系统退款与退单规则尚未确认。"},
            "rule_context": {"business_object": "订单", "trigger": "请求撤回",
                "required_or_forbidden": "已结算时禁止撤回", "exceptions": "草稿订单另行处理",
                "applicable_version": "本次确认的需求版本", "business_confirmation": "explicit_source",
                "implementation_status": "not_implemented"},
            "segment_ids": [m["segment_id"] for m in claim["materials"]],
            "evidence_refs": [{"segment_id": m["segment_id"], "text_hash": m["text_hash"]}
                              for m in claim["materials"]]}


def submit(job, claim, candidates, request="req-1", decisions=None):
    if decisions is None:
        decisions = [{"segment_id": sid, "decision": "distilled"}
                     for c in candidates for sid in c["segment_ids"]]
    return json.loads(job_submit_impl(job_id=job, batch_id=claim["batch_id"],
        lease_token=claim["lease_token"], fencing_token=claim["fencing_token"], request_id=request,
        candidates=candidates, segment_results=decisions, protocol_version=2))


def test_business_rule_becomes_staging_file_not_just_database_candidate(memory_repo):
    job, claim = prepare()
    out = submit(job, claim, [candidate(claim)])
    assert out["failed"] == 0, out
    assert out["saved_staging"] == 1 and out["saved_active"] == 0
    assert out["state"] == "needs_review"
    mid = out["results"][0]["memory_id"]
    meta, body, path = store.load(memory_repo, mid)
    assert path.parent.name == "staging"
    assert meta["type"] == "bizrule" and "用户需求确认" in meta["provenance"]
    assert meta["rule_context"]["implementation_status"] == "not_implemented"
    assert "已结算订单禁止撤回" in body
    conn = index.connect(memory_repo)
    assert not index.search(conn, "订单 撤回", scope="orders")
    assert conn.execute("SELECT COUNT(*) FROM meta WHERE id=? AND staging=1", (mid,)).fetchone()[0] == 1
    conn.close()


def test_top_level_scope_is_sufficient_and_conflicting_body_scope_is_rejected(memory_repo):
    job,claim=prepare()
    c=candidate(claim)
    c["body"]["scope"]="another-project"
    wrong=submit(job,claim,[c],request="wrong-body-scope")
    assert wrong["error"]=="VALIDATION_FAILED"
    assert "SCOPE_MISMATCH" in json.dumps(wrong)
    del c["body"]["scope"]
    out=submit(job,claim,[c],request="canonical-scope")
    assert out["saved_staging"]==1 and out["failed"]==0,out
    meta,_,path=store.load(memory_repo,out["results"][0]["memory_id"])
    assert meta["scope"]=="orders" and path.parent.name=="staging"


def test_new_staging_card_can_be_read_in_task_without_becoming_adoptable(memory_repo):
    from knowbase.server import read_impl,context_impl,use_impl
    job,claim=prepare()
    result=submit(job,claim,[candidate(claim)])
    mid=result["results"][0]["memory_id"]
    ctx=json.loads(context_impl(session_id="reader",event_id="lookup",scope="orders",
        current_goal="订单结算",host_profile="native-test"))
    assert not ctx["candidates"]
    text=read_impl(mid,session_id="reader",event_id="read-new",context_event_id="lookup",host_profile="native-test")
    assert "read_use_id=" in text and "已结算订单禁止撤回" in text
    from knowbase.mutations import _compute_revision
    meta,body,path=store.load(memory_repo,mid)
    use=json.loads(use_impl(session_id="reader",event_id="adopt-new",context_event_id="lookup",id=mid,
        content_revision=_compute_revision(meta,body),state="adopted",host_profile="native-test"))
    assert use["error"]=="CANDIDATE_NOT_DELIVERED" and path.parent.name=="staging"
    context_impl(session_id="other-scope",event_id="lookup",scope="unrelated",
                 current_goal="其他任务",host_profile="native-test")
    assert "SCOPE_MISMATCH" in read_impl(mid,session_id="other-scope",event_id="read-wrong-scope",
        context_event_id="lookup",host_profile="native-test")


def test_technical_experience_becomes_searchable_and_replays_after_lease_ends(memory_repo):
    job, claim = prepare()
    c = candidate(claim, dtype="pitfall")
    out = submit(job, claim, [c])
    assert out["saved_active"] == 1 and out["state"] == "succeeded", out
    assert submit(job, claim, [c]) == out
    conn = index.connect(memory_repo)
    assert len(index.search(conn, "已结算 订单 撤回", scope="orders")) == 1
    assert conn.execute("SELECT COUNT(*) FROM candidates WHERE job_id=?", (job,)).fetchone()[0] == 1
    conn.close()
    changed = copy.deepcopy(c)
    changed["title"] = "不同规则"
    assert submit(job, claim, [changed])["error"] == "IDEMPOTENCY_CONFLICT"


def test_no_candidate_is_an_explicit_complete_segment_decision(memory_repo):
    job, claim = prepare("这是一段一次性进度通知，本阶段没有可复用结论。")
    decisions = [{"segment_id": m["segment_id"], "decision": "no_value", "reason": "一次性进度，不包含规则或可复用经验"}
                 for m in claim["materials"]]
    out = submit(job, claim, [], decisions=decisions)
    assert out["state"] == "no_candidate" and out["pending_segments"] == 0
    assert submit(job, claim, [], decisions=decisions) == out


def test_invalid_candidate_does_not_advance_coverage(memory_repo):
    job, claim = prepare()
    c = candidate(claim)
    del c["provenance"]
    out = submit(job, claim, [c])
    assert out["error"] == "VALIDATION_FAILED"
    conn = index.connect(memory_repo)
    assert jobs.status(conn, job)["cursor"] == 0
    assert conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 0
    conn.close()


def test_evidence_hash_and_cross_project_candidate_are_rejected(memory_repo):
    job, claim = prepare()
    c = candidate(claim)
    c["scope"] = "another-project"
    c["evidence_refs"][0]["text_hash"] = "forged"
    out = submit(job, claim, [c])
    assert out["error"] == "VALIDATION_FAILED"
    errors = out["errors"][0]["errors"]
    assert "SCOPE_MISMATCH" in errors and "EVIDENCE_MISMATCH" in errors


def test_expired_submission_rejected_without_other_host_takeover(memory_repo):
    job, claim = prepare()
    conn = index.connect(memory_repo)
    conn.execute("UPDATE jobs SET lease_until=? WHERE job_id=?",
                 ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), job))
    conn.commit()
    conn.close()
    assert submit(job, claim, [candidate(claim)])["error"] == "LEASE_EXPIRED"


def test_transient_write_failure_recovers_via_status_without_duplicate(memory_repo, monkeypatch):
    job, claim = prepare()
    original = writer.create
    def dropped_response(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError("simulate lost response after local card saved")
    monkeypatch.setattr(writer, "create", dropped_response)
    out = submit(job, claim, [candidate(claim)])
    assert out["state"] == "validating" and out["failed"] == 1
    monkeypatch.setattr(writer, "create", original)
    monkeypatch.setenv("KNOWBASE_AGENT_NAME", "another-recovery-host")
    status = json.loads(job_status_impl(job, protocol_version=2))
    assert status["state"] == "needs_review", status
    assert len(list(store.iter_all(memory_repo, include_staging=True))) == 1
    recovered = submit(job, claim, [candidate(claim)])
    assert recovered["saved_staging"] == 1 and recovered["failed"] == 0


def test_unconfirmed_rule_can_be_retained_for_review_without_fake_provenance(memory_repo):
    job, claim = prepare("代码似乎在结算后禁止撤回，业务方尚未确认该实现是否正确。")
    c = candidate(claim)
    c["action"] = "review"
    c.pop("provenance")
    c["rule_context"]["business_confirmation"] = "unconfirmed"
    out = submit(job, claim, [c])
    assert out["state"] == "needs_review", out
    assert not out["results"][0].get("memory_id")
    assert not list(store.iter_all(memory_repo, include_staging=True))


def test_unread_tail_cannot_be_submitted_as_processed(memory_repo):
    job, _ = prepare()
    # 用另一任务创建只返回部分片段的实际批次。
    out = capture_impl(session_id="large", scope="orders", messages=[{"role": "user", "text": "规则正文" * 3000}])
    job = re.search(r"J-[a-f0-9]+", out).group()
    claim = json.loads(job_claim_impl(job_id=job, max_chars=20, protocol_version=2))
    result = submit(job, claim, [candidate(claim)])
    assert result["error"] == "VALIDATION_FAILED"
    assert any("UNDELIVERED_SEGMENT" in item.get("errors", []) for item in result["errors"])


def test_related_new_card_requires_explicit_reassessment(memory_repo):
    job, claim = prepare()
    from knowbase.extraction.validator import build_card_body
    first = candidate(claim, dtype="pitfall", key="draft")
    first["title"] = "订单草稿删除的独立处理方式"
    first["body"]["conclusion"] = "草稿订单删除使用独立入口。"
    stored = writer.create(memory_repo, "pitfall", first["title"], build_card_body(first), scope="orders")
    assert not stored.get("error"), stored
    c = candidate(claim)
    rejected = submit(job, claim, [c])
    assert rejected["error"] == "NEEDS_REASSESSMENT", rejected
    c["duplicate_evaluation"] = {"snapshot_hash": rejected["similarity_snapshot"],
        "evaluated_cards": [{"id": card["id"], "content_revision": card["content_revision"]}
                            for card in rejected["similar_cards"]],
        "rationale": "已有卡是草稿删除，本候选是已结算订单撤回，条件和结果均不同。"}
    accepted = submit(job, claim, [c], request="after-review")
    assert accepted["saved_staging"] == 1 and accepted["failed"] == 0


def test_stale_application_owner_cannot_advance_coverage(memory_repo, monkeypatch):
    job, claim = prepare()
    original = writer.create
    def steal_after_write(*args, **kwargs):
        result = original(*args, **kwargs)
        conn = index.connect(memory_repo)
        conn.execute("UPDATE job_submissions SET application_owner='other-owner',application_until=? WHERE job_id=?",
                     ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), job))
        conn.commit()
        conn.close()
        return result
    monkeypatch.setattr(writer, "create", steal_after_write)
    out = submit(job, claim, [candidate(claim)])
    assert out["state"] == "validating"
    conn = index.connect(memory_repo)
    assert jobs.status(conn, job)["cursor"] == 0
    conn.close()
    monkeypatch.setattr(writer, "create", original)
    recovered = json.loads(job_status_impl(job, protocol_version=2))
    assert recovered["state"] == "needs_review"
    assert len(list(store.iter_all(memory_repo, include_staging=True))) == 1


def test_bad_candidate_body_reports_validation_error_not_infinite_retry(memory_repo):
    job, claim = prepare()
    c = candidate(claim)
    c["body"] = "invalid body"
    assert submit(job, claim, [c])["error"] == "VALIDATION_FAILED"
