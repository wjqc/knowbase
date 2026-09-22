import json
import subprocess
from pathlib import Path

import pytest

from knowbase import config, gitops, hooks, sources
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
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", cfg_path)
    monkeypatch.setenv("KNOWBASE_REPO_PATH", str(repo))
    monkeypatch.setenv("KNOWBASE_AGENT_NAME", "pytest-agent")
    assert cmd_init() == 0
    return repo, cfg_path


STANDARD_CARD = (
    "## 结论\n必须审核。\n\n"
    "## 解决的问题\n防止越权。\n\n"
    "## 适用条件\n- 正式规则发布\n\n## 不适用条件\n- staging 提案\n\n"
    "## 可执行动作\n提交审核单后发布。\n\n"
    "## 关键证据\n治理规范条目。\n\n"
    "## 验证情况\n2026-09-21 治理评审确认。\n\n"
    "## 未知与待确认\n暂无。\n"
)


def test_human_source_parameter_cannot_bypass_staging(isolated_repo):
    repo, _ = isolated_repo
    result = save_impl("standard", "不可伪造的人工标准", STANDARD_CARD,
                       source="human:forged")
    mid = result.split()[1]
    assert (repo / "staging" / f"{mid}.md").exists()
    assert not (repo / "standards" / f"{mid}.md").exists()


def test_agent_cannot_update_active_governed_record(isolated_repo):
    result = save_impl("standard", "正式规则禁止 Agent 修改", STANDARD_CARD)
    mid = result.split()[1]
    assert cmd_promote(mid) == 0
    denied = update_impl(mid, body="## 规则\n已被篡改。\n\n## 理由\n无。")
    assert denied.startswith("错误：")
    assert "人工" in denied


def test_trusted_cli_can_revise_active_governed_record(isolated_repo, tmp_path):
    result = save_impl("standard", "人工修订正式标准", STANDARD_CARD)
    mid = result.split()[1]
    assert cmd_promote(mid) == 0
    body_file = tmp_path / "revision.md"
    body_file.write_text(STANDARD_CARD.replace("必须审核。", "必须复核并双签。"), encoding="utf-8")
    assert cmd_revise(mid, str(body_file)) == 0
    assert "必须复核并双签。" in (isolated_repo[0] / "standards" / f"{mid}.md").read_text(encoding="utf-8")


def test_import_creates_source_instead_of_governed_cards(isolated_repo, tmp_path):
    repo, _ = isolated_repo
    source = tmp_path / "standards-source"
    source.mkdir()
    (source / "rule.md").write_text("# 导入标准\n\n## 规则\n先审核。\n\n## 理由\n防越权。", encoding="utf-8")
    assert cmd_import(str(source), "standard", "demo", False, "human:import") == 0
    assert list((repo / sources.MANIFESTS_DIR).glob("SRC-*.yaml"))
    assert not list((repo / "staging").glob("S-*.md"))
    assert not list((repo / "standards").glob("S-*.md"))


def test_save_commits_index_and_leaves_clean_worktree(isolated_repo):
    repo, _ = isolated_repo
    result = save_impl(
        "pitfall", "事务顺序验证",
        "## 结论\n索引脏即提交过早。\n\n## 解决的问题\n保证 Markdown 与索引一致后再入 Git。\n\n"
        "## 适用条件\n- auto_commit 开启\n\n## 不适用条件\n- 只读操作\n\n"
        "## 可执行动作\n按 同步索引→INDEX→commit 顺序执行。\n\n"
        "## 关键证据\n脏索引事故复盘。\n\n## 验证情况\n2026-09-21 测试环境确认。\n\n"
        "## 未知与待确认\n暂无。\n",
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
        "## 结论\npush 必须在仓库锁外执行。\n\n## 解决的问题\n避免网络耗时阻塞同仓写入。\n\n"
        "## 适用条件\n- auto_push 开启\n\n## 不适用条件\n- 本地单机\n\n"
        "## 可执行动作\n写锁释放后再调度 push。\n\n"
        "## 关键证据\n锁内 push 超时事故记录。\n\n"
        "## 验证情况\n2026-09-21 测试环境确认。\n\n## 未知与待确认\n暂无。\n",
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


def test_sync_rebases_diverged_branch_when_changes_do_not_overlap(isolated_repo, monkeypatch):
    repo, _ = isolated_repo
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "file:///tmp/remote.git"],
                   check=True)
    calls = []
    pushed = []

    def fake_run(_target, *args, **_kwargs):
        calls.append(args)
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
        if args[:2] == ("merge-base", "--is-ancestor"):
            raise RuntimeError("not ancestor")
        if args[:2] == ("rebase", "origin/trunk"):
            return ""
        raise AssertionError(args)

    monkeypatch.setattr(gitops, "_run", fake_run)
    monkeypatch.setattr("knowbase.index.rebuild", lambda _repo: None)
    monkeypatch.setattr(gitops, "push", lambda *_a, **_k: pushed.append(True))
    cfg = config.load_config()
    cfg["git"]["auto_pull"] = True
    cfg["git"]["allowed_remote_prefixes"] = ["file://"]

    changed, warning = gitops.sync_before_read(repo, cfg)

    assert (changed, warning) == (True, None)
    assert ("rebase", "origin/trunk") in calls
    assert pushed == [True]


def test_sync_aborts_rebase_and_reports_only_real_content_conflicts(isolated_repo, monkeypatch):
    repo, _ = isolated_repo
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "file:///tmp/remote.git"],
                   check=True)
    calls = []

    def fake_run(_target, *args, **_kwargs):
        calls.append(args)
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
        if args[:2] == ("merge-base", "--is-ancestor"):
            raise RuntimeError("not ancestor")
        if args[:2] == ("rebase", "origin/trunk"):
            raise RuntimeError("CONFLICT (content): Merge conflict in memories/P-1.md")
        if args[:3] == ("diff", "--name-only", "--diff-filter=U"):
            return "memories/P-1.md"
        if args[:2] == ("rebase", "--abort"):
            return ""
        raise AssertionError(args)

    monkeypatch.setattr(gitops, "_run", fake_run)
    monkeypatch.setattr(gitops, "push", lambda *_a, **_k: pytest.fail("conflict must not push"))
    cfg = config.load_config()
    cfg["git"]["auto_pull"] = True
    cfg["git"]["allowed_remote_prefixes"] = ["file://"]

    changed, warning = gitops.sync_before_read(repo, cfg)

    assert changed is False
    assert "内容冲突" in warning
    assert "memories/P-1.md" in warning
    assert ("rebase", "--abort") in calls


def test_push_fetches_rebases_and_retries_when_remote_moves_first(isolated_repo, monkeypatch):
    repo, _ = isolated_repo
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "file:///tmp/remote.git"],
                   check=True)
    calls = []
    push_attempts = 0

    def fake_run(_target, *args, **_kwargs):
        nonlocal push_attempts
        calls.append(args)
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
        if args[:2] == ("merge-base", "--is-ancestor"):
            raise RuntimeError("diverged")
        if args[:2] == ("rebase", "origin/trunk"):
            return ""
        if args[:2] == ("push", "-u"):
            push_attempts += 1
            if push_attempts == 1:
                raise RuntimeError("rejected (non-fast-forward)")
            return ""
        raise AssertionError(args)

    monkeypatch.setattr(gitops, "_run", fake_run)
    monkeypatch.setattr(gitops, "_record_sync", lambda *_a, **_k: None)

    warning = gitops.push(repo, "file:///tmp/remote.git", ["file://"])

    assert warning is None
    assert push_attempts == 2
    assert calls.count(("fetch", "origin", "trunk")) == 2


def test_real_two_clones_push_non_overlapping_commits_without_manual_merge(
        isolated_repo, tmp_path, monkeypatch):
    _repo, _ = isolated_repo
    remote = tmp_path / "remote.git"
    alice = tmp_path / "alice"
    bob = tmp_path / "bob"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(remote), str(alice)], check=True, capture_output=True)
    for clone, name in ((alice, "Alice"),):
        subprocess.run(["git", "-C", str(clone), "config", "user.name", name], check=True)
        subprocess.run(["git", "-C", str(clone), "config", "user.email", f"{name.lower()}@test"], check=True)
    (alice / "base.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(alice), "add", "base.md"], check=True)
    subprocess.run(["git", "-C", str(alice), "commit", "-m", "base"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(alice), "branch", "-M", "main"], check=True)
    subprocess.run(["git", "-C", str(alice), "push", "-u", "origin", "main"],
                   check=True, capture_output=True)
    subprocess.run(["git", "--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/main"],
                   check=True)
    subprocess.run(["git", "clone", str(remote), str(bob)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(bob), "config", "user.name", "Bob"], check=True)
    subprocess.run(["git", "-C", str(bob), "config", "user.email", "bob@test"], check=True)

    (alice / "alice.md").write_text("alice\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(alice), "add", "alice.md"], check=True)
    subprocess.run(["git", "-C", str(alice), "commit", "-m", "alice"], check=True,
                   capture_output=True)
    (bob / "bob.md").write_text("bob\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(bob), "add", "bob.md"], check=True)
    subprocess.run(["git", "-C", str(bob), "commit", "-m", "bob"], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(bob), "push", "origin", "main"], check=True,
                   capture_output=True)
    monkeypatch.setattr(gitops, "_record_sync", lambda *_a, **_k: None)

    warning = gitops.push(alice, "", [])

    assert warning is None
    subprocess.run(["git", "-C", str(bob), "pull", "--ff-only"], check=True,
                   capture_output=True)
    assert (bob / "alice.md").read_text(encoding="utf-8") == "alice\n"
    assert (bob / "bob.md").read_text(encoding="utf-8") == "bob\n"
