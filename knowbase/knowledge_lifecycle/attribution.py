"""自动采用关联 L0/L1/L2（V2-10）与可信 L3 执行验证（V2-11）。

分层语义（设计 §8.1）：
- L0 曝光：delivery 记录存在（真实递送）。
- L1 引用/阅读线索：read/referenced 回执。
- L2 采用关联：任务内工具执行与卡的具体动作/产物形成可追溯映射——精确
  动作（文件路径/命令签名/code 路径）命中且不歧义才 L2；通用命令
  （pytest/git status 等）与纯文本相似不算采用；同动作命中多卡 → ambiguous
  不发奖。任务成功不反推采用。
- L3 有效性：只有已登记验证器核对真实执行报告（断言覆盖、产物 hash、
  期望对比）才产生 trusted 事件；Agent 自报（含 verification_source=
  independent）一律不是 L3。

事件落 evidence_events（本地 ledger；跨机交换在 V2-13）。
"""
import hashlib
import json
import re
from datetime import datetime, timezone

from . import contracts

GENERIC_COMMANDS = re.compile(
    r"^(pytest|python -m pytest|git status|git log|git diff|ls|ls -la|cat|pwd|cd|echo|"
    r"which|env|node -v|python --version)\b", re.I)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _record_event(conn, *, event_type, memory_id, revision_algo, content_revision,
                  level, polarity, run_id, logical_task_key, independence_group,
                  payload, issuer_id="local-attribution", trust_basis="",
                  claim_id="", native_key=""):
    event_id = contracts.stable_event_id(
        issuer_id, native_key or f"{run_id}:{memory_id}:{content_revision}:{claim_id}",
        event_type, {"memory_id": memory_id, "revision_algo": revision_algo,
                     "content_revision": content_revision, "claim_id": claim_id},
        event_type)
    payload_hash = hashlib.sha256(
        contracts.canonical_json(payload).encode("utf-8")).hexdigest()
    conn.execute(
        "INSERT OR IGNORE INTO evidence_events(event_id,payload_hash,event_type,"
        "subject_revision,run_id,logical_task_key,independence_group,issuer_id,"
        "trust_basis,level,polarity,supersedes_event_id,revokes_event_id,payload_json,"
        "import_state,occurred_at,claim_id,scope) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (event_id, payload_hash, event_type,
         f"{memory_id}@{revision_algo}:{content_revision}", run_id,
         logical_task_key, independence_group, issuer_id, trust_basis, level,
         polarity, "", "", json.dumps(payload, ensure_ascii=False)[:8192],
         "local", _now(), claim_id, payload.get("scope", "")))
    return event_id


def correlate(conn, repo, run_id: str, *, matcher_version="attribution-v1") -> dict:
    """把一个任务运行的观测关联为 L0/L1/L2 证据事件。

    输入：task_runs 行 + 同 (host,session) 的 native_events + 递送记录
    （retrieval_deliveries 按 session 关联）。卡的动作签名取自正文
    「可执行动作」小节的 code: 引用、文件路径与命令首词。
    """
    from .observation import run_summary
    summary = run_summary(conn, run_id)
    if summary.get("error"):
        return summary
    host, session = summary["host_profile"], summary["session_id"]
    issuer = f"local:{matcher_version}"
    # 该会话的递送（search → delivery → memory_id@revision）。
    deliveries = conn.execute(
        "SELECT d.delivery_id,d.memory_id,d.content_revision FROM retrieval_deliveries d "
        "WHERE d.host_profile=? AND d.session_id=?", (host, session)).fetchall()
    reads = {r[0] for r in conn.execute(
        "SELECT memory_id FROM knowledge_uses WHERE host_profile=? AND session_id=? "
        "AND state IN ('read','referenced')", (host, session))}
    results = {"L0": [], "L1": [], "L2": [], "ambiguous": [], "generic_rejected": []}
    group = summary["logical_task_key"]
    # 工具调用负载（脱敏后）里的可匹配 token。
    tool_tokens = set()
    for payload_json, tool_name in conn.execute(
            "SELECT event_json,tool_name FROM native_events WHERE host_profile=? AND "
            "session_id=? AND event_kind='tool_call'", (host, session)):
        try:
            payload = json.loads(payload_json)
        except ValueError:
            continue
        text = json.dumps(payload, ensure_ascii=False)
        tool_tokens |= set(re.findall(r"[\w./-]{4,}", text))
        if GENERIC_COMMANDS.match(str(payload.get("command") or "")):
            results["generic_rejected"].append(str(payload.get("command"))[:80])
    from .. import store
    matched_tokens_by_card = {}
    for delivery_id, memory_id, revision in deliveries:
        _record_event(conn, event_type="knowledge.exposed", memory_id=memory_id,
                      revision_algo="content-v1", content_revision=revision,
                      level="L0", polarity="neutral", run_id=run_id,
                      logical_task_key=group, independence_group=group,
                      payload={"delivery_id": delivery_id, "run_id": run_id,
                               "scope": (store.load(repo, memory_id)[0] or {}).get("scope", "")},
                      issuer_id=issuer)
        results["L0"].append(memory_id)
        if memory_id in reads:
            _record_event(conn, event_type="knowledge.referenced", memory_id=memory_id,
                          revision_algo="content-v1", content_revision=revision,
                          level="L1", polarity="neutral", run_id=run_id,
                          logical_task_key=group, independence_group=group,
                          payload={"read": True, "run_id": run_id,
                               "scope": (store.load(repo, memory_id)[0] or {}).get("scope", "")},
                      issuer_id=issuer)
            results["L1"].append(memory_id)
        meta, body, _ = store.load(repo, memory_id)
        if not meta:
            continue
        # 卡的动作签名：code: 引用、code_refs 路径、正文文件路径 token。
        signatures = set()
        for ref in meta.get("code_refs") or []:
            if isinstance(ref, dict) and ref.get("path"):
                signatures.add(ref["path"])
        for m in re.finditer(r"code:[\w.-]+/([\w./-]+)", body or ""):
            signatures.add(m.group(1))
        # 动作签名：含路径分隔的文件路径（带或不带反引号）。纯文件名过短
        # 易误命中，要求带 / 或至少 8 字符。
        for m in re.finditer(r"`?([\w][\w./-]*?/[\w./-]+\.(?:py|ts|js|java|md|json))`?",
                             body or ""):
            path_sig = m.group(1)
            if "/" in path_sig or len(path_sig) >= 8:
                signatures.add(path_sig)
        hits = {s for s in signatures if s in tool_tokens or
                any(s in t or t in s for t in tool_tokens if len(t) >= 6)}
        if hits:
            matched_tokens_by_card[memory_id] = hits
    # 歧义裁决：同一 token 命中多张已递送卡 → 不发奖。
    token_owners = {}
    for memory_id, hits in matched_tokens_by_card.items():
        for hit in hits:
            token_owners.setdefault(hit, set()).add(memory_id)
    for memory_id, hits in sorted(matched_tokens_by_card.items()):
        ambiguous = any(len(token_owners[h]) > 1 for h in hits)
        if ambiguous:
            results["ambiguous"].append(memory_id)
            continue
        revision = next(d[2] for d in deliveries if d[1] == memory_id)
        _record_event(conn, event_type="knowledge.adopted", memory_id=memory_id,
                      revision_algo="content-v1", content_revision=revision,
                      level="L2", polarity="positive", run_id=run_id,
                      logical_task_key=group, independence_group=group,
                      payload={"action_tokens": sorted(hits)[:10],
                               "matcher_version": matcher_version,
                               "association_basis": "exact_action_match",
                               "scope": (store.load(repo, memory_id)[0] or {}).get("scope", "")},
                      issuer_id=issuer, trust_basis="deterministic_matcher")
        results["L2"].append(memory_id)
    conn.commit()
    return {"run_id": run_id, "logical_task_key": group, **results}


# ---------- V2-11 可信 L3 ----------

def registered_validators() -> dict:
    """已登记验证器（本机私有配置）：validator_id → {kind}。

    未登记的来源（包括 Agent 自报 independent）没有 L3 资格。"""
    from .. import config
    raw = (config.load_config().get("knowledge_lifecycle") or {}).get("validators") or []
    return {v.get("validator_id"): {"kind": v.get("kind", "")}
            for v in raw if isinstance(v, dict) and v.get("validator_id")}


def record_execution(conn, *, memory_id: str, content_revision: str,
                     artifact_hash: str, run_id: str = "",
                     validator_id: str = "", executed_at: str = "") -> str:
    """登记一次真实执行（lifecycle verify --run 在执行时调用）。

    execution_id 由 memory_id+artifact_hash 派生，重复投喂同一原件幂等。
    """
    execution_id = hashlib.sha256(
        f"{memory_id}:{artifact_hash}".encode("utf-8")).hexdigest()[:20]
    conn.execute(
        "INSERT OR REPLACE INTO validator_executions"
        "(execution_id,memory_id,content_revision,artifact_hash,run_id,validator_id,executed_at)"
        " VALUES(?,?,?,?,?,?,?)",
        (execution_id, memory_id, content_revision, artifact_hash,
         run_id or "", validator_id or "", executed_at or _now()))
    conn.commit()
    return execution_id


def verify_execution(conn, repo, *, run_id, validator_id, report) -> dict:
    """核对一份真实执行报告，产出 L3 或 unknown。

    report：{artifact_hash, expected_hash, execution_stamp, checks:[...],
    target:{memory_id, content_revision, claim_id?}}。
    硬门：
    - validator 已登记；
    - 报告含非空 artifact_hash 与至少一个 check（空报告/自报不能过）；
    - 执行绑定（统一生命周期）：报告必须带执行戳，且 validator_executions
      存在 (memory_id, artifact_hash) 的执行记录，戳内版本、记录版本与目标
      版本三方一致——卡片改版后旧报告不再绑定新正文；
    - observed_result 全为 pass 才 pass；任何 fail → fail；缺失 → unknown；
    - claim 级核对：checks 引用的 claim_id 必须存在于卡（防“跑过测试但没
      测该卡的断言”）。
    """
    validators = registered_validators()
    if validator_id not in validators:
        return {"error": "VALIDATOR_NOT_REGISTERED",
                "detail": "only registered adapters produce trusted L3 evidence"}
    target = report.get("target") or {}
    memory_id = target.get("memory_id", "")
    revision = target.get("content_revision", "")
    if not memory_id or not revision:
        return {"error": "TARGET_REQUIRED"}
    stamp = report.get("execution_stamp") or {}
    artifact_hash = str(report.get("artifact_hash") or "")
    checks = report.get("checks") or []
    if not artifact_hash or not checks:
        return {"error": "EMPTY_REPORT", "detail": "no artifact or no checks: unknown, not pass"}
    if not stamp:
        return {"error": "EXECUTION_STAMP_REQUIRED",
                "detail": "reports must be stamped at execution time "
                          "(lifecycle verify --run); unstamped artifacts "
                          "cannot validate a specific card version"}
    if stamp.get("memory_id") != memory_id or stamp.get("content_revision") != revision:
        return {"error": "STALE_EXECUTION_RECORD",
                "detail": f"report was executed for {stamp.get('memory_id')}"
                          f"@{stamp.get('content_revision')}, "
                          f"not {memory_id}@{revision}"}
    rec = conn.execute(
        "SELECT content_revision FROM validator_executions WHERE memory_id=? AND artifact_hash=?",
        (memory_id, artifact_hash)).fetchone()
    if not rec:
        return {"error": "EXECUTION_RECORD_MISSING",
                "detail": "no validator_executions row for this artifact: "
                          "feed must reference a run recorded at execution time"}
    if rec[0] != revision:
        return {"error": "STALE_EXECUTION_RECORD",
                "detail": f"execution recorded for revision {rec[0]}, "
                          f"card is now {revision}"}
    from .. import store
    meta, body, _ = store.load(repo, memory_id)
    if not meta:
        return {"error": "NOT_FOUND"}
    claim_ids = {c.get("claim_id") for c in (meta.get("claims") or [])
                 if isinstance(c, dict)} or {"C-whole"}
    unknown_claims = [c.get("check_id") for c in checks
                      if c.get("claim_id") and c["claim_id"] not in claim_ids]
    results = [c.get("observed_result") for c in checks]
    if unknown_claims:
        outcome = "unknown"
        reason = f"checks reference unknown claims: {unknown_claims[:3]}"
    elif any(r == "fail" for r in results):
        outcome = "fail"
        reason = "observed failure in claimed checks"
    elif any(r != "pass" for r in results):
        outcome = "unknown"
        reason = "checks with missing/indeterminate results"
    else:
        outcome = "pass"
        reason = "all claimed checks passed with artifact"
    from .observation import run_summary
    summary = run_summary(conn, run_id) if run_id else {}
    event_type = "validation.completed"
    payload = {"validator_id": validator_id, "validator_kind": validators[validator_id]["kind"],
               "scope": (meta or {}).get("scope", ""),
               "artifact_hash": artifact_hash,
               "expected_hash": str(report.get("expected_hash") or ""),
               "checks": checks, "outcome": outcome, "reason": reason}
    checks_digest = hashlib.sha256(contracts.canonical_json(
        checks).encode("utf-8")).hexdigest()[:16]
    _record_event(conn, event_type=event_type, memory_id=memory_id,
                  revision_algo="content-v1", content_revision=revision,
                  level="L3",
                  polarity=("positive" if outcome == "pass"
                            else "negative" if outcome == "fail" else "neutral"),
                  run_id=run_id or "", logical_task_key=summary.get("logical_task_key", ""),
                  independence_group=summary.get("logical_task_key", ""),
                  payload=payload, issuer_id=f"validator:{validator_id}",
                  trust_basis="registered-adapter",
                  claim_id=target.get("claim_id", ""),
                  native_key=f"{run_id}:{memory_id}:{revision}:{validator_id}:{checks_digest}")
    conn.commit()
    return {"memory_id": memory_id, "content_revision": revision,
            "outcome": outcome, "reason": reason, "validator": validator_id,
            "checks_count": len(checks)}


def evidence_summary(conn, memory_id: str, content_revision: str) -> dict:
    """独立读回：一卡一版本的证据事件分层统计。"""
    rows = conn.execute(
        "SELECT level,event_type,polarity,trust_basis,logical_task_key,import_state "
        "FROM evidence_events WHERE subject_revision=?",
        (f"{memory_id}@content-v1:{content_revision}",)).fetchall()
    summary = {"L0": 0, "L1": 0, "L2": 0, "L3": 0, "trusted_L3": 0, "untrusted_L3": 0,
               "logical_tasks_L3": set(), "polarity_fail": 0}
    for level, _etype, polarity, trust, task, _state in rows:
        if level in summary:
            summary[level] += 1
        if level == "L3":
            if trust == "registered-adapter":
                summary["trusted_L3"] += 1
                summary["logical_tasks_L3"].add(task)
            else:
                summary["untrusted_L3"] += 1
        if polarity == "negative":
            summary["polarity_fail"] += 1
    summary["logical_tasks_L3"] = len(summary["logical_tasks_L3"])
    return summary
