"""完整的本地提炼材料及有界分页。复用 job_spool/jobs，不建立第二套队列。"""

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from .extraction.sanitizer import sanitize
from .credentials import transform

TERMINAL = frozenset({"saved", "updated", "linked_existing", "needs_review", "no_candidate"})


def digest(value) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def redact_payload(payload: dict) -> dict:
    """Redact user content without treating protocol IDs/lease tokens as secrets.

    Join each event before redaction: a credential can cross segment boundaries.
    Retain segment identities/counts for settled audit history, and rebuild offsets,
    hashes and input version. Callers must revoke existing leases after changing input.
    """
    clean = deepcopy(payload)
    groups = {}
    for segment in clean.get("segments", []):
        locator = segment.get("locator") or {}
        event = locator.get("event_index") if isinstance(locator, dict) else None
        groups.setdefault(("event", event) if event is not None else ("segment", segment["segment_id"]), []).append(segment)
    changed = False
    for segments in groups.values():
        original = "".join(s.get("text", "") for s in segments)
        redacted = transform(original)
        if redacted == original:
            continue
        changed = True
        start = 0
        for i, segment in enumerate(segments):
            end = min(len(redacted), start + len(segment.get("text", ""))) if i < len(segments) - 1 else len(redacted)
            segment.update(text=redacted[start:end], start_offset=start, end_offset=end, original_length=len(redacted))
            start = end
    for event in clean.get("checkpoint", {}).get("events", []):
        text = event.get("text", "")
        event["text"] = transform(text)
        changed = changed or event["text"] != text
    for key in ("text", "title", "messages", "events"):
        if key in clean:
            clean[key] = transform(clean[key])
            changed = changed or clean[key] != payload[key]
    if not changed:
        return payload
    if clean.get("protocol_version") == 2:
        clean["redaction"] = {"policy": "credential-v2", "previous_input_version": payload.get("input_version", "")}
        events = clean.get("checkpoint", {}).get("events")
        clean["input_version"] = digest({"scope": clean.get("scope", ""), "session_id": clean.get("session_id", ""),
                                         "events": events} if events is not None else clean["segments"])
    for segment in clean.get("segments", []):
        if clean.get("protocol_version") == 2:
            segment.update(text_hash=digest(segment["text"]), source_version=clean["input_version"])
        else:
            segment["text_hash"] = hashlib.sha256(segment["text"].encode("utf-8")).hexdigest()
    return clean


def capture_payload(events, *, scope: str, session_id: str, segment_chars: int = 4000) -> dict:
    """在任何模型预算截断前保留完整（脱敏后）事件，按语义边界有界切分。"""
    if not 128 <= segment_chars <= 32000:
        raise ValueError("segment_chars must be between 128 and 32000")
    records = []
    for ordinal, ev in enumerate(events):
        text = sanitize(ev.text)
        records.append({"event_id": ev.message_id or f"event-{ordinal}", "role": ev.role,
                        "text": text, "tool_name": ev.tool_name,
                        "tool_call_id": ev.tool_call_id, "timestamp": ev.timestamp})
    if len(json.dumps(records, ensure_ascii=False).encode("utf-8")) > 10 * 1024 * 1024:
        raise ValueError("material_too_large: split input into batches (10 MiB maximum)")
    version = digest({"scope": scope, "session_id": session_id, "events": records})
    segments = []
    for event_index, event in enumerate(records):
        text = event["text"]
        start = 0
        while start < len(text):
            end = min(len(text), start + segment_chars)
            if end < len(text):
                cut = text.rfind("\n", start + segment_chars // 2, end)
                if cut >= 0:
                    end = cut + 1
            part = text[start:end]
            ordinal = len(segments)
            segments.append({
                "segment_id": f"seg-{ordinal:04d}", "ordinal": ordinal, "text": part,
                "text_hash": digest(part), "source_version": version,
                "original_length": len(text), "start_offset": start, "end_offset": end,
                "evidence_group_id": event["tool_call_id"] or event["event_id"],
                "role": event["role"], "tool_name": event["tool_name"],
                "locator": {"event_index": event_index, "event_id": event["event_id"]},
            })
            start = end
    return {"protocol_version": 2, "scope": scope, "session_id": session_id,
            "input_version": version, "segments": segments, "complete": True,
            "checkpoint": {"events": records}, "total_segments": len(segments)}


def register_manifest(conn, job_id: str, payload: dict) -> None:
    segments = payload["segments"]
    for i, segment in enumerate(segments):
        conn.execute(
            "INSERT OR IGNORE INTO job_segments VALUES(?,?,?,?,?,?,?,?,?,?)",
            (job_id, segment["segment_id"], i, payload["input_version"], segment["text_hash"],
             segment["original_length"], json.dumps(segment.get("locator", {}), ensure_ascii=False),
             segment.get("start_offset", 0), segment.get("end_offset", len(segment["text"])),
             segment.get("evidence_group_id", "")),
        )
    conn.commit()


def payload_for_job(conn, job_id: str) -> dict:
    row = conn.execute("SELECT input_json,checksum FROM job_spool WHERE job_id=? ORDER BY rowid DESC LIMIT 1",
                       (job_id,)).fetchone()
    if row and row[1] and hashlib.sha256(row[0].encode("utf-8")).hexdigest()[:16] != row[1]:
        raise ValueError("MATERIAL_CHECKSUM_MISMATCH")
    payload = json.loads(row[0]) if row else {}
    if redact_payload(payload) != payload:
        raise ValueError("MATERIAL_REDACTION_REQUIRED")
    return payload


def manifest_for_job(conn, job_id: str) -> list[dict]:
    payload = payload_for_job(conn, job_id)
    return payload.get("segments", []) if payload.get("protocol_version") == 2 else []


def create_batch(conn, claim: dict, job_id: str, owner: str) -> dict:
    payload = payload_for_job(conn, job_id)
    segments = manifest_for_job(conn, job_id)
    done = {r[0] for r in conn.execute(
        "SELECT segment_id FROM segment_results WHERE job_id=? AND settlement_state IN "
        "('saved','updated','linked_existing','needs_review','no_candidate')", (job_id,))}
    ids = [s["segment_id"] for s in segments if s["segment_id"] not in done][:32]
    scope = claim["scope"]
    query = " ".join(s["text"][:200] for s in segments if s["segment_id"] in ids)[:2000]
    similar = similar_snapshot(conn, query, scope)
    conn.execute("INSERT INTO job_batches(batch_id,job_id,input_version,manifest_hash,"
                 "segment_ids_json,owner,lease_token,fencing_token,expires_at,similar_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                 (claim["batch_id"], job_id, payload["input_version"], digest(segments),
                  json.dumps(ids), owner, claim["lease_token"], claim["fencing_token"], claim["expires_at"],
                  json.dumps({"query": query, "scope": scope, "cards": similar}, ensure_ascii=False)))
    conn.commit()
    return {"batch_segment_ids": ids, "input_version": payload["input_version"],
            "manifest_hash": digest(segments), "pending_segments": len(segments) - len(done),
            "similar_cards": similar, "similarity_snapshot": digest(similar)}


def similar_snapshot(conn, query: str, scope: str) -> list[dict]:
    from .extraction.recall import wide_recall
    result = []
    for candidate in wide_recall(conn, query, scope, limit_per_route=10)[:10]:
        from . import store
        from .mutations import _compute_revision
        meta, body, _ = store.load(configured_repo(conn), candidate["id"])
        result.append({"id": candidate["id"], "title": candidate["title"], "scope": candidate["scope"],
                       "content_revision": _compute_revision(meta, body) if meta else "unknown"})
    return sorted(result, key=lambda card: card["id"])


def configured_repo(conn):
    """由连接确定所属仓库，避免多库测试/并存服务误读全局配置。"""
    from pathlib import Path
    return Path(conn.execute("PRAGMA database_list").fetchone()[2]).parent


def validate_batch(conn, job_id, batch_id, lease_token, fencing_token=None):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    row = conn.execute(
        "SELECT b.segment_ids_json,b.delivered_json,b.read_cursor,b.fencing_token,b.state,"
        "j.lease_owner,j.lease_until,j.state,j.fencing_token "
        "FROM job_batches b JOIN jobs j ON j.job_id=b.job_id "
        "WHERE b.job_id=? AND b.batch_id=? AND b.lease_token=?", (job_id, batch_id, lease_token)).fetchone()
    if (not row or row[4] != "claimed" or row[7] != "claimed" or not row[5]
            or not row[5].endswith(":" + lease_token) or not row[6] or row[6] <= now
            or row[3] != row[8] or (fencing_token is not None and fencing_token != row[3])):
        return {"error": "LEASE_EXPIRED"}
    return {"segment_ids": json.loads(row[0]), "delivered": json.loads(row[1]),
            "read_cursor": row[2], "fencing_token": row[3], "lease_owner": row[5]}


def page(conn, job_id: str, batch_id: str, lease_token: str, *, fencing_token=None,
         cursor: str = "", max_chars: int = 8000) -> dict:
    batch = validate_batch(conn, job_id, batch_id, lease_token, fencing_token)
    if "error" in batch:
        return batch
    try:
        position = tuple(int(x) for x in (cursor or batch["read_cursor"]).split(":"))
        current = tuple(int(x) for x in batch["read_cursor"].split(":"))
        if len(position) != 2 or min(position) < 0 or position > current:
            raise ValueError()
        budget = max(1, min(int(max_chars), 32000))
    except (ValueError, TypeError):
        return {"error": "INVALID_CURSOR"}
    by_id = {s["segment_id"]: s for s in manifest_for_job(conn, job_id)}
    segments = [by_id[sid] for sid in batch["segment_ids"]]
    index, offset = position
    output, delivered = [], set(batch["delivered"])
    while index < len(segments) and budget:
        segment = segments[index]
        text = segment["text"]
        part = text[offset:offset + budget]
        end = offset + len(part)
        output.append({**{k: v for k, v in segment.items() if k != "text"},
                       "text": part, "page_start": offset, "page_end": end,
                       "complete": end == len(text), "truncated": end < len(text)})
        budget -= len(part)
        if end < len(text):
            offset = end
            break
        delivered.add(segment["segment_id"])
        index, offset = index + 1, 0
    next_cursor = f"{index}:{offset}"
    # 允许重复读取旧页；只按连续实际交付页向前推进。
    if position == current:
        conn.execute("UPDATE job_batches SET delivered_json=?,read_cursor=? WHERE batch_id=?",
                     (json.dumps(sorted(delivered)), next_cursor, batch_id))
        # 前进读页即续租：分页读取是持有人在活跃处理的信号，
        # 避免协议要求的多页读完+查重期间租约到期，宿主被误判失联。
        from .jobs import DEFAULT_LEASE_SECONDS
        renewed = (datetime.now(timezone.utc)
                   + timedelta(seconds=DEFAULT_LEASE_SECONDS)).isoformat(timespec="seconds")
        conn.execute("UPDATE jobs SET lease_until=? WHERE job_id=? AND state='claimed' AND lease_owner=?",
                     (renewed, job_id, batch["lease_owner"]))
        conn.commit()
    return {"materials": output, "next_cursor": next_cursor, "has_more": index < len(segments),
            "complete": index == len(segments), "truncated": index < len(segments),
            "delivered_segment_ids": sorted(delivered)}


def contiguous_cursor(conn, job_id: str) -> int:
    rows = conn.execute(
        "SELECT s.ordinal,COALESCE(r.settlement_state,'pending') FROM job_segments s "
        "LEFT JOIN segment_results r ON r.job_id=s.job_id AND r.segment_id=s.segment_id "
        "WHERE s.job_id=? ORDER BY s.ordinal", (job_id,))
    cursor = 0
    for ordinal, state in rows:
        if ordinal != cursor or state not in TERMINAL:
            break
        cursor += 1
    return cursor
