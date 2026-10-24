"""Read-only daily collection. Identity/sequence/evidence live outside memory.db."""
import json
import sqlite3
import uuid
from collections import Counter, defaultdict
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .. import index, telemetry
from . import state
from .contracts import TOKEN_FIELDS, digest, stamp, summary_path

DISPLAY_TZ = ZoneInfo("Asia/Shanghai")


def parse_time(value, source_timezone):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo(source_timezone))
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def collect(repo, conn, *, now=None, usage_records=()):
    """Enqueue changed cumulative days; caller owns the team lock/transaction."""
    now = now or datetime.now(timezone.utc)
    cfg = state.get(conn, "enrollment")
    if not cfg:
        raise ValueError("team enrollment required")
    enrolled = parse_time(cfg["enrolled_at"], "UTC")
    source_tz = cfg["source_timezone"]
    db = sqlite3.connect(index.db_path(repo).resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    days = defaultdict(lambda: {"commands": Counter(), "known": 0, "hit": 0, "missing": 0,
                               "legacy": 0, "adoption": 0, "helpful": 0, "negative": 0,
                               "promoted": 0, "retired": 0, "usage": defaultdict(list), "ids": set()})
    invalid_times = 0
    first_call = enrolled

    def bucket(ts, source, key):
        nonlocal invalid_times
        value = parse_time(ts, source_tz)
        if value is None:
            invalid_times += 1
            return None
        if value > now or (not cfg.get("include_history") and value < enrolled):
            return None
        day = value.astimezone(DISPLAY_TZ).date().isoformat()
        b = days[day]
        b["ids"].add(digest([source, str(key)]))
        return b

    try:
        db.execute("BEGIN")  # all local sources from the same SQLite read snapshot
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        current_id = db.execute("SELECT value FROM value_identity WHERE key='installation_id'").fetchone()
        if not current_id or current_id[0] != cfg["installation_id"]:
            raise ValueError("database device identity changed; run team recover after checking database origin")
        if "command_calls" in tables:
            query = ("SELECT c.*,COALESCE(h.host,'unknown') AS host FROM command_calls c LEFT JOIN command_call_hosts h ON h.call_id=c.call_id"
                     if "command_call_hosts" in tables else "SELECT c.*,'unknown' AS host FROM command_calls c")
            for r in db.execute(query):
                allowed_ids = {cfg["installation_id"]} | set(cfg.get("historical_ids", []))
                if cfg.get("include_history"):
                    allowed_ids.add("")
                if r["installation_id"] not in allowed_ids:
                    continue
                b = bucket(r["ts"], "call", r["call_id"])
                if b is not None:
                    first_call = min(first_call, parse_time(r["ts"], source_tz))
                    # Do not expose arbitrary strings written into a damaged DB.
                    allowed = telemetry.MCP_COMMANDS | telemetry.CLI_COMMANDS | {f"hook.{e}" for e in telemetry.HOOK_EVENTS}
                    if r["command"] not in allowed:
                        raise ValueError("unknown command in local telemetry")
                    transport = "hook" if r["command"].startswith("hook.") else r["transport"]
                    b["commands"][(r["command"], telemetry.host_name(r["host"]), transport, r["status"])] += 1
        if "usage_log" in tables:
            for r in db.execute("SELECT rowid,ts,tool,detail FROM usage_log"):
                b = bucket(r["ts"], "legacy", r["rowid"])
                if b is None:
                    continue
                b["legacy"] += 1
                if r["tool"] in {"search", "auto_search"}:
                    try:
                        hits = json.loads(r["detail"]).get("hits")
                    except (ValueError, AttributeError, TypeError):
                        hits = None
                    if isinstance(hits, list):
                        b["known"] += 1
                        b["hit"] += bool(hits)
                    else:
                        b["missing"] += 1
        if "knowledge_uses" in tables:
            for r in db.execute("SELECT use_id,created_at,state FROM knowledge_uses"):
                b = bucket(r["created_at"], "use", r["use_id"])
                if b is not None and r["state"] == "adopted":
                    b["adoption"] += 1
        if "feedback_events" in tables:
            for r in db.execute("SELECT receipt_id,created_at,outcome,result_json FROM feedback_events WHERE state='applied'"):
                b = bucket(r["created_at"], "feedback", r["receipt_id"])
                if b is None:
                    continue
                b["helpful"] += r["outcome"] == "helpful"
                b["negative"] += r["outcome"] in {"not_helpful", "outdated", "incorrect"}
                try:
                    events = json.loads(r["result_json"]).get("events", [])
                    b["promoted"] += any("→ verified" in e or "自动激活" in e for e in events)
                    b["retired"] += any("→ stale" in e for e in events)
                except (ValueError, TypeError, AttributeError):
                    pass
        # Claude request logs are the single authority for Claude usage when enabled;
        # do not also add their mirrored model.usage outbox events.
        usage_records = list(usage_records)
        usage = {}
        for r in usage_records:
            key = r.get("request_id")
            if key:
                usage[("claude-code", str(key))] = (r, "request_log")
        if "value_outbox" in tables:
            for r in db.execute("SELECT payload_json FROM value_outbox WHERE event_type='model.usage' AND state<>'quarantined'"):
                try:
                    event = json.loads(r[0]); p = event["payload"]
                    host = telemetry.host_name(event.get("host_profile", event.get("host", "unknown")))
                    if host == "claude-code" and usage_records:
                        continue
                    # Explicit request id/time only: account-period totals are not daily usage.
                    if not p.get("request_id") or p.get("usage_source") not in {"claude_transcripts", "request_log", "model_usage"}:
                        continue
                    rec = {**p, "occurred_at": event.get("occurred_at", "")}
                    usage[(host, p["request_id"])] = (rec, "model_usage")
                except (ValueError, TypeError, KeyError):
                    continue
        for (host, request), (r, source) in usage.items():
            b = bucket(r.get("occurred_at"), "request", [host, request])
            if b is not None:
                model = r.get("model") or "unknown"
                # Provider model labels are identifiers, not arbitrary free text.
                import re
                if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", model):
                    model = "unknown"
                b["usage"][(host, source, model)].append(r)
    finally:
        db.close()
    today = now.astimezone(DISPLAY_TZ).date()
    days[today.isoformat()]
    old_days = {r[0] for r in conn.execute("SELECT day FROM evidence")}
    for day in old_days:
        days[day]
    earliest = min(datetime.fromisoformat(day).date() for day in days)
    if (today - earliest).days > 3660:
        raise ValueError("history exceeds 10 years; explicit migration required")
    for offset in range((today - earliest).days + 1):
        days[(earliest + timedelta(days=offset)).isoformat()]
    lost = []
    for day, b in days.items():
        row = conn.execute("SELECT ids FROM evidence WHERE day=?", (day,)).fetchone()
        if row and set(json.loads(row[0])) - b["ids"]:
            lost.append(day)
    day_epochs = state.get(conn, "day_epochs", {})
    for day in days:
        day_epochs.setdefault(day, state.get(conn, "epoch"))
    if lost:
        new_epoch = "E-" + uuid.uuid4().hex
        state.put(conn, "epoch", new_epoch)
        for day in lost:
            day_epochs[day] = new_epoch
        state.put(conn, "recovery_days", sorted(set(state.get(conn, "recovery_days", [])) | set(lost)))
    recovery = set(state.get(conn, "recovery_days", []))
    changed = 0
    for day, b in sorted(days.items()):
        epoch = day_epochs[day]
        start = datetime.combine(datetime.fromisoformat(day).date(), time(), DISPLAY_TZ).astimezone(timezone.utc)
        end = start + timedelta(days=1)
        usages = []
        for (host, source, model), records in sorted(b["usage"].items()):
            u = {"host": host, "source": source, "model": model, "requests": len(records)}
            for field in TOKEN_FIELDS:
                vals = [r.get(field) for r in records]
                u[field] = sum(vals) if all(type(v) is int and v >= 0 for v in vals) else None
            usages.append(u)
        calls_known = "command_calls" in tables and end > first_call
        metrics = {"calls": sum(b["commands"].values()) if calls_known else None,
                   "legacy_calls": b["legacy"], "known_searches": b["known"], "hit_searches": b["hit"],
                   "missing_searches": b["missing"], "updates": sum(n for k, n in b["commands"].items() if k[0] in {"memory_update", "update"}) if calls_known else None,
                   "adoption_receipts": b["adoption"], "helpful_receipts": b["helpful"], "negative_receipts": b["negative"],
                   "promotions": b["promoted"], "retirements": b["retired"], "usage": usages,
                   "commands": [dict(zip(("command", "host", "transport", "status", "count"), (*k, n))) for k, n in sorted(b["commands"].items())]}
        coverage = "recovery_required" if day in recovery else "partial"
        if coverage != "recovery_required" and start >= enrolled and end <= now and not invalid_times:
            coverage = "complete"
        v = {"schema": "knowbase.team-summary.v1", "definition_version": "daily-observed-v1",
             "team_id": cfg["team_id"], "member_id": cfg["member_id"], "installation_id": cfg["installation_id"],
             "db_epoch": epoch, "snapshot_id": "S-" + digest([cfg["installation_id"], epoch, day])[:40],
             "period_start": stamp(start), "period_end": stamp(end), "source_timezone": source_tz,
             "sequence": 1, "generated_at": stamp(now), "data_watermark": stamp(min(end, now)),
             "coverage": coverage, "metrics": metrics}
        path = summary_path(v)
        prior = conn.execute("SELECT body FROM outgoing WHERE path=?", (path,)).fetchone()
        old = json.loads(prior[0]) if prior else None
        heartbeat_due = day == today.isoformat() and (not old or now - parse_time(old["generated_at"], "UTC") >= timedelta(hours=6))
        if not old or old["metrics"] != metrics or old["coverage"] != coverage or heartbeat_due:
            v["sequence"] = old["sequence"] + 1 if old else 1
            state.queue(conn, path, v)
            changed += 1
        conn.execute("INSERT INTO evidence VALUES(?,?) ON CONFLICT(day) DO UPDATE SET ids=excluded.ids",
                     (day, json.dumps(sorted(b["ids"]))))
    state.put(conn, "last_collection", {"at": stamp(now), "invalid_timestamps": invalid_times, "recovery_days": sorted(recovery)})
    state.put(conn, "day_epochs", day_epochs)
    return changed
