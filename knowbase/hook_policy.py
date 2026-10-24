"""Conservative automatic-capture filter; explicit capture/import stay lossless.

Keep original observations for audit. Filter only obvious host/maintenance
envelopes and one-off reports before creating extraction jobs.
"""
import json
import re


def _explicit_knowledge(text):
    return (re.search(r"业务规则\s*[:：]", text) or
            all(f"## {heading}" in text for heading in ("结论", "关键证据", "验证情况")))


def _envelope(event):
    value = event.text
    for _ in range(4):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (ValueError, TypeError):
                return {}
        if not isinstance(value, dict):
            return {}
        if "hook_event" in value or "payload" in value:
            return value
        nested = value.get("text")
        if not isinstance(nested, (str, dict)):
            return value
        value = nested
    return {}


def capture_reason(event):
    """Return a reason only for deterministic noise, never guess business value."""
    text = event.text or ""
    if event.role == "system" or not text.strip():
        return "host_envelope"
    if text.lstrip().startswith("<system-reminder>") and any(
            marker in text for marker in ("[knowbase", "Stop hook additional context", "# agentsMd")):
        return "host_reminder"
    raw = _envelope(event)
    if raw.get("hook_event") in {"tool-before", "stop", "recover", "before-compact"}:
        return "host_envelope"
    payload = raw.get("payload", raw)
    if isinstance(payload, dict):
        tool = event.tool_name or payload.get("tool_name") or payload.get("toolName") or payload.get("name")
        args = payload.get("tool_input") or payload.get("toolInput") or payload.get("input") or {}
        if tool == "Read" and isinstance(args, dict):
            path = str(args.get("file_path") or args.get("path") or "").replace("\\", "/")
            if (path.rsplit("/", 1)[-1].lower() in {"agents.md", "readme.md", "progress.md", "findings.md", "task_plan.md"}
                    and not _explicit_knowledge(json.dumps(payload, ensure_ascii=False))):
                return "operational_document"
    if event.role == "assistant":
        # Explicit rule/knowledge structures remain eligible, even inside reports.
        explicit = _explicit_knowledge(text)
        if not explicit and re.match(r"\s*#{1,3}[^\n]{0,100}(?:产品评估|项目评估|评测报告|状态汇总|执行进度)", text):
            return "one_off_report"
    return ""


def filter_capture(events):
    kept, counts = [], {}
    for event in events:
        reason = capture_reason(event)
        if reason:
            counts[reason] = counts.get(reason, 0) + 1
        else:
            kept.append(event)
    return kept, counts
