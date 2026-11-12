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
import shlex
from datetime import datetime, timezone

from . import contracts

GENERIC_COMMANDS = re.compile(
    r"^(pytest|python -m pytest|git status|git log|git diff|ls|ls -la|cat|pwd|cd|echo|"
    r"which|env|node -v|python --version)\b", re.I)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


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


def _actions(conn, run_id, result):
    rows = conn.execute(
        "SELECT c.tool_name,c.event_json,c.occurred_at,c.tool_call_id,r.event_json "
        "FROM native_events c JOIN native_events r ON r.run_id=c.run_id "
        "AND r.tool_call_id=c.tool_call_id AND r.event_kind='tool_result' AND r.rowid>c.rowid "
        "WHERE c.run_id=? AND c.event_kind='tool_call' AND c.tool_call_id!=''",
        (run_id,)).fetchall()
    for tool, raw_args, occurred, call_id, raw_result in rows:
        args, observed = json.loads(raw_args), json.loads(raw_result)
        if observed.get("succeeded") is False or observed.get("exit_code") not in (0, "0", None):
            continue
        command = args.get("command", "")
        if GENERIC_COMMANDS.match(command):
            result["generic_rejected"].append(command[:80])
            continue
        action = ""
        if tool.casefold() in ("edit", "write", "multiedit", "replace", "write_file", "edit_file"):
            if observed.get("succeeded") is not True:
                continue
            if args.get("delivery_id"):
                action = args.get("file_path") or args.get("filePath") or args.get("path") or ""
        elif tool.casefold() in ("bash", "shell", "terminal", "run_command", "exec_command") and command:
            if observed.get("exit_code") not in (0, "0") or re.search(r"[;|<>\n`$()]|(?<!&)&(?!&)", command):
                continue
            try:
                argv = shlex.split(command.split("&&", 1)[0])
            except ValueError:
                continue
            if argv and argv[0] in ("python", "python3", "bash", "sh") and len(argv) >= 2:
                if argv[1].endswith((".py", ".sh")):
                    action = shlex.join(argv)
        if action:
            yield action, occurred, call_id, args.get("delivery_id", "")


def correlate(conn, repo, run_id: str, *, matcher_version="attribution-v2") -> dict:
    """关联任务内实际递送、链接的读取以及读取后的成功动作。"""
    from .. import store
    from ..mutations import _compute_revision
    from .observation import run_summary

    summary = run_summary(conn, run_id)
    if summary.get("error"):
        return summary
    host, session, task = summary["host_profile"], summary["session_id"], summary["task_id"]
    logical = summary["logical_task_key"]
    issuer = f"local:{matcher_version}"
    results = {"L0": [], "L1": [], "L2": [], "ambiguous": [], "generic_rejected": [],
               "adopted_revisions": []}
    if not task:
        return {"run_id": run_id, "logical_task_key": logical, **results}
    deliveries = conn.execute(
        "SELECT delivery_id,memory_id,content_revision,created_at,scope FROM retrieval_deliveries "
        "WHERE host_profile=? AND session_id=? AND task_id=?", (host, session, task)).fetchall()
    actions = list(_actions(conn, run_id, results))
    matches = {}
    for delivery_id, mid, revision, delivered_at, scope in deliveries:
        key = (mid, revision)
        fields = dict(memory_id=mid, revision_algo="content-v1", content_revision=revision,
                      run_id=run_id, logical_task_key=logical, independence_group="", issuer_id=issuer)
        payload = {"delivery_id": delivery_id, "scope": scope}
        _record_event(conn, event_type="knowledge.exposed", level="L0", polarity="neutral",
                      payload=payload, **fields)
        if mid not in results["L0"]:
            results["L0"].append(mid)
        uses = conn.execute(
            "SELECT u.use_id,u.state,u.created_at FROM use_retrieval_links l "
            "JOIN knowledge_uses u ON u.use_id=l.use_id WHERE l.delivery_id=? AND u.task_id=? "
            "AND u.memory_id=? AND u.content_revision=? AND u.current_version=1 "
            "AND u.host_profile=? AND u.session_id=?",
            (delivery_id, task, mid, revision, host, session)).fetchall()
        references = [u for u in uses if u[1] in ("read", "referenced") and u[2] >= delivered_at]
        if references:
            _record_event(conn, event_type="knowledge.referenced", level="L1", polarity="neutral",
                          payload={**payload, "use_id": references[0][0]}, **fields)
            if mid not in results["L1"]:
                results["L1"].append(mid)
        meta, body, path = store.load(repo, mid)
        if not meta or meta.get("status") != "active" or path.parent.name == "staging":
            continue
        if _compute_revision(meta, body) != revision:
            continue
        explicit = next((u for u in uses if u[1] == "adopted" and u[2] >= delivered_at), None)
        if explicit:
            matches.setdefault(key, []).append((fields, {**payload, "use_id": explicit[0],
                "association_basis": "explicit_adoption"}, ""))
        if not references:
            continue
        section = re.search(r"^## +可执行动作[^\n]*\n(.*?)(?=^## +|\Z)", body, re.M | re.S)
        signatures = set(re.findall(r"`((?:python3?|bash|sh) [^`\n]+)`", section[1] if section else ""))
        for action, at, call_id, token in actions:
            if (at > min(u[2] for u in references)
                    and ((token == delivery_id) if token else action in signatures)):
                matches.setdefault(key, []).append((fields, {**payload, "tool_call_id": call_id,
                    "action": action, "association_basis": "bound_action" if token else "exact_action_match"}, call_id))
    owners = {}
    for key, candidates in matches.items():
        for _, _, call_id in candidates:
            if call_id:
                owners.setdefault(call_id, set()).add(key)
    for (mid, revision), candidates in matches.items():
        match = next((item for item in candidates if not item[2] or len(owners[item[2]]) == 1), None)
        if not match:
            results["ambiguous"].append(mid)
            continue
        fields, payload, _ = match
        _record_event(conn, event_type="knowledge.adopted", level="L2", polarity="neutral",
                      payload=payload, trust_basis=payload["association_basis"], **fields)
        results["L2"].append(mid)
        results["adopted_revisions"].append((mid, revision))
    conn.commit()
    return {"run_id": run_id, "logical_task_key": logical, **results}


# ---------- V2-11 可信 L3 ----------

def registered_validators() -> dict:
    """已登记验证器（本机私有配置）：validator_id → {kind}。

    未登记的来源（包括 Agent 自报 independent）没有 L3 资格。"""
    from .. import config
    raw = (config.load_config().get("knowledge_lifecycle") or {}).get("validators") or []
    return {v.get("validator_id"): {"kind": v.get("kind", "")}
            for v in raw if isinstance(v, dict) and v.get("validator_id")}


def record_execution(conn, *, memory_id: str, content_revision: str,
                     artifact_hash: str, run_id: str = "", validator_id: str = "",
                     executed_at: str = "", binding=None, report=None, result=None) -> str:
    """登记原件及可信执行计划的指纹。旧式执行戳本身没有治理效力。"""
    execution_id = hashlib.sha256(f"{memory_id}:{artifact_hash}".encode()).hexdigest()[:20]
    conn.execute(
        "INSERT OR IGNORE INTO validator_executions "
        "(execution_id,memory_id,content_revision,artifact_hash,run_id,validator_id,executed_at,"
        "binding_hash,checks_hash,independence_group,source_key,verdict) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (execution_id, memory_id, content_revision, artifact_hash, run_id or "", validator_id or "",
         executed_at or _now(), binding["binding_hash"] if binding else "",
         _checks_hash(report["checks"]) if binding else "",
         binding["independence_group"] if binding else "", result["source_key"] if binding else "",
         result["verdict"] if binding else ""))
    conn.commit()
    return execution_id


def _checks_hash(checks):
    return hashlib.sha256(contracts.canonical_json(checks).encode()).hexdigest()


def verify_execution(conn, repo, *, run_id, validator_id, report) -> dict:
    """校验预登记计划、执行原件、检查项与当前知识版本后才入账。"""
    from .. import store
    from ..mutations import _compute_revision
    from . import governance, validators
    from .observation import run_summary

    if validator_id not in registered_validators():
        return {"error": "VALIDATOR_NOT_REGISTERED"}
    target = report.get("target") or {}
    memory_id, revision = target.get("memory_id"), target.get("content_revision")
    if not memory_id or not revision:
        return {"error": "TARGET_REQUIRED"}
    stamp = report.get("execution_stamp") or {}
    artifact_hash = report.get("artifact_hash")
    checks = report.get("checks") or []
    if not artifact_hash or not checks:
        return {"error": "EMPTY_REPORT"}
    if not stamp:
        return {"error": "EXECUTION_STAMP_REQUIRED"}
    if (stamp.get("memory_id"), stamp.get("content_revision")) != (memory_id, revision):
        return {"error": "STALE_EXECUTION_RECORD"}
    rec = conn.execute(
        "SELECT content_revision,run_id,validator_id,binding_hash,checks_hash,independence_group,"
        "source_key,verdict FROM validator_executions WHERE memory_id=? AND artifact_hash=?",
        (memory_id, artifact_hash)).fetchone()
    if not rec:
        return {"error": "EXECUTION_RECORD_MISSING"}
    if rec[0] != revision:
        return {"error": "STALE_EXECUTION_RECORD"}
    if rec[1:3] != (run_id or "", validator_id) or (stamp.get("run_id") or "", stamp.get("validator_id")) != rec[1:3]:
        return {"error": "EXECUTION_IDENTITY_MISMATCH"}
    meta, body, _ = store.load(repo, memory_id)
    if not meta:
        return {"error": "NOT_FOUND"}
    if _compute_revision(meta, body) != revision:
        return {"error": "STALE_EXECUTION_RECORD"}
    binding = validators.binding_for(validator_id, meta, body, revision)
    if not binding or not rec[3] or binding["binding_hash"] != rec[3]:
        return {"error": "VALIDATOR_BINDING_REQUIRED"}
    if _checks_hash(checks) != rec[4]:
        return {"error": "VALIDATOR_CHECKS_MISMATCH"}
    summary = run_summary(conn, run_id) if run_id else {}
    if run_id and (summary.get("error") or summary.get("project_key") != binding.get("project_key")):
        return {"error": "VALIDATOR_TASK_PROJECT_MISMATCH"}
    if not verification_workspace_matches(summary, binding):
        return {"error": "VALIDATOR_TASK_WORKSPACE_MISMATCH"}
    outcome = rec[7]
    if outcome not in ("pass", "fail"):
        return {"memory_id": memory_id, "content_revision": revision, "outcome": "unknown",
                "validator": validator_id, "checks_count": len(checks)}
    task = summary.get("logical_task_key", "") if summary.get("identity_basis") in ("native", "host_prompt") else ""
    payload = {"validator_id": validator_id, "scope": meta["scope"], "artifact_hash": artifact_hash,
               "checks": checks[:12], "checks_hash": rec[4], "outcome": outcome,
               "junit_sha256": report.get("junit_sha256", ""),
               "contract_fingerprint": binding["contract_fingerprint"],
               "binding_hash": rec[3], "validation_source_key": rec[6],
               "origin_run_key": summary.get("origin_run_key", "")}
    event_id = _record_event(conn, event_type="validation.completed", memory_id=memory_id,
        revision_algo="content-v1", content_revision=revision, level="L3",
        polarity="positive" if outcome == "pass" else "negative", run_id=run_id or "",
        logical_task_key=task, independence_group=rec[5], payload=payload,
        issuer_id=f"validator:{validator_id}", trust_basis="registered-adapter",
        native_key=f"{run_id}:{memory_id}:{revision}:{rec[3]}:{rec[6]}:{outcome}")
    conn.commit()
    projected = governance.project_after_evidence(repo, conn, memory_id)
    return {"memory_id": memory_id, "content_revision": revision, "outcome": outcome,
            "validator": validator_id, "checks_count": len(checks), "event_id": event_id,
            "governance": projected}


def run_verification(conn, repo, *, memory_id, revision, validator_id, binding, run_id="", timeout=600):
    from . import validators
    from .observation import run_summary

    if run_id:
        summary = run_summary(conn, run_id)
        if summary.get("error") or summary.get("project_key") != binding.get("project_key"):
            return {"error": "VALIDATOR_TASK_PROJECT_MISMATCH"}, None
        if not verification_workspace_matches(summary, binding):
            return {"error": "VALIDATOR_TASK_WORKSPACE_MISMATCH"}, None

    result = validators.run_registered(binding, memory_id=memory_id, revision=revision, run_id=run_id,
                                       validator_id=validator_id, executed_at=_now(), timeout=timeout)
    if result.get("error"):
        return result, None
    report = result["report"]
    record_execution(conn, memory_id=memory_id, content_revision=revision, artifact_hash=report["artifact_hash"],
                     run_id=run_id, validator_id=validator_id, binding=binding, report=report, result=result)
    if result["verdict"] == "unknown":
        return {"outcome": "unknown", "executed_command_exit": result["exit_code"]}, result["raw"]
    report["target"] = {"memory_id": memory_id, "content_revision": revision}
    verified = verify_execution(conn, repo, run_id=run_id, validator_id=validator_id, report=report)
    return {**verified, "executed_command_exit": result["exit_code"]}, result["raw"]


def verification_workspace_matches(run, binding):
    from .observe import workspace_fingerprint

    observed = run.get("environment_fingerprint", "")
    if not observed.startswith("workspace:"):
        return not binding.get("auto") or not run
    return bool(binding.get("workspace") and workspace_fingerprint(binding["workspace"]) == observed)


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
