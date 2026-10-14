"""P1 回归：迁移事务原子性 + retry_wait 到期重领。

覆盖：
- apply_all 逐条执行，中途失败整体回滚
- 失败版本不写入 schema_migrations，恢复后可重放
- 到期 retry_wait 可被 claim，未到期不可
- retry 重领 attempt / fencing 语义
- 租约过期接管不增加 attempt
- 并发领取不会双领（CAS）
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from knowbase import config, index, jobs, migrations
from knowbase.__main__ import cmd_init


@pytest.fixture()
def isolated_repo(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    cfg_path = tmp_path / "config.json"
    cfg = config._deep_merge(config.DEFAULTS, {
        "repo_path": str(repo),
        "knowledge_path": "",
        "git": {"auto_commit": False, "auto_push": False, "auto_pull": False},
    })
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", cfg_path)
    monkeypatch.setenv("KNOWBASE_REPO_PATH", str(repo))
    monkeypatch.setenv("KNOWBASE_AGENT_NAME", "pytest-agent")
    assert cmd_init() == 0
    return repo


def _fresh_conn(repo: Path):
    """裸 sqlite 连接（不走 index.connect，避免自动迁移）。"""
    import sqlite3
    repo = Path(repo)
    repo.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(index.db_path(repo))
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ---------- migrations 事务 ----------


class TestMigrationAtomicity:
    def test_failed_migration_rolls_back_all(self, tmp_path):
        conn = _fresh_conn(tmp_path / "repo")
        try:
            conn.execute(migrations.MIGRATION_TABLE)
            conn.commit()

            saved = migrations.MIGRATIONS
            migrations.MIGRATIONS = [
                (900, "CREATE TABLE kb_ok(id INTEGER PRIMARY KEY);"),
                (901, "CREATE TABLE kb_partial(id INTEGER PRIMARY KEY);"
                      "CREATE TABLE kb_bad(id INTEGER PRIMARY KEY);"
                      "THIS IS NOT SQL;"),
            ]
            try:
                with pytest.raises(Exception):
                    migrations.apply_all(conn)
            finally:
                migrations.MIGRATIONS = saved

            tables = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            # 前一个迁移的 DDL 必须随整体回滚消失
            assert "kb_ok" not in tables
            assert "kb_partial" not in tables

            recorded = [
                r[0] for r in conn.execute(
                    "SELECT version FROM schema_migrations"
                ).fetchall()
            ]
            assert 900 not in recorded
            assert 901 not in recorded
        finally:
            conn.close()

    def test_rollback_leaves_connection_usable(self, tmp_path):
        conn = _fresh_conn(tmp_path / "repo")
        try:
            conn.execute(migrations.MIGRATION_TABLE)
            conn.commit()

            saved = migrations.MIGRATIONS
            migrations.MIGRATIONS = [
                (910, "CREATE TABLE kb_x(id INTEGER); NOT VALID SQL"),
            ]
            try:
                with pytest.raises(Exception):
                    migrations.apply_all(conn)
            finally:
                migrations.MIGRATIONS = saved

            # 回滚后连接未被留在半开事务里
            tables = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert "kb_x" not in tables
            conn.execute("INSERT INTO schema_migrations VALUES(999,'now','x')")
            conn.commit()
        finally:
            conn.close()

    def test_replay_after_failure_succeeds(self, tmp_path):
        conn = _fresh_conn(tmp_path / "repo")
        try:
            conn.execute(migrations.MIGRATION_TABLE)
            conn.commit()

            good = (920, "CREATE TABLE kb_replay(id INTEGER PRIMARY KEY);")
            saved = migrations.MIGRATIONS
            migrations.MIGRATIONS = [
                good,
                (921, "BROKEN SQL HERE"),
            ]
            try:
                with pytest.raises(Exception):
                    migrations.apply_all(conn)
                # 修复后重放
                migrations.MIGRATIONS = [good]
                assert migrations.apply_all(conn) == [920]
            finally:
                migrations.MIGRATIONS = saved

            tables = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert "kb_replay" in tables
        finally:
            conn.close()

    def test_multi_statement_migration_applied_atomically(self, isolated_repo):
        """真实迁移脚本（多语句）执行后全部落库且可校验。"""
        conn = index.connect(isolated_repo)
        try:
            assert migrations.apply_all(conn) == []
            assert migrations.verify(conn) == []
            # v6 含两个 CREATE TABLE，必须都建出来
            tables = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert {"applied_operations", "entity_heads"} <= tables
        finally:
            conn.close()

    def test_statements_split_keeps_semicolons_in_literals(self):
        sql = (
            "CREATE TABLE t(a TEXT);\n"
            "INSERT INTO t VALUES('a;b');\n"
            "-- 注释里带分号 ;;;\n"
            "INSERT INTO t VALUES('c');\n"
        )
        stmts = migrations._statements(sql)
        assert len(stmts) == 3
        assert any("'a;b'" in s for s in stmts)

    def test_statements_reject_unterminated_tail(self):
        """尾部注释不得把最后一条语句整段吞掉。"""
        with pytest.raises(ValueError):
            migrations._statements(
                "CREATE TABLE t(a TEXT);\n-- 注释\nINSERT INTO t VALUES('尾')\n"
            )

    def test_statements_allow_comment_only_tail(self):
        assert migrations._statements(
            "CREATE TABLE t(a TEXT);\n-- 只有注释\n"
        ) == ["CREATE TABLE t(a TEXT);"]


# ---------- retry_wait ----------


def _prepare_awaiting(conn, scope="proj-a", max_attempts=3):
    r = jobs.enqueue(conn, jobs.KIND_CAPTURE, scope, {"data": "x"},
                     max_attempts=max_attempts)
    job_id = r["job_id"]
    jobs.set_preparing(conn, job_id)
    jobs.set_awaiting(conn, job_id, 1)
    return job_id


class TestRetryWaitClaim:
    def test_due_retry_wait_is_claimable(self, isolated_repo):
        conn = index.connect(isolated_repo)
        try:
            job_id = _prepare_awaiting(conn)

            c = jobs.claim(conn, job_id, "host-1")
            assert "error" not in c
            assert jobs.fail(conn, job_id, "boom", retryable=True)["state"] == jobs.STATE_RETRY_WAIT

            # 未到期不可领
            early = jobs.claim(conn, job_id, "host-2")
            assert early["error"] == jobs.NO_WORK
            assert early["state"] == jobs.STATE_RETRY_WAIT
            assert early["retry_at"]

            # 把 next_run_at 拨到过去
            past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(
                timespec="seconds"
            )
            conn.execute("UPDATE jobs SET next_run_at=? WHERE job_id=?", (past, job_id))
            conn.commit()

            again = jobs.claim(conn, job_id, "host-2")
            assert "error" not in again
            assert again["lease_token"] != c["lease_token"]
            assert again["fencing_token"] > c["fencing_token"]
            assert conn.execute(
                "SELECT attempt FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()[0] == 2
        finally:
            conn.close()

    def test_retry_wait_next_run_at_null_is_due(self, isolated_repo):
        conn = index.connect(isolated_repo)
        try:
            job_id = _prepare_awaiting(conn)
            jobs.claim(conn, job_id, "host-1")
            jobs.fail(conn, job_id, "boom")
            conn.execute("UPDATE jobs SET next_run_at=NULL WHERE job_id=?", (job_id,))
            conn.commit()

            again = jobs.claim(conn, job_id, "host-2")
            assert "error" not in again
        finally:
            conn.close()

    def test_future_retry_wait_blocks_claim(self, isolated_repo):
        conn = index.connect(isolated_repo)
        try:
            job_id = _prepare_awaiting(conn)
            jobs.claim(conn, job_id, "host-1")
            jobs.fail(conn, job_id, "boom")

            future = (datetime.now(timezone.utc) + timedelta(seconds=600)).isoformat(
                timespec="seconds"
            )
            conn.execute("UPDATE jobs SET next_run_at=? WHERE job_id=?", (future, job_id))
            conn.commit()

            r = jobs.claim(conn, job_id, "host-2")
            assert r["error"] == jobs.NO_WORK
            assert r["state"] == jobs.STATE_RETRY_WAIT
            assert r["retry_at"] == future
            assert conn.execute(
                "SELECT state FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()[0] == jobs.STATE_RETRY_WAIT
        finally:
            conn.close()

    def test_retry_reclaim_clears_error_and_keeps_cursor(self, isolated_repo):
        conn = index.connect(isolated_repo)
        try:
            job_id = _prepare_awaiting(conn)
            c = jobs.claim(conn, job_id, "host-1")
            jobs.fail(conn, job_id, "boom")

            past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(
                timespec="seconds"
            )
            conn.execute("UPDATE jobs SET next_run_at=? WHERE job_id=?", (past, job_id))
            conn.commit()

            again = jobs.claim(conn, job_id, "host-2")
            row = conn.execute(
                "SELECT state, error, attempt, lease_owner FROM jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
            assert row[0] == jobs.STATE_CLAIMED
            assert row[1] is None
            assert row[2] == 2
            assert row[3].startswith("host-2:")
            assert again["cursor"] == c["cursor"]
            # 重领后仍可正常提交
            res = jobs.submit(
                conn, job_id, again["batch_id"], again["lease_token"], "req-retry", []
            )
            assert "error" not in res
        finally:
            conn.close()

    def test_lease_expiry_takeover_does_not_increment_attempt(self, isolated_repo):
        conn = index.connect(isolated_repo)
        try:
            job_id = _prepare_awaiting(conn)
            c1 = jobs.claim(conn, job_id, "host-1")
            assert conn.execute(
                "SELECT attempt FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()[0] == 1

            conn.execute(
                "UPDATE jobs SET lease_until='2000-01-01T00:00:00+00:00' WHERE job_id=?",
                (job_id,),
            )
            conn.commit()

            c2 = jobs.claim(conn, job_id, "host-2")
            assert "error" not in c2
            assert conn.execute(
                "SELECT attempt FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()[0] == 1
            assert c2["fencing_token"] > c1["fencing_token"]
        finally:
            conn.close()

    def test_retry_chain_bounded_by_max_attempts(self, isolated_repo):
        conn = index.connect(isolated_repo)
        try:
            job_id = _prepare_awaiting(conn, max_attempts=2)
            jobs.claim(conn, job_id, "host-1")
            assert jobs.fail(conn, job_id, "e1")["state"] == jobs.STATE_RETRY_WAIT

            past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(
                timespec="seconds"
            )
            conn.execute("UPDATE jobs SET next_run_at=? WHERE job_id=?", (past, job_id))
            conn.commit()
            jobs.claim(conn, job_id, "host-2")

            # attempt 已达上限，再次失败直接 failed
            assert jobs.fail(conn, job_id, "e2")["state"] == jobs.STATE_FAILED
            r = jobs.claim(conn, job_id, "host-3")
            assert r["error"] == "already_done"
        finally:
            conn.close()

    def test_concurrent_claim_only_one_wins(self, isolated_repo):
        """CAS 生效：过期 fencing_token 的写入被拒绝，第二个领取者看到真实持有者。"""
        conn = index.connect(isolated_repo)
        try:
            job_id = _prepare_awaiting(conn)
            c1 = jobs.claim(conn, job_id, "host-1")

            # 模拟并发持有旧快照：用过期 fencing_token 写入必须失败
            cur = conn.execute(
                "UPDATE jobs SET lease_owner='ghost' "
                "WHERE job_id=? AND state=? AND fencing_token=?",
                (job_id, jobs.STATE_CLAIMED, c1["fencing_token"] - 1),
            )
            assert cur.rowcount == 0

            c2 = jobs.claim(conn, job_id, "host-2")
            assert c2["error"] == jobs.LEASE_EXPIRED
            assert c2["held_by"].startswith("host-1:")
            assert conn.execute(
                "SELECT lease_owner FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()[0].startswith("host-1:")
        finally:
            conn.close()

    def test_queued_state_still_not_claimable(self, isolated_repo):
        """修复不能放宽到任意状态：queued 仍需先 set_preparing。"""
        conn = index.connect(isolated_repo)
        try:
            r = jobs.enqueue(conn, jobs.KIND_CAPTURE, "proj-a", {"data": "x"})
            c = jobs.claim(conn, r["job_id"], "host-1")
            assert c["error"] == jobs.NO_WORK
            assert c["state"] == jobs.STATE_QUEUED
        finally:
            conn.close()