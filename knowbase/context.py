"""执行中主题检索。语义查询由宿主提交；内核不调用模型或等待网络。"""

import json
import re
import time
from datetime import datetime, timezone

from . import config, index, materials, store
from .locking import RepoLock, LockTimeout

STAGES = {"task_plan", "subtask_start", "new_failure", "decision", "verified_result",
          "checkpoint", "before_compact", "recover", "observe"}
CAPTURE_STAGES = {"verified_result", "checkpoint", "before_compact"}


def provisional_global(scope, topics_json, coverage_json):
    bookkeeping = {"_hook_plan_notice", "_hook_notices"}
    return scope == "global" and not json.loads(topics_json) and not (set(json.loads(coverage_json)) - bookkeeping)


def _section(body: str, name: str) -> str:
    match = re.search(r"^#{1,4}\s*" + re.escape(name) + r"[^\n]*\n(.*?)(?=^#{1,4}\s|\Z)",
                      body, re.M | re.S)
    return match.group(1).strip() if match else "unknown"


def _summary(body: str) -> str:
    """兼容旧卡三小节，但不把缺失的适用条件猜成已知。"""
    from .extraction.sanitizer import sanitize
    conclusion = _section(body, "结论")
    if conclusion != "unknown":
        return sanitize(conclusion)[:500]
    for heading in ("正确做法", "解决方案", "处理方法"):
        existing = _section(body, heading)
        if existing != "unknown":
            return ("旧卡原文（" + heading + "）：" + sanitize(existing))[:500]
    excerpt = "\n".join(line for line in body.splitlines() if line.strip() and not line.lstrip().startswith("#"))
    return ("旧卡原文片段：" + sanitize(excerpt))[:500] if excerpt else "unknown"


def _candidate(repo, mid, scope, automatic, timeout):
    from .mutations import _compute_revision
    with RepoLock(repo, timeout=timeout):
        meta, body, path = store.load(repo, mid)
        if (not meta or path.parent.name == "staging" or meta.get("type") == "reference"
                or meta.get("status") != "active" or meta.get("scope") not in (scope,"global")
                or (automatic and meta.get("confidence") != "verified")
                or any(r.get("type")=="contradicts" for r in meta.get("relations",[]))):
            return None
        return {"id":mid,"scope":meta["scope"],"content_revision":_compute_revision(meta,body),
                "title":meta.get("title", ""),"summary":_summary(body),
                "applicable_when":_section(body,"适用条件")[:300],
                "confidence":meta.get("confidence", "once"),"status":meta["status"]}


def execute(repo, *, session_id: str, event_id: str, scope: str = "",
            stage: str = "subtask_start", task_id: str = "", task_revision: str = "",
            current_goal: str = "", topics=None, evidence_refs=None, observed_cursor: str = "",
            request_id: str = "", host_profile: str = "", automatic: bool = False, project_path: str = "",
            observation: dict | None = None) -> dict:
    started = time.monotonic()
    cfg = config.load_config()
    options = cfg["inflight"]
    host = host_profile or options.get("host_profile") or config.agent_name()
    if not session_id or not event_id or stage not in STAGES:
        return {"error": "INVALID_CONTEXT_EVENT"}
    topics = topics or []
    if not isinstance(topics, list) or len(topics) > 128 or not all(isinstance(t, dict) for t in topics):
        return {"error": "INVALID_TOPICS", "max_topics": 128}
    payload = {"scope": scope, "stage": stage, "task_id": task_id, "task_revision": task_revision,
               "current_goal": current_goal, "topics": topics, "evidence_refs": evidence_refs or [],
               "observed_cursor": observed_cursor, "automatic": automatic, "project_path": project_path,
               "observation_hash": materials.digest(observation)}
    phash = materials.digest(payload)
    conn = index.connect(repo)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        existing = conn.execute("SELECT payload_hash,result_json FROM context_events "
                                "WHERE host_profile=? AND session_id=? AND event_id=?",
                                (host, session_id, event_id)).fetchone()
        if existing:
            return (json.loads(existing[1]) if existing[0] == phash
                    else {"error": "IDEMPOTENCY_CONFLICT"})
        prior = conn.execute("SELECT scope,task_revision,topics_json,coverage_json,observed_cursor,settled_cursor "
                             "FROM context_sessions WHERE host_profile=? AND session_id=?",
                             (host, session_id)).fetchone()
        # 开场缺项目时建立的空 global 占位不应锁死稍后才出现的正式项目。
        if (prior and provisional_global(prior[0], prior[2], prior[3])
                and ((scope and scope != "global") or project_path)):
            prior = None
        result = {"event_id": event_id, "capability_mode": options.get("mode", "assisted"),
                  "attribution": {"host_profile": host, "member_label": options.get("member_label", "unknown"),
                                  "device_label": options.get("device_label", "unknown")},
                  "retrieval_status": "no_hit", "candidates": [], "next_topics": [],
                  "capture_due": stage in CAPTURE_STAGES, "job_ids": [], "pending_segments": 0,
                  "resume_cursor": observed_cursor or (prior[4] if prior else ""),
                  "settled_cursor": prior[5] if prior else "",
                  "budget_used": {"queries": 0, "tokens_estimated": 0}, "warnings": []}
        if prior and scope and prior[0] != scope:
            result.update(error="SCOPE_MISMATCH", retrieval_status="scope_unresolved")
        else:
            scope = scope or (prior[0] if prior else "")
            if not scope:
                from .hooks import _infer_scope, has_project_context
                if project_path:
                    from pathlib import Path
                    path = Path(project_path).expanduser().resolve()
                    scope = config.mapped_scope(project_path, cfg) or ""
                else:
                    scope = _infer_scope(conn) or ("" if has_project_context() else "global")
            if not scope:
                result.update(error="SCOPE_UNRESOLVED", retrieval_status="scope_unresolved")
        if not result.get("error"):
            result["scope"] = scope
            from .refresh import request as request_refresh
            result["freshness"] = request_refresh(repo,cfg)
            if result["freshness"].get("warning"):
                result["warnings"].append(result["freshness"]["warning"])
            if not task_revision and prior:
                task_revision = prior[1]
            if not topics and prior:
                topics = json.loads(prior[2])
            elif topics and prior and task_revision == prior[1] and stage != "task_plan":
                merged = {t.get("topic_id", ""): t for t in json.loads(prior[2])}
                merged.update({t.get("topic_id", ""): t for t in topics})
                topics = list(merged.values())
            if not topics and current_goal:
                topics = [{"topic_id": task_id or "current", "queries": [current_goal], "status": "active"}]
            previous_coverage = json.loads(prior[3]) if prior else {}
            coverage = (previous_coverage if prior and prior[1] == task_revision else
                        {k: v for k, v in previous_coverage.items() if k.startswith("_hook_")})
            knowledge_version = materials.digest(list(conn.execute(
                "SELECT e.memory_id,e.content_hash,m.confidence,m.status,m.staging,h.content_revision FROM embedding_index e "
                "JOIN meta m ON m.id=e.memory_id LEFT JOIN entity_heads h ON h.entity_id=m.id "
                "WHERE m.scope IN (?, 'global') ORDER BY e.memory_id", (scope,))))
            seen, found = set(), []
            deferred_sync = False
            performed = 0
            limit = max(1, min(20, int(options["max_queries_per_event"])))
            per_topic = max(1, min(6, int(options["max_queries_per_topic"])))
            budget_chars = max(128, min(32000, int(options["context_token_budget"]) * 2))
            remaining = budget_chars
            deferred = []
            for topic in topics:
                tid = str(topic.get("topic_id", ""))
                if topic.get("status", "active") not in ("active", "current"):
                    deferred.append(topic)
                    continue
                queries = topic.get("queries") or [current_goal]
                if not isinstance(queries, list) or not all(isinstance(q, str) for q in queries):
                    result.update(error="INVALID_QUERIES", retrieval_status="failed")
                    break
                signature = materials.digest({"topic": topic, "knowledge": knowledge_version,
                    "scope": scope, "evidence": ([r.get("error_hash") for r in (evidence_refs or []) if isinstance(r, dict)]
                                                  if automatic else evidence_refs or []), "task_revision": task_revision,
                    "automatic": automatic})
                cached = coverage.get(tid)
                completed = (set(cached.get("queries", [])) if isinstance(cached, dict)
                             and cached.get("signature") == signature and stage != "recover" else set())
                topic_performed = 0
                for query in queries:
                    query = query.strip()
                    if not query or query in completed:
                        continue
                    if (performed >= limit or topic_performed >= per_topic or
                            (time.monotonic() - started) * 1000 > options["fast_path_timeout_ms"]):
                        break
                    hits = index.search(conn, query, scope=scope, limit=10)
                    index.record_search(conn, query, scope, hits, tool="inflight_search")
                    performed += 1
                    topic_performed += 1
                    completed.add(query)
                    for hit in hits:
                        if hit["id"] in seen:
                            continue
                        remaining_time = options["fast_path_timeout_ms"]/1000 - (time.monotonic()-started)
                        try:
                            if remaining_time <= 0:
                                raise LockTimeout("读取预算耗尽")
                            candidate = _candidate(repo,hit["id"],scope,automatic,min(0.02,remaining_time))
                        except LockTimeout:
                            completed.discard(query)
                            deferred_sync = True
                            break
                        if not candidate:
                            continue
                        candidate["topic_ids"] = [tid]
                        seen.add(hit["id"])
                        size = len(json.dumps(candidate, ensure_ascii=False))
                        if len(found) < options["max_candidates"] and size <= remaining:
                            found.append(candidate)
                            remaining -= size
                coverage[tid] = {"signature": signature, "queries": sorted(completed)}
                unresolved = [q for q in queries if q.strip() and q.strip() not in completed]
                if unresolved:
                    deferred.append({**topic, "status": "deferred", "remaining_queries": unresolved})
            result["candidates"] = found
            result["next_topics"] = deferred
            result["budget_used"] = {"queries": performed, "tokens_estimated": (budget_chars - remaining + 1) // 2}
            if not result.get("error"):
                result["retrieval_status"] = ("deferred_sync" if deferred_sync else "deferred_budget" if any(t.get("status") == "deferred" for t in deferred)
                    else "executed" if found else "no_hit" if performed else "skipped_cache")
            result["job_ids"] = [r[0] for r in conn.execute(
                "SELECT job_id FROM jobs WHERE scope=? AND state IN ('awaiting_agent','validating') "
                "ORDER BY created_at LIMIT ?", (scope, options["max_batches_per_opportunity"]))]
            result["pending_segments"] = conn.execute(
                "SELECT COUNT(*) FROM job_segments s LEFT JOIN segment_results r ON "
                "r.job_id=s.job_id AND r.segment_id=s.segment_id WHERE s.job_id IN "
                "(SELECT job_id FROM jobs WHERE scope=?) AND COALESCE(r.settlement_state,'pending') "
                "NOT IN ('saved','updated','linked_existing','needs_review','no_candidate')", (scope,)).fetchone()[0]
            conn.execute("INSERT INTO context_sessions(host_profile,session_id,scope,task_revision,topics_json,"
                         "observed_cursor,coverage_json,updated_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT "
                         "(host_profile,session_id) DO UPDATE SET task_revision=excluded.task_revision,"
                         "scope=excluded.scope,"
                         "topics_json=excluded.topics_json,observed_cursor=excluded.observed_cursor,"
                         "coverage_json=excluded.coverage_json,updated_at=excluded.updated_at",
                         (host, session_id, scope, task_revision, json.dumps(topics, ensure_ascii=False),
                          result["resume_cursor"], json.dumps(coverage), now))
        conn.execute("INSERT INTO context_events(host_profile,session_id,event_id,task_id,trigger,"
                     "payload_hash,status,reason,result_json,latency_ms,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (host, session_id, event_id, task_id, stage, phash, result["retrieval_status"],
                      result.get("error", ""), json.dumps(result, ensure_ascii=False),
                      (time.monotonic() - started) * 1000, now))
        if observation:
            conn.execute("UPDATE context_events SET observation_json=? WHERE host_profile=? AND session_id=? AND event_id=?",
                         (json.dumps(observation, ensure_ascii=False), host, session_id, event_id))
        conn.commit()
        return result
    except Exception as exc:
        conn.rollback()
        failure = {"event_id": event_id, "retrieval_status": "failed",
                   "error": type(exc).__name__, "candidates": [], "capture_due": False}
        # 并发相同事件可能由另一进程先完成；同 payload 返回其原回执。
        try:
            row = conn.execute("SELECT payload_hash,result_json FROM context_events WHERE "
                               "host_profile=? AND session_id=? AND event_id=?",
                               (host, session_id, event_id)).fetchone()
            if row:
                return json.loads(row[1]) if row[0] == phash else {"error": "IDEMPOTENCY_CONFLICT"}
            conn.execute("INSERT INTO context_events(host_profile,session_id,event_id,task_id,trigger,"
                         "payload_hash,status,reason,result_json,latency_ms,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         (host, session_id, event_id, task_id, stage, phash, "failed", type(exc).__name__,
                          json.dumps(failure), (time.monotonic() - started) * 1000, now))
            if observation:
                conn.execute("UPDATE context_events SET observation_json=? WHERE host_profile=? AND session_id=? AND event_id=?",
                             (json.dumps(observation, ensure_ascii=False), host, session_id, event_id))
            conn.commit()
        except Exception:
            conn.rollback()  # 数据库本身不可写时返回明确失败，不能伪装成 no_hit。
        return failure
    finally:
        conn.close()
