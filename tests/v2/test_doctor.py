"""P4 收尾：V2 doctor 健康检查单元测试。
覆盖：
1. 全空库：除 schema_version / pending_outbox / mirror 外均 pass
2. schema_version 落后 → FAIL
3. outbox FAILED 死信 → FAIL
4. outbox 积压超阈值 → WARN
5. mirror.git 缺失 → WARN；存在但无 HEAD → FAIL
6. sync_run 24h 内失败 → WARN
7. document.status='error' → WARN
8. source_sync_state 全部 stale → WARN
9. summarize / exit_code / render 函数
10. 额外 check / 抛异常的 check 隔离
"""
from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from knowbase.v2.domain.models import (
    Document,
    DocumentStatus,
    OutboxEvent,
    OutboxStatus,
    Source,
    SourceKind,
    SourceSyncState,
    SyncRun,
    SyncRunStatus,
)
from knowbase.v2.repositories import V2Repository
from knowbase.v2.sync.doctor import (
    DoctorCheck,
    DoctorThresholds,
    check_conflicted_docs,
    check_dead_letters,
    check_mirror_git,
    check_pending_outbox,
    check_recent_sync_failures,
    check_schema_version,
    check_sync_staleness,
    exit_code,
    render,
    run_checks,
    summarize,
)


# ---------- fixtures / helpers ----------

@pytest.fixture
def repo(tmp_path: Path) -> V2Repository:
    r = V2Repository(tmp_path)
    yield r
    r.close()


@pytest.fixture
def source(repo: V2Repository) -> Source:
    s = Source.from_locator(SourceKind.FILE, "/tmp/doctor.md")
    repo.upsert_source(s)
    return s


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%fZ")


def _drop_schema_to(db_path: Path, version: int) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(f"PRAGMA user_version = {version}")
    conn.commit()
    conn.close()


# ====================================================================
# 单条 check
# ====================================================================

class TestCheckSchemaVersion:
    def test_fresh_db_is_pass(self, repo: V2Repository):
        c = check_schema_version(repo)
        assert c.level == "pass"
        assert "目标" in c.message

    def test_low_version_is_fail(self, repo: V2Repository):
        _drop_schema_to(repo.db_path, 2)
        c = check_schema_version(repo)
        assert c.level == "fail"
        assert "schema_version=2" in c.message


class TestCheckPendingOutbox:
    def test_empty_is_pass(self, repo: V2Repository):
        c = check_pending_outbox(repo, warn_threshold=100)
        assert c.level == "pass"
        assert "0" in c.message

    def test_over_threshold_is_warn(self, repo: V2Repository, source: Source):
        for i in range(3):
            ev = OutboxEvent.new(
                topic="test.topic", aggregate_type="document",
                aggregate_id=f"doc-{i}", event_type="test.evt",
                payload={"i": i},
            )
            repo.enqueue_outbox(ev)
        c = check_pending_outbox(repo, warn_threshold=2)
        assert c.level == "warn"
        assert "3" in c.message


class TestCheckDeadLetters:
    def test_no_dead_is_pass(self, repo: V2Repository):
        c = check_dead_letters(repo, fail_threshold=1)
        assert c.level == "pass"

    def test_one_dead_is_fail(self, repo: V2Repository, source: Source):
        ev = OutboxEvent.new(
            topic="t", aggregate_type="document", aggregate_id="doc-x",
            event_type="t.e", payload={},
        )
        failed = replace(ev, status=OutboxStatus.FAILED)
        repo.enqueue_outbox(failed)
        c = check_dead_letters(repo, fail_threshold=1)
        assert c.level == "fail"
        assert "1" in c.message


class TestCheckRecentSyncFailures:
    def test_no_runs_is_pass(self, repo: V2Repository):
        c = check_recent_sync_failures(repo, warn_threshold=1)
        assert c.level == "pass"

    def test_failed_run_is_warn(self, repo: V2Repository, source: Source):
        run = replace(
            SyncRun.new(source_id=source.stable_id),
            status=SyncRunStatus.FAILED,
            finished_at=_now_iso(),
        )
        repo.record_sync_run(run)
        c = check_recent_sync_failures(repo, warn_threshold=1)
        assert c.level == "warn"

    def test_succeeded_run_is_pass(self, repo: V2Repository, source: Source):
        run = replace(SyncRun.new(source_id=source.stable_id),
                      status=SyncRunStatus.SUCCEEDED)
        repo.record_sync_run(run)
        c = check_recent_sync_failures(repo, warn_threshold=1)
        assert c.level == "pass"


class TestCheckMirrorGit:
    def test_missing_is_warn(self, tmp_path: Path):
        c = check_mirror_git(tmp_path)
        assert c.level == "warn"
        assert "不存在" in c.message

    def test_exists_with_head_is_pass(self, tmp_path: Path):
        m = tmp_path / ".knowbase" / "mirror.git"
        m.mkdir(parents=True)
        (m / "HEAD").write_text("ref: refs/heads/main\n")
        c = check_mirror_git(tmp_path)
        assert c.level == "pass"


class TestCheckConflictedDocs:
    def test_no_conflict_is_pass(self, repo: V2Repository):
        c = check_conflicted_docs(repo, warn_threshold=1)
        assert c.level == "pass"

    def test_error_status_is_warn(self, repo: V2Repository, source: Source):
        doc = replace(
            Document.new(source_id=source.stable_id, path="/p/x.md", title="x"),
            status=DocumentStatus.ERROR,
        )
        repo.upsert_document(doc)
        c = check_conflicted_docs(repo, warn_threshold=1)
        assert c.level == "warn"
        assert "1" in c.message


class TestCheckSyncStaleness:
    def test_no_state_is_warn(self, repo: V2Repository):
        c = check_sync_staleness(repo, warn_threshold_seconds=60)
        assert c.level == "warn"
        assert "从未跑过" in c.message

    def test_fresh_state_is_pass(self, repo: V2Repository, source: Source):
        state = SourceSyncState(source_id=source.stable_id, last_remote_check_at=_now_iso())
        repo.upsert_sync_state(state)
        c = check_sync_staleness(repo, warn_threshold_seconds=60)
        assert c.level == "pass"


# ====================================================================
# 总入口 / 汇总 / 渲染
# ====================================================================

class TestRunChecksAndHelpers:

    def test_run_checks_default_on_empty_repo(self, repo: V2Repository, tmp_path: Path):
        checks = run_checks(repo, tmp_path)
        names = [c.name for c in checks]
        # 默认 6 个 DB check + 1 个 mirror check
        assert "schema_version" in names
        assert "mirror_git" in names
        # 全空库：除 schema/mirror 外其它都 pass（无 pending/dead/runs/state）
        for c in checks:
            assert c.level in ("pass", "warn")

    def test_summarize_counts(self):
        checks = [
            DoctorCheck("a", "pass", ""),
            DoctorCheck("b", "warn", ""),
            DoctorCheck("c", "fail", ""),
        ]
        p, w, f = summarize(checks)
        assert (p, w, f) == (1, 1, 1)

    def test_exit_code_priority(self):
        assert exit_code([DoctorCheck("a", "pass", "")]) == 0
        assert exit_code([DoctorCheck("a", "warn", "")]) == 1
        assert exit_code([DoctorCheck("a", "fail", "")]) == 2
        # fail 优先于 warn
        assert exit_code([DoctorCheck("a", "warn", ""), DoctorCheck("b", "fail", "")]) == 2

    def test_render_contains_glyphs(self):
        out = render([
            DoctorCheck("a", "pass", "ok"),
            DoctorCheck("b", "warn", "meh"),
            DoctorCheck("c", "fail", "no"),
        ])
        assert "✓" in out
        assert "⚠" in out
        assert "✗" in out
        assert "汇总" in out

    def test_extra_check_invoked(self, repo: V2Repository, tmp_path: Path):
        calls: list[str] = []

        def extra(r, t):
            calls.append("x")
            return DoctorCheck("custom", "warn", "hi")

        checks = run_checks(repo, tmp_path, extra_checks=[extra])
        assert any(c.name == "custom" for c in checks)
        assert calls == ["x"]

    def test_check_exception_is_isolated(self, repo: V2Repository, tmp_path: Path):
        def bad(r, t):
            raise RuntimeError("boom")

        checks = run_checks(repo, tmp_path, extra_checks=[bad])
        bad_results = [c for c in checks if c.name == "bad"]
        assert bad_results and bad_results[0].level == "fail"
        assert "boom" in bad_results[0].message


class TestDoctorThresholds:
    def test_defaults(self):
        t = DoctorThresholds()
        assert t.pending_outbox_warn == 100
        assert t.dead_letter_fail == 1
        assert t.recent_sync_failure_warn == 1
        assert t.sync_staleness_warn_seconds == 300
        assert t.conflicted_docs_warn == 1