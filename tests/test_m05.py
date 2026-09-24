"""M0.5 测试：统一写入服务 / UUID ID / scope 隔离 / 同步观测 / 错误分类。

覆盖：
- mutations.py: CAS (REVISION_CONFLICT)、幂等 (IDEMPOTENCY_CONFLICT)、create/update/get_revisions
- store.py / sources.py: UUID 唯一 ID 格式 + 旧格式兼容
- sources.py: find_by_sha scope 隔离
- gitops.py: _is_auth_error / _is_network_error / _is_push_race 错误分类
- gitops.py: record_sync_attempt / _check_sync_lease / _release_sync_lease
- migrations.py: v6 applied_operations + entity_heads 表
"""

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from knowbase import config, index, migrations, store, sources, mutations
from knowbase.__main__ import cmd_init
from knowbase.gitops import (
    _is_auth_error, _is_network_error, _is_push_race,
    record_sync_attempt, _check_sync_lease, _release_sync_lease,
)


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


# ---------- UUID 唯一 ID ----------

class TestUUIDIds:
    def test_store_alloc_id_uuid_format(self, isolated_repo):
        """新分配的卡片 ID 应为 <PREFIX>-<uuid12> 格式。"""
        for dtype in ("pitfall", "standard", "decision", "workflow", "preference", "reference", "bizrule"):
            mid = store.alloc_id(isolated_repo, dtype)
            prefix = store.PREFIX[dtype]
            assert mid.startswith(f"{prefix}-"), f"{mid} 不以 {prefix}- 开头"
            suffix = mid[len(prefix) + 1:]
            assert len(suffix) == 12, f"{mid} 后缀长度不为 12: {suffix}"
            assert re.fullmatch(r"[0-9a-f]{12}", suffix), f"{mid} 后缀非 hex: {suffix}"

    def test_store_alloc_id_unique(self, isolated_repo):
        """连续分配的 ID 不应重复。"""
        ids = {store.alloc_id(isolated_repo, "pitfall") for _ in range(100)}
        assert len(ids) == 100

    def test_store_id_re_accepts_both_formats(self):
        """ID_RE 应同时匹配新旧格式。"""
        assert store.ID_RE.match("P-2026-0001")
        assert store.ID_RE.match("P-a1b2c3d4e5f6")
        assert store.ID_RE.match("PR-abcdef012345")
        assert not store.ID_RE.match("P-invalid")
        assert not store.ID_RE.match("")

    def test_sources_alloc_id_uuid_format(self, isolated_repo):
        """source ID 应为 SRC-<uuid12> 格式。"""
        sid = sources.alloc_id(isolated_repo)
        assert sid.startswith("SRC-")
        suffix = sid[4:]
        assert len(suffix) == 12
        assert re.fullmatch(r"[0-9a-f]{12}", suffix)

    def test_sources_alloc_id_unique(self, isolated_repo):
        ids = {sources.alloc_id(isolated_repo) for _ in range(100)}
        assert len(ids) == 100

    def test_sources_id_re_accepts_both_formats(self):
        """SRC_ID_RE 应同时匹配新旧格式。"""
        assert sources.SRC_ID_RE.match("SRC-2026-0001.yaml")
        assert sources.SRC_ID_RE.match("SRC-a1b2c3d4e5f6.yaml")
        assert not sources.SRC_ID_RE.match("SRC-invalid.yaml")


# ---------- find_by_sha scope 隔离 ----------

class TestFindByShaScope:
    def test_same_scope_found(self, isolated_repo):
        text = "scope isolation test content"
        sid, sha, created = sources.import_source(
            isolated_repo, text, title="Test", scope="proj-a",
            fmt="md", parser_version="1.0", imported_by="test",
        )
        assert created
        found = sources.find_by_sha(isolated_repo, sha, scope="proj-a")
        assert found is not None
        assert found["id"] == sid

    def test_different_scope_not_found(self, isolated_repo):
        text = "scope isolation test content 2"
        sid, sha, created = sources.import_source(
            isolated_repo, text, title="Test", scope="proj-a",
            fmt="md", parser_version="1.0", imported_by="test",
        )
        assert created
        # 不同 scope 不应命中
        found = sources.find_by_sha(isolated_repo, sha, scope="proj-b")
        assert found is None

    def test_empty_scope_matches_all(self, isolated_repo):
        text = "scope isolation test content 3"
        sid, sha, created = sources.import_source(
            isolated_repo, text, title="Test", scope="proj-a",
            fmt="md", parser_version="1.0", imported_by="test",
        )
        assert created
        # scope="" 退化为全局查找
        found = sources.find_by_sha(isolated_repo, sha, scope="")
        assert found is not None
        assert found["id"] == sid

    def test_same_content_different_scope_creates_new(self, isolated_repo):
        """同内容不同 scope 应各自独立创建 manifest。"""
        text = "shared content across scopes"
        sid_a, sha_a, created_a = sources.import_source(
            isolated_repo, text, title="Test A", scope="proj-a",
            fmt="md", parser_version="1.0", imported_by="test",
        )
        assert created_a
        sid_b, sha_b, created_b = sources.import_source(
            isolated_repo, text, title="Test B", scope="proj-b",
            fmt="md", parser_version="1.0", imported_by="test",
        )
        assert created_b  # 不同 scope 应创建新 manifest
        assert sha_a == sha_b  # 内容相同，sha 相同
        assert sid_a != sid_b  # 但 manifest ID 不同


# ---------- mutations.py: CAS + 幂等 ----------

class TestMutations:
    def test_create_card(self, isolated_repo):
        conn = index.connect(isolated_repo)
        try:
            result = mutations.create_card(
                isolated_repo, conn, "pitfall", "测试标题", "## 结论\n内容\n## 解决的问题\n问题",
                scope="test-proj", tags=["tag1"], source="agent:test:s1",
            )
            assert result["created"] is True
            assert result["entity_id"].startswith("P-")
            assert len(result["content_revision"]) == 16
            assert len(result["governance_revision"]) == 16
        finally:
            conn.close()

    def test_create_card_idempotent(self, isolated_repo):
        """同 request_id + 同 payload 应幂等返回既有结果。"""
        conn = index.connect(isolated_repo)
        try:
            r1 = mutations.create_card(
                isolated_repo, conn, "pitfall", "幂等测试", "内容",
                scope="test-proj", request_id="REQ-001",
            )
            assert r1["created"] is True

            r2 = mutations.create_card(
                isolated_repo, conn, "pitfall", "幂等测试", "内容",
                scope="test-proj", request_id="REQ-001",
            )
            assert r2.get("idempotent") is True
            assert r2["entity_id"] == r1["entity_id"]
        finally:
            conn.close()

    def test_create_card_idempotency_conflict(self, isolated_repo):
        """同 request_id + 不同 payload 应返回 IDEMPOTENCY_CONFLICT。"""
        conn = index.connect(isolated_repo)
        try:
            r1 = mutations.create_card(
                isolated_repo, conn, "pitfall", "标题A", "内容A",
                scope="test-proj", request_id="REQ-002",
            )
            assert r1["created"] is True

            r2 = mutations.create_card(
                isolated_repo, conn, "pitfall", "标题B", "内容B",
                scope="test-proj", request_id="REQ-002",
            )
            assert r2.get("error") == mutations.IDEMPOTENCY_CONFLICT
        finally:
            conn.close()

    def test_update_card_cas_success(self, isolated_repo):
        """正确 revision 的 CAS 更新应成功。"""
        conn = index.connect(isolated_repo)
        try:
            r1 = mutations.create_card(
                isolated_repo, conn, "pitfall", "CAS 测试", "原始内容",
                scope="test-proj",
            )
            mid = r1["entity_id"]
            content_rev = r1["content_revision"]

            r2 = mutations.update_card(
                isolated_repo, conn, mid,
                expected_content_revision=content_rev,
                body="更新后内容",
            )
            assert r2["updated"] is True
            assert r2["entity_id"] == mid
            assert r2["content_revision"] != content_rev
        finally:
            conn.close()

    def test_update_card_revision_conflict(self, isolated_repo):
        """错误 revision 的 CAS 更新应返回 REVISION_CONFLICT。"""
        conn = index.connect(isolated_repo)
        try:
            r1 = mutations.create_card(
                isolated_repo, conn, "pitfall", "CAS 冲突测试", "原始内容",
                scope="test-proj",
            )
            mid = r1["entity_id"]

            r2 = mutations.update_card(
                isolated_repo, conn, mid,
                expected_content_revision="wrong_revision_0",
                body="更新后内容",
            )
            assert r2.get("error") == mutations.REVISION_CONFLICT
            assert "current_content_revision" in r2
        finally:
            conn.close()

    def test_update_card_governance_revision_conflict(self, isolated_repo):
        """错误 governance_revision 的 CAS 更新应返回 REVISION_CONFLICT。"""
        conn = index.connect(isolated_repo)
        try:
            r1 = mutations.create_card(
                isolated_repo, conn, "pitfall", "治理 CAS 测试", "内容",
                scope="test-proj",
            )
            mid = r1["entity_id"]

            r2 = mutations.update_card(
                isolated_repo, conn, mid,
                expected_governance_revision="wrong_gov_rev",
                status="archived",
            )
            assert r2.get("error") == mutations.REVISION_CONFLICT
            assert "current_governance_revision" in r2
        finally:
            conn.close()

    def test_update_card_idempotent(self, isolated_repo):
        """同 request_id + 同 payload 的 update 应幂等。"""
        conn = index.connect(isolated_repo)
        try:
            r1 = mutations.create_card(
                isolated_repo, conn, "pitfall", "幂等更新测试", "内容",
                scope="test-proj",
            )
            mid = r1["entity_id"]
            content_rev = r1["content_revision"]

            u1 = mutations.update_card(
                isolated_repo, conn, mid,
                expected_content_revision=content_rev,
                request_id="REQ-UPD-001",
                body="新内容",
            )
            assert u1["updated"] is True

            u2 = mutations.update_card(
                isolated_repo, conn, mid,
                expected_content_revision=u1["content_revision"],
                request_id="REQ-UPD-001",
                body="新内容",
            )
            assert u2.get("idempotent") is True
        finally:
            conn.close()

    def test_get_revisions(self, isolated_repo):
        conn = index.connect(isolated_repo)
        try:
            r1 = mutations.create_card(
                isolated_repo, conn, "pitfall", "版本查询测试", "内容",
                scope="test-proj",
            )
            mid = r1["entity_id"]

            rev = mutations.get_revisions(conn, mid)
            assert rev is not None
            assert rev["content_revision"] == r1["content_revision"]
            assert rev["governance_revision"] == r1["governance_revision"]

            # 不存在的 entity
            assert mutations.get_revisions(conn, "P-nonexistent") is None
        finally:
            conn.close()

    def test_compute_revision_deterministic(self):
        """同输入应产生同 revision。"""
        meta = {"id": "P-test", "type": "pitfall", "title": "标题", "scope": "s",
                "tags": ["a", "b"], "relations": [], "code_refs": []}
        body = "内容"
        r1 = mutations._compute_revision(meta, body)
        r2 = mutations._compute_revision(meta, body)
        assert r1 == r2
        assert len(r1) == 16

    def test_compute_revision_changes_with_content(self):
        """内容变化应导致 revision 变化。"""
        meta = {"id": "P-test", "type": "pitfall", "title": "标题", "scope": "s",
                "tags": [], "relations": [], "code_refs": []}
        r1 = mutations._compute_revision(meta, "内容A")
        r2 = mutations._compute_revision(meta, "内容B")
        assert r1 != r2


# ---------- gitops 错误分类 ----------

class TestGitopsErrorClassification:
    def test_auth_error_detection(self):
        assert _is_auth_error(RuntimeError("Permission denied (publickey)"))
        assert _is_auth_error(RuntimeError("Authentication failed"))
        assert _is_auth_error(RuntimeError("could not read username"))
        assert _is_auth_error(RuntimeError("invalid username or password"))
        assert _is_auth_error(RuntimeError("access denied"))
        assert not _is_auth_error(RuntimeError("non-fast-forward"))
        assert not _is_auth_error(RuntimeError("connection refused"))

    def test_network_error_detection(self):
        assert _is_network_error(RuntimeError("Could not resolve host github.com"))
        assert _is_network_error(RuntimeError("Connection refused"))
        assert _is_network_error(RuntimeError("Connection timed out"))
        assert _is_network_error(RuntimeError("Network is unreachable"))
        assert _is_network_error(RuntimeError("SSL certificate problem"))
        assert _is_network_error(RuntimeError("TLS error"))
        assert not _is_network_error(RuntimeError("Permission denied"))
        assert not _is_network_error(RuntimeError("non-fast-forward"))

    def test_push_race_detection(self):
        assert _is_push_race(RuntimeError("non-fast-forward"))
        assert _is_push_race(RuntimeError("fetch first"))
        assert _is_push_race(RuntimeError("failed to push some refs"))
        assert _is_push_race(RuntimeError("[rejected]"))
        assert not _is_push_race(RuntimeError("Permission denied"))
        assert not _is_push_race(RuntimeError("Could not resolve host"))


# ---------- sync_lease 跨进程协调 ----------

class TestSyncLease:
    def test_check_sync_lease_grants(self, isolated_repo):
        """首次检查应获得租约。"""
        can_sync, warning = _check_sync_lease(isolated_repo, "origin", "main", 60)
        assert can_sync is True

    def test_check_sync_lease_blocks_other(self, isolated_repo):
        """租约未过期时，其他进程应被阻止。"""
        # 先获取租约
        can_sync_1, _ = _check_sync_lease(isolated_repo, "origin", "main", 3600)
        assert can_sync_1 is True

        # 第二次检查（模拟另一个进程）应被阻止
        can_sync_2, warning = _check_sync_lease(isolated_repo, "origin", "main", 60)
        assert can_sync_2 is False
        assert "租约" in warning

    def test_release_sync_lease(self, isolated_repo):
        """释放租约后应允许重新获取。"""
        _check_sync_lease(isolated_repo, "origin", "main", 3600)
        _release_sync_lease(isolated_repo, "origin", "main", success=True)

        # 释放后应可重新获取
        can_sync, _ = _check_sync_lease(isolated_repo, "origin", "main", 60)
        assert can_sync is True

    def test_expired_lease_allows_new_owner(self, isolated_repo):
        """过期租约应允许新持有者接管。"""
        from knowbase import index as idx
        conn = idx.connect(isolated_repo)
        now = datetime.now(timezone.utc)
        past = (now - timedelta(hours=1)).isoformat(timespec="seconds")
        conn.execute(
            "INSERT OR REPLACE INTO sync_lease(repo,remote,branch,lease_owner,lease_until,last_attempt) "
            "VALUES(?,?,?,?,?,?)",
            (str(isolated_repo.resolve()), "origin", "main", "old-owner", past, past),
        )
        conn.commit()
        conn.close()

        can_sync, _ = _check_sync_lease(isolated_repo, "origin", "main", 60)
        assert can_sync is True


# ---------- record_sync_attempt ----------

class TestRecordSyncAttempt:
    def test_record_and_query(self, isolated_repo):
        conn = index.connect(isolated_repo)
        try:
            attempt_id = record_sync_attempt(
                isolated_repo, "origin", "main", "abc123",
                result="ok", duration_ms=150,
                confirmed_revision="def456",
            )
            assert attempt_id.startswith("SA-")

            row = conn.execute(
                "SELECT result, duration_ms, confirmed_revision FROM sync_attempts WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
            assert row is not None
            assert row[0] == "ok"
            assert row[1] == 150
            assert row[2] == "def456"
        finally:
            conn.close()

    def test_record_with_error(self, isolated_repo):
        conn = index.connect(isolated_repo)
        try:
            attempt_id = record_sync_attempt(
                isolated_repo, "origin", "main", "abc123",
                result="error", duration_ms=5000,
                error="connection timeout",
            )
            assert attempt_id.startswith("SA-")

            row = conn.execute(
                "SELECT result, error FROM sync_attempts WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
            assert row[0] == "error"
            assert "timeout" in row[1]
        finally:
            conn.close()


# ---------- migrations v6 ----------

class TestMigrationsV6:
    def test_applied_operations_table_exists(self, isolated_repo):
        conn = index.connect(isolated_repo)
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            assert "applied_operations" in tables
            assert "entity_heads" in tables
        finally:
            conn.close()

    def test_applied_operations_schema(self, isolated_repo):
        conn = index.connect(isolated_repo)
        try:
            cols = {row[1] for row in conn.execute(
                "PRAGMA table_info(applied_operations)"
            ).fetchall()}
            assert "op_id" in cols
            assert "entity_id" in cols
            assert "request_id" in cols
            assert "payload_hash" in cols
            assert "kind" in cols
            assert "base_revision" in cols
            assert "new_revision" in cols
        finally:
            conn.close()

    def test_entity_heads_schema(self, isolated_repo):
        conn = index.connect(isolated_repo)
        try:
            cols = {row[1] for row in conn.execute(
                "PRAGMA table_info(entity_heads)"
            ).fetchall()}
            assert "entity_id" in cols
            assert "content_revision" in cols
            assert "governance_revision" in cols
        finally:
            conn.close()
