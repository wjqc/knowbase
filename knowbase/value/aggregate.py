"""汇总机独立分析库与最小团队快照。

只保存统计证据，不是第二套知识权威；连接时拒绝个人 memory.db 形态的
目标文件（不混写个人业务库）。成员名单为人工登记（团队覆盖率的分母
权威）；未登记/未确认时快照显式 unknown，不估算。
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SNAPSHOT_SCHEMA = "knowbase.value.snapshot.v1"
DEFINITION_VERSION = "value-metrics-v0"
MAX_TASK_DETAIL = 500
MAX_REUSE_EVIDENCE = 100
MAX_WEEKLY_BUCKETS = 12

_SCHEMA = """
CREATE TABLE IF NOT EXISTS members(
  member_id TEXT PRIMARY KEY,
  display_label TEXT NOT NULL DEFAULT '',
  confirmed_at TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS installations(
  installation_id TEXT PRIMARY KEY,
  member_id TEXT,
  member_label TEXT NOT NULL DEFAULT '',
  evidence TEXT NOT NULL DEFAULT '',
  member_binding_id TEXT NOT NULL DEFAULT '',
  first_seen TEXT,
  last_seen TEXT
);
CREATE TABLE IF NOT EXISTS agg_events(
  team_id TEXT NOT NULL,
  event_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  installation_id TEXT NOT NULL DEFAULT '',
  member_binding_id TEXT NOT NULL DEFAULT '',
  host TEXT NOT NULL DEFAULT '',
  task_id TEXT NOT NULL DEFAULT '',
  occurred_at TEXT NOT NULL DEFAULT '',
  payload_json TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  source_key TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT 'accepted',
  imported_at TEXT NOT NULL,
  PRIMARY KEY(team_id,event_id)
);
CREATE INDEX IF NOT EXISTS idx_agg_events_time ON agg_events(team_id,occurred_at);
CREATE INDEX IF NOT EXISTS idx_agg_events_type ON agg_events(team_id,event_type);
CREATE TABLE IF NOT EXISTS imports(
  package_id TEXT PRIMARY KEY,
  team_id TEXT NOT NULL DEFAULT '',
  receipt_id TEXT NOT NULL DEFAULT '',
  counts_json TEXT NOT NULL DEFAULT '{}',
  imported_at TEXT NOT NULL DEFAULT ''
);
"""

_PERSONAL_MARKERS = ("meta", "knowledge_uses", "feedback_events")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path) -> sqlite3.Connection:
    """打开/创建汇总分析库；目标若为个人 memory.db 形态则拒绝。"""
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if any(m in tables for m in _PERSONAL_MARKERS) and "agg_events" not in tables:
        conn.close()
        raise ValueError("IMPORT_TARGET_PERSONAL_DB：汇总库不能是个人 memory.db")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def register_member(conn, member_id: str, display_label: str = "") -> dict:
    conn.execute(
        "INSERT INTO members(member_id,display_label,confirmed_at,note) VALUES(?,?,?,'') "
        "ON CONFLICT(member_id) DO UPDATE SET display_label=excluded.display_label",
        (member_id, display_label, now()))
    conn.commit()
    return {"member_id": member_id, "display_label": display_label}


def confirm_member(conn, installation_id: str, member_id: str) -> dict:
    """把安装实例归属到登记成员（人工确认）；不追认历史事件为认证事实。"""
    conn.execute(
        "INSERT INTO installations(installation_id,member_id,member_label,evidence,"
        "member_binding_id,first_seen,last_seen) VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(installation_id) DO UPDATE SET member_id=excluded.member_id",
        (installation_id, member_id, "", "", "", now(), now()))
    conn.commit()
    return {"installation_id": installation_id, "member_id": member_id}


def _member_keys(conn, team_id: str) -> tuple[dict[str, str], set[str]]:
    """事件身份归并：installation→member 映射优先，其次 member_binding_id；
    两者皆缺 → 未归属（不得把 host/工具标签当成员）。"""
    mapping = {r[0]: r[1] for r in conn.execute(
        "SELECT installation_id,member_id FROM installations WHERE member_id IS NOT NULL "
        "AND member_id<>''").fetchall() if r[0]}
    unknown: set[str] = set()
    for inst, binding in conn.execute(
            "SELECT DISTINCT installation_id,member_binding_id FROM agg_events WHERE team_id=?",
            (team_id,)).fetchall():
        if not (mapping.get(inst) or binding):
            unknown.add(inst)
    return mapping, unknown


def _week_key(occurred_at: str):
    from datetime import date, datetime
    if not occurred_at:
        return None
    try:
        return datetime.fromisoformat(occurred_at.replace("Z", "+00:00")).isocalendar()[:2]
    except ValueError:
        try:
            return date.fromisoformat(occurred_at[:10]).isocalendar()[:2]
        except ValueError:
            return None


def snapshot(conn, *, team_id: str, since: str = "", until: str = "") -> dict:
    where, params = "team_id=? AND state='accepted'", [team_id]
    if since:
        where += " AND occurred_at>=?"
        params.append(since)
    if until:
        where += " AND occurred_at<?"
        params.append(until)
    rows = conn.execute(f"SELECT event_type,installation_id,member_binding_id,task_id,"
                        f"occurred_at,payload_json FROM agg_events WHERE {where} "
                        f"ORDER BY occurred_at,event_id", params).fetchall()
    by_type: dict[str, int] = {}
    delivered_tasks: set[str] = set()
    adopted_tasks: set[str] = set()
    adopted_records: list[tuple] = []
    consumer_keys: set[str] = set()
    feedback_by_outcome: dict[str, int] = {}
    feedback_by_source: dict[str, int] = {}
    feedback_records: list[tuple] = []
    timeline_by_task: dict[str, list] = {}
    usage_by_model: dict[str, dict] = {}
    usage_sources: set[str] = set()
    task_details: list[dict] = []
    binding_members: set[str] = set()
    sources: dict[str, dict] = {}
    watermark = ""
    mapping, _unknown = _member_keys(conn, team_id)

    def member_key(inst: str, binding: str) -> str | None:
        return mapping.get(inst) or (f"binding:{binding}" if binding else None)

    def owner_of(key, inst: str) -> str:
        """任务归属人：成员映射/绑定优先，退回安装实例——不同成员的同名
        task_id 不合并（方案 §5.3：不按相近性合并跨宿主任务）。"""
        return key or (f"inst:{inst}" if inst else "unknown")

    for event_type, inst, binding, task_id, occurred, payload_json in rows:
        by_type[event_type] = by_type.get(event_type, 0) + 1
        key = member_key(inst, binding)
        if key:
            binding_members.add(key)
        src = sources.setdefault(inst, {"installation_id": inst, "events": 0,
                                        "first_event": occurred, "last_event": occurred})
        src["events"] += 1
        src["first_event"] = min(src["first_event"], occurred)
        src["last_event"] = max(src["last_event"], occurred)
        watermark = max(watermark, occurred)
        owner = owner_of(key, inst)
        identity = (owner, task_id)
        if task_id:
            entries = timeline_by_task.setdefault(identity, [])
            if len(entries) < 64:
                entries.append({"type": event_type, "at": occurred, "actor": owner})
        if event_type in ("knowledge.used", "knowledge.read"):
            payload = json.loads(payload_json).get("payload", {})
            if event_type == "knowledge.used":
                if task_id:
                    delivered_tasks.add(identity)
                if payload.get("state") == "adopted":
                    adopted_tasks.add(identity)
                    adopted_records.append((identity, task_id,
                                            payload.get("memory_id", ""),
                                            payload.get("content_revision", ""), key, owner))
                if payload.get("state") in ("referenced", "adopted") and key:
                    consumer_keys.add(key)
                if len(task_details) < MAX_TASK_DETAIL:
                    task_details.append({"task_id": task_id, "owner": owner,
                                         "memory_id": payload.get("memory_id", ""),
                                         "state": payload.get("state", "")})
        elif event_type == "feedback.recorded":
            payload = json.loads(payload_json).get("payload", {})
            outcome = payload.get("outcome", "unknown")
            source_kind = payload.get("feedback_source", "unknown")
            feedback_by_outcome[outcome] = feedback_by_outcome.get(outcome, 0) + 1
            feedback_by_source[source_kind] = feedback_by_source.get(source_kind, 0) + 1
            if payload.get("use_id"):
                fb_task = payload.get("task_id") or task_id
                if fb_task:
                    # 反馈关联到（反馈人, 任务, 卡, 版本）完整消费身份：
                    # 旧版本反馈不给新版本采纳计收益。
                    feedback_records.append(((owner, fb_task),
                                             payload.get("memory_id", ""),
                                             payload.get("content_revision", ""),
                                             outcome, source_kind))
        elif event_type == "model.usage":
            payload = json.loads(payload_json).get("payload", {})
            model = payload.get("model") or "unknown"
            slot = usage_by_model.setdefault(model, {"requests": 0})
            slot["requests"] += 1
            usage_sources.add(payload.get("usage_source", "unknown"))
            for field in ("input_tokens", "output_tokens", "cache_read_tokens",
                          "cache_creation_tokens"):
                value = payload.get(field)
                if isinstance(value, (int, float)):
                    # 缺失字段不填零：未采集与实际零消耗分开（None=未采集）。
                    slot[field] = slot.get(field, 0) + value
            slot.setdefault("_missing", {"fields": set(), "requests": 0})
            missing = [f for f in ("input_tokens", "output_tokens", "cache_read_tokens",
                                   "cache_creation_tokens")
                       if not isinstance(payload.get(f), (int, float))]
            if missing:
                slot["_missing"]["requests"] += 1
                slot["_missing"]["fields"].update(missing)

    # 贡献者与任务结果取全量事件（不随展示窗口过滤）：内容版本的贡献事实和
    # 任务结局与窗口无关——上月创建、本周采纳仍可归因，窗口外完成的任务结局
    # 仍约束本期判定。
    contributor_of: dict[tuple, str | None] = {}
    for event_type, inst, binding, payload_json in conn.execute(
            "SELECT event_type,installation_id,member_binding_id,payload_json FROM agg_events "
            "WHERE team_id=? AND state='accepted' AND event_type IN "
            "('knowledge.created','knowledge.revised') ORDER BY occurred_at,event_id",
            (team_id,)).fetchall():
        payload = json.loads(payload_json).get("payload", {})
        card = (payload.get("memory_id", ""), payload.get("content_revision", ""))
        if card[0]:
            contributor_of[card] = member_key(inst, binding)
    task_result_of: dict[tuple, str] = {}
    for result_inst, result_binding, result_payload in conn.execute(
            "SELECT installation_id,member_binding_id,payload_json FROM agg_events "
            "WHERE team_id=? AND state='accepted' "
            "AND event_type IN ('task.completed','task.abandoned') ORDER BY occurred_at,event_id",
            (team_id,)).fetchall():
        payload = json.loads(result_payload).get("payload", {})
        task_id = payload.get("task_id", "")
        if task_id:
            owner = owner_of(member_key(result_inst, result_binding), result_inst)
            task_result_of[(owner, task_id)] = payload.get("result", "unknown")

    roster = conn.execute("SELECT COUNT(*) FROM members").fetchone()[0]
    # 覆盖率分子＝有明确 referenced/adopted 的已确认成员（方案 §8.1 有效消费成员），
    # 不是"有任意事件的成员"；绑定未确认身份不计入分子。
    confirmed_effective_consumers = {k for k in consumer_keys if not k.startswith("binding:")}
    coverage = None
    coverage_known = False
    if roster:
        unattributed = conn.execute(
            f"SELECT COUNT(DISTINCT installation_id) FROM agg_events WHERE {where} "
            "AND member_binding_id='' AND installation_id NOT IN (SELECT installation_id "
            "FROM installations WHERE member_id IS NOT NULL AND member_id<>'')",
            params).fetchone()[0]
        # 分母只认登记名单；存在无归属安装时覆盖率口径不完整，如实标注。
        coverage_known = unattributed == 0
        coverage = round(len(confirmed_effective_consumers) / roster, 4) if coverage_known else None
    import_row = conn.execute("SELECT MAX(imported_at) FROM imports WHERE team_id=?",
                              (team_id,)).fetchone()[0] or ""
    cross_member_tasks: set[str] = set()
    unattributed_tasks: set[tuple] = set()
    cross_member_cards: dict[tuple, set] = {}
    for identity, task_id, mid, rev, consumer, owner in adopted_records:
        contributor = contributor_of.get((mid, rev))
        if not contributor or not consumer:
            unattributed_tasks.add(identity)  # 贡献者或消费者身份未知：单列，不猜
        elif contributor != consumer:
            cross_member_tasks.add(identity)  # 任务按（归属人,task_id）去重，只计一次
            cross_member_cards.setdefault(identity, set()).add((mid, rev))
    # 任务级有效性判定（方案 §8.1/T10）：反馈按（反馈人,任务,卡,版本）完整消费
    # 身份关联——旧版本反馈不给新版本采纳计收益，同任务其他卡的反馈不串联；
    # 来源冲突与任务失败不得产出"明确有帮助"。
    scoped_feedback: dict[tuple, list] = {}
    for identity, mid, rev, outcome, source_kind in feedback_records:
        if identity in cross_member_cards and (mid, rev) in cross_member_cards[identity]:
            scoped_feedback.setdefault(identity, []).append((outcome, source_kind))
    verdicts = {"helpful": set(), "agent_reported": set(), "conflicted": set(),
                "not_helpful": set(), "failed_excluded": set()}
    for identity in cross_member_tasks:
        entries = scoped_feedback.get(identity, [])
        result = task_result_of.get(identity)
        failed = result in ("failed", "abandoned")
        positives = [e for e in entries if e[0] == "helpful"]
        negatives = [e for e in entries if e[0] in ("not_helpful", "outdated", "incorrect")]
        if failed and positives:
            verdicts["conflicted"].add(identity)  # 有帮助声称但任务失败：保留冲突，不报成功
        elif failed:
            verdicts["failed_excluded"].add(identity)
        elif positives and negatives:
            verdicts["conflicted"].add(identity)
        elif negatives:
            verdicts["not_helpful"].add(identity)
        elif positives:
            # Agent 声明与人工确认分开展示；user 来源的正向才算用户确认。
            (verdicts["helpful"] if any(e[1] == "user" for e in positives)
             else verdicts["agent_reported"]).add(identity)
    feedback_covered = {t for t in cross_member_tasks if scoped_feedback.get(t)}
    # 复用证据时间线：每个跨成员任务的证据链（递送→采纳→反馈→任务结局），
    # 有界 100 个任务；缺失环节如实不补（方案 §5.1 每个箭头独立可缺失）。
    consumer_of_task: dict[tuple, str] = {}
    for identity, task_id, mid, rev, consumer, owner in adopted_records:
        if identity in cross_member_tasks and consumer:
            consumer_of_task[identity] = consumer
    reuse_evidence = []
    for identity in sorted(cross_member_tasks)[:MAX_REUSE_EVIDENCE]:
        cards = [{"memory_id": mid,
                  "contributor": contributor_of.get((mid, rev)) or "unknown"}
                 for other, task_id2, mid, rev, consumer, owner in adopted_records
                 if other == identity and contributor_of.get((mid, rev))]
        reuse_evidence.append({"task_id": identity[1], "owner": identity[0],
                               "consumer": consumer_of_task.get(identity, "unknown"),
                               "cards": cards,
                               "timeline": sorted(timeline_by_task.get(identity, []),
                                                  key=lambda e: e["at"])})
    # 周趋势序列（内核计算，页面只渲染）
    weekly_buckets: dict[tuple, dict] = {}
    for event_type, _inst, _binding, task_id, occurred, payload_json in rows:
        key = _week_key(occurred)
        if not key:
            continue
        slot = weekly_buckets.setdefault(key, {"week": f"{key[0]}-W{key[1]:02d}",
                                               "events": 0, "adopted": 0, "feedback": 0})
        slot["events"] += 1
        if event_type == "knowledge.used" and \
                json.loads(payload_json).get("payload", {}).get("state") == "adopted":
            slot["adopted"] += 1
        elif event_type == "feedback.recorded":
            slot["feedback"] += 1
    weekly = [weekly_buckets[k] for k in sorted(weekly_buckets)][-MAX_WEEKLY_BUCKETS:]
    if contributor_of:
        cross_member_reuse = {
            "available": True,
            "adopted_tasks": len(adopted_tasks),
            "cross_member_tasks": len(cross_member_tasks),
            "unattributed_tasks": len(unattributed_tasks),
            "feedback_covered_tasks": len(feedback_covered),
            "helpful_tasks": len(verdicts["helpful"]),
            "agent_reported_helpful_tasks": len(verdicts["agent_reported"]),
            "conflicted_tasks": len(verdicts["conflicted"]),
            "failed_excluded_tasks": len(verdicts["failed_excluded"]),
            "feedback_coverage": round(len(feedback_covered) / len(cross_member_tasks), 4)
            if cross_member_tasks else None,
            "note": "明确有帮助=正向反馈且无反馈权威冲突、任务未失败；仅统计该任务跨成员"
                    "采纳卡的反馈；Agent 单独声称单列（agent_reported），不与用户确认混算",
        }
    else:
        cross_member_reuse = {"available": False,
                              "reason": "无 knowledge.created/revised 贡献者事件，无法区分贡献者"}
    for slot in usage_by_model.values():
        missing = slot.pop("_missing", None)
        if missing:
            slot["fields_missing_on_requests"] = missing["requests"]
            slot["missing_fields"] = sorted(missing["fields"])
        for field in ("input_tokens", "output_tokens", "cache_read_tokens",
                      "cache_creation_tokens"):
            slot.setdefault(field, None)  # None=未采集，不是 0
    cost = {
        "available": bool(usage_by_model),
        "by_model": usage_by_model,
        "usage_sources": sorted(usage_sources),
        # 字段分列汇总：缓存不计入输入重复计算；归属只到会话级，不冒充任务实测。
        "attribution": "session" if usage_by_model else "none",
        "currency": "token 计数（不按单价换算成本；账单实付与估算分开，不在本快照推算）",
    } if usage_by_model else {"available": False,
                              "reason": "窗口内无 model.usage 事件（ZCode/Trae 本机取数不支持已明确）"}
    return {
        "schema": SNAPSHOT_SCHEMA,
        "definition_version": DEFINITION_VERSION,
        "team_id": team_id,
        "window": {"since": since, "until": until},
        "generated_at": now(),
        "data_watermark": {"max_event_time": watermark, "max_import_time": import_row},
        "counts": {"events_total": len(rows), "events_by_type": by_type},
        "members": {"observed": len(binding_members), "effective_consumers": len(consumer_keys),
                    "roster_size": roster or None, "coverage": coverage,
                    "coverage_known": coverage_known},
        "tasks": {"delivered": len(delivered_tasks), "adopted": len(adopted_tasks),
                  "adoption_rate": round(len(adopted_tasks) / len(delivered_tasks), 4)
                  if delivered_tasks else None},
        "feedback": {"by_outcome": feedback_by_outcome, "by_source": feedback_by_source},
        "cross_member_reuse": cross_member_reuse,
        "cost": cost,
        "weekly": weekly,
        "reuse_evidence": reuse_evidence,
        "reuse_evidence_truncated": len(cross_member_tasks) > MAX_REUSE_EVIDENCE,
        "sources": sorted(sources.values(), key=lambda s: -s["events"]),
        "task_details": task_details,
        "task_details_truncated": len(task_details) == MAX_TASK_DETAIL,
        "limitations": ["覆盖率分母仅认人工登记成员名单，未登记则 unknown",
                        "member_binding_id 为文件汇总确认证据，非中心认证",
                        "重叠窗口/重复导入按 (team,event_id) 去重，不重复累计"],
    }


def write_snapshot(snapshot: dict, output) -> Path:
    out = Path(output).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    return out
