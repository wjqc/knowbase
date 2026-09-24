"""数据库迁移框架：同库增量迁移，版本闸门。

所有新表通过 migration 注册，按版本号顺序执行；
已执行的迁移跳过（幂等），checksum 不匹配时停止并告警。
"""

import hashlib
import sqlite3
from datetime import datetime
from pathlib import Path

MIGRATION_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations(
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL,
  checksum TEXT NOT NULL
)
"""

# 每条迁移：(version, sql)。sql 必须是幂等的（IF NOT EXISTS / IF EXISTS）。
MIGRATIONS: list[tuple[int, str]] = [
    # v1: 任务队列 + 候选提炼
    (1, """
    CREATE TABLE IF NOT EXISTS jobs(
      job_id TEXT PRIMARY KEY,
      kind TEXT NOT NULL,
      scope TEXT NOT NULL DEFAULT '',
      input_hash TEXT NOT NULL DEFAULT '',
      idempotency_key TEXT NOT NULL DEFAULT '',
      state TEXT NOT NULL DEFAULT 'queued',
      attempt INTEGER NOT NULL DEFAULT 0,
      max_attempts INTEGER NOT NULL DEFAULT 3,
      next_run_at TEXT,
      lease_owner TEXT,
      lease_until TEXT,
      fencing_token INTEGER NOT NULL DEFAULT 0,
      batch_cursor INTEGER NOT NULL DEFAULT 0,
      batch_total INTEGER NOT NULL DEFAULT 0,
      result TEXT,
      error TEXT,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);
    CREATE INDEX IF NOT EXISTS idx_jobs_idempotency ON jobs(idempotency_key);
    CREATE INDEX IF NOT EXISTS idx_jobs_kind_scope ON jobs(kind, scope);
    """),
    # v2: 候选提炼结果
    (2, """
    CREATE TABLE IF NOT EXISTS candidates(
      candidate_id TEXT PRIMARY KEY,
      job_id TEXT NOT NULL,
      batch_id TEXT NOT NULL DEFAULT '',
      candidate_key TEXT NOT NULL DEFAULT '',
      scope TEXT NOT NULL DEFAULT '',
      body_json TEXT NOT NULL DEFAULT '{}',
      evidence_json TEXT NOT NULL DEFAULT '[]',
      decision TEXT NOT NULL DEFAULT 'pending',
      duplicate_of TEXT,
      version INTEGER NOT NULL DEFAULT 1,
      request_id TEXT NOT NULL DEFAULT '',
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      UNIQUE(job_id, candidate_key)
    );
    CREATE INDEX IF NOT EXISTS idx_candidates_job ON candidates(job_id);
    CREATE INDEX IF NOT EXISTS idx_candidates_request ON candidates(request_id);
    """),
    # v3: 同步观测记录（M0.5 止血）
    (3, """
    CREATE TABLE IF NOT EXISTS sync_attempts(
      attempt_id TEXT PRIMARY KEY,
      ts TEXT NOT NULL,
      remote TEXT NOT NULL DEFAULT '',
      branch TEXT NOT NULL DEFAULT '',
      target_sha TEXT NOT NULL DEFAULT '',
      result TEXT NOT NULL DEFAULT '',
      duration_ms INTEGER NOT NULL DEFAULT 0,
      confirmed_revision TEXT NOT NULL DEFAULT '',
      pending_push_count INTEGER NOT NULL DEFAULT 0,
      error TEXT NOT NULL DEFAULT '',
      notes TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_sync_attempts_ts ON sync_attempts(ts);
    """),
    # v4: 跨进程同步租约（替代进程内 _LAST_FETCH_MONO）
    (4, """
    CREATE TABLE IF NOT EXISTS sync_lease(
      repo TEXT NOT NULL,
      remote TEXT NOT NULL,
      branch TEXT NOT NULL,
      lease_owner TEXT NOT NULL DEFAULT '',
      lease_until TEXT NOT NULL DEFAULT '',
      last_attempt TEXT,
      last_success TEXT,
      next_check_at TEXT,
      PRIMARY KEY(repo, remote, branch)
    );
    """),
    # v5: 本地 spool（持久化任务输入，崩溃恢复）
    (5, """
    CREATE TABLE IF NOT EXISTS job_spool(
      spool_id TEXT PRIMARY KEY,
      job_id TEXT NOT NULL,
      input_json TEXT NOT NULL DEFAULT '{}',
      checksum TEXT NOT NULL DEFAULT '',
      created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_spool_job ON job_spool(job_id);
    """),
    # v6: 操作记录 + 实体版本头（M0.5 CAS 与幂等）
    (6, """
    CREATE TABLE IF NOT EXISTS applied_operations(
      op_id TEXT PRIMARY KEY,
      entity_id TEXT NOT NULL,
      scope TEXT NOT NULL DEFAULT '',
      kind TEXT NOT NULL,
      request_id TEXT UNIQUE,
      payload_hash TEXT NOT NULL DEFAULT '',
      result TEXT NOT NULL DEFAULT '',
      base_revision TEXT NOT NULL DEFAULT '',
      new_revision TEXT NOT NULL DEFAULT '',
      created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_ops_entity ON applied_operations(entity_id);
    CREATE INDEX IF NOT EXISTS idx_ops_request ON applied_operations(request_id);
    CREATE TABLE IF NOT EXISTS entity_heads(
      entity_id TEXT PRIMARY KEY,
      content_revision TEXT NOT NULL DEFAULT '',
      governance_revision TEXT NOT NULL DEFAULT '',
      updated_at TEXT NOT NULL
    );
    """),
    # v7: 写入意图表（L1 崩溃恢复用）
    (7, """
    CREATE TABLE IF NOT EXISTS write_intents(
      intent_id TEXT PRIMARY KEY,
      request_id TEXT NOT NULL DEFAULT '',
      entity_id TEXT NOT NULL DEFAULT '',
      scope TEXT NOT NULL DEFAULT '',
      kind TEXT NOT NULL,
      payload_hash TEXT NOT NULL DEFAULT '',
      status TEXT NOT NULL DEFAULT 'pending',
      result TEXT NOT NULL DEFAULT '',
      created_at TEXT NOT NULL,
      completed_at TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_intent_request ON write_intents(request_id);
    CREATE INDEX IF NOT EXISTS idx_intent_status ON write_intents(status);
    """),
    # v8: segment_results 表（M3-3: segment 级别处理记录）
    (8, """
    CREATE TABLE IF NOT EXISTS segment_results(
      segment_id TEXT PRIMARY KEY,
      job_id TEXT NOT NULL,
      batch_id TEXT NOT NULL,
      decision TEXT NOT NULL DEFAULT 'pending',
      reason TEXT NOT NULL DEFAULT '',
      candidate_id TEXT NOT NULL DEFAULT '',
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_segment_job ON segment_results(job_id);
    CREATE INDEX IF NOT EXISTS idx_segment_decision ON segment_results(decision);
    """),
]


def _checksum(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()[:16]


def pending(conn: sqlite3.Connection) -> list[tuple[int, str]]:
    """返回尚未执行的迁移列表。"""
    conn.execute(MIGRATION_TABLE)
    conn.commit()
    applied = set(
        row[0] for row in conn.execute("SELECT version FROM schema_migrations").fetchall()
    )
    return [(v, sql) for v, sql in MIGRATIONS if v not in applied]


def apply_all(conn: sqlite3.Connection) -> list[int]:
    """执行所有待迁移，返回已执行的版本号列表。

    使用 BEGIN IMMEDIATE 保证事务性；单条迁移失败则整体回滚。
    """
    todo = pending(conn)
    if not todo:
        return []
    applied = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        for version, sql in todo:
            conn.executescript(sql)
            cs = _checksum(sql)
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at, checksum) VALUES(?,?,?)",
                (version, datetime.now().isoformat(timespec="seconds"), cs),
            )
            applied.append(version)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return applied


def verify(conn: sqlite3.Connection) -> list[str]:
    """校验已执行迁移的 checksum，返回不一致的版本号。"""
    conn.execute(MIGRATION_TABLE)
    conn.commit()
    errors = []
    migration_map = {v: sql for v, sql in MIGRATIONS}
    for row in conn.execute("SELECT version, checksum FROM schema_migrations").fetchall():
        version, stored_cs = row
        if version in migration_map:
            expected = _checksum(migration_map[version])
            if stored_cs != expected:
                errors.append(f"v{version}: checksum mismatch (stored={stored_cs}, expected={expected})")
    return errors
