"""Strict, bounded statistics contracts; content fields never enter transport."""
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

HOSTS = {"zcode", "claude-code", "trae", "unknown"}
COUNTS = {"calls", "legacy_calls", "known_searches", "hit_searches", "missing_searches",
          "updates", "adoption_receipts", "helpful_receipts", "negative_receipts", "promotions", "retirements"}
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")
MAX_BYTES = 512 * 1024


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", value):
        raise ValueError("invalid statistics identifier")
    return value


def utc(value):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("UTC Z timestamp required")
    result = datetime.fromisoformat(value[:-1] + "+00:00")
    if result.utcoffset() != timedelta(0):
        raise ValueError("UTC timestamp required")
    return result


def stamp(value=None):
    return (value or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def fields(value, required, optional=()):
    if not isinstance(value, dict) or set(value) - (set(required) | set(optional)) or set(required) - set(value):
        raise ValueError("unexpected or missing statistics fields")


def count(value, nullable=True):
    if value is None and nullable:
        return
    if type(value) is not int or not 0 <= value <= 2**53 - 1:
        raise ValueError("invalid statistics count")


def safe_label(value, limit=80):
    # A model/member label is not a free-text upload channel.
    if not isinstance(value, str) or not 1 <= len(value) <= limit or any(c in value for c in "\n\r\t/\\<>|`"):
        raise ValueError("invalid statistics label")
    if re.search(r"(?i)(bearer\s|password\s*[:=]|secret\s*[:=]|sk-[A-Za-z0-9]{8})", value):
        raise ValueError("private value in statistics label")
    return value


def validate_member(v):
    fields(v, {"schema", "team_id", "member_id", "label", "status", "joined_at", "retired_at", "sequence", "confirmation"})
    if v["schema"] != "knowbase.team-member.v1" or v["status"] not in {"active", "pending", "retired"}:
        raise ValueError("unsupported member contract")
    for k in ("team_id", "member_id"):
        identifier(v[k])
    safe_label(v["label"])
    joined = utc(v["joined_at"])
    if v["retired_at"] is not None and utc(v["retired_at"]) < joined:
        raise ValueError("retirement precedes membership")
    if v["confirmation"] not in {"pending", "self_confirmed", "publisher_confirmed"}:
        raise ValueError("invalid confirmation")
    if (v["status"] == "active" and (v["retired_at"] is not None or v["confirmation"] == "pending")) or (v["status"] == "retired" and v["retired_at"] is None):
        raise ValueError("inconsistent membership")
    count(v["sequence"], False)
    if not v["sequence"]:
        raise ValueError("sequence must be positive")
    return v


def validate_summary(v):
    fields(v, {"schema", "definition_version", "team_id", "member_id", "installation_id", "db_epoch", "snapshot_id", "period_start", "period_end", "source_timezone", "sequence", "generated_at", "data_watermark", "coverage", "metrics"})
    if v["schema"] != "knowbase.team-summary.v1" or v["definition_version"] != "daily-observed-v1":
        raise ValueError("unsupported statistics contract")
    for k in ("team_id", "member_id", "installation_id", "db_epoch", "snapshot_id"):
        identifier(v[k])
    start, end, generated = (utc(v[k]) for k in ("period_start", "period_end", "generated_at"))
    local = start.astimezone(ZoneInfo("Asia/Shanghai"))
    if end - start != timedelta(days=1) or (local.hour, local.minute, local.second, local.microsecond) != (0, 0, 0, 0):
        raise ValueError("statistics bucket must be one Shanghai calendar day")
    if v["data_watermark"] is not None and not start <= utc(v["data_watermark"]) <= min(end, generated):
        raise ValueError("invalid collection watermark")
    ZoneInfo(v["source_timezone"])
    if v["coverage"] not in {"complete", "partial", "recovery_required", "unknown"}:
        raise ValueError("invalid coverage")
    if v["coverage"] == "complete" and v["data_watermark"] != v["period_end"]:
        raise ValueError("unfinished day cannot be complete")
    count(v["sequence"], False)
    if not v["sequence"]:
        raise ValueError("sequence must be positive")
    m = v["metrics"]
    fields(m, {"calls", "known_searches", "hit_searches", "missing_searches", "updates", "commands", "usage"}, COUNTS)
    for k in COUNTS & set(m):
        count(m[k])
    for k, limit in (("commands", 2000), ("usage", 200)):
        if not isinstance(m[k], list) or len(m[k]) > limit:
            raise ValueError("oversized statistics list")
    keys = set()
    for c in m["commands"]:
        fields(c, {"command", "host", "transport", "status", "count"})
        if not isinstance(c["command"], str) or not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,80}", c["command"]):
            raise ValueError("invalid command name")
        from .. import telemetry
        allowed = telemetry.MCP_COMMANDS | telemetry.CLI_COMMANDS | {f"hook.{e}" for e in telemetry.HOOK_EVENTS}
        if c["command"] not in allowed:
            raise ValueError("unsupported observed command")
        if c["host"] not in HOSTS | {"cli"} or c["transport"] not in {"mcp", "hook", "cli"} or c["status"] not in {"ok", "returned_error", "raised", "unknown"}:
            raise ValueError("invalid command dimensions")
        count(c["count"], False)
        key = tuple(c[k] for k in ("command", "host", "transport", "status"))
        if key in keys:
            raise ValueError("duplicate command group")
        keys.add(key)
    if m["calls"] is not None and sum(c["count"] for c in m["commands"]) != m["calls"]:
        raise ValueError("command totals do not reconcile")
    if m["calls"] is None and m["commands"]:
        raise ValueError("unknown calls cannot carry a known command total")
    if m["updates"] is not None and m["updates"] != sum(c["count"] for c in m["commands"] if c["command"] in {"memory_update", "update"}):
        raise ValueError("update totals do not reconcile")
    if m["known_searches"] is not None and m["hit_searches"] is not None and m["hit_searches"] > m["known_searches"]:
        raise ValueError("search totals do not reconcile")
    for u in m["usage"]:
        fields(u, {"host", "source", "model", "requests", *TOKEN_FIELDS})
        if u["host"] not in HOSTS or u["source"] not in {"request_log", "model_usage", "unknown"}:
            raise ValueError("invalid daily usage source")
        if not isinstance(u["model"], str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{0,160}", u["model"]):
            raise ValueError("model must be an identifier, not content")
        safe_label(u["model"] or "unknown", 160)
        for k in ("requests", *TOKEN_FIELDS):
            count(u[k])
    if len(canonical(v)) > MAX_BYTES:
        raise ValueError("statistics file too large")
    return v


def summary_path(v):
    validate_summary(v)
    day = utc(v["period_start"]).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
    return f"devices/{v['installation_id']}/{v['db_epoch']}/{day}.json"
