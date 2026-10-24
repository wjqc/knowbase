"""汇总机独立分析库与最小团队快照。

只保存统计证据，不是第二套知识权威。默认归属 = 实例密钥 + 首次 enroll
记录链（实例侧绑定，不靠汇总端人工确认）；人工 confirm_member 保留为
纠错入口（发现归属错误时修正并留痕），不再是归属必需路径。覆盖率分母
为已登记实例数（有归属的实例），未归属实例单列 unknown，不计入分母；
host/工具标签永远不当成员。
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
MAX_DAILY_DAYS = 370

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
CREATE TABLE IF NOT EXISTS attribution_corrections(
  correction_id INTEGER PRIMARY KEY AUTOINCREMENT,
  installation_id TEXT NOT NULL,
  old_member_id TEXT NOT NULL DEFAULT '',
  new_member_id TEXT NOT NULL,
  actor TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  corrected_at TEXT NOT NULL
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
  session_id TEXT NOT NULL DEFAULT '',
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
    # 旧汇总库补会话来源列（任务运行身份用；个人 memory.db 迁移链不涉及此表），
    # 并从历史事件 JSON 回填 session_id——否则旧消费与新反馈的身份会断裂。
    columns = {r[1] for r in conn.execute("PRAGMA table_info(agg_events)").fetchall()}
    if "session_id" not in columns:
        conn.execute("ALTER TABLE agg_events ADD COLUMN session_id TEXT NOT NULL DEFAULT ''")
    backfill = []
    for rowid, payload_json in conn.execute(
            "SELECT rowid, payload_json FROM agg_events WHERE session_id=''").fetchall():
        try:
            session = str((json.loads(payload_json) or {}).get("session_id", "") or "")
        except ValueError:
            continue
        if session:
            backfill.append((session, rowid))
    if backfill:
        conn.executemany("UPDATE agg_events SET session_id=? WHERE rowid=?", backfill)
    conn.commit()
    return conn


def register_member(conn, member_id: str, display_label: str = "") -> dict:
    conn.execute(
        "INSERT INTO members(member_id,display_label,confirmed_at,note) VALUES(?,?,?,'') "
        "ON CONFLICT(member_id) DO UPDATE SET display_label=excluded.display_label",
        (member_id, display_label, now()))
    conn.commit()
    return {"member_id": member_id, "display_label": display_label}


def confirm_member(conn, installation_id: str, member_id: str, *,
                   actor: str = "", note: str = "") -> dict:
    """纠错入口：把安装实例归属修正到指定成员，并留痕。

    不再是归属必需路径——默认归属走实例 enroll 链（见 attribution），
    只有发现归属错误时才调用本函数。修正只改 installations 映射行，
    不改写历史事件行（不追认历史事件为认证事实）；每次修正写入
    attribution_corrections（旧归属、新归属、操作人、时间），不得静默改写。
    """
    old = conn.execute("SELECT member_id FROM installations WHERE installation_id=?",
                       (installation_id,)).fetchone()
    old_member = old[0] if old and old[0] else ""
    conn.execute(
        "INSERT INTO installations(installation_id,member_id,member_label,evidence,"
        "member_binding_id,first_seen,last_seen) VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(installation_id) DO UPDATE SET member_id=excluded.member_id",
        (installation_id, member_id, "", "", "", now(), now()))
    cur = conn.execute("INSERT INTO attribution_corrections(installation_id,old_member_id,"
                       "new_member_id,actor,note,corrected_at) VALUES(?,?,?,?,?,?)",
                       (installation_id, old_member, member_id, actor, note, now()))
    conn.commit()
    return {"installation_id": installation_id, "member_id": member_id,
            "old_member_id": old_member, "correction_id": cur.lastrowid}


def attribution(conn, team_id: str) -> dict:
    """实例归属总览（Phase 3-A，snapshot 与验收共用同一口径）。

    归属优先级：纠错映射（confirm_member）> 事件自带绑定 > enroll 记录绑定；
    host/工具标签永远不产生归属。返回：
    - mapping：安装实例 → 纠错成员 id；
    - enroll：安装实例 → 首次 enroll 绑定（first-enroll-wins，见 exchange）；
    - attributed：有归属的安装实例集合（映射或任一绑定）；
    - unknown：事件中出现但无任何归属的安装实例 → {实例: 事件数}；
    - registered：installations 表行数（已登记实例，含未归属者）。
    """
    mapping = {r[0]: r[1] for r in conn.execute(
        "SELECT installation_id,member_id FROM installations WHERE member_id IS NOT NULL "
        "AND member_id<>''").fetchall() if r[0]}
    enroll = {r[0]: r[1] for r in conn.execute(
        "SELECT installation_id,member_binding_id FROM installations "
        "WHERE member_binding_id IS NOT NULL AND member_binding_id<>''").fetchall() if r[0]}
    event_bindings: dict[str, set[str]] = {}
    event_counts: dict[str, int] = {}
    for inst, binding in conn.execute(
            "SELECT installation_id,member_binding_id FROM agg_events WHERE team_id=?",
            (team_id,)).fetchall():
        if not inst:
            continue
        event_counts[inst] = event_counts.get(inst, 0) + 1
        if binding:
            event_bindings.setdefault(inst, set()).add(binding)
    attributed = {inst for inst in set(mapping) | set(enroll) | set(event_bindings)}
    unknown = {inst: event_counts[inst] for inst in event_counts if inst not in attributed}
    registered = conn.execute("SELECT COUNT(*) FROM installations").fetchone()[0]
    return {"mapping": mapping, "enroll": enroll, "attributed": attributed,
            "unknown": unknown, "registered": registered}


def _member_keys(conn, team_id: str) -> tuple[dict[str, str], set[str]]:
    """事件身份归并：installation→member 映射优先，其次 member_binding_id；
    两者皆缺 → 未归属（不得把 host/工具标签当成员）。"""
    attr = attribution(conn, team_id)
    return attr["mapping"], set(attr["unknown"])


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
                        f"occurred_at,session_id,payload_json FROM agg_events WHERE {where} "
                        f"ORDER BY occurred_at,event_id", params).fetchall()
    by_type: dict[str, int] = {}
    delivered_tasks: set[str] = set()
    adopted_tasks: set[str] = set()
    adopted_records: list[tuple] = []
    consumer_keys: set[str] = set()
    consumer_insts: set[str] = set()
    feedback_by_outcome: dict[str, int] = {}
    feedback_by_source: dict[str, int] = {}
    feedback_records: list[tuple] = []
    timeline_by_task: dict[str, list] = {}
    usage_by_model: dict[str, dict] = {}
    usage_sources: set[str] = set()
    task_details: list[dict] = []
    binding_members: set[str] = set()
    consumption_receipts: set[tuple] = set()
    sources: dict[str, dict] = {}
    watermark = ""
    attr = attribution(conn, team_id)
    mapping = attr["mapping"]

    def member_key(inst: str, binding: str) -> str | None:
        return mapping.get(inst) or (f"binding:{binding}" if binding else None)

    def owner_of(key, inst: str) -> str:
        """任务归属人：成员映射/绑定优先，退回安装实例——不同成员的同名
        task_id 不合并（方案 §5.3：不按相近性合并跨宿主任务）。"""
        return key or (f"inst:{inst}" if inst else "unknown")

    def run_source(session_id: str, inst: str) -> str:
        """任务运行来源：会话优先，缺会话退回安装实例。同一成员跨安装/跨会话
        的同名 task_id 是不同任务运行；受控转交引用落地前一律不自动关联。"""
        return session_id or (f"inst:{inst}" if inst else "unknown")

    for event_type, inst, binding, task_id, occurred, session_id, payload_json in rows:
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
        identity = (owner, run_source(session_id, inst), task_id)
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
                if payload.get("state") in ("referenced", "adopted"):
                    # 实际消费回执登记：反馈必须命中其中一条（含 use_id）。
                    consumption_receipts.add((identity, payload.get("memory_id", ""),
                                              payload.get("content_revision", ""),
                                              payload.get("use_id", "")))
                    if key:
                        consumer_keys.add(key)
                        if inst:
                            consumer_insts.add(inst)
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
                    # 反馈关联到（归属人,运行来源,任务,卡,版本,use_id）完整消费
                    # 身份：必须命中实际消费回执，否则按未关联单列不计入。
                    feedback_records.append(((owner, run_source(session_id, inst), fb_task),
                                             payload.get("memory_id", ""),
                                             payload.get("content_revision", ""),
                                             payload.get("use_id", ""),
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
    for result_inst, result_binding, result_session, result_payload in conn.execute(
            "SELECT installation_id,member_binding_id,session_id,payload_json FROM agg_events "
            "WHERE team_id=? AND state='accepted' "
            "AND event_type IN ('task.completed','task.abandoned') ORDER BY occurred_at,event_id",
            (team_id,)).fetchall():
        payload = json.loads(result_payload).get("payload", {})
        task_id = payload.get("task_id", "")
        if task_id:
            owner = owner_of(member_key(result_inst, result_binding), result_inst)
            identity = (owner, run_source(result_session, result_inst), task_id)
            task_result_of[identity] = payload.get("result", "unknown")

    roster = conn.execute("SELECT COUNT(*) FROM members").fetchone()[0]
    # 覆盖率（Phase 3-A）：分母 = 有归属的已登记实例数（纠错映射、enroll
    # 绑定或事件自带绑定三者任一），分子 = 其中在窗口内有 referenced/adopted
    # 有效消费的实例数。未归属实例不计入分母，单列 unknown_instances；
    # unknown 不再毒化覆盖率口径（coverage_known 只要求分母 > 0）。
    # 事件级归属仍只认映射与事件自带绑定：enroll 绑定决定实例级归属，
    # 不追认绑定确立前的历史事件（与 confirm_member 纠错语义一致）。
    attributed_insts = attr["attributed"]
    coverage = round(len(consumer_insts) / len(attributed_insts), 4) if attributed_insts else None
    coverage_known = bool(attributed_insts)
    unknown_list = [{"installation_id": inst, "events": n}
                    for inst, n in sorted(attr["unknown"].items())]
    unknown_truncated = len(unknown_list) > 200
    unknown_list = unknown_list[:200]
    corr_total = conn.execute("SELECT COUNT(*) FROM attribution_corrections").fetchone()[0]
    corrections = [{"installation_id": r[0], "old_member_id": r[1], "new_member_id": r[2],
                    "actor": r[3], "note": r[4], "corrected_at": r[5]}
                   for r in conn.execute(
                       "SELECT installation_id,old_member_id,new_member_id,actor,note,"
                       "corrected_at FROM attribution_corrections "
                       "ORDER BY correction_id DESC LIMIT 50").fetchall()]
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
    # 任务级有效性判定（方案 §8.1/T10）：反馈按（归属人,运行来源,任务,卡,版本,
    # use_id）完整消费身份关联——必须命中实际消费回执；未命中回执（伪造/漂移的
    # use_id）按未关联单列，不计入任何任务结论。
    scoped_feedback: dict[tuple, list] = {}
    unlinked_feedback_events = 0
    for identity, mid, rev, use_id, outcome, source_kind in feedback_records:
        if (identity, mid, rev, use_id) in consumption_receipts and \
                identity in cross_member_cards and (mid, rev) in cross_member_cards[identity]:
            scoped_feedback.setdefault(identity, []).append((outcome, source_kind))
        elif (identity, mid, rev, use_id) not in consumption_receipts:
            unlinked_feedback_events += 1
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
        reuse_evidence.append({"task_id": identity[2], "owner": identity[0],
                               "run": identity[1],
                               "consumer": consumer_of_task.get(identity, "unknown"),
                               "cards": cards,
                               "timeline": sorted(timeline_by_task.get(identity, []),
                                                  key=lambda e: e["at"])})
    # 周趋势序列（内核计算，页面只渲染）；按宿主分列供筛选联动。
    # 另存日粒度序列：页面先按事件日期过滤、再重聚为周——跨筛选边界的周
    # 不会把范围外事件计入（周桶整体保留无法做到这一点）。
    weekly_buckets: dict[tuple, dict] = {}
    weekly_by_host: dict[str, dict[tuple, dict]] = {}
    daily_buckets: dict[str, dict] = {}
    daily_by_host: dict[str, dict[str, dict]] = {}
    host_names: set[str] = set()
    for event_type, _inst, _binding, _task_id, occurred, _session, payload_json, host in conn.execute(
            f"SELECT event_type,installation_id,member_binding_id,task_id,occurred_at,"
            f"session_id,payload_json,host FROM agg_events WHERE {where} "
            f"ORDER BY occurred_at,event_id", params).fetchall():
        key = _week_key(occurred)
        if not key:
            continue
        label = f"{key[0]}-W{key[1]:02d}"
        adopted = event_type == "knowledge.used" and \
            json.loads(payload_json).get("payload", {}).get("state") == "adopted"
        is_feedback = event_type == "feedback.recorded"
        slot = weekly_buckets.setdefault(key, {"week": label, "events": 0,
                                               "adopted": 0, "feedback": 0, "last": occurred})
        slot["events"] += 1
        slot["adopted"] += int(adopted)
        slot["feedback"] += int(is_feedback)
        slot["last"] = max(slot["last"], occurred)
        day = occurred[:10]
        dslot = daily_buckets.setdefault(day, {"date": day, "events": 0,
                                               "adopted": 0, "feedback": 0})
        dslot["events"] += 1
        dslot["adopted"] += int(adopted)
        dslot["feedback"] += int(is_feedback)
        if host:
            host_names.add(host)
            hslot = weekly_by_host.setdefault(host, {}).setdefault(
                key, {"week": label, "events": 0, "adopted": 0, "feedback": 0, "last": occurred})
            hslot["events"] += 1
            hslot["adopted"] += int(adopted)
            hslot["feedback"] += int(is_feedback)
            hslot["last"] = max(hslot["last"], occurred)
            hdslot = daily_by_host.setdefault(host, {}).setdefault(
                day, {"date": day, "events": 0, "adopted": 0, "feedback": 0})
            hdslot["events"] += 1
            hdslot["adopted"] += int(adopted)
            hdslot["feedback"] += int(is_feedback)
    weekly = [weekly_buckets[k] for k in sorted(weekly_buckets)][-MAX_WEEKLY_BUCKETS:]
    weekly_by_host = {h: [buckets[k] for k in sorted(buckets)][-MAX_WEEKLY_BUCKETS:]
                      for h, buckets in weekly_by_host.items()}
    daily = [daily_buckets[d] for d in sorted(daily_buckets)][-MAX_DAILY_DAYS:]
    daily_by_host = {h: [buckets[d] for d in sorted(buckets)][-MAX_DAILY_DAYS:]
                     for h, buckets in daily_by_host.items()}
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
            "unlinked_feedback_events": unlinked_feedback_events,
            "feedback_coverage": round(len(feedback_covered) / len(cross_member_tasks), 4)
            if cross_member_tasks else None,
            "note": "明确有帮助=正向反馈且无反馈权威冲突、任务未失败；反馈须命中该任务"
                    "跨成员采纳卡的实际消费回执（含 use_id），未命中回执按未关联单列；"
                    "Agent 单独声称单列（agent_reported），不与用户确认混算",
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
                    "effective_consumer_instances": len(consumer_insts),
                    "roster_size": roster or None, "coverage": coverage,
                    "coverage_known": coverage_known,
                    "instances_registered": attr["registered"],
                    "instances_attributed": len(attributed_insts),
                    "unknown_instances": unknown_list,
                    "unknown_instances_truncated": unknown_truncated,
                    "attribution_corrections": corrections,
                    "attribution_corrections_total": corr_total},
        "tasks": {"delivered": len(delivered_tasks), "adopted": len(adopted_tasks),
                  "adoption_rate": round(len(adopted_tasks) / len(delivered_tasks), 4)
                  if delivered_tasks else None},
        "feedback": {"by_outcome": feedback_by_outcome, "by_source": feedback_by_source},
        "cross_member_reuse": cross_member_reuse,
        "cost": cost,
        "weekly": weekly,
        "weekly_by_host": weekly_by_host,
        "daily": daily,
        "daily_by_host": daily_by_host,
        "hosts": sorted(host_names),
        "reuse_evidence": reuse_evidence,
        "reuse_evidence_truncated": len(cross_member_tasks) > MAX_REUSE_EVIDENCE,
        "sources": sorted(sources.values(), key=lambda s: -s["events"]),
        "task_details": task_details,
        "task_details_truncated": len(task_details) == MAX_TASK_DETAIL,
        "limitations": ["覆盖率分母为有归属的已登记实例数，未归属实例单列 "
                        "unknown_instances、不计入分母",
                        "默认归属走实例 enroll 链（首次绑定为准）；人工 confirm 仅为"
                        "纠错入口，修正留痕于 attribution_corrections",
                        "member_binding_id 为实例侧绑定证据，非中心认证；host/工具"
                        "标签永远不当成员",
                        "重叠窗口/重复导入按 (team,event_id) 去重，不重复累计"],
    }


def write_snapshot(snapshot: dict, output) -> Path:
    out = Path(output).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    return out
