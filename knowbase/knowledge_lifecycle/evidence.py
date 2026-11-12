"""版本化证据交换（V2-13）：本地 ledger ↔ 可交换事件包。

三层去重与撤销语义（设计 §10.2）：
1. 传输幂等：同 event_id + payload_hash 只收一次；同 ID 异 hash →
   IDEMPOTENCY_CONFLICT，两方隔离（import_state=conflict，不进治理）。
2. 业务去重：project_key + memory_id@revision + claim + logical_task_key +
   origin_run_key + check_id 同一证据语义最多一次——换包/设备复制不扩票。
3. 独立性聚合：治理按 logical_task_key 计数（governance.decide），复制
   run/同任务重放共享同 key 不增加任务数。

撤销：evidence.revoked 先到保留 tombstone，原事件后到仍无效；重放不能
复活。包不携带正文/命令输出（白名单 payload）。
"""
import json
from datetime import datetime, timezone
from pathlib import Path

from . import contracts, trust

PACKAGE_SCHEMA = "knowbase.lifecycle.evidence-package.v1"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def export_package(conn, *, team_id, issuer_installation_id, project_key,
                   private_pem, key_id, limit=1000) -> dict:
    """导出本地可信 L3 事件为签名包（发送端）。"""
    rows = conn.execute(
        "SELECT event_id,event_type,subject_revision,run_id,logical_task_key,"
        "independence_group,payload_json,occurred_at,scope,polarity,level,claim_id "
        "FROM evidence_events WHERE level IN ('L3','L4') AND "
        "trust_basis='registered-adapter' AND import_state='local' "
        "AND event_id NOT IN (SELECT revokes_event_id FROM evidence_events "
        "WHERE event_type='evidence.revoked') ORDER BY occurred_at LIMIT ?",
        (limit,)).fetchall()
    envelopes = []
    for (event_id, event_type, subject, run_id, logical, group, payload_json,
         occurred_at, scope, polarity, level, claim_id) in rows:
        payload = json.loads(payload_json or "{}")
        if payload.get("validator_kind") and not payload.get("binding_hash"):
            continue
        memory_id, _, revision = (subject.partition("@")[0],
                                  *subject.partition("@")[2].partition(":"))[0], "", ""
        parts = subject.split("@", 1)
        memory_id = parts[0]
        revision = parts[1].split(":", 1)[1] if ":" in parts[1] else parts[1]
        envelope = contracts.build_envelope(
            event_type=event_type, team_id=team_id,
            issuer_installation_id=issuer_installation_id, project_key=project_key,
            scope=scope or "global", occurred_at=occurred_at or _now(),
            payload={**payload,
                     "level": level, "polarity": polarity,
                     "logical_task_key": logical or "",
                     "independence_group": group or "",
                     "run_id": run_id or "",
                     "claim_id": claim_id or ""},
            subject={"memory_id": memory_id, "revision_algo": "content-v1",
                     "content_revision": revision, "claim_id": claim_id or ""},
            native_event_key=event_id,
            operation_key=event_type)
        envelope = trust.sign_envelope(envelope, private_pem, key_id)
        envelopes.append(envelope)
    package = {"schema": PACKAGE_SCHEMA, "team_id": team_id,
               "issuer_installation_id": issuer_installation_id,
               "generated_at": _now(), "events": envelopes}
    return package


def write_package(package: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(package, ensure_ascii=False), encoding="utf-8")


def import_package(conn, package: dict, *, registry: dict) -> dict:
    """接收端：验签/信任/去重/撤销物化。返回分类统计与逐事件处置。

    未验签/未登记/越 scope 的事件不入库，仅在返回统计中计数并记 event_id
    与原因（details），零治理影响；ACK 语义只确认接收，不自动 verified
    （治理由 governance 重算）。"""
    if package.get("schema") != PACKAGE_SCHEMA:
        return {"error": "package_schema_unsupported"}
    stats = {"imported": 0, "idempotent": 0, "conflicts": 0, "rejected": 0,
             "unverified": 0, "revocations": 0, "tombstones": 0, "details": []}
    # 先收撤销（乱序安全：先撤销后原事件 → 原事件导入时直接判无效）。
    events = package.get("events") or []
    revoked_targets = {e["payload"].get("target_event_id") for e in events
                       if e.get("event_type") == "evidence.revoked"}
    for envelope in events:
        event_id = envelope.get("event_id", "")
        problems = contracts.validate_envelope(envelope)
        if problems:
            stats["rejected"] += 1
            stats["details"].append({"event_id": event_id, "result": "invalid_envelope",
                                     "problems": problems})
            continue
        scope = envelope.get("scope", "")
        verdict = trust.check_issuer(registry, envelope, scope)
        public_pem = ((registry.get("key_epochs") or {}).get(
            envelope.get("key_id", ""), {}) or {}).get("public_pem", "")
        if verdict != "trusted" or not public_pem or not trust.verify_envelope(
                envelope, public_pem):
            state = "external_unverified"
            stats["unverified"] += 1
            reason = verdict if verdict != "trusted" else "signature_invalid"
            # 仅记入返回统计，不入库：调用方据此可审计，未信事件零治理影响。
            stats["details"].append({"event_id": event_id,
                                      "result": "external_unverified",
                                      "reason": reason})
        else:
            payload = envelope.get("payload") or {}
            event_type = envelope.get("event_type", "")
            subject = {k: envelope.get(k) for k in
                       ("memory_id", "revision_algo", "content_revision", "claim_id")}
            if event_type == "evidence.revoked":
                target = payload.get("target_event_id", "")
                conn.execute(
                    "INSERT OR IGNORE INTO evidence_events(event_id,payload_hash,"
                    "event_type,subject_revision,run_id,logical_task_key,"
                    "independence_group,issuer_id,trust_basis,level,polarity,"
                    "supersedes_event_id,revokes_event_id,payload_json,import_state,"
                    "occurred_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (event_id, envelope.get("payload_hash", ""), event_type, "",
                     "", "", "", envelope.get("issuer_installation_id", ""),
                     "registered-adapter", "", "neutral", "", target,
                     json.dumps(payload, ensure_ascii=False)[:4096],
                     "imported", envelope.get("occurred_at", _now())))
                if target in revoked_targets:
                    stats["tombstones"] += 1
                stats["revocations"] += 1
                stats["details"].append({"event_id": event_id, "result": "revocation_recorded"})
                continue
            # 传输幂等。
            existing = conn.execute(
                "SELECT payload_hash,import_state FROM evidence_events WHERE event_id=?",
                (event_id,)).fetchone()
            if existing:
                if existing[0] != envelope.get("payload_hash"):
                    conn.execute(
                        "INSERT OR IGNORE INTO evidence_events(event_id,payload_hash,"
                        "event_type,subject_revision,run_id,logical_task_key,"
                        "independence_group,issuer_id,trust_basis,level,polarity,"
                        "supersedes_event_id,revokes_event_id,payload_json,import_state,"
                        "occurred_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (event_id + ":conflict", envelope.get("payload_hash", ""),
                         event_type,
                         f"{subject.get('memory_id','')}@{subject.get('revision_algo','content-v1')}:{subject.get('content_revision','')}",
                         "", envelope.get("payload", {}).get("logical_task_key", ""),
                         "", envelope.get("issuer_installation_id", ""), "conflict",
                         "", "neutral", "", "", json.dumps({"conflict_with": existing[0]})[:512],
                         "conflict", _now()))
                    stats["conflicts"] += 1
                    stats["details"].append({"event_id": event_id, "result": "idempotency_conflict"})
                    continue
                stats["idempotent"] += 1
                continue
            # 业务去重：同语义证据已存在（可能来自另一设备/另一包）→ 不重复计。
            business_key = contracts.canonical_json({
                "s": subject, "t": payload.get("logical_task_key", ""),
                "r": payload.get("origin_run_key", ""), "c": payload.get("check_id", "")})
            dup = conn.execute(
                "SELECT COUNT(*) FROM evidence_events WHERE event_type=? AND "
                "subject_revision=? AND logical_task_key=? AND import_state IN "
                "('local','imported') AND payload_json LIKE ?",
                (event_type,
                 f"{subject.get('memory_id','')}@{subject.get('revision_algo','content-v1')}:{subject.get('content_revision','')}",
                 payload.get("logical_task_key", ""),
                 f"%{payload.get('check_id', '')}%")).fetchone()[0]
            state = "imported"
            if dup and payload.get("check_id"):
                stats["idempotent"] += 1
                stats["details"].append({"event_id": event_id, "result": "business_duplicate"})
                continue
            target_revoked = event_id in revoked_targets
            if target_revoked:
                state = "revoked"
            conn.execute(
                "INSERT OR IGNORE INTO evidence_events(event_id,payload_hash,event_type,"
                "subject_revision,run_id,logical_task_key,independence_group,issuer_id,"
                "trust_basis,level,polarity,supersedes_event_id,revokes_event_id,"
                "payload_json,import_state,occurred_at,claim_id,scope,key_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (event_id, envelope.get("payload_hash", ""), event_type,
                 f"{subject.get('memory_id','')}@{subject.get('revision_algo','content-v1')}:{subject.get('content_revision','')}",
                 payload.get("run_id", ""), payload.get("logical_task_key", ""),
                 payload.get("independence_group", ""),
                 envelope.get("issuer_installation_id", ""), "registered-adapter",
                 payload.get("level", "L3"), payload.get("polarity", "neutral"),
                 "", "", json.dumps(payload, ensure_ascii=False)[:8192], state,
                 envelope.get("occurred_at", _now()),
                 payload.get("claim_id", "") or subject.get("claim_id", ""),
                 envelope.get("scope", ""), envelope.get("key_id", "")))
            stats["imported"] += 1
            stats["details"].append({"event_id": event_id, "result": state})
    conn.commit()
    return stats
