"""本机安装身份与成员归属确认（文件汇总模式，无中心认证）。

installation_id 首次使用时生成并持久化在 memory.db（不进知识 Git）；
成员绑定须显式确认并保留证据等级与时间。未确认时 member_binding_id 为空，
事件层据此省略该字段——工具标签、宿主标识、Git 作者名都不冒充成员。
"""

import uuid
from datetime import datetime, timezone

from .. import materials

# certified 仅在可选 S1 认证绑定后可用；本版拒绝设置，避免伪造中心认证强度。
EVIDENCE_LEVELS = frozenset({"manual_confirmed", "device_confirmed"})

_KEY_INSTALLATION = "installation_id"
_KEY_MEMBER_BINDING = "member_binding_id"
_KEY_MEMBER_LABEL = "member_label"
_KEY_MEMBER_EVIDENCE = "member_evidence"
_KEY_MEMBER_CONFIRMED_AT = "member_confirmed_at"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _get(conn, key: str) -> str:
    row = conn.execute("SELECT value FROM value_identity WHERE key=?", (key,)).fetchone()
    return row[0] if row else ""


def _put(conn, key: str, value: str) -> None:
    conn.execute("INSERT INTO value_identity(key,value,updated_at) VALUES(?,?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                 (key, value, _now()))


def install(conn) -> str:
    """读取或生成本机 installation_id；须在调用方事务内执行。"""
    existing = _get(conn, _KEY_INSTALLATION)
    if existing:
        return existing
    value = "INST-" + uuid.uuid4().hex[:24]
    conn.execute("INSERT OR IGNORE INTO value_identity(key,value,updated_at) VALUES(?,?,?)",
                 (_KEY_INSTALLATION, value, _now()))
    return _get(conn, _KEY_INSTALLATION) or value


def set_member(conn, label: str, *, evidence: str = "manual_confirmed") -> dict:
    """登记本设备归属的成员（人工/设备确认）。清空传空 label。"""
    if evidence not in EVIDENCE_LEVELS:
        raise ValueError(f"unsupported evidence: {evidence}")
    if not isinstance(label, str) or len(label) > 128:
        raise ValueError("member label must be a string up to 128 chars")
    if not label:
        for key in (_KEY_MEMBER_BINDING, _KEY_MEMBER_LABEL, _KEY_MEMBER_EVIDENCE,
                    _KEY_MEMBER_CONFIRMED_AT):
            _put(conn, key, "")
        return get(conn)
    installation_id = install(conn)
    # 绑定按成员标签稳定：同一人在多台安装确认同一标签 → 同一 member_binding_id，
    # 不会把自己的两台机器算成两个人/跨成员。受信任团队文件模式下同名不同人
    # 会被误并，汇总端 confirm_member 人工归属仍是权威纠正入口。
    binding = "MBR-" + materials.digest(["member", label])[:24]
    _put(conn, _KEY_MEMBER_BINDING, binding)
    _put(conn, _KEY_MEMBER_LABEL, label)
    _put(conn, _KEY_MEMBER_EVIDENCE, evidence)
    _put(conn, _KEY_MEMBER_CONFIRMED_AT, _now())
    return get(conn)


def get(conn) -> dict:
    """当前身份快照；未确认成员时 member_binding_id/member_label 为空串。"""
    return {
        "installation_id": _get(conn, _KEY_INSTALLATION),
        "member_binding_id": _get(conn, _KEY_MEMBER_BINDING),
        "member_label": _get(conn, _KEY_MEMBER_LABEL),
        "member_evidence": _get(conn, _KEY_MEMBER_EVIDENCE),
        "member_confirmed_at": _get(conn, _KEY_MEMBER_CONFIRMED_AT),
    }
