"""V2 领域 dataclass（P1-A + P3-A）。

设计原则：
- frozen=True 的实体视为「不可变快照」；状态字段变更走 Operation 审计流
- id 使用 uuid4 hex（Source 除外，使用稳定 path 哈希以便增量定位）
- 时间字段统一 ISO-8601 str，避免 tz 漂移；序数排序依赖 created_at 字典序
- 与 V1 store.Meta 解耦，桥接由 ingestion.adapters 提供
- ACL 模型（P3-A §7）：
  - Organization / Project / Principal / Membership / ProjectMembership
  - Document + org_id / project_id / owner_id / visibility
  - visibility 4 类：public-template / organization-global / project-shared / personal
  - 角色 5 类：viewer / contributor / reviewer / project_admin / platform_admin
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_id() -> str:
    return uuid.uuid4().hex


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------- 枚举 ----------

class SourceKind(str, Enum):
    FILE = "file"                # 本地 / 仓库内文件
    REMOTE = "remote"            # 远程 HTTP / S3
    MCP = "mcp"                  # MCP server 输出
    HUMAN = "human"              # 人手输入


class DocumentStatus(str, Enum):
    DISCOVERED = "discovered"
    FETCHED = "fetched"
    STORED = "stored"
    PARSING = "parsing"
    PARSED = "parsed"
    CHUNKED = "chunked"
    EMBEDDING = "embedding"
    INDEXING = "indexing"
    READY = "ready"
    ERROR = "error"
    TOMBSTONED = "tombstoned"   # 软删除


class OperationKind(str, Enum):
    APPLY = "apply"              # 写一条记忆
    INGEST = "ingest"            # 摄取一份新版本
    PROMOTE = "promote"          # staging → active
    ARCHIVE = "archive"          # active → archived
    VERIFY = "verify"            # 人工 verify
    TOMBSTONE = "tombstone"      # 软删除
    RETRY = "retry"


class OperationStatus(str, Enum):
    """P4-A §4.1 + §8.3：写动作状态机 + 幂等 + lease。

    - PENDING：已记录但未开始（待 worker claim）
    - RUNNING：被 worker claim + lease 持有
    - SUCCEEDED：完成（终端态；按 operation_id 查重）
    - FAILED：失败（终端态；不再 claim）
    - UNKNOWN：worker 崩溃后无法确定结果（按 operation_id 查询人工裁决）
    """
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class OutboxStatus(str, Enum):
    """P4-A §4.1：outbox 事件投递状态。

    - PENDING：待派发
    - DISPATCHED：已派发且 worker ack（终端）
    - FAILED：达到最大重试次数进入死信（终端；不阻塞其它事件）
    """
    PENDING = "pending"
    DISPATCHED = "dispatched"
    FAILED = "failed"


class SyncRunStatus(str, Enum):
    """P4-A §8.3：同步运行状态机。

    - RUNNING：进行中（持有 lease）
    - SUCCEEDED：完成（终端）
    - FAILED：失败但非冲突（终端）
    - CONFLICTED：内容冲突保留双方版本（终端，禁止自动覆盖）
    """
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CONFLICTED = "conflicted"


class KnowledgeKind(str, Enum):
    """V1 store.TYPES 的语义镜像；用于跨 V1/V2 桥接。"""
    PITFALL = "pitfall"
    DECISION = "decision"
    WORKFLOW = "workflow"
    STANDARD = "standard"
    PREFERENCE = "preference"
    BIZRULE = "bizrule"
    REFERENCE = "reference"


class Role(str, Enum):
    """P3-A 5 类角色（V2 计划 §7.1）。"""
    VIEWER = "viewer"                    # 读已激活
    CONTRIBUTOR = "contributor"          # 写记忆、看自己操作
    REVIEWER = "reviewer"                # 审批 standard/bizrule
    PROJECT_ADMIN = "project_admin"      # 管项目成员 / 来源 / 策略
    PLATFORM_ADMIN = "platform_admin"    # 组织级运维（默认无敏感正文）


VALID_ROLES = frozenset(r.value for r in Role)


class Visibility(str, Enum):
    """P3-A 4 类 visibility（V2 计划 §7.3，global 拆解）。"""
    PUBLIC_TEMPLATE = "public-template"        # 所有人（含未登录）
    ORG_GLOBAL = "organization-global"        # 组织内任意成员
    PROJECT_SHARED = "project-shared"         # 指定项目集合共享
    PERSONAL = "personal"                     # 仅创建者


VALID_VISIBILITY = frozenset(v.value for v in Visibility)


class PrincipalKind(str, Enum):
    """Principal 类型（V2 计划 §7.1 末尾：Agent 身份必须绑定 principal）。"""
    USER = "user"
    SERVICE = "service"
    AGENT = "agent"


# ---------- 实体 ----------

@dataclass(frozen=True)
class Source:
    """数据来源；stable_id 由 path/url 派生，便于跨进程定位。"""
    stable_id: str              # sha256(canonical_path)[:16]
    kind: SourceKind
    locator: str                # 原始 path / url
    config: dict = field(default_factory=dict)
    created_at: str = field(default_factory=_now_iso)

    @staticmethod
    def from_locator(kind: SourceKind, locator: str, config: dict | None = None) -> "Source":
        canonical = f"{kind.value}::{locator.strip().lower()}"
        return Source(
            stable_id=hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16],
            kind=kind,
            locator=locator,
            config=config or {},
        )


@dataclass(frozen=True)
class Document:
    """一份文档（一个文件 / 一个远程对象）。同一 Source 下按 path 区分。

    P3-A 增列：
    - org_id / project_id：所属 ACL 域
    - owner_id：personal visibility 的拥有者
    - visibility：四类可见性
    """
    id: str                     # uuid4 hex
    source_id: str              # Source.stable_id
    path: str                   # Source 内相对路径
    kind: KnowledgeKind | None  # 若是 knowbase 记忆，标注；普通文档为空
    title: str = ""
    content_hash: str = ""      # 最新活跃版本的内容哈希
    status: DocumentStatus = DocumentStatus.DISCOVERED
    meta: dict = field(default_factory=dict)
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    # --- P3-A ACL ---
    org_id: str | None = None
    project_id: str | None = None
    owner_id: str | None = None
    visibility: str = Visibility.ORG_GLOBAL.value

    @staticmethod
    def new(source_id: str, path: str, *, kind: KnowledgeKind | None = None,
            title: str = "", org_id: str | None = None,
            project_id: str | None = None, owner_id: str | None = None,
            visibility: str | None = None) -> "Document":
        return Document(
            id=_new_id(),
            source_id=source_id,
            path=path,
            kind=kind,
            title=title,
            org_id=org_id,
            project_id=project_id,
            owner_id=owner_id,
            visibility=visibility or Visibility.ORG_GLOBAL.value,
        )


@dataclass(frozen=True)
class DocumentVersion:
    """一份文档的不可变版本。相同 content_hash + doc_id 即视为重复摄取。"""
    id: str                     # uuid4 hex
    doc_id: str
    version_no: int             # 单调递增
    content_hash: str           # sha256
    body: str                   # 原始正文（V1 走 markdown，其他格式走 plain text）
    meta: dict = field(default_factory=dict)
    created_at: str = field(default_factory=_now_iso)
    superseded_by: str | None = None  # id of newer version; None 表示当前活跃

    @staticmethod
    def from_content(doc_id: str, version_no: int, body: str,
                     meta: dict | None = None) -> "DocumentVersion":
        return DocumentVersion(
            id=_new_id(),
            doc_id=doc_id,
            version_no=version_no,
            content_hash=_sha256_text(body),
            body=body,
            meta=meta or {},
        )


@dataclass(frozen=True)
class Chunk:
    """切分单元。text 即检索/索引的最小可见内容。"""
    id: str                     # uuid4 hex
    doc_id: str
    version_id: str
    ordinal: int                # 0..N-1
    text: str
    start_offset: int = 0
    end_offset: int = 0
    meta: dict = field(default_factory=dict)
    created_at: str = field(default_factory=_now_iso)

    @staticmethod
    def new(doc_id: str, version_id: str, ordinal: int, text: str,
            start: int = 0, end: int = 0) -> "Chunk":
        return Chunk(
            id=_new_id(),
            doc_id=doc_id,
            version_id=version_id,
            ordinal=ordinal,
            text=text,
            start_offset=start,
            end_offset=end,
        )


@dataclass(frozen=True)
class Operation:
    """写动作审计；与 V1 lifecycle.events 对齐，但结构化。

    P4-A 扩展：
    - status：状态机（PENDING/RUNNING/SUCCEEDED/FAILED/UNKNOWN）
    - request_hash：相同 request_hash + by_actor + target_id 视为幂等同操作
    - result / error：执行结果或失败原因
    - attempts / lease_owner / lease_until：worker 并发控制
    - updated_at / completed_at：审计时间戳
    """
    id: str
    kind: OperationKind
    target_id: str              # 关联 doc_id / chunk_id / version_id
    by: str                     # agent:<name>:<ctx> | human:<name>
    payload: dict = field(default_factory=dict)
    created_at: str = field(default_factory=_now_iso)
    # --- P4-A ---
    status: OperationStatus = OperationStatus.SUCCEEDED  # 旧 record 默认 succeeded 兼容
    request_hash: str | None = None
    result: dict = field(default_factory=dict)
    error: str | None = None
    attempts: int = 0
    lease_owner: str | None = None
    lease_until: str | None = None
    updated_at: str | None = None
    completed_at: str | None = None

    @staticmethod
    def new(kind: OperationKind, target_id: str, by: str,
            payload: dict | None = None,
            request_hash: str | None = None) -> "Operation":
        return Operation(
            id=_new_id(),
            kind=kind,
            target_id=target_id,
            by=by,
            payload=payload or {},
            status=OperationStatus.PENDING,
            request_hash=request_hash,
        )


@dataclass(frozen=True)
class OutboxEvent:
    """Phase 4 启用：写后未同步事件。

    P4-A 扩展：
    - aggregate_type / aggregate_id：事件归属（document / knowledge_record / ...）
    - event_type：领域事件类型（created / updated / version_activated / ...）
    - status：PENDING/DISPATCHED/FAILED
    - next_attempt_at / attempts / lease_owner / lease_until：指数退避 + worker lease
    - last_error / last_delivered_at：失败与投递审计
    """
    id: str
    topic: str                  # 兼容旧字段；新代码优先用 event_type
    payload: dict
    created_at: str = field(default_factory=_now_iso)
    delivered_at: str | None = None
    attempts: int = 0
    # --- P4-A ---
    aggregate_type: str | None = None
    aggregate_id: str | None = None
    event_type: str | None = None
    status: OutboxStatus = OutboxStatus.PENDING
    next_attempt_at: str | None = None
    lease_owner: str | None = None
    lease_until: str | None = None
    last_error: str | None = None
    last_delivered_at: str | None = None

    @staticmethod
    def new(topic: str, payload: dict, *,
            aggregate_type: str | None = None,
            aggregate_id: str | None = None,
            event_type: str | None = None) -> "OutboxEvent":
        return OutboxEvent(
            id=_new_id(),
            topic=topic,
            payload=payload,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            event_type=event_type or topic,
            status=OutboxStatus.PENDING,
        )


@dataclass(frozen=True)
class SyncRun:
    """Phase 4 启用：一次同步任务。

    P4-A 扩展：
    - cursor_before / cursor_after：增量同步边界（commit SHA / ETag / mtime+size）
    - status：RUNNING/SUCCEEDED/FAILED/CONFLICTED
    - discovered / created_n / updated_n / deleted_n / failed_n：同步计数
    - lease_owner / lease_until：worker 抢占（防多实例重复）
    """
    id: str
    source_id: str
    started_at: str = field(default_factory=_now_iso)
    finished_at: str | None = None
    stats: dict = field(default_factory=dict)
    error: str | None = None
    # --- P4-A ---
    cursor_before: str | None = None
    cursor_after: str | None = None
    status: SyncRunStatus = SyncRunStatus.RUNNING
    discovered: int = 0
    created_n: int = 0
    updated_n: int = 0
    deleted_n: int = 0
    failed_n: int = 0
    lease_owner: str | None = None
    lease_until: str | None = None

    @staticmethod
    def new(source_id: str, *,
            cursor_before: str | None = None) -> "SyncRun":
        return SyncRun(
            id=_new_id(),
            source_id=source_id,
            cursor_before=cursor_before,
            status=SyncRunStatus.RUNNING,
        )


# ---------- P4-D：Lite Profile source_sync_state ----------

@dataclass(frozen=True)
class SourceSyncState:
    """Lite Profile 一个 source 的同步状态快照。

    P4-D 设计（V2 计划 §8.2 + §8.3）：
    - last_remote_check_at：最近一次 fetch 成功时间
    - last_remote_revision：最近一次 fetch 到的远端 commit SHA
    - local_revision：本地 HEAD commit SHA
    - last_pull_at：最近一次成功 pull/rebase 时间
    - last_push_at：最近一次成功 push 时间
    - last_failed_at：最近一次失败时间（任何步骤失败都更新）
    - last_error：最近一次失败原因
    - pending_ops：未推送 outbox 事件数（P4-B 接入后填）
    - conflicted_docs：内容冲突仍未解决的文档数
    - last_sync_run_id：最近一次 sync_run 引用（运维回溯）

    语义：
    - 全部时间字段 ISO-8601 str（与 V2 时间约定一致）
    - 字段变更走 dataclasses.replace；不要直接赋值
    - 该表不会随 sync_run 终结被覆盖；只更新单字段
    """
    source_id: str
    last_remote_check_at: str | None = None
    last_remote_revision: str | None = None
    local_revision: str | None = None
    last_pull_at: str | None = None
    last_push_at: str | None = None
    last_failed_at: str | None = None
    last_error: str | None = None
    pending_ops: int = 0
    conflicted_docs: int = 0
    last_sync_run_id: str | None = None
    updated_at: str = field(default_factory=_now_iso)


@dataclass(frozen=True)
class SyncStatusReport:
    """sync_status API 返回：单 source 同步状态摘要。

    聚合字段（不一定直接来自 SourceSyncState）：
    - source_id / remote_url / branch：身份
    - local_revision / remote_revision：当前双方 commit
    - behind_count / ahead_count：本地落后 / 领先远端的提交数
    - diverged：双方各有对方没有的提交
    - last_remote_check_at / last_pull_at / last_push_at：最近操作时间
    - last_failed_at / last_error：失败信息
    - pending_ops / conflicted_docs：积压 + 冲突
    - is_stale：超过 staleness_sla_seconds 未检查
    - last_sync_run_id / last_sync_run_status：最近一次 run 引用
    """
    source_id: str
    local_revision: str | None
    remote_revision: str | None
    behind_count: int
    ahead_count: int
    diverged: bool
    last_remote_check_at: str | None
    last_pull_at: str | None
    last_push_at: str | None
    last_failed_at: str | None
    last_error: str | None
    pending_ops: int
    conflicted_docs: int
    is_stale: bool
    last_sync_run_id: str | None
    last_sync_run_status: str | None


# ---------- ACL 实体（P3-A §7）----------

@dataclass(frozen=True)
class Organization:
    """组织（最高一级 ACL 域）。slug 唯一可作 URL 友好标识。"""
    id: str
    name: str
    slug: str
    created_at: str = field(default_factory=_now_iso)

    @staticmethod
    def new(name: str, slug: str | None = None) -> "Organization":
        s = (slug or name.lower().replace(" ", "-"))[:64] or "org"
        return Organization(id=_new_id(), name=name, slug=s)


@dataclass(frozen=True)
class Project:
    """项目（属于组织；project-shared visibility 用其做 ACL 域）。"""
    id: str
    org_id: str
    name: str
    slug: str
    created_at: str = field(default_factory=_now_iso)

    @staticmethod
    def new(org_id: str, name: str, slug: str | None = None) -> "Project":
        s = (slug or name.lower().replace(" ", "-"))[:64] or "project"
        return Project(id=_new_id(), org_id=org_id, name=name, slug=s)


@dataclass(frozen=True)
class Principal:
    """身份（user / service / agent）。Phase 6 之前默认构造 local_user。"""
    id: str
    display_name: str
    kind: PrincipalKind = PrincipalKind.USER
    created_at: str = field(default_factory=_now_iso)

    @staticmethod
    def new(display_name: str, kind: PrincipalKind = PrincipalKind.USER) -> Principal:
        return Principal(id=_new_id(), display_name=display_name, kind=kind)

    @staticmethod
    def local_user() -> Principal:
        """本机单用户身份（Phase 6 之前的默认；Phase 3 起所有 V2 检索都走它）。"""
        return Principal(id="local-user", display_name="local user", kind=PrincipalKind.USER)


@dataclass(frozen=True)
class Membership:
    """组织成员 + 角色。"""
    id: int
    principal_id: str
    org_id: str
    role: str                    # Role.value

    @staticmethod
    def new(principal_id: str, org_id: str, role: str) -> Membership:
        if role not in VALID_ROLES:
            raise ValueError(f"invalid role: {role}")
        return Membership(id=0, principal_id=principal_id, org_id=org_id, role=role)


@dataclass(frozen=True)
class ProjectMembership:
    """项目成员 + 角色。"""
    id: int
    principal_id: str
    project_id: str
    role: str                    # Role.value（不含 platform_admin）

    @staticmethod
    def new(principal_id: str, project_id: str, role: str) -> ProjectMembership:
        if role not in VALID_ROLES:
            raise ValueError(f"invalid role: {role}")
        return ProjectMembership(id=0, principal_id=principal_id, project_id=project_id, role=role)
