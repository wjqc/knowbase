"""V2 SQLite DDL 与迁移（P1-A + P3-A + P4-A）。

数据库文件：<repo>/.knowbase/v2.db（与 V1 index.db 分离）。
schema_version 通过 PRAGMA user_version 管理；每次 apply_migrations 幂等升级。

设计：
- 8 张核心表（v1）：schema_migrations / knowledge_source / document / document_version
  / document_chunk / operation / outbox_event / sync_run
- 5 张 ACL 表（v2）：organization / project / principal / membership / project_membership
- 所有表都带 created_at / updated_at
- 索引：按 content_hash、source_id+path、doc_id+version_no、status 覆盖典型查询
- ACL 字段（org_id / project_id / owner_id / visibility）作为预留列；Phase 3 启用

Schema 版本：
- v1：P1-A 初始 8 表
- v2：P3-A ACL 5 表 + document 扩列（org_id / project_id / owner_id / visibility）
- v3：P4-A 同步原语扩列
    - operation: status / request_hash / result / error / attempts / lease_owner / lease_until
      / updated_at / completed_at
    - outbox_event: aggregate_type / aggregate_id / event_type / status / next_attempt_at
      / lease_owner / lease_until / last_error / last_delivered_at
    - sync_run: cursor_before / cursor_after / status / lease_owner / lease_until
      / discovered / created_n / updated_n / deleted_n / failed_n
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

V2_DB_FILENAME = "v2.db"
SCHEMA_VERSION = 4


# ---------- v1 DDL ----------

_V1_DDL = [
    # 元表：记录已应用的 schema 迁移
    """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version    INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL,
        note       TEXT
    )
    """,

    # knowledge_source：数据源（文件 / 远程 / MCP / human）
    """
    CREATE TABLE IF NOT EXISTS knowledge_source (
        stable_id  TEXT PRIMARY KEY,
        kind       TEXT NOT NULL,
        locator    TEXT NOT NULL,
        config     TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        UNIQUE(kind, locator)
    )
    """,

    # document：一份文档
    """
    CREATE TABLE IF NOT EXISTS document (
        id            TEXT PRIMARY KEY,
        source_id     TEXT NOT NULL REFERENCES knowledge_source(stable_id),
        path          TEXT NOT NULL,
        kind          TEXT,                       -- KnowledgeKind enum；可空
        title         TEXT NOT NULL DEFAULT '',
        content_hash  TEXT NOT NULL DEFAULT '',
        status        TEXT NOT NULL DEFAULT 'discovered',
        meta          TEXT NOT NULL DEFAULT '{}',
        created_at    TEXT NOT NULL,
        updated_at    TEXT NOT NULL,
        UNIQUE(source_id, path)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_document_source ON document(source_id)",
    "CREATE INDEX IF NOT EXISTS idx_document_status ON document(status)",
    "CREATE INDEX IF NOT EXISTS idx_document_kind ON document(kind)",

    # document_version：不可变版本
    """
    CREATE TABLE IF NOT EXISTS document_version (
        id             TEXT PRIMARY KEY,
        doc_id         TEXT NOT NULL REFERENCES document(id) ON DELETE CASCADE,
        version_no     INTEGER NOT NULL,
        content_hash   TEXT NOT NULL,
        body           TEXT NOT NULL,
        meta           TEXT NOT NULL DEFAULT '{}',
        created_at     TEXT NOT NULL,
        superseded_by  TEXT,
        UNIQUE(doc_id, version_no),
        UNIQUE(doc_id, content_hash)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_docver_doc ON document_version(doc_id)",
    "CREATE INDEX IF NOT EXISTS idx_docver_hash ON document_version(content_hash)",
    "CREATE INDEX IF NOT EXISTS idx_docver_active ON document_version(doc_id, superseded_by)",

    # document_chunk：分块
    """
    CREATE TABLE IF NOT EXISTS document_chunk (
        id            TEXT PRIMARY KEY,
        doc_id        TEXT NOT NULL REFERENCES document(id) ON DELETE CASCADE,
        version_id    TEXT NOT NULL REFERENCES document_version(id) ON DELETE CASCADE,
        ordinal       INTEGER NOT NULL,
        text          TEXT NOT NULL,
        start_offset  INTEGER NOT NULL DEFAULT 0,
        end_offset    INTEGER NOT NULL DEFAULT 0,
        meta          TEXT NOT NULL DEFAULT '{}',
        created_at    TEXT NOT NULL,
        UNIQUE(version_id, ordinal)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_chunk_doc ON document_chunk(doc_id)",
    "CREATE INDEX IF NOT EXISTS idx_chunk_version ON document_chunk(version_id)",

    # operation：写动作审计
    """
    CREATE TABLE IF NOT EXISTS operation (
        id          TEXT PRIMARY KEY,
        kind        TEXT NOT NULL,         -- OperationKind
        target_id   TEXT NOT NULL,
        by_actor    TEXT NOT NULL,
        payload     TEXT NOT NULL DEFAULT '{}',
        created_at  TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_op_target ON operation(target_id)",
    "CREATE INDEX IF NOT EXISTS idx_op_kind ON operation(kind)",
    "CREATE INDEX IF NOT EXISTS idx_op_time ON operation(created_at)",

    # outbox_event：Phase 4 启用
    """
    CREATE TABLE IF NOT EXISTS outbox_event (
        id            TEXT PRIMARY KEY,
        topic         TEXT NOT NULL,
        payload       TEXT NOT NULL,
        created_at    TEXT NOT NULL,
        delivered_at  TEXT,
        attempts      INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_outbox_pending ON outbox_event(delivered_at) "
    "WHERE delivered_at IS NULL",

    # sync_run：Phase 4 启用
    """
    CREATE TABLE IF NOT EXISTS sync_run (
        id           TEXT PRIMARY KEY,
        source_id    TEXT NOT NULL REFERENCES knowledge_source(stable_id),
        started_at   TEXT NOT NULL,
        finished_at  TEXT,
        stats        TEXT NOT NULL DEFAULT '{}',
        error        TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_syncrun_source ON sync_run(source_id)",
]


# ---------- v2 DDL：ACL 5 表 + document 扩列 ----------

_V2_DDL = [
    # organization：组织
    """
    CREATE TABLE IF NOT EXISTS organization (
        id          TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        slug        TEXT NOT NULL UNIQUE,
        created_at  TEXT NOT NULL
    )
    """,

    # project：项目（属于一个组织）
    """
    CREATE TABLE IF NOT EXISTS project (
        id          TEXT PRIMARY KEY,
        org_id      TEXT NOT NULL REFERENCES organization(id),
        name        TEXT NOT NULL,
        slug        TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        UNIQUE(org_id, slug)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_project_org ON project(org_id)",

    # principal：身份（user / service / agent）
    """
    CREATE TABLE IF NOT EXISTS principal (
        id            TEXT PRIMARY KEY,
        display_name  TEXT NOT NULL,
        kind          TEXT NOT NULL DEFAULT 'user',  -- user / service / agent
        created_at    TEXT NOT NULL
    )
    """,

    # membership：组织成员 + 角色
    """
    CREATE TABLE IF NOT EXISTS membership (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        principal_id  TEXT NOT NULL REFERENCES principal(id) ON DELETE CASCADE,
        org_id        TEXT NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
        role          TEXT NOT NULL,                  -- viewer / contributor / reviewer / project_admin / platform_admin
        UNIQUE(principal_id, org_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_membership_principal ON membership(principal_id)",
    "CREATE INDEX IF NOT EXISTS idx_membership_org ON membership(org_id)",

    # project_membership：项目成员 + 角色
    """
    CREATE TABLE IF NOT EXISTS project_membership (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        principal_id  TEXT NOT NULL REFERENCES principal(id) ON DELETE CASCADE,
        project_id    TEXT NOT NULL REFERENCES project(id) ON DELETE CASCADE,
        role          TEXT NOT NULL,
        UNIQUE(principal_id, project_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pm_principal ON project_membership(principal_id)",
    "CREATE INDEX IF NOT EXISTS idx_pm_project ON project_membership(project_id)",

    # document 扩列（v1 → v2 平滑升级）
    "ALTER TABLE document ADD COLUMN org_id TEXT",
    "ALTER TABLE document ADD COLUMN project_id TEXT",
    "ALTER TABLE document ADD COLUMN owner_id TEXT",
    "ALTER TABLE document ADD COLUMN visibility TEXT DEFAULT 'organization-global'",

    "CREATE INDEX IF NOT EXISTS idx_document_org ON document(org_id)",
    "CREATE INDEX IF NOT EXISTS idx_document_project ON document(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_document_visibility ON document(visibility)",
]


# ---------- v3 DDL：同步原语扩列（P4-A）----------

_V3_DDL = [
    # operation：状态机 + 幂等 + lease
    "ALTER TABLE operation ADD COLUMN status TEXT NOT NULL DEFAULT 'succeeded'",
    "ALTER TABLE operation ADD COLUMN request_hash TEXT",
    "ALTER TABLE operation ADD COLUMN result TEXT NOT NULL DEFAULT '{}'",
    "ALTER TABLE operation ADD COLUMN error TEXT",
    "ALTER TABLE operation ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE operation ADD COLUMN lease_owner TEXT",
    "ALTER TABLE operation ADD COLUMN lease_until TEXT",
    "ALTER TABLE operation ADD COLUMN updated_at TEXT",
    "ALTER TABLE operation ADD COLUMN completed_at TEXT",
    "CREATE INDEX IF NOT EXISTS idx_op_status ON operation(status)",
    "CREATE INDEX IF NOT EXISTS idx_op_lease ON operation(status, lease_until)",

    # outbox_event：aggregate 维度 + lease + 退避
    "ALTER TABLE outbox_event ADD COLUMN aggregate_type TEXT",
    "ALTER TABLE outbox_event ADD COLUMN aggregate_id TEXT",
    "ALTER TABLE outbox_event ADD COLUMN event_type TEXT",
    "ALTER TABLE outbox_event ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'",
    "ALTER TABLE outbox_event ADD COLUMN next_attempt_at TEXT",
    "ALTER TABLE outbox_event ADD COLUMN lease_owner TEXT",
    "ALTER TABLE outbox_event ADD COLUMN lease_until TEXT",
    "ALTER TABLE outbox_event ADD COLUMN last_error TEXT",
    "ALTER TABLE outbox_event ADD COLUMN last_delivered_at TEXT",
    "CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox_event(status)",
    "CREATE INDEX IF NOT EXISTS idx_outbox_lease ON outbox_event(status, lease_until)",
    "CREATE INDEX IF NOT EXISTS idx_outbox_next_attempt ON outbox_event(status, next_attempt_at)",
    "CREATE INDEX IF NOT EXISTS idx_outbox_aggregate ON outbox_event(aggregate_type, aggregate_id)",

    # sync_run：cursor + 计数 + lease
    "ALTER TABLE sync_run ADD COLUMN cursor_before TEXT",
    "ALTER TABLE sync_run ADD COLUMN cursor_after TEXT",
    "ALTER TABLE sync_run ADD COLUMN status TEXT NOT NULL DEFAULT 'running'",
    "ALTER TABLE sync_run ADD COLUMN discovered INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE sync_run ADD COLUMN created_n INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE sync_run ADD COLUMN updated_n INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE sync_run ADD COLUMN deleted_n INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE sync_run ADD COLUMN failed_n INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE sync_run ADD COLUMN lease_owner TEXT",
    "ALTER TABLE sync_run ADD COLUMN lease_until TEXT",
    "CREATE INDEX IF NOT EXISTS idx_syncrun_status ON sync_run(status)",
    "CREATE INDEX IF NOT EXISTS idx_syncrun_lease ON sync_run(status, lease_until)",
]


# ---------- v4 DDL：Lite Profile source_sync_state（P4-D）----------

_V4_DDL = [
    # source_sync_state：每个 source 的同步快照（Lite Profile）
    # - last_remote_check_at / last_remote_revision / local_revision：fetch 进度
    # - last_pull_at / last_push_at / last_failed_at：操作时间
    # - last_error / pending_ops / conflicted_docs：失败 & 积压 & 冲突计数
    """
    CREATE TABLE IF NOT EXISTS source_sync_state (
        source_id             TEXT PRIMARY KEY REFERENCES knowledge_source(stable_id),
        last_remote_check_at  TEXT,
        last_remote_revision  TEXT,
        local_revision        TEXT,
        last_pull_at          TEXT,
        last_push_at          TEXT,
        last_failed_at        TEXT,
        last_error            TEXT,
        pending_ops           INTEGER NOT NULL DEFAULT 0,
        conflicted_docs       INTEGER NOT NULL DEFAULT 0,
        last_sync_run_id      TEXT REFERENCES sync_run(id),
        updated_at            TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_syncstate_failed ON source_sync_state(last_failed_at)",
    "CREATE INDEX IF NOT EXISTS idx_syncstate_run ON source_sync_state(last_sync_run_id)",
]


# ---------- visibility 枚举值 ----------

VISIBILITY_PUBLIC_TEMPLATE = "public-template"
VISIBILITY_ORG_GLOBAL = "organization-global"
VISIBILITY_PROJECT_SHARED = "project-shared"
VISIBILITY_PERSONAL = "personal"

_VALID_VISIBILITY = {
    VISIBILITY_PUBLIC_TEMPLATE,
    VISIBILITY_ORG_GLOBAL,
    VISIBILITY_PROJECT_SHARED,
    VISIBILITY_PERSONAL,
}


def valid_visibility() -> set[str]:
    return set(_VALID_VISIBILITY)


# ---------- 迁移 ----------

def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_v2_db(repo: Path, db_path: Path | None = None) -> Path:
    """初始化 V2 数据库；返回实际 db_path。已存在时幂等。"""
    if db_path is None:
        db_path = repo / ".knowbase" / V2_DB_FILENAME
    conn = _connect(db_path)
    try:
        with conn:
            for stmt in _V1_DDL:
                conn.execute(stmt)
        apply_migrations(conn, SCHEMA_VERSION, note=f"upgrade to v{SCHEMA_VERSION}")
    finally:
        conn.close()
    return db_path


def apply_migrations(conn: sqlite3.Connection, target_version: int,
                     note: str = "") -> None:
    """将 schema_version 推进到 target_version；按 v1→v2→... 增量执行。"""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current >= target_version:
        return
    # v1 → v1 初次：仅记录 migration（建表已在 _V1_DDL 完成）
    if current < 1 <= target_version:
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at, note) "
            "VALUES (?, datetime('now'), ?)",
            (1, note or "initial P1-A schema"),
        )
        conn.execute("PRAGMA user_version = 1")
        current = 1
    # v1 → v2：P3-A ACL 表 + document 扩列
    if current < 2 <= target_version:
        with conn:
            for stmt in _V2_DDL:
                conn.execute(stmt)
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at, note) "
            "VALUES (?, datetime('now'), ?)",
            (2, "P3-A ACL 5 tables + document columns"),
        )
        conn.execute("PRAGMA user_version = 2")
        current = 2
    # v2 → v3：P4-A 同步原语扩列
    if current < 3 <= target_version:
        with conn:
            for stmt in _V3_DDL:
                conn.execute(stmt)
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at, note) "
            "VALUES (?, datetime('now'), ?)",
            (3, "P4-A sync primitives (operation/outbox/sync_run) extended columns"),
        )
        conn.execute("PRAGMA user_version = 3")
        current = 3
    # v3 → v4：P4-D Lite source_sync_state 表
    if current < 4 <= target_version:
        with conn:
            for stmt in _V4_DDL:
                conn.execute(stmt)
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at, note) "
            "VALUES (?, datetime('now'), ?)",
            (4, "P4-D Lite source_sync_state table"),
        )
        conn.execute("PRAGMA user_version = 4")
        current = 4
    # 后续 v4 → v5 在此按需添加


def current_schema_version(db_path: Path) -> int:
    """读取当前 schema_version（库不存在则返回 0）。"""
    if not db_path.exists():
        return 0
    conn = _connect(db_path)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()
