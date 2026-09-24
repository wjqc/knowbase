"""L1 测试：提交适配器 + 崩溃恢复 + CAS + 幂等。

覆盖：
- commit_adapter.py: adapter_create/update/feedback 带 intent 持久化
- 崩溃恢复：recover_intents 扫描 pending intent
- 幂等：同 request_id 不重复创建
- CAS：revision 冲突检测
- 原子写：临时文件 + rename
- 回执：intent 状态记录
- CLI revision 检查：--expect-revision 参数
"""

import json
from pathlib import Path

import pytest

from knowbase import config, index, commit_adapter, store
from knowbase.__main__ import cmd_init, cmd_verify, cmd_archive
from knowbase.mutations import REVISION_CONFLICT, IDEMPOTENCY_CONFLICT


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


# ---------- write_intents 表 ----------

class TestWriteIntentsTable:
    def test_table_exists(self, isolated_repo):
        conn = index.connect(isolated_repo)
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            assert "write_intents" in tables
        finally:
            conn.close()

    def test_schema(self, isolated_repo):
        conn = index.connect(isolated_repo)
        try:
            cols = {row[1] for row in conn.execute(
                "PRAGMA table_info(write_intents)"
            ).fetchall()}
            assert "intent_id" in cols
            assert "request_id" in cols
            assert "entity_id" in cols
            assert "kind" in cols
            assert "payload_hash" in cols
            assert "status" in cols
            assert "result" in cols
        finally:
            conn.close()


# ---------- adapter_create ----------

class TestAdapterCreate:
    def test_create_success(self, isolated_repo):
        conn = index.connect(isolated_repo)
        try:
            result = commit_adapter.adapter_create(
                isolated_repo, conn, "pitfall", "测试标题",
                "## 结论\n内容\n## 解决的问题\n问题",
                scope="test-proj",
            )
            assert result["created"] is True
            assert result["entity_id"].startswith("P-")
            assert "content_revision" in result
            assert "governance_revision" in result
        finally:
            conn.close()

    def test_create_idempotent(self, isolated_repo):
        """同 request_id + 同 payload 应幂等返回既有结果。"""
        conn = index.connect(isolated_repo)
        try:
            r1 = commit_adapter.adapter_create(
                isolated_repo, conn, "pitfall", "幂等测试",
                "## 结论\n内容\n## 解决的问题\n问题",
                scope="test-proj", request_id="REQ-001",
            )
            assert r1["created"] is True

            r2 = commit_adapter.adapter_create(
                isolated_repo, conn, "pitfall", "幂等测试",
                "## 结论\n内容\n## 解决的问题\n问题",
                scope="test-proj", request_id="REQ-001",
            )
            assert r2.get("idempotent") is True
            assert r2["entity_id"] == r1["entity_id"]
        finally:
            conn.close()

    def test_create_idempotency_conflict(self, isolated_repo):
        """同 request_id + 不同 payload 应返回 IDEMPOTENCY_CONFLICT。"""
        conn = index.connect(isolated_repo)
        try:
            r1 = commit_adapter.adapter_create(
                isolated_repo, conn, "pitfall", "标题A",
                "## 结论\n内容A\n## 解决的问题\n问题",
                scope="test-proj", request_id="REQ-002",
            )
            assert r1["created"] is True

            r2 = commit_adapter.adapter_create(
                isolated_repo, conn, "pitfall", "标题B",
                "## 结论\n内容B\n## 解决的问题\n问题",
                scope="test-proj", request_id="REQ-002",
            )
            assert r2.get("error") == IDEMPOTENCY_CONFLICT
        finally:
            conn.close()

    def test_intent_persisted(self, isolated_repo):
        """创建后 intent 应被标记为 completed。"""
        conn = index.connect(isolated_repo)
        try:
            result = commit_adapter.adapter_create(
                isolated_repo, conn, "pitfall", "intent 测试",
                "## 结论\n内容\n## 解决的问题\n问题",
                scope="test-proj", request_id="REQ-003",
            )
            assert result["created"] is True

            row = conn.execute(
                "SELECT status, result FROM write_intents WHERE request_id=?",
                ("REQ-003",),
            ).fetchone()
            assert row is not None
            assert row[0] == "completed"
            assert result["entity_id"] in row[1]
        finally:
            conn.close()


# ---------- adapter_update ----------

class TestAdapterUpdate:
    def test_update_cas_success(self, isolated_repo):
        """正确 revision 的 CAS 更新应成功。"""
        conn = index.connect(isolated_repo)
        try:
            r1 = commit_adapter.adapter_create(
                isolated_repo, conn, "pitfall", "CAS 测试",
                "## 结论\n原始内容\n## 解决的问题\n问题",
                scope="test-proj",
            )
            mid = r1["entity_id"]
            content_rev = r1["content_revision"]

            r2 = commit_adapter.adapter_update(
                isolated_repo, conn, mid,
                expected_content_revision=content_rev,
                body="## 结论\n更新后内容\n## 解决的问题\n问题",
            )
            assert r2["updated"] is True
            assert r2["entity_id"] == mid
            assert r2["content_revision"] != content_rev
        finally:
            conn.close()

    def test_update_revision_conflict(self, isolated_repo):
        """错误 revision 的 CAS 更新应返回 REVISION_CONFLICT。"""
        conn = index.connect(isolated_repo)
        try:
            r1 = commit_adapter.adapter_create(
                isolated_repo, conn, "pitfall", "CAS 冲突测试",
                "## 结论\n原始内容\n## 解决的问题\n问题",
                scope="test-proj",
            )
            mid = r1["entity_id"]

            r2 = commit_adapter.adapter_update(
                isolated_repo, conn, mid,
                expected_content_revision="wrong_revision_0",
                body="## 结论\n更新后内容\n## 解决的问题\n问题",
            )
            assert r2.get("error") == REVISION_CONFLICT
        finally:
            conn.close()

    def test_update_idempotent(self, isolated_repo):
        """同 request_id + 同 payload 的 update 应幂等。"""
        conn = index.connect(isolated_repo)
        try:
            r1 = commit_adapter.adapter_create(
                isolated_repo, conn, "pitfall", "幂等更新测试",
                "## 结论\n内容\n## 解决的问题\n问题",
                scope="test-proj",
            )
            mid = r1["entity_id"]
            content_rev = r1["content_revision"]

            u1 = commit_adapter.adapter_update(
                isolated_repo, conn, mid,
                expected_content_revision=content_rev,
                request_id="REQ-UPD-001",
                body="## 结论\n新内容\n## 解决的问题\n问题",
            )
            assert u1["updated"] is True

            u2 = commit_adapter.adapter_update(
                isolated_repo, conn, mid,
                expected_content_revision=u1["content_revision"],
                request_id="REQ-UPD-001",
                body="## 结论\n新内容\n## 解决的问题\n问题",
            )
            assert u2.get("idempotent") is True
        finally:
            conn.close()


# ---------- adapter_feedback ----------

class TestAdapterFeedback:
    def test_feedback_success(self, isolated_repo):
        conn = index.connect(isolated_repo)
        try:
            r1 = commit_adapter.adapter_create(
                isolated_repo, conn, "pitfall", "反馈测试",
                "## 结论\n内容\n## 解决的问题\n问题",
                scope="test-proj",
            )
            mid = r1["entity_id"]

            result = commit_adapter.adapter_feedback(
                isolated_repo, conn, mid, "helpful", "test-agent",
            )
            assert "governance_revision" in result
        finally:
            conn.close()

    def test_feedback_governance_revision_conflict(self, isolated_repo):
        """错误 governance_revision 的反馈应返回 REVISION_CONFLICT。"""
        conn = index.connect(isolated_repo)
        try:
            r1 = commit_adapter.adapter_create(
                isolated_repo, conn, "pitfall", "治理 CAS 测试",
                "## 结论\n内容\n## 解决的问题\n问题",
                scope="test-proj",
            )
            mid = r1["entity_id"]

            result = commit_adapter.adapter_feedback(
                isolated_repo, conn, mid, "helpful", "test-agent",
                expected_governance_revision="wrong_gov_rev",
            )
            assert result.get("error") == REVISION_CONFLICT
        finally:
            conn.close()


# ---------- 崩溃恢复 ----------

class TestCrashRecovery:
    def test_recover_pending_intent(self, isolated_repo):
        """pending intent 应被恢复。"""
        conn = index.connect(isolated_repo)
        try:
            # 先创建一个卡片
            r1 = commit_adapter.adapter_create(
                isolated_repo, conn, "pitfall", "恢复测试",
                "## 结论\n内容\n## 解决的问题\n问题",
                scope="test-proj",
            )
            mid = r1["entity_id"]

            # 手动插入一个 pending intent（模拟崩溃）
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            conn.execute(
                "INSERT INTO write_intents(intent_id,request_id,entity_id,scope,kind,"
                "payload_hash,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                ("INT-test12345678", "REQ-RECOVER", mid, "test-proj",
                 "update", "abc123", "pending", now),
            )
            conn.commit()

            # 执行恢复
            recovery = commit_adapter.recover_intents(isolated_repo, conn)
            assert recovery["recovered"] >= 1

            # 验证 intent 状态已更新
            row = conn.execute(
                "SELECT status FROM write_intents WHERE intent_id=?",
                ("INT-test12345678",),
            ).fetchone()
            assert row[0] == "completed"
        finally:
            conn.close()

    def test_recover_no_pending(self, isolated_repo):
        """无 pending intent 时恢复应返回空。"""
        conn = index.connect(isolated_repo)
        try:
            recovery = commit_adapter.recover_intents(isolated_repo, conn)
            assert recovery["recovered"] == 0
            assert recovery["failed"] == 0
        finally:
            conn.close()


# ---------- CLI revision 检查 ----------

class TestCLIRevisionCheck:
    def test_verify_with_correct_revision(self, isolated_repo, monkeypatch):
        """正确 revision 的 verify 应成功。"""
        conn = index.connect(isolated_repo)
        try:
            r1 = commit_adapter.adapter_create(
                isolated_repo, conn, "pitfall", "CLI verify 测试",
                "## 结论\n内容\n## 解决的问题\n问题",
                scope="test-proj",
            )
            mid = r1["entity_id"]
            gov_rev = r1["governance_revision"]

            # 正确 revision 应成功
            result = cmd_verify(mid, expect_revision=gov_rev)
            assert result == 0
        finally:
            conn.close()

    def test_verify_with_wrong_revision(self, isolated_repo, capsys):
        """错误 revision 的 verify 应失败。"""
        conn = index.connect(isolated_repo)
        try:
            r1 = commit_adapter.adapter_create(
                isolated_repo, conn, "pitfall", "CLI verify 冲突测试",
                "## 结论\n内容\n## 解决的问题\n问题",
                scope="test-proj",
            )
            mid = r1["entity_id"]

            # 错误 revision 应失败
            result = cmd_verify(mid, expect_revision="wrong_revision")
            assert result == 1

            captured = capsys.readouterr()
            assert "版本冲突" in captured.out
        finally:
            conn.close()

    def test_verify_without_revision(self, isolated_repo):
        """不携带 revision 的 verify 应成功（人工操作不强制）。"""
        conn = index.connect(isolated_repo)
        try:
            r1 = commit_adapter.adapter_create(
                isolated_repo, conn, "pitfall", "CLI verify 无 revision 测试",
                "## 结论\n内容\n## 解决的问题\n问题",
                scope="test-proj",
            )
            mid = r1["entity_id"]

            # 不携带 revision 应成功
            result = cmd_verify(mid)
            assert result == 0
        finally:
            conn.close()


# ---------- read_impl 返回 revision ----------

class TestReadImplRevision:
    def test_read_returns_revision(self, isolated_repo):
        """read_impl 应返回 content_revision 和 governance_revision。"""
        from knowbase.server import read_impl

        conn = index.connect(isolated_repo)
        try:
            r1 = commit_adapter.adapter_create(
                isolated_repo, conn, "pitfall", "read revision 测试",
                "## 结论\n内容\n## 解决的问题\n问题",
                scope="test-proj",
            )
            mid = r1["entity_id"]
            content_rev = r1["content_revision"]
            gov_rev = r1["governance_revision"]

            result = read_impl(mid)
            assert f"content_revision={content_rev}" in result
            assert f"governance_revision={gov_rev}" in result
        finally:
            conn.close()
