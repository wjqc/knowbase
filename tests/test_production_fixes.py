import json
import subprocess
from pathlib import Path

import pytest

from knowbase import config, gitops, hooks
from knowbase.__main__ import cmd_import, cmd_init, cmd_promote, cmd_revise
from knowbase.locking import RepoLock
from knowbase.server import save_impl, search_impl, update_impl


@pytest.fixture()
def isolated_repo(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    cfg_path = tmp_path / "config.json"
    cfg = config._deep_merge(config.DEFAULTS, {
        "repo_path": str(repo),
        "knowledge_path": "",
        "git": {"auto_commit": True, "auto_push": False, "auto_pull": False},
    })
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", cfg_path)
    monkeypatch.setenv("KNOWBASE_REPO_PATH", str(repo))
    monkeypatch.setenv("KNOWBASE_AGENT_NAME", "pytest-agent")
    assert cmd_init() == 0
    return repo, cfg_path


def test_human_source_parameter_cannot_bypass_staging(isolated_repo):
    repo, _ = isolated_repo
    result = save_impl(
        "standard", "不可伪造的人工标准",
        "## 规则\n必须审核。\n\n## 理由\n防止越权。",
        source="human:forged",
    )
    mid = result.split()[1]
    assert (repo / "staging" / f"{mid}.md").exists()
    assert not (repo / "standards" / f"{mid}.md").exists()


def test_agent_cannot_update_active_governed_record(isolated_repo):
    result = save_impl(
        "standard", "正式规则禁止 Agent 修改",
        "## 规则\n必须审核。\n\n## 理由\n防止越权。",
    )
    mid = result.split()[1]
    assert cmd_promote(mid) == 0
    denied = update_impl(mid, body="## 规则\n已被篡改。\n\n## 理由\n无。")
    assert denied.startswith("错误：")
    assert "人工" in denied


def test_trusted_cli_can_revise_active_governed_record(isolated_repo, tmp_path):
    result = save_impl(
        "standard", "人工修订正式标准",
        "## 规则\n旧规则。\n\n## 理由\n旧理由。",
    )
    mid = result.split()[1]
    assert cmd_promote(mid) == 0
    body_file = tmp_path / "revision.md"
    body_file.write_text("## 规则\n新规则。\n\n## 理由\n人工确认。", encoding="utf-8")
    assert cmd_revise(mid, str(body_file)) == 0
    assert "新规则" in (isolated_repo[0] / "standards" / f"{mid}.md").read_text(encoding="utf-8")


def test_imported_governed_content_is_always_staged(isolated_repo, tmp_path):
    repo, _ = isolated_repo
    source = tmp_path / "standards-source"
    source.mkdir()
    (source / "rule.md").write_text("# 导入标准\n\n## 规则\n先审核。\n\n## 理由\n防越权。", encoding="utf-8")
    assert cmd_import(str(source), "standard", "demo", False, "human:import") == 0
    assert list((repo / "staging").glob("S-*.md"))
    assert not list((repo / "standards").glob("S-*.md"))


def test_save_commits_index_and_leaves_clean_worktree(isolated_repo):
    repo, _ = isolated_repo
    result = save_impl(
        "pitfall", "事务顺序验证",
        "## 现象\n索引脏。\n\n## 原因\n提交过早。\n\n## 正确做法\n最后提交。",
    )
    assert result.startswith("已保存")
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert status == ""


def test_push_is_only_scheduled_after_repo_lock_is_released(isolated_repo, monkeypatch):
    repo, cfg_path = isolated_repo
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["git"]["auto_push"] = True
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    called = []

    def schedule_without_outer_lock(target, *_args, **_kwargs):
        with RepoLock(target, timeout=0.2):
            called.append(True)
        return None

    monkeypatch.setattr("knowbase.server.gitops.schedule_push", schedule_without_outer_lock)
    monkeypatch.setattr("knowbase.server.gitops.push",
                        lambda *_a, **_k: pytest.fail("write path must not perform network push"))
    result = save_impl(
        "pitfall", "推送不持有仓库锁",
        "## 现象\n写入阻塞。\n\n## 原因\npush 在锁内。\n\n## 正确做法\n释放锁后推送。",
    )
    assert result.startswith("已保存")
    assert called == [True]


def test_project_context_requires_resolvable_scope(isolated_repo, monkeypatch, tmp_path):
    project = tmp_path / "unknown-project"
    project.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    result = search_impl("任意查询")
    assert result.startswith("错误：")
    assert "scope" in result


def test_hook_uses_unified_search_for_chinese_prompt(isolated_repo, monkeypatch):
    called = []

    def fake_search(_conn, query, **kwargs):
        called.append((query, kwargs.get("scope")))
        return []

    monkeypatch.setattr("knowbase.hooks.index.search", fake_search)
    hooks.user_prompt("日报怎么写才规范")
    assert called == [("日报怎么写才规范", "global")]


def test_push_validates_actual_remote_not_declared_url(isolated_repo):
    repo, _ = isolated_repo
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "https://public.invalid/repo.git"],
                   check=True)
    result = gitops.push(repo, "http://allowed.internal/repo.git", ["http://allowed.internal/"])
    assert "实际 remote" in result
    assert "不在白名单" in result


def test_sync_uses_dynamic_branch_and_holds_repo_lock(isolated_repo, monkeypatch):
    repo, _ = isolated_repo
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "file:///tmp/remote.git"],
                   check=True)
    observed = []

    def fake_run(target, *args, **_kwargs):
        if args[:3] == ("remote", "get-url", "origin"):
            return "file:///tmp/remote.git"
        if args[:4] == ("symbolic-ref", "--quiet", "--short", "HEAD"):
            return "trunk"
        if args[:2] == ("status", "--porcelain"):
            return ""
        if args[:3] == ("fetch", "origin", "trunk"):
            from knowbase.locking import LockTimeout
            with pytest.raises(LockTimeout):
                with RepoLock(target, timeout=0.05):
                    pass
            observed.append("locked")
            return ""
        if args[:2] == ("rev-parse", "HEAD"):
            return "abc"
        if args[:2] == ("rev-parse", "origin/trunk"):
            return "abc"
        raise AssertionError(args)

    monkeypatch.setattr(gitops, "_run", fake_run)
    cfg = config.load_config()
    cfg["git"]["auto_pull"] = True
    cfg["git"]["allowed_remote_prefixes"] = ["file://"]
    changed, warning = gitops.sync_before_read(repo, cfg)
    assert (changed, warning) == (False, None)
    assert observed == ["locked"]


def test_sync_pushes_local_ahead_branch_after_releasing_lock(isolated_repo, monkeypatch):
    repo, _ = isolated_repo
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "file:///tmp/remote.git"],
                   check=True)
    pushed = []

    def fake_run(_target, *args, **_kwargs):
        if args[:3] == ("remote", "get-url", "origin"):
            return "file:///tmp/remote.git"
        if args[:4] == ("symbolic-ref", "--quiet", "--short", "HEAD"):
            return "trunk"
        if args[:2] == ("status", "--porcelain") or args[:3] == ("fetch", "origin", "trunk"):
            return ""
        if args[:2] == ("rev-parse", "HEAD"):
            return "local"
        if args[:2] == ("rev-parse", "origin/trunk"):
            return "remote"
        if args[:3] == ("merge-base", "--is-ancestor", "local"):
            raise RuntimeError("local is not ancestor of remote")
        if args[:3] == ("merge-base", "--is-ancestor", "remote"):
            return ""
        raise AssertionError(args)

    def fake_push(target, *_args, **_kwargs):
        with RepoLock(target, timeout=0.2):
            pushed.append(True)
        return None

    monkeypatch.setattr(gitops, "_run", fake_run)
    monkeypatch.setattr(gitops, "push", fake_push)
    cfg = config.load_config()
    cfg["git"]["auto_pull"] = True
    cfg["git"]["allowed_remote_prefixes"] = ["file://"]
    changed, warning = gitops.sync_before_read(repo, cfg)
    assert (changed, warning) == (False, None)
    assert pushed == [True]
