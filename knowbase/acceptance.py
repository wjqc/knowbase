"""Machine-verifiable acceptance gates (Phase 3-B).

The four historical thresholds, rewritten as script/report verdicts.
"Real" keeps its original meaning — real task sessions with machine-
verifiable evidence — and is never downgraded to simulation: every
check below reads persisted evidence (eval datasets, aggregate event
imports, control reports) and recomputes its conclusion. A missing or
inconsistent input fails the check; nothing is left as an open item
for a human to close.
"""

import json
import math
from datetime import datetime, timezone
from pathlib import Path

from . import benefit_control, execution_eval
from .embedding_provider import digest
from .value import aggregate

SCHEMA = "knowbase.acceptance.v1"

# Threshold proposals (Phase 3-B, item 2): the historical threshold was a
# single real long task; the plan generalizes to N sessions across
# instances. 3 sessions is the smallest count showing repetition, and 2
# distinct instances the smallest count showing cross-instance evidence,
# without silently requiring threshold 3's three-instance setup.
FULL_CHAIN_MIN_SESSIONS = 3
FULL_CHAIN_MIN_INSTANCES = 2
CROSS_MEMBER_MIN_INSTANCES = 3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _owner_key(mapping: dict, inst: str, binding: str) -> str | None:
    """Event attribution, mirroring aggregate: correction mapping first,
    then the event's own binding; host/tool labels never attribute."""
    return mapping.get(inst) or (f"binding:{binding}" if binding else None)


def _run_source(session_id: str, inst: str) -> str:
    return session_id or (f"inst:{inst}" if inst else "unknown")


def _load_events(agg_conn, team_id: str):
    return agg_conn.execute(
        "SELECT event_type,installation_id,member_binding_id,task_id,occurred_at,"
        "session_id,payload_json FROM agg_events WHERE team_id=? AND state='accepted' "
        "ORDER BY occurred_at,event_id", (team_id,)).fetchall()


def _fail(check: str, reason: str, **details) -> dict:
    return {"check": check, "passed": False, "reason": reason, "details": details}


def _pass(check: str, **details) -> dict:
    return {"check": check, "passed": True, "details": details}


def check_derived_eval(personal_conn, report_path, dataset_path) -> dict:
    """Threshold 1: derived eval verdict PASS and backtest agreement.

    The frozen report must be execution_derived with verdict PASS, its
    dataset digest must match the frozen dataset file, and every frozen
    row must re-derive to the same class from current evidence (drift
    fails; dataset growth alone does not).
    """
    check = "derived_eval"
    if personal_conn is None or not report_path or not dataset_path:
        return _fail(check, "missing input: personal db, eval report and dataset required")
    try:
        report = json.loads(Path(report_path).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return _fail(check, f"eval report unreadable: {type(exc).__name__}")
    if not isinstance(report, dict) or report.get("schema") != "knowbase.retrieval-evaluation.v1":
        return _fail(check, "eval report schema unsupported")
    if report.get("annotation_status") != "execution_derived":
        return _fail(check, "eval report is not execution_derived")
    if report.get("verdict") != "PASS":
        return _fail(check, f"eval report verdict is {report.get('verdict')}")
    try:
        rows = [json.loads(line) for line in
                Path(dataset_path).expanduser().read_text(encoding="utf-8").splitlines()
                if line.strip()]
    except (OSError, ValueError) as exc:
        return _fail(check, f"dataset unreadable: {type(exc).__name__}")
    if digest(rows) != report.get("dataset_sha256"):
        return _fail(check, "dataset digest does not match frozen report")
    if any((row.get("labels") or {}).get("status") != "execution_derived" for row in rows):
        return _fail(check, "dataset rows are not execution_derived")
    fresh_rows, _stats = execution_eval.derive_dataset(personal_conn)
    fresh_by_id = {row["id"]: row for row in fresh_rows}
    drift = []
    for row in rows:
        fresh = fresh_by_id.get(row["id"])
        if fresh is None:
            drift.append({"row": row["id"], "reason": "missing on re-derivation"})
            continue
        old_labels, new_labels = row.get("labels") or {}, fresh.get("labels") or {}
        if (old_labels.get("sample_class") != new_labels.get("sample_class")
                or old_labels.get("relevant_ids") != new_labels.get("relevant_ids")
                or old_labels.get("unanswerable") != new_labels.get("unanswerable")):
            drift.append({"row": row["id"], "reason": "class changed on re-derivation"})
    if drift:
        return _fail(check, f"backtest drift on {len(drift)} rows", drift=drift[:10])
    return _pass(check, rows=len(rows), verdict="PASS",
                 dataset_sha256=report.get("dataset_sha256"))


def check_full_chain(agg_conn, team_id: str) -> dict:
    """Threshold 2: N real task sessions (distinct instances) with the
    complete chain delivery -> adoption -> completed task result, all
    from attributed instances (host/tool labels never count)."""
    check = "full_chain_sessions"
    attr = aggregate.attribution(agg_conn, team_id)
    mapping, attributed = attr["mapping"], attr["attributed"]
    delivered, adopted, completed = {}, {}, {}
    identity_inst: dict[tuple, str] = {}
    for event_type, inst, binding, task_id, _occurred, session_id, payload_json in \
            _load_events(agg_conn, team_id):
        if not task_id:
            continue
        owner = _owner_key(mapping, inst, binding)
        if owner is None:
            continue
        identity = (owner, _run_source(session_id, inst), task_id)
        identity_inst.setdefault(identity, inst)
        if event_type == "knowledge.used":
            # 当前事件词表中递送由 use 事件本身见证（adopted 蕴含已递送；
            # retrieval.completed/delivered 尚未启用），故 delivery 取全部
            # knowledge.used，链的核心约束是 adopted + completed。
            delivered[identity] = True
            try:
                state = (json.loads(payload_json).get("payload") or {}).get("state", "")
            except ValueError:
                state = ""
            if state == "adopted":
                adopted[identity] = True
        elif event_type == "task.completed":
            try:
                result = (json.loads(payload_json).get("payload") or {}).get("result", "")
            except ValueError:
                result = ""
            if result == "completed":
                completed[identity] = True
    sessions = sorted(set(delivered) & set(adopted) & set(completed))
    instances = sorted({identity_inst[s] for s in sessions if identity_inst.get(s)})
    if len(sessions) < FULL_CHAIN_MIN_SESSIONS:
        return _fail(check, f"full-chain sessions {len(sessions)} < {FULL_CHAIN_MIN_SESSIONS}",
                     sessions=len(sessions), instances=len(instances))
    if len(instances) < FULL_CHAIN_MIN_INSTANCES:
        return _fail(check, f"full-chain instances {len(instances)} < {FULL_CHAIN_MIN_INSTANCES}",
                     sessions=len(sessions), instances=len(instances))
    return _pass(check, sessions=len(sessions), instances=len(instances),
                 session_list=[{"owner": s[0], "run": s[1], "task_id": s[2]} for s in sessions[:20]])


def check_cross_member(agg_conn, team_id: str) -> dict:
    """Threshold 3: three distinct attributed instances with real tasks
    each, plus an A->B->C consumption path in the contributor->consumer
    instance graph (same card adopted twice is a star, not a chain)."""
    check = "cross_member_reuse"
    attr = aggregate.attribution(agg_conn, team_id)
    mapping, attributed = attr["mapping"], attr["attributed"]
    contributor_of: dict[tuple, str] = {}
    adopted_uses: list[tuple] = []
    instance_has_task: dict[str, bool] = {}
    adopted_by_inst: dict[str, bool] = {}
    completed_by_inst: dict[str, bool] = {}
    for event_type, inst, binding, task_id, _occurred, _session, payload_json in \
            _load_events(agg_conn, team_id):
        try:
            payload = (json.loads(payload_json).get("payload") or {})
        except ValueError:
            continue
        if event_type in ("knowledge.created", "knowledge.revised"):
            card = (payload.get("memory_id", ""), payload.get("content_revision", ""))
            if card[0] and inst:
                contributor_of.setdefault(card, inst)
        elif event_type == "knowledge.used" and payload.get("state") == "adopted":
            card = (payload.get("memory_id", ""), payload.get("content_revision", ""))
            if card[0] and inst:
                adopted_uses.append((card, inst))
                adopted_by_inst[inst] = True
        elif event_type == "task.completed" and payload.get("result") == "completed":
            if inst:
                completed_by_inst[inst] = True
    for inst in set(adopted_by_inst) & set(completed_by_inst):
        if inst in attributed:
            instance_has_task[inst] = True
    edges: dict[str, set[str]] = {}
    for (mid, rev), consumer in adopted_uses:
        contributor = contributor_of.get((mid, rev))
        if contributor and contributor != consumer \
                and contributor in attributed and consumer in attributed:
            edges.setdefault(contributor, set()).add(consumer)
    path = None
    for b in sorted(edges):
        for a in sorted(edges):
            if b not in edges.get(a, set()):
                continue
            for c in sorted(edges.get(b, set())):
                if len({a, b, c}) == 3 and all(instance_has_task.get(x) for x in (a, b, c)):
                    path = [a, b, c]
                    break
            if path:
                break
        if path:
            break
    if path is None:
        return _fail(check, "no A->B->C consumption path across three attributed instances "
                            "with real tasks each",
                     edges=sum(len(v) for v in edges.values()),
                     instances_with_tasks=len(instance_has_task))
    if len({x for x in path}) < CROSS_MEMBER_MIN_INSTANCES:
        return _fail(check, "consumption path spans fewer than three instances", path=path)
    return _pass(check, path=path, edges=sum(len(v) for v in edges.values()),
                 instances_with_tasks=len(instance_has_task))


def check_benefit_control(report_path) -> dict:
    """Threshold 4: a paired control report with real numbers.

    Missing, digest-invalid, or non-recomputing reports fail; None is
    never accepted as a net figure."""
    check = "benefit_control"
    if not report_path:
        return _fail(check, "missing input: control report required")
    try:
        report = benefit_control.load_report(report_path)
    except (OSError, ValueError) as exc:
        return _fail(check, f"control report invalid: {exc}")
    pairs = report.get("pairs") or []
    totals = report.get("totals") or {}
    if not pairs:
        return _fail(check, "control report has no paired tasks")
    with_tokens = without_tokens = 0
    with_seconds = without_seconds = 0.0
    for pair in pairs:
        for arm_name in ("with_library", "without_library"):
            arm = pair.get(arm_name) or {}
            for key in ("input_tokens", "output_tokens", "seconds"):
                value = arm.get(key)
                if not isinstance(value, (int, float)) or isinstance(value, bool) \
                        or not math.isfinite(value) or value < 0:
                    return _fail(check, f"pair {pair.get('task_id')} has invalid {arm_name}.{key}")
        with_tokens += pair["with_library"]["input_tokens"] + pair["with_library"]["output_tokens"]
        without_tokens += pair["without_library"]["input_tokens"] + pair["without_library"]["output_tokens"]
        with_seconds += pair["with_library"]["seconds"]
        without_seconds += pair["without_library"]["seconds"]
    net = totals.get("net_token_savings")
    if isinstance(net, bool) or not isinstance(net, (int, float)) \
            or net != without_tokens - with_tokens:
        return _fail(check, "net_token_savings missing or does not recompute from pairs")
    if totals.get("task_count") != len(pairs):
        return _fail(check, "task_count does not match pairs")
    return _pass(check, tasks=len(pairs), net_token_savings=net,
                 net_seconds=without_seconds - with_seconds)


def run_all(*, agg_conn, team_id: str, personal_conn=None,
            eval_report=None, dataset=None, control_report=None) -> dict:
    """Run all four checks and judge the overall verdict by machine."""
    checks = {
        "derived_eval": check_derived_eval(personal_conn, eval_report, dataset),
        "full_chain_sessions": check_full_chain(agg_conn, team_id),
        "cross_member_reuse": check_cross_member(agg_conn, team_id),
        "benefit_control": check_benefit_control(control_report),
    }
    return {"schema": SCHEMA, "verdict": "PASS" if all(c["passed"] for c in checks.values()) else "FAIL",
            "checks": checks, "generated_at": _now()}


def render_markdown(report: dict) -> str:
    """Deterministic acceptance report text; verdicts only, no open items."""
    lines = [f"# knowbase 验收结论：{report['verdict']}", "",
             f"生成时间：{report.get('generated_at', '')}", ""]
    for name, check in report["checks"].items():
        status = "通过" if check["passed"] else "未通过"
        lines.append(f"## {name}：{status}")
        if not check["passed"]:
            lines.append(f"- 原因：{check.get('reason', '')}")
        for key, value in sorted((check.get("details") or {}).items()):
            lines.append(f"- {key}：{value}")
        lines.append("")
    lines.append("以上结论全部由脚本按落盘证据判定。")
    return "\n".join(lines) + "\n"
