"""P3 v2 最小事件包 schema：统计与采用证据分别交换（方案 §6.3）。

v2 与 v1 的区别：v2 仅含白名单最小字段，新增 retrieval.delivered
事件类型；v1 类型复用 events.validate_payload 的字段检查。
v2 包走单独证据分支，不混入知识检索目录。

白名单外的字段一律拒绝入库：无查询、知识正文、原始工具结果、
机器硬件标识或凭据。未知事件类型不丢弃、不称通过：导入端返回
unsupported，发送端留待（PEER_SCHEMA_UNSUPPORTED）而不重发丢弃。
"""

from . import events

SCHEMA_V2 = "knowbase-value-events-v2"
# v2 协议上限：比 v1 的 20000 事件/64MiB 更严格（§6.3）。
MAX_EVENTS = 1000
MAX_BYTES = 1024 * 1024

# These scalar identities are required by aggregation and acceptance, not raw material.
CONTEXT_FIELDS = ("member_binding_id", "host", "session_id", "task_id",
                  "task_run_id", "task_revision", "schema_version", "adapter_version", "host_version")

# v2 事件类型 → 允许字段（payload 内）。v1 类型复用 events.PAYLOAD_FIELDS
# 的 required/optional（主线真实存在的类型）；model.usage 不入 v2 包，
# 计数继续走 team daily summaries（§6.3：统计与证据口径分离）。
# 合并复核（P0 已合入）：P0 在主线 PAYLOAD_FIELDS 补齐了
# knowledge.delivered（与下述 retrieval.delivered 字段完全一致），
# 故 v2 直接复用该口径；knowledge.delivered 是 P0 唯一实际发射的
# 递送类型（context.execute），acceptance 全链路亦只认该类型，
# 因此必须入 v2 白名单，否则递送证据走 v2 通道时会被 export 静默过滤、
# 永远到不了汇总库。retrieval.delivered 保留作 P3 兼容别名。
def _v1_fields(etype: str) -> dict:
    spec = events.PAYLOAD_FIELDS.get(etype) or {"required": set(), "optional": set()}
    return {"required": set(spec["required"]), "optional": set(spec["optional"])}


V2_FIELDS = {
    # v2 新增递送事件：字段口径与 P0 证据修复一致，但 v2 自带白名单，
    # 不依赖 P0 分支合入。
    "retrieval.delivered": {
        "required": {"delivery_id", "memory_id", "content_revision", "task_id"},
        "optional": {"search_id", "context_event_id", "scope", "rank"},
    },
}
for _etype in ("knowledge.used", "knowledge.read", "knowledge.created",
               "knowledge.revised", "knowledge.archived", "knowledge.activated",
               "knowledge.delivered",
               "task.completed", "task.abandoned", "feedback.recorded"):
    V2_FIELDS[_etype] = _v1_fields(_etype)


def validate_v2_event(event: dict) -> dict:
    """校验一条 v2 事件：返回 {"ok": True} 或 {"error": ...}。

    拒绝：非 dict、缺 event_id/类型/时间戳、未知类型、缺必填字段、
    含白名单外字段。被拒绝的事件只隔离单条，不使整包停滞。
    """
    if not isinstance(event, dict):
        return {"error": "not_a_mapping"}
    event_id = event.get("event_id")
    etype = event.get("event_type")
    if not event_id or not isinstance(event_id, str):
        return {"error": "missing_event_id"}
    if not etype or etype not in V2_FIELDS:
        return {"error": "unknown_event_type", "event_type": str(etype)}
    if not event.get("occurred_at"):
        return {"error": "missing_occurred_at"}
    if any(event.get(key) is not None and not isinstance(event[key], str)
           for key in CONTEXT_FIELDS if key in event):
        return {"error": "invalid_context_identity"}
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return {"error": "payload_not_a_mapping"}
    if any(not isinstance(v,(str,int,float,bool,type(None))) for v in payload.values()):
        return {'error':'payload_value_not_scalar'}
    if etype in events.PAYLOAD_FIELDS:
        try: events.validate_payload(etype,payload)
        except (ValueError,TypeError): return {'error':'invalid_payload'}
    spec = V2_FIELDS[etype]
    allowed = spec["required"] | spec["optional"]
    extra = sorted(set(payload) - allowed)
    if extra:
        return {"error": "payload_field_not_allowed", "fields": extra}
    missing = sorted(spec["required"] - set(payload))
    if missing:
        return {"error": "payload_field_missing", "fields": missing}
    return {"ok": True}


def strip_v2_event(event: dict) -> dict:
    """只保留 v2 白名单字段，构造可入 Git 的最小事件表示。"""
    payload = event.get("payload", {})
    spec = V2_FIELDS[event["event_type"]]
    allowed = spec["required"] | spec["optional"]
    return {
        **{key: event[key] for key in CONTEXT_FIELDS if key in event and event[key] is not None},
        "event_id": event["event_id"],
        "event_type": event["event_type"],
        "occurred_at": event.get("occurred_at", ""),
        "team_id": event.get("team_id", ""),
        "member_id": event.get("member_id", ""),
        "installation_id": event.get("installation_id", ""),
        "payload": {k: payload[k] for k in sorted(allowed) if k in payload},
    }
