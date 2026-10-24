"""本机价值摘要与确定性报告：指标全部在 Python 内核计算，页面/报告只展示。

零值、未知、未启用分别表达，不互相冒充；历史治理累计（旧 usage/反馈计数）
与新事件窗口指标分开口径，不相加。
"""

import json
from datetime import datetime, timezone

from .. import config

SUMMARY_SCHEMA = "knowbase.value.personal.v1"
MAX_COMMAND_ROWS = 20
MAX_RECENT_EVENTS = 500
MAX_WEEKLY_BUCKETS = 12


def _week_key(occurred_at: str) -> str:
    """ISO 周（周一为一周起点，方案 §7）；无法解析返回空串。"""
    from datetime import date, datetime
    if not occurred_at:
        return ""
    try:
        return datetime.fromisoformat(occurred_at.replace("Z", "+00:00")).isocalendar()[:2]
    except ValueError:
        try:
            return date.fromisoformat(occurred_at[:10]).isocalendar()[:2]
        except ValueError:
            return ""


def _weekly_series(events: list[dict]) -> list[dict]:
    """按 ISO 周聚合的事件/采纳/反馈序列（指标在内核计算，页面只渲染）。"""
    buckets: dict[tuple, dict] = {}
    for event in events:
        key = _week_key(event.get("occurred_at", ""))
        if not key:
            continue
        slot = buckets.setdefault(key, {"week": f"{key[0]}-W{key[1]:02d}", "events": 0,
                                        "adopted": 0, "feedback": 0})
        slot["events"] += 1
        if event.get("event_type") == "knowledge.used" and \
                event.get("state") == "adopted":
            slot["adopted"] += 1
        elif event.get("event_type") == "feedback.recorded":
            slot["feedback"] += 1
    return [buckets[k] for k in sorted(buckets)][-MAX_WEEKLY_BUCKETS:]


def _window(where: list[str], params: list, *, since: str, until: str, column: str = "created_at"):
    if since:
        where.append(f"{column}>=?")
        params.append(since)
    if until:
        where.append(f"{column}<?")
        params.append(until)


def personal_summary(conn, *, since: str = "", until: str = "") -> dict:
    """从本机 memory.db 汇总价值分区数据；表缺失按 unknown 表达，不填零冒充。"""
    events_enabled = bool(config.load_config().get("value", {}).get("events_enabled"))
    summary = {"schema": SUMMARY_SCHEMA, "window": {"since": since, "until": until},
               "events_enabled": events_enabled, "notes": []}
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

    if "value_outbox" in tables:
        where, params = ["state<>'quarantined'"], []
        _window(where, params, since=since, until=until)
        cond = " AND ".join(where)
        by_type = dict(conn.execute(
            f"SELECT event_type,COUNT(*) FROM value_outbox WHERE {cond} GROUP BY event_type").fetchall())
        by_state = dict(conn.execute(
            "SELECT state,COUNT(*) FROM value_outbox GROUP BY state").fetchall())
        lo, hi = conn.execute(
            f"SELECT MIN(created_at),MAX(created_at) FROM value_outbox WHERE {cond}").fetchone()
        summary["outbox"] = {"total": sum(by_type.values()), "by_type": by_type,
                             "by_state": by_state,
                             "watermark": {"min": lo or "", "max": hi or ""}}
        # 联动筛选与周趋势的数据底座：内嵌最近事件（有界），指标序列在内核算好。
        raw = conn.execute(
            f"SELECT payload_json FROM value_outbox WHERE {cond} "
            "ORDER BY created_at DESC, event_id DESC", params).fetchall()
        recent = []
        for (payload_json,) in raw[:MAX_RECENT_EVENTS]:
            try:
                event = json.loads(payload_json)
            except ValueError:
                continue
            payload = event.get("payload", {}) if isinstance(event.get("payload"), dict) else {}
            recent.append({"event_type": event.get("event_type", ""),
                           "occurred_at": event.get("occurred_at", ""),
                           "host": event.get("host", ""),
                           "task_id": event.get("task_id", "") or payload.get("task_id", ""),
                           "state": payload.get("state", "")})
        recent.reverse()
        summary["recent_events"] = {"events": recent,
                                    "truncated": len(raw) > MAX_RECENT_EVENTS,
                                    "max": MAX_RECENT_EVENTS}
        summary["weekly"] = _weekly_series(recent)
    else:
        summary["outbox"] = None
        summary["notes"].append("value_outbox 不存在（unknown，不是 0）")

    if "knowledge_uses" in tables:
        where, params = ["1=1"], []
        _window(where, params, since=since, until=until)
        cond = " AND ".join(where)
        by_state = dict(conn.execute(
            f"SELECT state,COUNT(*) FROM knowledge_uses WHERE {cond} GROUP BY state").fetchall())
        tasks = conn.execute(
            f"SELECT COUNT(DISTINCT task_id) FROM knowledge_uses WHERE {cond}").fetchone()[0]
        adopted_tasks = conn.execute(
            f"SELECT COUNT(DISTINCT task_id) FROM knowledge_uses WHERE {cond} "
            "AND state='adopted'").fetchone()[0]
        summary["uses"] = {"by_state": by_state, "tasks": tasks, "adopted_tasks": adopted_tasks}
    else:
        summary["uses"] = None

    if "feedback_events" in tables:
        where, params = ["state='applied'"], []
        _window(where, params, since=since, until=until, column="created_at")
        cond = " AND ".join(where)
        by_outcome = dict(conn.execute(
            f"SELECT outcome,COUNT(*) FROM feedback_events WHERE {cond} GROUP BY outcome").fetchall())
        by_binding = dict(conn.execute(
            f"SELECT binding,COUNT(*) FROM feedback_events WHERE {cond} GROUP BY binding").fetchall())
        summary["feedback"] = {"by_outcome": by_outcome, "by_binding": by_binding,
                               "basis": "版本/任务绑定反馈才驱动治理；旧调用单列"}
    else:
        summary["feedback"] = None

    if "task_outcomes" in tables:
        by_result = dict(conn.execute(
            "SELECT result,COUNT(*) FROM task_outcomes GROUP BY result").fetchall())
        by_source = dict(conn.execute(
            "SELECT verification_source,COUNT(*) FROM task_outcomes GROUP BY verification_source").fetchall())
        summary["task_outcomes"] = {"by_result": by_result, "by_source": by_source}
    else:
        summary["task_outcomes"] = None

    if "command_calls" in tables:
        where, params = ["1=1"], []
        _window(where, params, since=since, until=until, column="ts")
        cond = " AND ".join(where)
        rows = conn.execute(
            f"SELECT transport,command,status,COUNT(*) FROM command_calls WHERE {cond} "
            "GROUP BY transport,command,status ORDER BY COUNT(*) DESC,command LIMIT ?",
            (*params, MAX_COMMAND_ROWS)).fetchall()
        total = conn.execute(f"SELECT COUNT(*) FROM command_calls WHERE {cond}", params).fetchone()[0]
        summary["commands"] = {"total": total,
                               "rows": [{"transport": r[0], "command": r[1], "status": r[2],
                                         "count": r[3]} for r in rows],
                               "truncated": total and len(rows) == MAX_COMMAND_ROWS,
                               "basis": "尽力记录口径：进程崩溃可能丢失，不宣称完整调用量"}
    else:
        summary["commands"] = None
        summary["notes"].append("command_calls 不存在：旧库未知口径（unknown，不是 0）")

    identity = {"installation_id": "", "member_label": "", "member_evidence": "",
                "member_confirmed_at": ""}
    if "value_identity" in tables:
        for key in identity:
            row = conn.execute("SELECT value FROM value_identity WHERE key=?", (key,)).fetchone()
            identity[key] = row[0] if row else ""
    summary["identity"] = identity
    summary["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return summary


def render_markdown(*, team: dict | None = None, personal: dict | None = None,
                    title: str = "knowbase 价值周报") -> str:
    """确定性生成报告文本：不含模型润色，数值与证据等级不得被改写。"""
    lines = [f"# {title}", ""]
    stamp = (team or personal or {}).get("generated_at", "")
    lines.append(f"生成时间：{stamp}")
    lines.append("")
    if personal:
        lines += ["## 本机（事件窗口口径）", ""]
        lines.append(f"- 事件发射：{'已启用' if personal['events_enabled'] else '未启用（展示既有记录）'}")
        if personal.get("outbox"):
            ob = personal["outbox"]
            lines.append(f"- 事件总数：{ob['total']}；类型分布："
                         + "、".join(f"{k} {v}" for k, v in sorted(ob["by_type"].items())))
            lines.append(f"- 状态：{'、'.join(f'{k} {v}' for k, v in sorted(ob['by_state'].items())) or '无'}"
                         f"；数据水位：{ob['watermark']['min'] or '无'} ~ {ob['watermark']['max'] or '无'}")
        else:
            lines.append("- 事件记录：unknown（value_outbox 不存在）")
        if personal.get("uses"):
            uses = personal["uses"]
            states = "、".join(f"{k} {v}" for k, v in sorted(uses["by_state"].items())) or "无"
            lines.append(f"- 消费回执：{states}；任务 {uses['tasks']}（其中采纳 {uses['adopted_tasks']}）")
        if personal.get("feedback"):
            fb = personal["feedback"]
            outcomes = "、".join(f"{k} {v}" for k, v in sorted(fb["by_outcome"].items())) or "无"
            lines.append(f"- 反馈：{outcomes}（{fb['basis']}）")
        if personal.get("task_outcomes") and personal["task_outcomes"]["by_result"]:
            to = personal["task_outcomes"]
            lines.append("- 任务结果：" + "、".join(f"{k} {v}" for k, v in sorted(to["by_result"].items()))
                         + "；验证来源：" + "、".join(f"{k} {v}" for k, v in sorted(to["by_source"].items())))
        if personal.get("commands"):
            cmd = personal["commands"]
            lines.append(f"- 命令调用（尽力口径）：共 {cmd['total']}"
                         + ("；Top：" + "、".join(f"{r['transport']}/{r['command']}×{r['count']}"
                                                 for r in cmd["rows"][:5]) if cmd["rows"] else ""))
        for note in personal.get("notes", []):
            lines.append(f"- 注意：{note}")
        lines.append("")
    if team:
        lines += ["## 团队快照（汇总机导入范围）", ""]
        lines.append(f"- 窗口：{team['window']['since'] or '开始'} ~ {team['window']['until'] or '最新'}；"
                     f"数据水位：事件 {team['data_watermark']['max_event_time'] or '无'} / "
                     f"导入 {team['data_watermark']['max_import_time'] or '无'}")
        members = team["members"]
        coverage = (f"{members['coverage']:.0%}" if members.get("coverage_known")
                    and members.get("coverage") is not None else "unknown（无有归属的已登记实例）")
        lines.append(f"- 成员：观测 {members['observed']}，有效消费实例 "
                     f"{members.get('effective_consumer_instances', members['effective_consumers'])}，"
                     f"已登记实例 {members.get('instances_registered', '未知')}（有归属 "
                     f"{members.get('instances_attributed', '未知')}），覆盖率 {coverage}")
        unknown = members.get("unknown_instances") or []
        if unknown:
            lines.append(f"- 未归属实例单列 {len(unknown)} 个"
                         + ("（截断）" if members.get("unknown_instances_truncated") else "")
                         + "：" + "、".join(f"{u['installation_id']}×{u['events']}"
                                           for u in unknown[:10]))
        if members.get("attribution_corrections_total"):
            lines.append(f"- 归属纠错留痕 {members['attribution_corrections_total']} 条")
        tasks = team["tasks"]
        rate = f"{tasks['adoption_rate']:.0%}" if tasks.get("adoption_rate") is not None else "未知"
        lines.append(f"- 任务：收到 {tasks['delivered']}，采纳 {tasks['adopted']}，采纳率 {rate}")
        cross = team["cross_member_reuse"]
        if cross.get("available"):
            lines.append(f"- 跨成员复用：任务 {cross['cross_member_tasks']}（未归属单列 "
                         f"{cross['unattributed_tasks']}）；明确有帮助 {cross['helpful_tasks']}；"
                         f"反馈覆盖率 {cross['feedback_coverage'] if cross['feedback_coverage'] is not None else '未知'}")
        else:
            lines.append(f"- 跨成员复用：不可计算——{cross.get('reason', '')}")
        for note in team.get("limitations", []):
            lines.append(f"- 限制：{note}")
        lines.append("")
    lines.append("个人与团队分属不同范围，本报告不相加、不互相推导。")
    lines.append("迟到数据会生成新版本报告；本版本数值与证据等级不可被文字润色改写。")
    return "\n".join(lines) + "\n"


_SCALARS = (str, int, float, bool, type(None))


def _scalar_map(value, allowed):
    return {k: v for k, v in (value or {}).items()
            if k in allowed and isinstance(v, _SCALARS)} if isinstance(value, dict) else {}


def _scalar_list(value):
    return [v for v in value if isinstance(v, _SCALARS)] if isinstance(value, list) else []


def _sanitize_snapshot(snap: dict) -> dict:
    """深层结构化裁剪：每个容器只保留已知键、叶子只保留标量——嵌套位置
    夹带的正文/查询（如 task_details[].raw_query）不会进入看板数据。"""
    out: dict = {}
    for key in ("schema", "definition_version", "team_id", "generated_at"):
        if isinstance(snap.get(key), str):
            out[key] = snap[key]
    out["window"] = _scalar_map(snap.get("window"), {"since", "until"})
    out["data_watermark"] = _scalar_map(snap.get("data_watermark"),
                                        {"max_event_time", "max_import_time"})
    counts = snap.get("counts") if isinstance(snap.get("counts"), dict) else {}
    out["counts"] = {"events_total": counts.get("events_total") if isinstance(
        counts.get("events_total"), int) else 0,
        "events_by_type": {k: v for k, v in (counts.get("events_by_type") or {}).items()
                           if isinstance(k, str) and isinstance(v, int)}}
    out["members"] = _scalar_map(snap.get("members"), {
        "observed", "effective_consumers", "effective_consumer_instances", "roster_size",
        "coverage", "coverage_known", "instances_registered", "instances_attributed",
        "unknown_instances_truncated", "attribution_corrections_total"})
    members_raw = snap.get("members") if isinstance(snap.get("members"), dict) else {}
    out["members"]["unknown_instances"] = [
        _scalar_map(u, {"installation_id", "events"})
        for u in members_raw.get("unknown_instances") or [] if isinstance(u, dict)]
    out["members"]["attribution_corrections"] = [
        _scalar_map(c, {"installation_id", "old_member_id", "new_member_id", "actor",
                        "note", "corrected_at"})
        for c in members_raw.get("attribution_corrections") or [] if isinstance(c, dict)]
    out["tasks"] = _scalar_map(snap.get("tasks"), {"delivered", "adopted", "adoption_rate"})
    feedback = snap.get("feedback") if isinstance(snap.get("feedback"), dict) else {}
    out["feedback"] = {"by_outcome": {k: v for k, v in (feedback.get("by_outcome") or {}).items()
                                      if isinstance(v, int)},
                       "by_source": {k: v for k, v in (feedback.get("by_source") or {}).items()
                                     if isinstance(v, int)}}
    out["cross_member_reuse"] = _scalar_map(snap.get("cross_member_reuse"), {
        "available", "adopted_tasks", "cross_member_tasks", "unattributed_tasks",
        "feedback_covered_tasks", "helpful_tasks", "agent_reported_helpful_tasks",
        "conflicted_tasks", "failed_excluded_tasks", "unlinked_feedback_events",
        "feedback_coverage", "reason", "note"})
    cost = snap.get("cost") if isinstance(snap.get("cost"), dict) else {}
    by_model = {}
    for model, slot in (cost.get("by_model") or {}).items():
        if isinstance(model, str) and isinstance(slot, dict):
            by_model[model] = _scalar_map(slot, {
                "requests", "input_tokens", "output_tokens", "cache_read_tokens",
                "cache_creation_tokens", "fields_missing_on_requests", "missing_fields"})
    out["cost"] = {"available": cost.get("available") if isinstance(
        cost.get("available"), bool) else False,
        "by_model": by_model, "usage_sources": _scalar_list(cost.get("usage_sources")),
        "attribution": cost.get("attribution", "") if isinstance(cost.get("attribution"), str) else "",
        "currency": cost.get("currency", "") if isinstance(cost.get("currency"), str) else "",
        "reason": cost.get("reason", "") if isinstance(cost.get("reason"), str) else ""}
    out["weekly"] = [_scalar_map(w, {"week", "events", "adopted", "feedback", "last"})
                     for w in snap.get("weekly") or [] if isinstance(w, dict)]
    out["weekly_by_host"] = {
        host: [_scalar_map(w, {"week", "events", "adopted", "feedback", "last"})
               for w in buckets if isinstance(w, dict)]
        for host, buckets in (snap.get("weekly_by_host") or {}).items()
        if isinstance(host, str) and isinstance(buckets, list)}
    out["daily"] = [_scalar_map(d, {"date", "events", "adopted", "feedback"})
                    for d in snap.get("daily") or [] if isinstance(d, dict)]
    out["daily_by_host"] = {
        host: [_scalar_map(d, {"date", "events", "adopted", "feedback"})
               for d in days if isinstance(d, dict)]
        for host, days in (snap.get("daily_by_host") or {}).items()
        if isinstance(host, str) and isinstance(days, list)}
    out["hosts"] = _scalar_list(snap.get("hosts"))
    out["sources"] = [_scalar_map(s, {"installation_id", "events", "first_event", "last_event"})
                      for s in snap.get("sources") or [] if isinstance(s, dict)]
    out["task_details"] = [_scalar_map(t, {"task_id", "owner", "memory_id", "state"})
                           for t in snap.get("task_details") or [] if isinstance(t, dict)]
    out["reuse_evidence"] = []
    for entry in snap.get("reuse_evidence") or []:
        if not isinstance(entry, dict):
            continue
        out["reuse_evidence"].append({
            **_scalar_map(entry, {"task_id", "owner", "run", "consumer"}),
            "cards": [_scalar_map(c, {"memory_id", "contributor"})
                      for c in entry.get("cards") or [] if isinstance(c, dict)],
            "timeline": [_scalar_map(e, {"type", "at", "actor"})
                         for e in entry.get("timeline") or [] if isinstance(e, dict)]})
    out["reuse_evidence_truncated"] = bool(snap.get("reuse_evidence_truncated"))
    out["task_details_truncated"] = bool(snap.get("task_details_truncated"))
    out["limitations"] = _scalar_list(snap.get("limitations"))
    return out


def load_team_snapshot(path) -> dict:
    """校验并裁剪团队快照：只接受本工具 schema、拒绝过大文件、丢弃未识别顶层键。"""
    from pathlib import Path
    p = Path(path).expanduser()
    raw = p.read_bytes()
    if len(raw) > 10 * 1024 * 1024:
        raise ValueError("TEAM_SNAPSHOT_TOO_LARGE")
    try:
        snap = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("TEAM_SNAPSHOT_MALFORMED") from exc
    if not isinstance(snap, dict) or snap.get("schema") != "knowbase.value.snapshot.v1":
        raise ValueError("TEAM_SNAPSHOT_UNSUPPORTED_SCHEMA")
    return _sanitize_snapshot(snap)
