"""候选提交到真实卡片。先持久回执，再幂等应用，最后结算片段。"""

import json
import uuid
from datetime import datetime, timedelta, timezone

from .. import index, materials, store, writer
from . import validator


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _settle_session_cursor(conn, job_id):
    current = materials.payload_for_job(conn, job_id)
    capture_range = current.get("capture_range", {})
    host, session = current.get("host_profile"), current.get("session_id")
    if not host or not session or not capture_range:
        return
    kind = capture_range["source_kind"]
    ranges = []
    for raw, state in conn.execute("SELECT s.input_json,j.state FROM job_spool s JOIN jobs j ON j.job_id=s.job_id "
                                   "WHERE j.kind='capture' AND j.scope=?", (current["scope"],)):
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        r = entry.get("capture_range", {})
        if entry.get("host_profile") == host and entry.get("session_id") == session and r.get("source_kind") == kind:
            ranges.append((r["start"], r["end"], state))
    end = 0
    for start, finish, state in sorted(ranges):
        if finish <= end:
            continue
        if start != end or state not in ("succeeded", "no_candidate", "needs_review"):
            break
        end = finish
    conn.execute("UPDATE context_sessions SET settled_cursor=? WHERE host_profile=? AND session_id=?",
                 (f"{kind}:{end}", host, session))


def _candidate_errors(repo, candidate, scope, owned, delivered, manifest):
    if not isinstance(candidate, dict):
        return ["candidate 必须为对象"]
    if not isinstance(candidate.get("body"), dict):
        return ["body 必须为对象"]
    errors = validator.validate_candidate(candidate, scope=scope, repo_path=repo).errors
    for key in ("candidate_key", "title", "type"):
        if not isinstance(candidate.get(key), str) or not candidate[key].strip():
            errors.append(f"{key} 必填")
    action = candidate.get("action", "create")
    if action not in {"create", "update", "link_existing", "review"}:
        errors.append("action 非法")
    if candidate.get("scope") != scope or (candidate.get("body") or {}).get("scope", scope) != scope:
        errors.append("SCOPE_MISMATCH")
    ids = candidate.get("segment_ids", [])
    if not isinstance(ids, list) or not ids or not all(isinstance(s, str) for s in ids):
        errors.append("segment_ids 必须为非空列表")
        ids = []
    if set(ids) - owned or set(ids) - delivered:
        errors.append("UNDELIVERED_SEGMENT")
    refs = candidate.get("evidence_refs", [])
    if not refs:
        errors.append("evidence_refs 必填")
    elif not isinstance(refs, list):
        errors.append("evidence_refs 必须为列表")
    else:
        for ref in refs:
            if (not isinstance(ref, dict) or ref.get("segment_id") not in ids
                    or ref.get("text_hash") != manifest.get(ref.get("segment_id"), {}).get("text_hash")):
                errors.append("EVIDENCE_MISMATCH")
    if candidate.get("type") == "bizrule" and not isinstance(candidate.get("rule_context"), dict):
        errors.append("bizrule.rule_context 必填")
    if action in {"update", "link_existing"}:
        target, _, _ = store.load(repo, candidate.get("target_id", ""))
        allowed_scopes = (scope,) if action == "update" else (scope, "global")
        if not target or target.get("scope") not in allowed_scopes:
            errors.append("TARGET_SCOPE_MISMATCH")
        if target and target.get("status") != "active":
            errors.append("TARGET_NOT_ACTIVE")
        if not candidate.get("expected_content_revision"):
            errors.append("expected_content_revision 必填")
        if action == "update" and not candidate.get("expected_governance_revision"):
            errors.append("expected_governance_revision 必填")
    if candidate.get("type") in store.TYPES and candidate.get("type") != "reference":
        meta = store.new_meta(candidate["type"], candidate.get("title", ""), scope, [],
                              "agent:knowbase:candidate")
        for key in ("provenance", "code_refs", "source_refs", "rule_context"):
            if candidate.get(key) is not None:
                meta[key] = candidate[key]
        card_errors = store.lint(meta, validator.build_card_body(candidate), repo)
        if action == "review":
            card_errors = [e for e in card_errors if "业务规则必须带出处" not in e]
        errors.extend(card_errors)
    else:
        errors.append("INVALID_TYPE")
    return errors


def submit(repo, conn, job_id, batch_id, lease_token, request_id, candidates,
           segment_results, fencing_token=None):
    payload = {"candidates": candidates, "segment_results": segment_results, "batch_id": batch_id}
    phash = materials.digest(payload)
    # 已提交回执重放先于过期租约检查。相同 request 的不同内容不允许复用。
    prior = conn.execute("SELECT payload_hash,state,result_json FROM job_submissions "
                         "WHERE job_id=? AND request_id=?", (job_id, request_id)).fetchone()
    if prior:
        if prior[0] != phash:
            return {"error": "IDEMPOTENCY_CONFLICT"}
        return json.loads(prior[2]) if prior[1] == "done" else apply_submission(repo, conn, job_id, request_id)
    conn.execute("BEGIN IMMEDIATE")
    try:
        batch = materials.validate_batch(conn, job_id, batch_id, lease_token, fencing_token)
        if batch.get("error"):
            conn.rollback()
            return batch
        scope = conn.execute("SELECT scope FROM jobs WHERE job_id=?", (job_id,)).fetchone()[0]
        snapshot_row = conn.execute("SELECT similar_json FROM job_batches WHERE batch_id=?", (batch_id,)).fetchone()
        baseline = json.loads(snapshot_row[0]) if snapshot_row else {}
        similar = materials.similar_snapshot(conn, baseline.get("query", ""), scope)
        snapshot_hash = materials.digest(similar)
        owned, delivered = set(batch["segment_ids"]), set(batch["delivered"])
        manifest = {s["segment_id"]: s for s in materials.manifest_for_job(conn, job_id)}
        if not isinstance(candidates, list) or not isinstance(segment_results, list):
            conn.rollback()
            return {"error": "INVALID_SUBMISSION"}
        keys, errors = set(), []
        for candidate in candidates:
            problems = _candidate_errors(repo, candidate, scope, owned, delivered, manifest)
            key = candidate.get("candidate_key", "") if isinstance(candidate, dict) else ""
            if key in keys:
                problems.append("DUPLICATE_CANDIDATE_KEY")
            keys.add(key)
            existing = conn.execute("SELECT payload_hash FROM candidates WHERE job_id=? AND candidate_key=?",
                                    (job_id, key)).fetchone()
            if existing and existing[0] != materials.digest(candidate):
                problems.append("CANDIDATE_VERSION_CONFLICT")
            if problems:
                errors.append({"candidate_key": key, "errors": problems})
        if errors:
            conn.rollback()
            return {"error": "VALIDATION_FAILED", "errors": errors}
        if not errors and similar and candidates:
            required = {card["id"]: card["content_revision"] for card in similar}
            for candidate in candidates:
                evaluation = candidate.get("duplicate_evaluation") or {}
                evaluated = {card.get("id"): card.get("content_revision") for card in evaluation.get("evaluated_cards", [])
                             if isinstance(card, dict)}
                if (evaluation.get("snapshot_hash") != snapshot_hash or not evaluation.get("rationale")
                        or any(evaluated.get(mid) != rev for mid, rev in required.items())):
                    conn.rollback()
                    return {"error": "NEEDS_REASSESSMENT", "similar_cards": similar,
                            "similarity_snapshot": snapshot_hash,
                            "message": "读取相似卡并判断相同/补充/独立/矛盾后，用新 request_id 在每个 candidates[i].duplicate_evaluation 提交评估；不是 memory_job_submit 顶层参数"}
        linked = {sid for c in candidates if isinstance(c, dict) for sid in c.get("segment_ids", [])}
        segments_seen = set()
        for item in segment_results:
            if not isinstance(item, dict):
                errors.append({"error": "INVALID_SEGMENT_RESULT"})
                continue
            sid, decision = item.get("segment_id"), item.get("decision")
            if sid not in owned or sid not in delivered or sid in segments_seen:
                errors.append({"segment_id": sid, "error": "INVALID_OR_UNDELIVERED_SEGMENT"})
            segments_seen.add(sid)
            if decision not in {"distilled", "skipped", "no_value"}:
                errors.append({"segment_id": sid, "error": "INVALID_DECISION"})
            if decision == "distilled" and sid not in linked:
                errors.append({"segment_id": sid, "error": "MISSING_CANDIDATE"})
            if decision in {"skipped", "no_value"} and (sid in linked or not item.get("reason")):
                errors.append({"segment_id": sid, "error": "INVALID_SKIP"})
        if linked - segments_seen or not segment_results:
            errors.append({"error": "BATCH_INCOMPLETE"})
        if errors:
            conn.rollback()
            return {"error": "VALIDATION_FAILED", "errors": errors}
        now = _now()
        for candidate in candidates:
            raw = json.dumps(candidate, ensure_ascii=False, sort_keys=True)
            conn.execute("INSERT OR IGNORE INTO candidates(candidate_id,job_id,batch_id,candidate_key,scope,"
                         "body_json,evidence_json,decision,version,request_id,created_at,updated_at,"
                         "payload_json,payload_hash,materialization_state) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         ("C-" + uuid.uuid4().hex[:12], job_id, batch_id, candidate["candidate_key"], scope,
                          json.dumps(candidate["body"], ensure_ascii=False), json.dumps(candidate["evidence_refs"]),
                          "pending", 1, request_id, now, now, raw, materials.digest(candidate), "pending"))
        conn.execute("INSERT INTO job_submissions(job_id,request_id,batch_id,payload_hash,state,payload_json,"
                     "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                     (job_id, request_id, batch_id, phash, "pending", json.dumps(payload, ensure_ascii=False), now, now))
        conn.execute("UPDATE job_batches SET state='validating' WHERE batch_id=?", (batch_id,))
        conn.execute("UPDATE jobs SET state='validating' WHERE job_id=?", (job_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return apply_submission(repo, conn, job_id, request_id)


def _apply_candidate(repo, conn, job_id, candidate, row):
    cid, state, mid, revision = row
    if state in {"saved", "updated", "linked_existing", "needs_review"}:
        meta, _, path = store.load(repo, mid) if mid else (None, None, None)
        if mid and not meta:
            return {"candidate_id": cid, "error": "READBACK_FAILED", "retryable": True}
        return {"candidate_id": cid, "outcome": state, "memory_id": mid, "content_revision": revision,
                "governance_status": "staging" if path and path.parent.name == "staging" else "active_once" if mid else "unconfirmed",
                "local_state": "local_saved" if mid else "review_pending", "sync_state": "unknown"}
    action = candidate.get("action", "create")
    if action == "review":
        return {"candidate_id": cid, "outcome": "needs_review", "governance_status": "unconfirmed"}
    stable_key = f"candidate:{job_id}:{candidate['candidate_key']}:v1"
    if action == "link_existing":
        from ..mutations import _compute_revision
        target, body, path = store.load(repo, candidate["target_id"])
        revision = _compute_revision(target, body) if target else ""
        if not target or target.get("status") != "active" or revision != candidate["expected_content_revision"]:
            return {"candidate_id": cid, "error": "REVISION_CONFLICT", "retryable": False}
        result = {"entity_id": candidate["target_id"], "content_revision": revision,
                  "staging": path.parent.name == "staging", "local_state": "existing", "sync_state": "unknown"}
        outcome = "linked_existing"
    else:
        arguments = {key: candidate[key] for key in ("tags", "provenance", "source_refs", "code_refs", "rule_context", "relations")
                     if key in candidate}
        body = validator.build_card_body(candidate)
        if action == "update":
            result = writer.update(repo, candidate["target_id"], request_id=stable_key, push=False,
                expected_content_revision=candidate["expected_content_revision"],
                expected_governance_revision=candidate["expected_governance_revision"],
                title=candidate["title"], body=body, **arguments)
            outcome = "updated"
        else:
            result = writer.create(repo, candidate["type"], candidate["title"], body,
                                   scope=candidate["scope"], request_id=stable_key, push=False,
                                   source=f"agent:knowbase:candidate-{cid}", **arguments)
            outcome = "saved"
    if result.get("error"):
        return {"candidate_id": cid, **result, "retryable": result.get("retryable", False)}
    return {"candidate_id": cid, "outcome": outcome, "memory_id": result["entity_id"],
            "content_revision": result["content_revision"], "governance_status": "staging" if result["staging"] else "active_once",
            "local_state": result["local_state"], "sync_state": result["sync_state"]}


def apply_submission(repo, conn, job_id, request_id):
    owner = uuid.uuid4().hex
    now = _now()
    until = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat(timespec="seconds")
    cur = conn.execute("UPDATE job_submissions SET state='applying',application_owner=?,application_until=? "
                       "WHERE job_id=? AND request_id=? AND state!='done' "
                       "AND (application_until='' OR application_until<=?)", (owner, until, job_id, request_id, now))
    conn.commit()
    if cur.rowcount != 1:
        row = conn.execute("SELECT state,result_json FROM job_submissions WHERE job_id=? AND request_id=?",
                           (job_id, request_id)).fetchone()
        return json.loads(row[1]) if row and row[0] == "done" else {"state": "validating", "retryable": True}
    row = conn.execute("SELECT payload_json,batch_id FROM job_submissions WHERE job_id=? AND request_id=?",
                       (job_id, request_id)).fetchone()
    payload, batch_id = json.loads(row[0]), row[1]
    results, mapping = [], {}
    for candidate in payload["candidates"]:
        until = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat(timespec="seconds")
        authority = conn.execute("UPDATE job_submissions SET application_until=? WHERE job_id=? AND "
                                 "request_id=? AND application_owner=? AND application_until>?",
                                 (until, job_id, request_id, owner, _now()))
        conn.commit()
        if authority.rowcount != 1:
            return {"state": "validating", "retryable": True}
        row = conn.execute("SELECT candidate_id,materialization_state,memory_id,memory_revision "
                           "FROM candidates WHERE job_id=? AND candidate_key=?", (job_id, candidate["candidate_key"])).fetchone()
        try:
            applied = _apply_candidate(repo, conn, job_id, candidate, row)
        except Exception as exc:
            applied = {"candidate_id": row[0], "error": type(exc).__name__, "retryable": True}
        conn.execute("BEGIN IMMEDIATE")
        authority = conn.execute("SELECT 1 FROM job_submissions WHERE job_id=? AND request_id=? "
                                 "AND application_owner=? AND application_until>?",
                                 (job_id, request_id, owner, _now())).fetchone()
        if not authority:
            conn.rollback()
            return {"state": "validating", "retryable": True}
        conn.execute("UPDATE candidates SET materialization_state=?,memory_id=?,memory_revision=?,"
                     "decision=?,updated_at=? WHERE candidate_id=?",
                     (applied.get("outcome", "retry_wait"), applied.get("memory_id", ""),
                      applied.get("content_revision", ""), applied.get("outcome", "failed"), _now(), row[0]))
        conn.commit()
        results.append(applied)
        for sid in candidate["segment_ids"]:
            mapping.setdefault(sid, []).append(applied)
    conn.execute("BEGIN IMMEDIATE")
    authority = conn.execute("SELECT 1 FROM job_submissions WHERE job_id=? AND request_id=? "
                             "AND application_owner=? AND application_until>?",
                             (job_id, request_id, owner, _now())).fetchone()
    if not authority:
        conn.rollback()
        return {"state": "validating", "retryable": True}
    for segment in payload["segment_results"]:
        sid = segment["segment_id"]
        applied = mapping.get(sid, [])
        state = "pending" if any(r.get("error") for r in applied) else (
            "needs_review" if any(r.get("outcome") == "needs_review" or r.get("governance_status") == "staging" for r in applied)
            else "saved" if applied else "no_candidate")
        conn.execute("INSERT INTO segment_results(segment_id,job_id,batch_id,decision,reason,candidate_id,"
                     "created_at,updated_at,settlement_state,receipt_id,linked_ids_json) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                     "ON CONFLICT(job_id,segment_id) DO UPDATE SET decision=excluded.decision,reason=excluded.reason,"
                     "updated_at=excluded.updated_at,settlement_state=excluded.settlement_state,"
                     "receipt_id=excluded.receipt_id,linked_ids_json=excluded.linked_ids_json",
                     (sid, job_id, batch_id, segment["decision"], segment.get("reason", ""),
                      applied[0]["candidate_id"] if applied else "", now, _now(), state, request_id,
                      json.dumps([r["memory_id"] for r in applied if r.get("memory_id")])))
    cursor = materials.contiguous_cursor(conn, job_id)
    total = conn.execute("SELECT batch_total FROM jobs WHERE job_id=?", (job_id,)).fetchone()[0]
    failures = [r for r in results if r.get("error")]
    transient = any(r.get("retryable") for r in failures)
    if failures:
        state = "validating" if transient else "awaiting_agent"
    elif cursor == total:
        review = conn.execute("SELECT COUNT(*) FROM segment_results WHERE job_id=? AND settlement_state='needs_review'", (job_id,)).fetchone()[0]
        state = "needs_review" if review else "succeeded" if results else "no_candidate"
    else:
        state = "awaiting_agent"
    output = {"state": state, "results": results, "next_contiguous_cursor": cursor,
              "pending_segments": total - cursor, "saved_active": sum(r.get("outcome") == "saved" and r.get("governance_status") != "staging" for r in results),
              "saved_staging": sum(r.get("governance_status") == "staging" for r in results), "failed": len(failures)}
    conn.execute("UPDATE jobs SET state=?,batch_cursor=?,updated_at=? WHERE job_id=?", (state, cursor, _now(), job_id))
    _settle_session_cursor(conn, job_id)
    conn.execute("UPDATE job_submissions SET state=?,result_json=?,application_owner='',application_until='',"
                 "updated_at=? WHERE job_id=? AND request_id=? AND application_owner=?",
                 ("retry_wait" if transient else "done", json.dumps(output, ensure_ascii=False), _now(), job_id, request_id, owner))
    conn.execute("UPDATE job_batches SET state=? WHERE batch_id=?", ("validating" if transient else "settled", batch_id))
    conn.commit()
    return output


def recover(repo, conn, job_id):
    receipts = conn.execute("SELECT request_id FROM job_submissions WHERE job_id=? AND state!='done'", (job_id,)).fetchall()
    return [apply_submission(repo, conn, job_id, r[0]) for r in receipts]


def resubmit_review(repo, conn, job_id, candidate_key, new_candidate):
    """Phase 1：needs_review 候选的 Agent 重提交出口（G6）。

    原候选行须存在且 materialization_state == 'needs_review'；新候选须用不同
    candidate_key（candidates 表对 (job_id, candidate_key) 唯一，同键改内容会判
    CANDIDATE_VERSION_CONFLICT）、action 不得为 review、segment_ids 须与原候选
    一致。新候选走与正常提交相同的 _candidate_errors 校验与 _apply_candidate
    成卡路径；成功后原行置 superseded，相关 segment 的 settlement_state 按新
    结果更新；仅当本 job 再无 needs_review 段且游标已满时才把 job 状态翻为
    succeeded。失败时原行不动，可换新 key 再次重提交。
    防伪底线不变：action 非 review 时不再豁免“业务规则必须带出处”校验，
    无出处内容永不进库；新行用 duplicate_of 列指向原候选 id，可审计。
    """
    if not isinstance(new_candidate, dict):
        return {"error": "INVALID_CANDIDATE"}
    new_key = new_candidate.get("candidate_key", "")
    if not isinstance(new_key, str) or not new_key.strip():
        return {"error": "CANDIDATE_KEY_REQUIRED"}
    if new_key == candidate_key:
        return {"error": "CANDIDATE_KEY_REUSE",
                "message": "重提交须用新的 candidate_key（同键改内容会被判版本冲突）"}
    if new_candidate.get("action", "create") == "review":
        return {"error": "RESUBMIT_MUST_NOT_BE_REVIEW",
                "message": "重提交须补充出处并给出 create/update/link_existing，不得再次 review"}
    orig = conn.execute("SELECT candidate_id,batch_id,scope,payload_json,materialization_state "
                        "FROM candidates WHERE job_id=? AND candidate_key=?",
                        (job_id, candidate_key)).fetchone()
    if not orig:
        return {"error": "CANDIDATE_NOT_FOUND"}
    orig_id, batch_id, scope, orig_payload, orig_state = orig
    # 新 key 的幂等重放先于原行状态检查：原行已因本次重提交 superseded 后，
    # 再次提交相同 key 与内容应重放结果，而非报 CANDIDATE_NOT_IN_REVIEW。
    existing = conn.execute("SELECT candidate_id,materialization_state,memory_id,memory_revision,payload_hash "
                            "FROM candidates WHERE job_id=? AND candidate_key=?", (job_id, new_key)).fetchone()
    if existing:
        if existing[4] != materials.digest(new_candidate):
            return {"error": "CANDIDATE_VERSION_CONFLICT"}
        if existing[1] in ("saved", "updated", "linked_existing", "needs_review"):
            return {"candidate_id": existing[0], "outcome": existing[1], "memory_id": existing[2],
                    "content_revision": existing[3], "superseded_candidate_key": candidate_key,
                    "replayed": True}
        return {"error": "CANDIDATE_KEY_CONFLICT",
                "message": "该 key 已有一行未完成记录（中断的重提交），请换新 key"}
    if orig_state != "needs_review":
        return {"error": "CANDIDATE_NOT_IN_REVIEW", "state": orig_state}
    try:
        orig_segment_ids = json.loads(orig_payload).get("segment_ids", [])
    except ValueError:
        orig_segment_ids = []
    if not orig_segment_ids:
        return {"error": "CANDIDATE_SEGMENTS_UNKNOWN"}
    if set(new_candidate.get("segment_ids", [])) != set(orig_segment_ids):
        return {"error": "SEGMENT_MISMATCH",
                "message": "重提交的 segment_ids 须与原候选一致"}
    owned = delivered = set(orig_segment_ids)
    manifest = {s["segment_id"]: s for s in materials.manifest_for_job(conn, job_id)}
    problems = _candidate_errors(repo, new_candidate, scope, owned, delivered, manifest)
    if problems:
        return {"error": "VALIDATION_FAILED",
                "errors": [{"candidate_key": new_key, "errors": problems}]}
    now = _now()
    cid = "C-" + uuid.uuid4().hex[:12]
    request_id = "resubmit-" + uuid.uuid4().hex
    conn.execute("INSERT INTO candidates(candidate_id,job_id,batch_id,candidate_key,scope,"
                 "body_json,evidence_json,decision,version,request_id,created_at,updated_at,"
                 "payload_json,payload_hash,materialization_state,duplicate_of) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (cid, job_id, batch_id, new_key, scope,
                  json.dumps(new_candidate["body"], ensure_ascii=False),
                  json.dumps(new_candidate["evidence_refs"]), "pending", 1, request_id, now, now,
                  json.dumps(new_candidate, ensure_ascii=False, sort_keys=True),
                  materials.digest(new_candidate), "pending", orig_id))
    conn.commit()
    try:
        applied = _apply_candidate(repo, conn, job_id, new_candidate, (cid, "pending", "", ""))
    except Exception as exc:
        applied = {"candidate_id": cid, "error": type(exc).__name__, "retryable": True}
    outcome = applied.get("outcome", "retry_wait")
    conn.execute("UPDATE candidates SET materialization_state=?,memory_id=?,memory_revision=?,"
                 "decision=?,updated_at=? WHERE candidate_id=?",
                 (outcome, applied.get("memory_id", ""), applied.get("content_revision", ""),
                  outcome if not applied.get("error") else "failed", _now(), cid))
    if applied.get("error"):
        conn.commit()
        return applied
    conn.execute("UPDATE candidates SET decision='superseded',materialization_state='superseded',"
                 "updated_at=? WHERE candidate_id=?", (_now(), orig_id))
    for sid in orig_segment_ids:
        still_review = False
        for (other_payload,) in conn.execute("SELECT payload_json FROM candidates WHERE job_id=? "
                                             "AND materialization_state='needs_review' AND candidate_id!=?",
                                             (job_id, cid)).fetchall():
            try:
                if sid in json.loads(other_payload).get("segment_ids", []):
                    still_review = True
                    break
            except ValueError:
                continue
        settlement = "needs_review" if (outcome == "needs_review"
                                        or applied.get("governance_status") == "staging") else "saved"
        if still_review:
            settlement = "needs_review"
        conn.execute("UPDATE segment_results SET settlement_state=?,candidate_id=?,"
                     "linked_ids_json=?,updated_at=? WHERE job_id=? AND segment_id=? "
                     "AND settlement_state='needs_review'",
                     (settlement, cid,
                      json.dumps([applied["memory_id"]] if applied.get("memory_id") else []),
                      _now(), job_id, sid))
    job_row = conn.execute("SELECT state,batch_total FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    if job_row and job_row[0] == "needs_review":
        review = conn.execute("SELECT COUNT(*) FROM segment_results WHERE job_id=? "
                              "AND settlement_state='needs_review'", (job_id,)).fetchone()[0]
        cursor = materials.contiguous_cursor(conn, job_id)
        if review == 0 and cursor == job_row[1]:
            conn.execute("UPDATE jobs SET state='succeeded',batch_cursor=?,updated_at=? "
                         "WHERE job_id=?", (cursor, _now(), job_id))
            _settle_session_cursor(conn, job_id)
    conn.commit()
    return {**applied, "superseded_candidate_key": candidate_key}


def sweep_review_candidates(conn, *, today=None, ttl_days=None):
    """Phase 1：review 候选超期自动丢弃（G6）。

    materialization_state='needs_review' 且 created_at 距今超过 ttl_days 天的行，
    置 decision/materialization_state='discarded' 并记入返回清单。只动候选行本身，
    不改 segment_results 与 job 状态（丢弃是候选级记账；job 仍可凭其余段结算，
    丢弃清单由 doctor 的 review_discard_ledger 检查项呈现）。
    created_at 无法解析的行保守跳过（不丢弃）。ttl_days 默认 30，与配置默认值一致；
    CLI 应传入配置的 evolution.review_candidate_ttl_days。"""
    if today is None:
        today = datetime.now(timezone.utc).date()
    elif isinstance(today, datetime):
        today = today.date()
    ttl = 30 if ttl_days is None else int(ttl_days)
    discarded = []
    for cid, jid, key, created in conn.execute("SELECT candidate_id,job_id,candidate_key,created_at "
                                              "FROM candidates WHERE materialization_state='needs_review'").fetchall():
        try:
            created_day = datetime.fromisoformat(created.replace("Z", "+00:00")).date()
        except (ValueError, AttributeError):
            continue
        if (today - created_day).days <= ttl:
            continue
        conn.execute("UPDATE candidates SET decision='discarded',materialization_state='discarded',"
                     "updated_at=? WHERE candidate_id=?", (_now(), cid))
        discarded.append({"candidate_id": cid, "job_id": jid, "candidate_key": key,
                          "created_at": created, "discarded_at": today.isoformat()})
    if discarded:
        conn.commit()
    return discarded
