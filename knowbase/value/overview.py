"""只读统计卡片内核。只输出计数/口径，不输出查询、卡正文或任务明细。"""
import json
import math
from collections import Counter


def _numeric(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def _usage(records, source):
    records = list(records)
    fields = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")
    totals = {f: sum(r[f] for r in records if _numeric(r.get(f)))
              if any(_numeric(r.get(f)) for r in records) else None for f in fields}
    complete = bool(records) and all(all(_numeric(r.get(f)) for f in fields[:2]) for r in records)
    total = totals["input_tokens"] + totals["output_tokens"] if complete else None
    return {"requests": len(records), "total": total, "fields": totals, "source": source,
            "complete": complete, "saved_tokens": None,
            "savings_note": "暂无同模型、同任务的配对对照，净节省量待测"}


def build(conn, *, local_usage=None):
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    legacy = dict(conn.execute("SELECT tool,COUNT(*) FROM usage_log GROUP BY tool")) if "usage_log" in tables else {}
    commands = dict(conn.execute("SELECT command,COUNT(*) FROM command_calls GROUP BY command")) if "command_calls" in tables else {}
    current = commands or legacy
    calls_known = "command_calls" in tables or "usage_log" in tables
    by_status = dict(conn.execute("SELECT status,COUNT(*) FROM command_calls GROUP BY status")) if commands else {}
    status = Counter({k: 0 for k in ("verified", "once", "staging", "stale", "archived", "unknown")})
    reads = 0
    if "meta" in tables:
        for confidence, state, staging, hit_count in conn.execute("SELECT confidence,status,staging,hit_count FROM meta"):
            key = state if state in ("stale", "archived") else "staging" if staging else confidence
            status[key if key in status else "unknown"] += 1
            reads += hit_count or 0
    searches = known = hit_searches = candidate_hits = 0
    if "usage_log" in tables:
        for (detail,) in conn.execute("SELECT detail FROM usage_log WHERE tool IN ('search','auto_search')"):
            searches += 1
            try:
                decoded = json.loads(detail)
                hits = decoded.get("hits") if isinstance(decoded, dict) else None
            except (ValueError, TypeError):
                hits = None
            if isinstance(hits, list):
                known += 1
                hit_searches += bool(hits)
                candidate_hits += len(hits)
    usage_records = []
    evolution = Counter({"promoted": 0, "retired": 0})
    if "value_outbox" in tables:
        for event_type, raw in conn.execute("SELECT event_type,payload_json FROM value_outbox WHERE state<>'quarantined'"):
            if event_type == "model.usage":
                try:
                    usage_records.append(json.loads(raw)["payload"])
                except (ValueError, KeyError, TypeError):
                    pass
    if "feedback_events" in tables:
        for (raw,) in conn.execute("SELECT result_json FROM feedback_events WHERE state='applied'"):
            try:
                events = json.loads(raw).get("events", [])
                evolution["promoted"] += any("→ verified" in event or "自动激活" in event for event in events)
                evolution["retired"] += any("→ stale" in event for event in events)
            except (ValueError, TypeError):
                pass
    usage = _usage(usage_records, "本机已入账 model.usage")
    if not usage_records and local_usage is not None:
        usage = _usage(local_usage, "本机 Claude 请求记录（观测用量）")
    if not usage_records and local_usage is None:
        usage["source"] = "尚无可用请求计量"
    return {"scope": "本机", "calls": sum(current.values()) if calls_known else None,
            "calls_source": "command_calls · 含 Hook 与重试" if commands else "历史 usage_log",
            "legacy_calls": sum(legacy.values()), "command_status": by_status,
            "commands": [{"name": k, "count": v} for k, v in sorted(current.items(), key=lambda r: (-r[1], r[0]))],
            "knowledge_total": sum(status.values()) if "meta" in tables else None, "knowledge": dict(status),
            "searches": searches, "known_searches": known, "hit_searches": hit_searches,
            "candidate_hits": candidate_hits, "missing_searches": searches-known,
            "hit_rate": round(hit_searches / known * 100, 1) if known else None,
            "reads": reads, "updates": legacy["update"] if "update" in legacy else sum(v for k,v in commands.items() if k in ("update", "memory_update")),
            "updates_note": "历史 usage_log 更新调用 · 与新日志不相加" if "update" in legacy else "command_calls 更新调用 · 不等同于成功变更数",
            "usage": usage, "evolution": dict(evolution)}


def team(snapshot):
    """团队事件快照无命令/库存权威，不以事件数冒充调用或卡数量。"""
    types = snapshot.get("counts", {}).get("events_by_type", {})
    return {"scope": "团队", "calls": None, "calls_source": "团队快照未采集命令调用",
            "knowledge_total": None, "knowledge": {}, "commands": [], "legacy_calls": None,
            "searches": None, "known_searches": None, "hit_searches": None,
            "candidate_hits": None, "missing_searches": None, "hit_rate": None, "reads": types.get("knowledge.read", 0),
            "updates": types.get("knowledge.revised", 0), "updates_label": "知识修订事件",
            "evolution": {"promoted": types.get("knowledge.activated", 0), "retired": None},
            "usage": {**_usage([], "团队已导入请求用量"), **_team_usage(snapshot.get("cost", {}))},
            "reuse": snapshot.get("cross_member_reuse", {}), "members": snapshot.get("members", {}),
            "events": snapshot.get("counts", {}).get("events_total", 0)}


def _team_usage(cost):
    models = cost.get("by_model", {})
    fields = {}
    for field in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens"):
        vals = [m.get(field) for m in models.values()]
        fields[field] = sum(vals) if vals and all(_numeric(v) for v in vals) else None
    complete = bool(models) and all(not m.get("fields_missing_on_requests") for m in models.values())
    total = fields["input_tokens"] + fields["output_tokens"] if complete and all(fields[f] is not None for f in ("input_tokens", "output_tokens")) else None
    return {"requests": sum(m.get("requests", 0) for m in models.values()), "total": total,
            "fields": fields, "complete": complete}
