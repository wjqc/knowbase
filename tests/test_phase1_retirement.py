"""Phase 1 自进化回归测试：自动归档、矛盾裁决、review 候选出口。

负向断言覆盖：期限内有证据不归档、证据打平不选边、无出处不进库。
"""
import json
from datetime import date, timedelta

from knowbase import doctor, index, store, writer
from knowbase.extraction import materialize
from knowbase.lifecycle import auto_archive_sweep, human_archive
from knowbase.mutations import _compute_governance_revision, _compute_revision
from knowbase.server import job_resubmit_review_impl, job_status_impl
from test_candidate_materialization import candidate as build_candidate
from test_candidate_materialization import prepare as prepare_job
from test_candidate_materialization import submit as submit_candidates
from test_phase0_self_evolution import BODY, activate, searchable, validate

DAY = timedelta(days=1)


BODY2 = ("## 结论\n库存低于阈值时触发补货提醒。\n## 解决的问题\n避免库存耗尽导致缺货。\n"
         "## 适用条件\n库存量低于安全阈值。\n## 不适用条件\n预售商品。\n"
         "## 可执行动作\n发出补货提醒。\n## 关键证据\n仓库管理规范 W-9。\n"
         "## 验证情况\n使用场景测试验证。\n## 未知与待确认\n无。\n")


def standard_card(repo, title, provenance="PRD-4 §2", body=BODY):
    result = writer.create(repo, "standard", title, body, scope="orders", provenance=provenance)
    assert not result.get("error"), result
    assert result["staging"] is True
    return result["entity_id"]


def stale_card(repo, title, prefix):
    """激活后以 outdated 反馈打成 stale 的卡（stale_since 应为当天）。"""
    mid = standard_card(repo, title)
    activate(repo, mid, prefix)
    result = validate(repo, mid, f"{prefix}-outdated", "agent-a", outcome="outdated")
    assert not result.get("error"), result
    meta, _, _ = store.load(repo, mid)
    assert meta["status"] == "stale"
    assert meta.get("stale_since") == date.today().isoformat()
    return mid


def seed_success(repo, mid, created_at, task_key):
    """直接播种一条已采纳成功证据（ledger 行），供归档扫描的证据口径测试使用。"""
    meta, body, _ = store.load(repo, mid)
    rev = _compute_revision(meta, body)
    conn = index.connect(repo)
    try:
        conn.execute("INSERT INTO knowledge_uses(use_id,host_profile,session_id,event_id,payload_hash,"
                     "context_event_id,task_id,task_revision,scope,memory_id,content_revision,state,"
                     "current_version,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (f"use-{task_key}", "seed", f"s-{task_key}", f"e-{task_key}", "x", "lookup",
                      task_key, "", meta.get("scope", ""), mid, rev, "adopted", 1, created_at))
        conn.execute("INSERT INTO task_outcomes(task_outcome_id,task_id,task_run_id,result,"
                     "verification_source,host_profile,session_id,use_id,memory_id,created_at,updated_at)"
                     " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (f"out-{task_key}", task_key, "", "completed", "agent_report", "seed",
                      f"s-{task_key}", f"use-{task_key}", mid, created_at, created_at))
        conn.execute("INSERT INTO feedback_events(receipt_id,request_id,payload_hash,memory_id,"
                     "content_revision,use_id,task_key,task_outcome_id,agent,outcome,binding,state,"
                     "before_hash,after_hash,after_meta_json,result_json,created_at)"
                     " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (f"rc-{task_key}", "", "x", mid, rev, f"use-{task_key}", task_key,
                      f"out-{task_key}", "tool-seed", "helpful", "version_task", "applied",
                      "", "", "{}", "{}", created_at))
        conn.commit()
    finally:
        conn.close()


def declare_contradiction(repo, src_id, tgt_id, request):
    meta, body, _ = store.load(repo, src_id)
    result = writer.update(repo, src_id, request_id=request,
                           expected_content_revision=_compute_revision(meta, body),
                           expected_governance_revision=_compute_governance_revision(meta),
                           title=meta["title"], body=body,
                           relations=[{"id": tgt_id, "type": "contradicts"}])
    assert not result.get("error"), result
    return result


def doctor_check(repo, name):
    return next(c for c in doctor.inspect(repo) if c["name"] == name)


def review_candidate_job(memory_repo):
    """提交一个 action=review 的候选，返回 (job, claim, 原候选 key)。"""
    job, claim = prepare_job()
    cand = build_candidate(claim)
    cand["action"] = "review"
    cand["candidate_key"] = "review-1"
    out = submit_candidates(job, claim, [cand], request="req-review")
    assert out["state"] == "needs_review", out
    return job, claim, cand


# ---------------- 1-A 自动归档 ----------------

def test_stale_card_auto_archives_after_90_days_without_evidence(memory_repo):
    mid = stale_card(memory_repo, "归档测试卡", "arc")
    conn = index.connect(memory_repo)
    try:
        archived = auto_archive_sweep(memory_repo, conn, today=date.today() + 100 * DAY)
    finally:
        conn.close()
    assert [a["id"] for a in archived] == [mid]
    meta, _, _ = store.load(memory_repo, mid)
    assert meta["status"] == "archived" and meta["archived_at"] and meta["archive_reason"]
    assert not searchable(memory_repo, mid)
    ledger = json.loads(doctor_check(memory_repo, "archive_ledger")["message"])
    assert any(entry["id"] == mid for entry in ledger)


def test_stale_card_not_archived_before_deadline(memory_repo):
    mid = stale_card(memory_repo, "未满期卡", "early")
    conn = index.connect(memory_repo)
    try:
        assert auto_archive_sweep(memory_repo, conn) == []
    finally:
        conn.close()
    assert store.load(memory_repo, mid)[0]["status"] == "stale"


def test_recent_successful_evidence_blocks_archive(memory_repo):
    """负向断言：期限内（近 90 天）出现成功证据，不归档。"""
    mid = stale_card(memory_repo, "近期有证据卡", "recent")
    sweep_day = date.today() + 100 * DAY
    seed_success(memory_repo, mid, (sweep_day - 10 * DAY).isoformat(), "recent-ev")
    conn = index.connect(memory_repo)
    try:
        assert auto_archive_sweep(memory_repo, conn, today=sweep_day) == []
    finally:
        conn.close()
    assert store.load(memory_repo, mid)[0]["status"] == "stale"


def test_old_successful_evidence_does_not_block_archive(memory_repo):
    """90 天前的成功证据不构成保护期，满期仍归档。"""
    mid = stale_card(memory_repo, "旧证据卡", "old-ev")
    sweep_day = date.today() + 100 * DAY
    seed_success(memory_repo, mid, date.today().isoformat(), "old-ev")
    conn = index.connect(memory_repo)
    try:
        archived = auto_archive_sweep(memory_repo, conn, today=sweep_day)
    finally:
        conn.close()
    assert [a["id"] for a in archived] == [mid]


def test_superseded_card_gets_stale_clock_and_manual_archive_in_ledger(memory_repo):
    winner = standard_card(memory_repo, "新版结算规则")
    loser = standard_card(memory_repo, "旧版退款流程")
    activate(memory_repo, loser, "sup")
    meta, body, _ = store.load(memory_repo, winner)
    result = writer.update(memory_repo, winner, request_id="req-sup",
                           expected_content_revision=_compute_revision(meta, body),
                           expected_governance_revision=_compute_governance_revision(meta),
                           title=meta["title"], body=body,
                           relations=[{"id": loser, "type": "supersedes"}])
    assert not result.get("error"), result
    loser_meta, _, _ = store.load(memory_repo, loser)
    assert loser_meta["status"] == "stale" and loser_meta.get("stale_since") == date.today().isoformat()
    assert human_archive(memory_repo, loser) is not None
    conn = index.connect(memory_repo)
    try:
        index.upsert(conn, *store.load(memory_repo, loser))
    finally:
        conn.close()
    ledger = json.loads(doctor_check(memory_repo, "archive_ledger")["message"])
    entry = next(e for e in ledger if e["id"] == loser)
    assert entry["reason"] == "manual"


# ---------------- 1-B 矛盾裁决 ----------------

def test_stronger_card_wins_weaker_goes_stale(memory_repo):
    strong = standard_card(memory_repo, "强卡")
    weak = standard_card(memory_repo, "弱卡")
    activate(memory_repo, strong, "strong")
    activate(memory_repo, weak, "weak")
    validate(memory_repo, strong, "strong-3", "agent-a")
    validate(memory_repo, strong, "strong-4", "agent-b")
    result = declare_contradiction(memory_repo, weak, strong, "req-adj-strong")
    assert any("自动标 stale" in n for n in result["notes"]), result["notes"]
    assert store.load(memory_repo, strong)[0]["status"] == "active"
    weak_meta, _, _ = store.load(memory_repo, weak)
    assert weak_meta["status"] == "stale" and weak_meta.get("stale_since") == date.today().isoformat()
    assert strong in weak_meta.get("adjudication_note", "")
    assert not weak_meta.get("contradiction_quarantine")


def test_tied_evidence_quarantines_both_without_picking_side(memory_repo):
    """负向断言：证据打平不选边，双方同时降 stale 并隔离。"""
    card_a = standard_card(memory_repo, "打平卡 A")
    card_b = standard_card(memory_repo, "打平卡 B")
    activate(memory_repo, card_a, "tie-a")
    activate(memory_repo, card_b, "tie-b")
    declare_contradiction(memory_repo, card_b, card_a, "req-adj-tie")
    meta_a, _, _ = store.load(memory_repo, card_a)
    meta_b, _, _ = store.load(memory_repo, card_b)
    assert meta_a["status"] == "stale" and meta_b["status"] == "stale"
    assert meta_a.get("contradiction_quarantine") is True
    assert meta_b.get("contradiction_quarantine") is True
    assert "打平" in meta_a.get("adjudication_note", "")
    quarantine = json.loads(doctor_check(memory_repo, "contradiction_quarantine")["message"])
    assert {entry["id"] for entry in quarantine} == {card_a, card_b}


def test_both_negative_evidence_quarantines_both(memory_repo):
    card_a = standard_card(memory_repo, "负面卡 A")
    card_b = standard_card(memory_repo, "负面卡 B")
    activate(memory_repo, card_a, "neg-a")
    activate(memory_repo, card_b, "neg-b")
    validate(memory_repo, card_a, "neg-a-bad", "agent-a", outcome="not_helpful")
    validate(memory_repo, card_b, "neg-b-bad", "agent-a", outcome="not_helpful")
    assert store.load(memory_repo, card_a)[0]["status"] == "active"
    declare_contradiction(memory_repo, card_b, card_a, "req-adj-neg")
    meta_a, _, _ = store.load(memory_repo, card_a)
    meta_b, _, _ = store.load(memory_repo, card_b)
    assert meta_a["status"] == "stale" and meta_b["status"] == "stale"
    assert meta_a.get("contradiction_quarantine") is True
    assert "双方均有负面证据" in meta_a.get("adjudication_note", "")


def test_staging_contradiction_skips_adjudication(memory_repo):
    established = standard_card(memory_repo, "已生效卡")
    activate(memory_repo, established, "est")
    newborn = writer.create(memory_repo, "standard", "新生矛盾卡", BODY, scope="orders",
                            provenance="PRD-4 §2",
                            relations=[{"id": established, "type": "contradicts"}])
    assert not newborn.get("error"), newborn
    assert newborn["staging"] is True
    assert store.load(memory_repo, established)[0]["status"] == "active"
    assert "adjudication_note" not in store.load(memory_repo, newborn["entity_id"])[0]


def test_newborn_active_card_loses_to_established(memory_repo):
    established = standard_card(memory_repo, "已生效卡二")
    activate(memory_repo, established, "est2")
    newborn = writer.create(memory_repo, "pitfall", "新生陷阱卡", BODY, scope="orders",
                            provenance="PRD-4 §2",
                            relations=[{"id": established, "type": "contradicts"}])
    assert not newborn.get("error"), newborn
    assert newborn["staging"] is False
    newborn_meta, _, _ = store.load(memory_repo, newborn["entity_id"])
    assert newborn_meta["status"] == "stale"
    assert established in newborn_meta.get("adjudication_note", "")
    assert store.load(memory_repo, established)[0]["status"] == "active"


# ---------------- 1-C review 候选出口 ----------------

def test_review_candidate_resubmit_creates_card_and_supersedes(memory_repo):
    job, claim, orig = review_candidate_job(memory_repo)
    new = build_candidate(claim)
    new["candidate_key"] = "resubmit-1"
    out = json.loads(job_resubmit_review_impl(job_id=job, candidate_key="review-1", candidate=new))
    assert out.get("outcome") == "saved", out
    assert out["superseded_candidate_key"] == "review-1"
    assert out["governance_status"] == "staging"
    new_meta, _, new_path = store.load(memory_repo, out["memory_id"])
    assert new_path.parent.name == "staging"
    conn = index.connect(memory_repo)
    try:
        orig_row = conn.execute("SELECT candidate_id,materialization_state FROM candidates "
                                "WHERE job_id=? AND candidate_key='review-1'", (job,)).fetchone()
        dup = conn.execute("SELECT duplicate_of FROM candidates WHERE job_id=? "
                           "AND candidate_key='resubmit-1'", (job,)).fetchone()[0]
        segment = conn.execute("SELECT settlement_state FROM segment_results WHERE job_id=?",
                               (job,)).fetchone()
        job_state = conn.execute("SELECT state FROM jobs WHERE job_id=?", (job,)).fetchone()[0]
    finally:
        conn.close()
    assert orig_row[1] == "superseded"
    assert dup == orig_row[0]
    # 成卡路径与正常候选一致：bizrule 进 staging，段与 job 仍为 needs_review。
    assert segment[0] == "needs_review" and job_state == "needs_review"


def test_review_resubmit_rejects_missing_provenance(memory_repo):
    """负向断言：无出处内容永不进库；失败时原行不动。"""
    job, claim, orig = review_candidate_job(memory_repo)
    new = build_candidate(claim)
    new["candidate_key"] = "resubmit-noprov"
    del new["provenance"]
    out = json.loads(job_resubmit_review_impl(job_id=job, candidate_key="review-1", candidate=new))
    assert out.get("error") == "VALIDATION_FAILED", out
    conn = index.connect(memory_repo)
    try:
        state = conn.execute("SELECT materialization_state FROM candidates WHERE job_id=? "
                             "AND candidate_key='review-1'", (job,)).fetchone()[0]
        count = conn.execute("SELECT COUNT(*) FROM candidates WHERE job_id=? "
                             "AND candidate_key='resubmit-noprov'", (job,)).fetchone()[0]
    finally:
        conn.close()
    assert state == "needs_review" and count == 0


def test_review_resubmit_rejects_key_reuse_segment_mismatch_and_review(memory_repo):
    job, claim, orig = review_candidate_job(memory_repo)
    new = build_candidate(claim)
    out = json.loads(job_resubmit_review_impl(job_id=job, candidate_key="review-1",
                                              candidate={**new, "candidate_key": "review-1"}))
    assert out.get("error") == "CANDIDATE_KEY_REUSE", out
    out = json.loads(job_resubmit_review_impl(job_id=job, candidate_key="review-1",
                                              candidate={**new, "candidate_key": "resubmit-x",
                                                         "segment_ids": ["no-such-segment"]}))
    assert out.get("error") == "SEGMENT_MISMATCH", out
    out = json.loads(job_resubmit_review_impl(job_id=job, candidate_key="review-1",
                                              candidate={**new, "candidate_key": "resubmit-y",
                                                         "action": "review"}))
    assert out.get("error") == "RESUBMIT_MUST_NOT_BE_REVIEW", out


def test_review_resubmit_update_flips_job_to_succeeded(memory_repo):
    # 目标卡须与候选内容不相似，否则原提交会因 NEEDS_REASSESSMENT 被拒。
    target = standard_card(memory_repo, "库存补货提醒规则", provenance="仓库管理规范 W-9",
                           body=BODY2)
    activate(memory_repo, target, "upd-tgt")
    job, claim, orig = review_candidate_job(memory_repo)
    tmeta, tbody, _ = store.load(memory_repo, target)
    new = build_candidate(claim)
    new["candidate_key"] = "resubmit-upd"
    new["action"] = "update"
    new["type"] = "standard"
    new.pop("rule_context", None)
    new["target_id"] = target
    new["expected_content_revision"] = _compute_revision(tmeta, tbody)
    new["expected_governance_revision"] = _compute_governance_revision(tmeta)
    out = json.loads(job_resubmit_review_impl(job_id=job, candidate_key="review-1", candidate=new))
    assert out.get("outcome") == "updated", out
    assert out["governance_status"] == "active_once"
    status = json.loads(job_status_impl(job_id=job, protocol_version=2))
    assert status["state"] == "succeeded", status


def test_review_resubmit_replay_is_idempotent(memory_repo):
    job, claim, orig = review_candidate_job(memory_repo)
    new = build_candidate(claim)
    new["candidate_key"] = "resubmit-replay"
    first = json.loads(job_resubmit_review_impl(job_id=job, candidate_key="review-1", candidate=new))
    assert first.get("outcome") == "saved", first
    second = json.loads(job_resubmit_review_impl(job_id=job, candidate_key="review-1", candidate=new))
    assert second.get("replayed") is True
    assert second["memory_id"] == first["memory_id"]


def test_review_candidate_expires_and_appears_in_discard_ledger(memory_repo):
    job, claim, orig = review_candidate_job(memory_repo)
    conn = index.connect(memory_repo)
    try:
        old = (date.today() - 40 * DAY).isoformat()
        conn.execute("UPDATE candidates SET created_at=? WHERE job_id=? AND candidate_key='review-1'",
                     (old, job))
        conn.commit()
        discarded = materialize.sweep_review_candidates(conn, ttl_days=30)
    finally:
        conn.close()
    assert [d["candidate_key"] for d in discarded] == ["review-1"]
    ledger = json.loads(doctor_check(memory_repo, "review_discard_ledger")["message"])
    assert any(entry["candidate_key"] == "review-1" for entry in ledger)


def test_fresh_review_candidate_not_swept(memory_repo):
    """未超期的 review 候选不受丢弃扫描影响。"""
    job, claim, _ = review_candidate_job(memory_repo)
    conn = index.connect(memory_repo)
    try:
        assert materialize.sweep_review_candidates(conn, ttl_days=30) == []
        state = conn.execute("SELECT materialization_state FROM candidates WHERE job_id=? "
                             "AND candidate_key='review-1'", (job,)).fetchone()[0]
    finally:
        conn.close()
    assert state == "needs_review"


def test_sweep_commands_wire_up(memory_repo):
    from knowbase.__main__ import cmd_archive_sweep, cmd_review_sweep
    assert cmd_archive_sweep() == 0
    assert cmd_review_sweep() == 0
