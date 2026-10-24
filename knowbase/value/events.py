"""事件构造与校验：公共字段信封、类型白名单、规范化哈希。

只白名单内的类型/字段可构造；身份缺失（成员未确认）时 member_binding_id
不进入事件，工具标签与宿主标识不得冒充成员。
"""

import json
import uuid
from datetime import datetime, timezone

SCHEMA_VERSION = "knowbase.value.events.v1"

EVENT_TYPES = frozenset({
    "capability.snapshot", "task.started", "task.checkpoint", "task.completed",
    "task.abandoned", "knowledge.created", "knowledge.revised", "knowledge.activated",
    "retrieval.completed", "knowledge.delivered", "knowledge.read", "knowledge.used",
    "feedback.recorded", "model.usage", "collection.gap", "knowledge.archived",
})

FEEDBACK_SOURCES = frozenset({"agent", "user", "independent", "unknown"})

_USE_FIELDS = {
    "required": {"memory_id", "content_revision", "context_event_id", "use_id",
                 "state", "task_id", "scope"},
    "optional": {"task_revision", "current_version"},
}
# 本构建实际启用的发射点；其余类型白名单在后续批次接入时定义。
PAYLOAD_FIELDS = {
    "knowledge.used": _USE_FIELDS,
    "knowledge.read": _USE_FIELDS,
    "knowledge.created": {
        "required": {"memory_id", "content_revision", "scope", "dtype"},
        "optional": {"staging", "title"},
    },
    "knowledge.revised": {
        "required": {"memory_id", "content_revision", "scope"},
        "optional": {"staging"},
    },
    "knowledge.archived": {
        "required": {"memory_id", "content_revision", "scope"},
        "optional": {"reason", "stale_since"},
    },
    "knowledge.activated": {
        "required": {"memory_id", "content_revision", "scope"},
        "optional": set(),
    },
    "task.completed": {
        "required": {"task_id", "task_outcome_id", "result", "verification_source"},
        "optional": {"task_run_id", "use_id", "memory_id"},
    },
    "task.abandoned": {
        "required": {"task_id", "task_outcome_id", "result", "verification_source"},
        "optional": {"task_run_id", "use_id", "memory_id"},
    },
    "feedback.recorded": {
        "required": {"memory_id", "content_revision", "outcome", "binding",
                     "receipt_id", "feedback_source"},
        "optional": {"use_id", "task_outcome_id", "task_id"},
    },
    # 字段分列不求和换算：缓存不计入输入重复计算；单价差异不在事件层换算。
    "model.usage": {
        "required": {"usage_source", "session_id"},
        "optional": {"model", "provider", "input_tokens", "output_tokens",
                     "cache_read_tokens", "cache_creation_tokens",
                     "semantic_version", "request_id", "task_mapping"},
    },
}

TASK_RESULTS = frozenset({"completed", "failed", "abandoned"})
VERIFICATION_SOURCES = frozenset({"agent_report", "user_confirmed", "independent"})

# 事件顶层公共字段白名单；导入侧据此拒绝任何额外顶层键（含正文/查询旁路）。
TOP_LEVEL_FIELDS = frozenset({
    "event_id", "schema_version", "event_type", "operation_id", "attempt_id",
    "native_call_id", "installation_id", "emitter_instance_id", "member_binding_id",
    "host", "host_version", "adapter_version", "capability_id", "task_id",
    "task_run_id", "task_revision", "session_id", "occurred_at", "payload",
})
_SCALARS = (str, int, float, bool, type(None))


def validate_event_shape(event: dict) -> None:
    """顶层键白名单 + 除 payload 外全标量；payload 值也必须为标量。
    构造与导入两侧共用，防嵌套对象夹带正文。"""
    unknown = set(event) - TOP_LEVEL_FIELDS
    if unknown:
        raise ValueError(f"event top-level field not allowed: {sorted(unknown)}")
    for key, value in event.items():
        if key == "payload":
            if not isinstance(value, dict):
                raise ValueError("payload must be an object")
            for pkey, pvalue in value.items():
                if not isinstance(pvalue, _SCALARS):
                    raise ValueError(f"payload value must be scalar: {pkey}")
        elif not isinstance(value, _SCALARS):
            raise ValueError(f"event field must be scalar: {key}")

_EMITTER_INSTANCE_ID = uuid.uuid4().hex
_MAX_ID = 512


def canonical_payload(event: dict) -> str:
    return json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def payload_hash(event: dict) -> str:
    import hashlib
    return hashlib.sha256(canonical_payload(event).encode("utf-8")).hexdigest()


def _adapter_version() -> str:
    try:
        from knowbase import __version__
        return __version__
    except Exception:
        return ""


def validate_payload(event_type: str, payload: dict) -> None:
    """类型白名单 + payload 字段白名单 + 数值校验；构造与导入两侧共用。"""
    if event_type not in PAYLOAD_FIELDS:
        if event_type not in EVENT_TYPES:
            raise ValueError(f"unknown event_type: {event_type}")
        raise ValueError(f"event_type not yet enabled in this build: {event_type}")
    spec = PAYLOAD_FIELDS[event_type]
    keys = set(payload or {})
    unknown = keys - spec["required"] - spec["optional"]
    if unknown:
        raise ValueError(f"payload field not in whitelist: {sorted(unknown)}")
    missing = spec["required"] - keys
    if missing:
        raise ValueError(f"payload missing required field: {sorted(missing)}")
    if event_type == "feedback.recorded" and payload["feedback_source"] not in FEEDBACK_SOURCES:
        raise ValueError(f"invalid feedback_source: {payload['feedback_source']}")
    for key, value in payload.items():
        if key.endswith("_tokens"):
            import math
            if (not isinstance(value, (int, float)) or isinstance(value, bool)
                    or not math.isfinite(value) or value < 0):
                raise ValueError(f"token field must be finite and nonnegative: {key}")


def build_event(event_type: str, payload: dict, *, host: str, session_id: str = "",
                operation_id: str = "", attempt_id: int = 1, native_call_id: str = "",
                task_id: str = "", task_run_id: str = "", task_revision: str = "",
                installation_id: str = "", member_binding_id: str = "",
                host_version: str = "", capability_id: str = "",
                occurred_at: str | None = None, event_id: str = "") -> dict:
    if event_type not in PAYLOAD_FIELDS:
        if event_type not in EVENT_TYPES:
            raise ValueError(f"unknown event_type: {event_type}")
        raise ValueError(f"event_type not yet enabled in this build: {event_type}")
    spec = PAYLOAD_FIELDS[event_type]
    validate_payload(event_type, payload)
    for name, value in (("host", host), ("session_id", session_id),
                        ("operation_id", operation_id), ("native_call_id", native_call_id),
                        ("task_id", task_id), ("task_run_id", task_run_id),
                        ("task_revision", task_revision), ("installation_id", installation_id),
                        ("member_binding_id", member_binding_id), ("host_version", host_version),
                        ("capability_id", capability_id)):
        if not isinstance(value, str) or len(value) > _MAX_ID:
            raise ValueError(f"common field invalid: {name}")
    for key in spec["required"]:
        value = payload[key]
        if isinstance(value, str) and (not value or len(value) > _MAX_ID):
            raise ValueError(f"payload field invalid: {key}")

    event = {
        "event_id": event_id or ("VE-" + uuid.uuid4().hex),
        "schema_version": SCHEMA_VERSION,
        "event_type": event_type,
        "operation_id": operation_id,
        "attempt_id": attempt_id,
        "native_call_id": native_call_id,
        "installation_id": installation_id,
        "emitter_instance_id": _EMITTER_INSTANCE_ID,
        "host": host,
        "host_version": host_version,
        "adapter_version": _adapter_version(),
        "capability_id": capability_id,
        "task_id": task_id,
        "task_run_id": task_run_id,
        "task_revision": task_revision,
        "session_id": session_id,
        "occurred_at": occurred_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "payload": dict(payload),
    }
    if member_binding_id:
        event["member_binding_id"] = member_binding_id
    return event


def record(conn, event_type: str, payload: dict, *, source_key: str = "",
           host: str = "", session_id: str = "", operation_id: str = "",
           task_id: str = "", task_revision: str = "", task_run_id: str = "",
           occurred_at: str | None = None) -> dict | None:
    """业务事务内发射一条价值事件；未启用返回 None，不影响业务路径。

    身份统一取本机 value_identity（成员未确认则省略 member_binding_id）；
    host 缺省用 agent 标签，仅作归因上下文，不当成员。"""
    from .. import config
    if not config.load_config().get("value", {}).get("events_enabled"):
        return None
    from . import identity as _identity, outbox as _outbox
    ident = _identity.get(conn)
    installation = ident["installation_id"] or _identity.install(conn)
    event = build_event(event_type, payload, host=host or config.agent_name(),
                        session_id=session_id, operation_id=operation_id,
                        task_id=task_id, task_revision=task_revision,
                        task_run_id=task_run_id, installation_id=installation,
                        member_binding_id=ident["member_binding_id"],
                        occurred_at=occurred_at)
    return _outbox.append(conn, event, source_key=source_key)
