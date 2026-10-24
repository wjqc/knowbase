"""One aggregation kernel for dashboard and README; received observations only."""
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from . import state
from .contracts import COUNTS, TOKEN_FIELDS, stamp, utc


def _sum(values):
    known = [v for v in values if v is not None]
    return sum(known) if known else None


def totals(snapshots):
    rows = list(snapshots)
    result = {k: _sum(r["metrics"].get(k) for r in rows) for k in COUNTS}
    commands = Counter()
    hosts = Counter()
    for r in rows:
        for c in r["metrics"]["commands"]:
            commands[c["command"]] += c["count"]
            hosts[c["host"]] += c["count"]
    result["commands"] = [{"name": k, "count": n} for k, n in commands.most_common()]
    result["host_calls"] = dict(hosts)
    usage = [u for r in rows for u in r["metrics"]["usage"]]
    fields = {f: _sum(u[f] for u in usage) for f in TOKEN_FIELDS}
    complete = bool(usage) and all(u[f] is not None for u in usage for f in TOKEN_FIELDS[:2])
    result["usage"] = {"fields": fields, "requests": _sum(u["requests"] for u in usage),
                       "total": fields["input_tokens"] + fields["output_tokens"] if complete else None,
                       "complete": complete, "saved_tokens": None,
                       "source": "已上报设备的请求观测 · ZCode/Trae 未采集来源单列缺口",
                       "savings_note": "暂无同模型、同任务配对测量，净节省待测",
                       "observed_hosts": sorted({u["host"] for u in usage}),
                       "missing_hosts": sorted({"zcode", "claude-code", "trae"} - {u["host"] for u in usage})}
    return result


def build(conn, *, now=None, inventory=None, since=None, until=None):
    now = now or datetime.now(timezone.utc)
    from zoneinfo import ZoneInfo
    for boundary in (since, until):
        if boundary is not None:
            local = boundary.astimezone(ZoneInfo("Asia/Shanghai"))
            if boundary.tzinfo is None or (local.hour, local.minute, local.second, local.microsecond) != (0, 0, 0, 0):
                raise ValueError("team report windows must align with Shanghai calendar days")
    if since is not None and until is not None and since >= until:
        raise ValueError("empty team report window")
    members = {}
    buckets = defaultdict(list)
    rejected = state.get(conn, "rejected_files", 0)
    for row in conn.execute("SELECT path,body FROM accepted"):
        v = json.loads(row["body"])
        if row["path"].startswith("members/"):
            members[v["member_id"]] = v
        elif (since is None or utc(v["period_start"]) >= since) and (until is None or utc(v["period_end"]) <= until):
            buckets[(v["installation_id"], v["period_start"])].append(v)
    pending_registry = set()
    for row in conn.execute("SELECT body FROM outgoing WHERE path LIKE 'members/%' AND hash<>ack"):
        v = json.loads(row[0])
        if v["member_id"] not in members:
            members[v["member_id"]] = v
            pending_registry.add(v["member_id"])
    # Refuse ambiguous per-device ownership, including across days.
    owners = defaultdict(set)
    for values in buckets.values():
        for v in values:
            owners[v["installation_id"]].add(v["member_id"])
    selected, recovery = [], set()
    anchors = state.get(conn, "confirmed_epochs", {})
    for key, values in buckets.items():
        if len(owners[key[0]]) != 1:
            rejected += len(values)
            continue
        anchor_key = key[0] + ":" + key[1]
        if len(values) == 1:
            v = values[0]
            anchors.setdefault(anchor_key, v["db_epoch"])
        else:
            recovery.update(v["member_id"] for v in values)
            anchored = [v for v in values if v["db_epoch"] == anchors.get(anchor_key)]
            if not anchored:
                # A newly joined receiver cannot know which overlapping epoch is
                # authoritative. Preserve the gap instead of inventing a baseline.
                continue
            v = anchored[0]
        if v["coverage"] == "recovery_required":
            recovery.add(v["member_id"])
        if v["member_id"] not in members or members[v["member_id"]]["confirmation"] == "pending":
            rejected += 1
            continue
        selected.append(v)
    by_member = defaultdict(list)
    for v in selected:
        by_member[v["member_id"]].append(v)
    roster = []
    cohort_at = min(now, until - timedelta(microseconds=1)) if until else now
    for member_id, member in sorted(members.items()):
        observations = by_member[member_id]
        latest = max((v["generated_at"] for v in observations), default=None)
        fresh = any(utc(v["generated_at"]) >= now - timedelta(hours=24) and
                    utc(v["period_start"]) <= now < utc(v["period_end"]) for v in observations)
        current = utc(member["joined_at"]) <= cohort_at and (member["retired_at"] is None or utc(member["retired_at"]) > cohort_at)
        state_name = ("recovery_required" if member_id in recovery else "pending" if member["status"] == "pending" else
                      "missing" if not observations else "partial" if fresh and any(v["coverage"] != "complete" for v in observations) else
                      "received" if fresh else "stale")
        roster.append({"member_id": member_id, "label": member["label"], "status": member["status"], "current": current,
                       "registry_pending": member_id in pending_registry,
                       "coverage": state_name, "last_report": latest, "devices": len({v["installation_id"] for v in observations}),
                       "totals": totals(observations)})
    active = [m for m in roster if m["current"]]
    received = sum(m["coverage"] in {"received", "partial", "recovery_required"} and m["last_report"] is not None and
                   utc(m["last_report"]) >= now - timedelta(hours=24) for m in active)
    metric = totals(selected)
    state.put(conn, "confirmed_epochs", anchors)
    known, hits = metric["known_searches"], metric["hit_searches"]
    inventory = inventory or {}
    return {"scope": "团队", **metric,
            "calls_source": "已上报日桶 command_calls · 含 Hook 与重试 · 观测合计",
            "updates_note": "新命令更新调用 · 历史更新不重复相加",
            "searches": known + metric["missing_searches"] if known is not None and metric["missing_searches"] is not None else None,
            "hit_rate": round(hits / known * 100, 1) if known and hits is not None else None,
            "reads": None, "evolution": {"promoted": metric["promotions"], "retired": metric["retirements"]},
            "knowledge_total": inventory.get("total"), "knowledge": inventory.get("states", {}),
            "inventory_revision": inventory.get("revision"), "members": roster,
            "coverage": {"registered": len(active), "received": received, "recovery_members": len(recovery),
                         "pending_registry": len(pending_registry),
                         "rejected": rejected, "roster_revision": state.get(conn, "remote_revision"),
                         "since": min((v["period_start"] for v in selected), default=None),
                         "watermark": max((v["data_watermark"] for v in selected if v["data_watermark"]), default=None),
                         "note": "仅覆盖已登记成员；当日未结束，未知来源不补零。" + (f"本机有{len(pending_registry)}名登记待同步。" if pending_registry else "")}, "generated_at": stamp(now)}
