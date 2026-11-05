"""V2 生命周期契约：内容版本算法、事件信封、任务身份与变更 manifest。

本模块只放冻结定义与纯函数（无 I/O、无配置读取）：
- REVISION_ALGO_V1/V2：content-v1 为现存 16 位短 hash（保留兼容，不重算
  历史版本），content-v2 为完整 SHA256 且纳入 applicability/claims。
- 事件信封 knowbase.lifecycle.events.v1：规范化编码、稳定事件 ID、
  payload_hash、信封哈希与签名覆盖范围（Ed25519 落地见 trust.py）。
- 任务身份：project_key / logical_task_key / origin_run_key /
  independence_group——身份未知显式保留 unknown，不猜。
- 变更候选 manifest：八关系、base_heads/outputs/claim_map 校验。
"""
import hashlib
import json
import re

# ---------- 内容版本算法 ----------

REVISION_ALGO_V1 = "content-v1"
REVISION_ALGO_V2 = "content-v2"

EVENTS_SCHEMA = "knowbase.lifecycle.events.v1"
CHANGE_POLICY_REVISION = "lifecycle-v2.1"

# 八关系全集（V2 设计 §5.1）。一个 change 可含多个关系操作，但 relation
# 字段本身是主关系。
RELATIONS = ("NEW", "ENRICH", "CORRECT", "REPLACE", "MERGE", "SPLIT",
             "CONFLICT", "NO_CHANGE")
SOURCE_DISPOSITIONS = ("revise", "retain", "supersede", "archive", "redirect")
RISKS = ("R0", "R1", "R2", "R3")
CLAIM_MAPPINGS = ("preserved", "modified", "added", "removed", "moved")

ID_RE = re.compile(r"^[A-Z]{1,2}-\d{4}-\d{4}$|^[A-Z]{1,2}-[0-9a-f]{12}$")


def canonical_json(value) -> str:
    """事件/manifest 规范编码：UTF-8、键字典序、无多余空格、拒绝 NaN/Inf。"""
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                     allow_nan=False)
    raw.encode("utf-8")  # 触发校验；ensure_ascii=False 时亦然
    return raw


def _content_fields(meta: dict, body: str) -> dict:
    """v1 与 v2 共用的内容字段（与 mutations._compute_revision 保持一致口径）。"""
    return {
        "id": meta.get("id", ""),
        "type": meta.get("type", ""),
        "title": meta.get("title", ""),
        "scope": meta.get("scope", ""),
        "tags": sorted(meta.get("tags", [])),
        "relations": sorted(meta.get("relations", []), key=lambda r: r.get("id", "")),
        "code_refs": sorted(meta.get("code_refs", []), key=lambda r: (r.get("repo", ""), r.get("path", ""))),
        "body": body.strip(),
        "source_refs": meta.get("source_refs", []),
        "provenance": meta.get("provenance", ""),
        "rule_context": meta.get("rule_context", {}),
    }


def content_v1_hash(meta: dict, body: str) -> str:
    """与 mutations._compute_revision 完全一致（直接复用，防两处口径漂移）。"""
    from ..mutations import _compute_revision
    return _compute_revision(meta, body)


def content_v2_hash(meta: dict, body: str) -> str:
    """content-v2：完整 SHA256，纳入 applicability/claims/重要性适用字段。

    历史卡缺这些字段时以 unknown/空结构参与——不静默剔除（否则同一张卡
    补字段前后 hash 不变，字段就失去了版本语义）。
    """
    fields = _content_fields(meta, body)
    fields["revision_algo"] = REVISION_ALGO_V2
    fields["applicability"] = meta.get("applicability") or {}
    fields["claims"] = meta.get("claims") or []
    if meta.get("importance"):
        fields["importance"] = meta.get("importance")
    raw = canonical_json(fields)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def revision_identity(meta: dict, body: str) -> dict:
    """卡片当前内容版本身份：按卡片自带的 revision_algo 派发。

    未标记的存量卡返回 v1 身份（迁移不静默重算旧版本，历史反馈不失联）；
    显式标记 content-v2 的卡返回 v2 身份。
    """
    if meta.get("revision_algo") == REVISION_ALGO_V2:
        return {"revision_algo": REVISION_ALGO_V2,
                "content_revision": content_v2_hash(meta, body)}
    return {"revision_algo": REVISION_ALGO_V1,
            "content_revision": content_v1_hash(meta, body)}


def snapshot_hash(data: bytes) -> str:
    """不可变快照文件字节全文 SHA256（审计原件，区别于语义 hash）。"""
    return hashlib.sha256(data).hexdigest()


# ---------- 任务身份 ----------

def project_key_from_repo(repo_id: str) -> str:
    """稳定项目身份：repo:<canonical-id>。同名不同远端不得折叠（scope_binding
    的三级防同名由调用方完成，这里只做格式归一）。"""
    rid = str(repo_id or "").strip()
    if not rid:
        return "repo:unknown"
    return "repo:" + rid


def logical_task_key(project_key: str, native_task_key: str) -> str:
    """逻辑任务：用户想完成的同一工作，跨宿主/机器不变。

    native_task_key 为空（宿主无原生任务键）时返回 unknown 前缀键并显式
    标 identity_basis 由调用方维护——unknown 不参与独立性计数。"""
    if not str(native_task_key or "").strip():
        return f"unknown:{project_key or 'unknown'}"
    return f"task:{project_key}:{native_task_key}"


def origin_run_key(project_key: str, native_run_key: str) -> str:
    """一次实际执行及其观察副本身份；重放/复制共享同 key，不增加独立任务数。"""
    if not str(native_run_key or "").strip():
        return f"run-unknown:{project_key or 'unknown'}"
    return f"run:{project_key}:{native_run_key}"


def independence_group(project_key: str, *keys: str) -> str:
    """独立证据组：共享输入/原始运行/复制产物的证据合组。"""
    return "grp:" + hashlib.sha256(
        canonical_json({"project": project_key, "keys": [str(k) for k in keys]})
        .encode("utf-8")).hexdigest()[:24]


# ---------- 事件信封（knowbase.lifecycle.events.v1） ----------

EVENT_TYPES = (
    "knowledge.exposed", "knowledge.referenced", "knowledge.adoption_candidate",
    "knowledge.adopted", "validation.completed", "evidence.revoked",
    "evidence.superseded", "knowledge.change_proposed", "knowledge.change_published",
    "governance.decided", "environment.observed",
)

ENVELOPE_FIELDS = ("schema", "event_id", "event_type", "team_id",
                   "issuer_installation_id", "issuer_binding_id", "project_key",
                   "scope", "occurred_at", "payload_hash", "policy_revision",
                   "adapter_version", "source_event_id", "payload")


def stable_event_id(issuer_installation_id: str, native_event_key: str,
                    event_type: str, subject: dict, operation_key: str) -> str:
    """事件 ID 由服务按 issuer+native key+type+subject+operation 稳定派生：
    语义相同重试保持同 ID。"""
    basis = canonical_json({
        "issuer": issuer_installation_id, "native": native_event_key,
        "type": event_type,
        "subject": {k: subject.get(k) for k in
                    ("memory_id", "revision_algo", "content_revision", "claim_id")},
        "operation": operation_key,
    })
    return "EV-" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


def envelope_hash(envelope: dict) -> str:
    """对不含 payload_hash/signature 的完整信封（含 issuer/team/scope/subject
    与 payload 本体）计算 SHA256——不能只 hash 业务 payload。key_id 若存在
    纳入被 hash 的信封（先去掉 signature 再算）。"""
    core = {k: v for k, v in envelope.items()
            if k not in ("payload_hash", "signature", "signature_algo")}
    return hashlib.sha256(canonical_json(core).encode("utf-8")).hexdigest()


def build_envelope(*, event_type, team_id, issuer_installation_id, project_key,
                   scope, occurred_at, payload, issuer_binding_id="",
                   policy_revision=CHANGE_POLICY_REVISION, adapter_version="",
                   source_event_id="", native_event_key="", operation_key="",
                   subject=None):
    """构造规范事件信封。payload 先行规范化；payload_hash 为 payload 规范
    JSON 的 SHA256。重试须保留原 occurred_at 与 event_id（同 ID 改 payload
    是 conflict，由消费端拒绝）。"""
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unknown event type: {event_type}")
    payload = json.loads(canonical_json(payload))
    subject = dict(subject or {})
    event_id = stable_event_id(issuer_installation_id, native_event_key, event_type,
                               subject, operation_key)
    envelope = {
        "schema": EVENTS_SCHEMA, "event_id": event_id, "event_type": event_type,
        "team_id": team_id, "issuer_installation_id": issuer_installation_id,
        "issuer_binding_id": issuer_binding_id or None, "project_key": project_key,
        "scope": scope, "occurred_at": occurred_at,
        "payload_hash": hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest(),
        "policy_revision": policy_revision, "adapter_version": adapter_version,
        "source_event_id": source_event_id or None, "payload": payload,
    }
    envelope.update({k: v for k, v in subject.items() if k in
                     ("memory_id", "revision_algo", "content_revision", "claim_id")})
    return envelope


def validate_envelope(envelope: dict) -> list[str]:
    """信封结构校验（不含签名验证）：返回错误清单，空为通过。"""
    problems = []
    if envelope.get("schema") != EVENTS_SCHEMA:
        problems.append("schema_mismatch")
    if envelope.get("event_type") not in EVENT_TYPES:
        problems.append("unknown_event_type")
    for field in ("event_id", "team_id", "issuer_installation_id", "project_key",
                  "scope", "occurred_at", "payload"):
        if not envelope.get(field):
            problems.append(f"missing:{field}")
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        problems.append("payload_not_object")
        payload = {}
    expect = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    if envelope.get("payload_hash") != expect:
        problems.append("payload_hash_mismatch")
    return problems


# ---------- 变更候选 manifest（V2 设计 §5.1） ----------

def validate_change_manifest(manifest: dict) -> list[str]:
    """变更 manifest 最低字段契约校验；返回错误清单，空为通过。

    base_heads 必须覆盖全部会被修改/退役/重定向的输入；outputs 覆盖全部
    发布输出；NO_CHANGE 无内容输出、只带处置。不校验语义正确性（那是
    changes.py + 独立验证的职责）。
    """
    problems = []
    if manifest.get("relation") not in RELATIONS:
        problems.append(f"relation_invalid:{manifest.get('relation')}")
    if manifest.get("risk") not in RISKS:
        problems.append(f"risk_invalid:{manifest.get('risk')}")
    for field in ("change_id", "scope", "policy_revision"):
        if not str(manifest.get(field) or "").strip():
            problems.append(f"missing:{field}")
    base = manifest.get("base_heads")
    if not isinstance(base, list):
        problems.append("base_heads_not_list")
    else:
        for i, head in enumerate(base):
            if not isinstance(head, dict) or not head.get("memory_id") \
                    or not head.get("content_revision"):
                problems.append(f"base_head_invalid:{i}")
    outputs = manifest.get("outputs")
    if not isinstance(outputs, list):
        problems.append("outputs_not_list")
    else:
        for i, out in enumerate(outputs):
            if not isinstance(out, dict) or not out.get("memory_id") \
                    or not out.get("snapshot_hash"):
                problems.append(f"output_invalid:{i}")
            elif out.get("disposition") not in ("active", "alias", "retired", "redirect"):
                problems.append(f"output_disposition_invalid:{i}")
    relation = manifest.get("relation")
    if relation == "NO_CHANGE" and outputs:
        problems.append("no_change_with_outputs")
    if relation == "NEW" and base:
        problems.append("new_with_base_heads")
    dispositions = manifest.get("source_dispositions")
    if not isinstance(dispositions, list):
        problems.append("source_dispositions_not_list")
    else:
        for i, item in enumerate(dispositions):
            if not isinstance(item, dict) or not item.get("memory_id") \
                    or item.get("action") not in SOURCE_DISPOSITIONS:
                problems.append(f"source_disposition_invalid:{i}")
            elif item.get("action") == "redirect" and not item.get("redirect_ids"):
                problems.append(f"redirect_without_targets:{i}")
    claim_map = manifest.get("claim_map")
    if not isinstance(claim_map, list):
        problems.append("claim_map_not_list")
    else:
        for i, item in enumerate(claim_map):
            new_ids = item.get("new_claim_ids") if isinstance(item, dict) else None
            if not isinstance(item, dict) or not item.get("old_claim_id") \
                    or not isinstance(new_ids, list) \
                    or item.get("mapping") not in CLAIM_MAPPINGS:
                problems.append(f"claim_map_invalid:{i}")
            elif item.get("mapping") != "removed" and not new_ids:
                problems.append(f"claim_map_invalid:{i}")  # 非 removed 必须有新 claim
    return problems


def change_manifest_hash(manifest: dict) -> str:
    return hashlib.sha256(canonical_json(manifest).encode("utf-8")).hexdigest()


# ---------- 适用范围（applicability，V2 设计 §9.3） ----------

VERSION_SPEC_RE = re.compile(r"^\s*(?:[<>=!^~]=?|=)?\s*\d+(?:\.\d+){0,3}"
                             r"(?:\s*,\s*(?:[<>=!^~]=?|=)?\s*\d+(?:\.\d+){0,3})*\s*$")


def validate_applicability(applicability: dict) -> list[str]:
    """结构化适用范围校验：components 版本约束可解析；无法解析的自定义版本
    应移入 unknown_fields（只允许 exact 语义），不靠字符串大小推断。"""
    problems = []
    if not isinstance(applicability, dict):
        return ["applicability_not_object"]
    components = applicability.get("components")
    if components is not None:
        if not isinstance(components, list):
            problems.append("components_not_list")
        else:
            for i, comp in enumerate(components):
                if not isinstance(comp, dict) or not str(comp.get("name", "")).strip():
                    problems.append(f"component_invalid:{i}")
                elif comp.get("version_spec") and not VERSION_SPEC_RE.match(str(comp["version_spec"])):
                    problems.append(f"component_version_spec_unparseable:{i}")
    for field in ("platforms", "conditions", "unknown_fields"):
        value = applicability.get(field)
        if value is not None and not isinstance(value, list):
            problems.append(f"{field}_not_list")
    return problems
