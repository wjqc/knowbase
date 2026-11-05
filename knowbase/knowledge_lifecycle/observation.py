"""宿主原生态观测与任务身份（V2-09）。

设计要点（任务书 V2-09 §5）：
- native_key 跨重启稳定：同 (host, session, native_key) 只落一次；同 key
  不同 payload 是冲突（IDEMPOTENCY_CONFLICT 语义），不静默覆盖。
- tool_call/tool_result 按 tool_call_id 配对；未配对保留 gap_flags，不猜。
- 任务身份：有原生任务/运行键 → identity_basis=native（跨机可比）；无则
  本地段 local_segment + identity_unknown 显式声明，不参与独立性计数。
- 一会话多任务：以 native_task_key 区分；缺失时按会话建本地任务段。
- 完成终态只结算观测，不自动推知识有效（那是 V2-10/11 的证据链）。
- 事件本体经调用方脱敏后入库；此处不执行材料中的命令。
"""
import json
import hashlib
from datetime import datetime, timezone

from . import contracts

EVENT_KINDS = ("tool_call", "tool_result", "task_start", "task_end", "note")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ingest(conn, host_profile: str, session_id: str, events: list[dict], *,
           project_key: str, native_task_key: str = "", native_run_key: str = "",
           environment_fingerprint: str = "", task_revision: str = "") -> dict:
    """落一批原生事件并维护 task_runs。幂等：重复同批零增量。

    events 每项：{native_key, kind, tool_call_id?, tool_name?, payload,
    artifact_hash?, occurred_at?}。payload 为脱敏结构（调用方负责脱敏），
    本函数只 hash 与存储，不解释语义。
    """
    if not host_profile or not session_id:
        return {"error": "host_and_session_required"}
    host, session = str(host_profile)[:512], str(session_id)[:512]
    inserted = idempotent = conflicts = 0
    artifacts = set()
    for event in events:
        native_key = str(event.get("native_key") or "")
        kind = event.get("kind") or ""
        if not native_key or kind not in EVENT_KINDS:
            return {"error": "invalid_event", "native_key": native_key, "kind": kind}
        payload = event.get("payload") or {}
        phash = hashlib.sha256(
            contracts.canonical_json(payload).encode("utf-8")).hexdigest()
        artifact = str(event.get("artifact_hash") or "")[:128]
        if artifact:
            artifacts.add(artifact)
        row = conn.execute(
            "SELECT payload_hash FROM native_events WHERE host_profile=? AND "
            "session_id=? AND native_key=?", (host, session, native_key)).fetchone()
        if row:
            if row[0] != phash:
                conflicts += 1
                continue
            idempotent += 1
            continue
        conn.execute(
            "INSERT INTO native_events VALUES(?,?,?,?,?,?,?,?,?,?)",
            (host, session, native_key, kind,
             str(event.get("tool_call_id") or "")[:256],
             str(event.get("tool_name") or "")[:128],
             phash, artifact,
             json.dumps(payload, ensure_ascii=False)[:16384],
             event.get("occurred_at") or _now()))
        inserted += 1
    # 配对质量：会话内 tool_call/tool_result 按 tool_call_id 配对。
    calls = {r[0] for r in conn.execute(
        "SELECT tool_call_id FROM native_events WHERE host_profile=? AND session_id=? "
        "AND event_kind='tool_call' AND tool_call_id!=''", (host, session))}
    results = {r[0] for r in conn.execute(
        "SELECT tool_call_id FROM native_events WHERE host_profile=? AND session_id=? "
        "AND event_kind='tool_result' AND tool_call_id!=''", (host, session))}
    unpaired_calls = sorted(calls - results)
    unpaired_results = sorted(results - calls)
    gap_flags = []
    if unpaired_calls:
        gap_flags.append("unpaired_tool_calls")
    if unpaired_results:
        gap_flags.append("unpaired_tool_results")
    # 任务身份。
    basis = "native" if native_task_key else "local_segment"
    logical = contracts.logical_task_key(project_key, native_task_key)
    origin = contracts.origin_run_key(project_key, native_run_key or session_id)
    run_id = "RUN-" + hashlib.sha256(
        contracts.canonical_json({"h": host, "s": session, "o": origin,
                                  "t": logical}).encode("utf-8")).hexdigest()[:24]
    completion = ""
    for kind, value in conn.execute(
            "SELECT event_kind, event_json FROM native_events WHERE host_profile=? "
            "AND session_id=? AND event_kind='task_end' ORDER BY occurred_at DESC "
            "LIMIT 1", (host, session)):
        try:
            completion = str((json.loads(value) or {}).get("completion") or "")
        except ValueError:
            completion = ""
    artifact_set_hash = (hashlib.sha256(
        contracts.canonical_json({"artifacts": sorted(artifacts)})
        .encode("utf-8")).hexdigest()[:32] if artifacts else "")
    existing = conn.execute("SELECT run_id FROM task_runs WHERE run_id=?",
                            (run_id,)).fetchone()
    if existing:
        conn.execute(
            "UPDATE task_runs SET completion=?, gap_flags_json=?, artifact_set_hash=?, "
            "environment_fingerprint=? WHERE run_id=?",
            (completion, json.dumps(gap_flags), artifact_set_hash,
             environment_fingerprint, run_id))
    else:
        conn.execute(
            "INSERT INTO task_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, project_key, logical, origin, task_revision, host, session,
             basis, environment_fingerprint, artifact_set_hash, completion,
             json.dumps(gap_flags)))
    conn.commit()
    return {"run_id": run_id, "identity_basis": basis, "logical_task_key": logical,
            "origin_run_key": origin, "inserted": inserted, "idempotent": idempotent,
            "conflicts": conflicts, "paired": len(calls & results),
            "unpaired_tool_calls": len(unpaired_calls),
            "unpaired_tool_results": len(unpaired_results),
            "gap_flags": gap_flags, "artifact_set_hash": artifact_set_hash,
            "completion": completion}


def run_summary(conn, run_id: str) -> dict:
    """只读读回一个任务运行的观测状态（独立读回证据用）。"""
    row = conn.execute(
        "SELECT run_id,project_key,logical_task_key,origin_run_key,task_revision,"
        "host_profile,session_id,identity_basis,environment_fingerprint,"
        "artifact_set_hash,completion,gap_flags_json FROM task_runs WHERE run_id=?",
        (run_id,)).fetchone()
    if not row:
        return {"error": "NOT_FOUND"}
    return {"run_id": row[0], "project_key": row[1], "logical_task_key": row[2],
            "origin_run_key": row[3], "task_revision": row[4], "host_profile": row[5],
            "session_id": row[6], "identity_basis": row[7],
            "environment_fingerprint": row[8], "artifact_set_hash": row[9],
            "completion": row[10], "gap_flags": json.loads(row[11])}
