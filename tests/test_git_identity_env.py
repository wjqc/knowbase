"""宿主注入的 Git 身份环境变量不得覆盖项目提交身份。

背景：Trae/IDE/CI 会向宿主进程注入 GIT_AUTHOR_* 与 GIT_COMMITTER_*。
这些环境变量在 Git 中的优先级高于 user.name/user.email 配置，也高于
命令行 `git -c user.name=...`，因此会静默改写提交身份——企业 AIQGATE
类网关按提交者邮箱域校验时会直接拒绝推送。

本文件锁定三件事：
1. 子进程环境里四个身份变量被剔除，父进程 os.environ 不受影响；
2. 普通知识库提交使用仓库/全局 Git 配置的身份；
3. 团队统计提交使用显式指定的身份，并能通过企业邮箱域 pre-receive 校验。
"""

import os
import subprocess
import sys
from pathlib import Path

from knowbase import gitops
from knowbase.team.transport import GitData, TransportError

HOST_NAME = "trae-solo-agent"
HOST_EMAIL = "solo-agent@trae.ai"


def _inject_host_identity(monkeypatch):
    """模拟宿主注入的 Git 身份环境变量。"""
    monkeypatch.setenv("GIT_AUTHOR_NAME", HOST_NAME)
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", HOST_EMAIL)
    monkeypatch.setenv("GIT_COMMITTER_NAME", HOST_NAME)
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", HOST_EMAIL)


def test_git_env_drops_host_identity_variables(monkeypatch):
    _inject_host_identity(monkeypatch)
    env = gitops._git_env()
    for name in gitops._IDENTITY_ENV_VARS:
        assert name not in env
    # 父进程环境不得被修改：其他组件仍可能读取宿主身份。
    assert os.environ["GIT_COMMITTER_EMAIL"] == HOST_EMAIL


def test_run_git_child_process_never_sees_host_identity(monkeypatch):
    _inject_host_identity(monkeypatch)
    probe = (
        "import os;"
        "print('|'.join(sorted(k for k in os.environ if k.startswith('GIT_'))))"
    )
    code, out, err = gitops._run_git([sys.executable, "-c", probe], 10)
    assert code == 0, err
    for name in gitops._IDENTITY_ENV_VARS:
        assert name not in out
    # 非身份相关的 GIT_* 变量照常传递。
    assert "GIT_TERMINAL_PROMPT" in out


def test_commit_all_uses_repo_identity_over_host_env(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "repo-user"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "repo-user@corp.example"],
                   check=True)
    _inject_host_identity(monkeypatch)

    (repo / "card.md").write_text("body\n", encoding="utf-8")
    assert gitops.commit_all(repo, "add card") is None

    code, out, _ = gitops._run_git(
        ["git", "-C", str(repo), "log", "-1", "--format=%an|%ae|%cn|%ce"], 10)
    assert code == 0
    assert out.strip() == "repo-user|repo-user@corp.example|repo-user|repo-user@corp.example"
    # 宿主身份未被写回父进程。
    assert os.environ["GIT_COMMITTER_EMAIL"] == HOST_EMAIL


def _email_gate(bare: Path, domain: str):
    """模拟企业 AIQGATE 类 pre-receive 邮箱域校验。"""
    hook = bare / "hooks" / "pre-receive"
    hook.write_text(
        '#!/bin/sh\n'
        'zero=0000000000000000000000000000000000000000\n'
        'while read old new ref; do\n'
        '  [ "$new" = "$zero" ] && continue\n'
        '  email=$(git log -1 --format=%ce "$new")\n'
        '  case "$email" in\n'
        f'    *@{domain}) ;;\n'
        '    *) echo "Email domain should be ' + domain + '. Your current email is $email" >&2; exit 1;;\n'
        '  esac\n'
        'done\n'
        'exit 0\n',
        encoding="utf-8",
    )
    hook.chmod(0o755)


def test_team_statistics_commit_uses_explicit_identity_over_host_env(tmp_path: Path, monkeypatch):
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    _email_gate(bare, "corp.example")
    url = bare.as_uri()
    _inject_host_identity(monkeypatch)

    git = GitData(tmp_path / "gitdata", url, [url],
                  identity=("erin", "erin@corp.example"))
    commit = git.commit(None, {"devices/a/epoch-a/2026-10-23.json": '{"calls":1}'})
    git.push("knowbase-stats", commit)

    code, out, _ = gitops._run_git(
        ["git", "--git-dir", str(bare), "log", "-1", "--format=%ce", "refs/heads/knowbase-stats"], 10)
    assert code == 0
    assert out.strip() == "erin@corp.example"


def test_team_statistics_default_identity_is_still_rejected_by_gate(tmp_path: Path, monkeypatch):
    """对照组：未指定真实身份时仍应被网关拒绝，不因清理环境变量而放行。"""
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    _email_gate(bare, "corp.example")
    url = bare.as_uri()
    _inject_host_identity(monkeypatch)

    git = GitData(tmp_path / "gitdata", url, [url])
    commit = git.commit(None, {"devices/a/epoch-a/2026-10-23.json": '{"calls":1}'})
    try:
        git.push("knowbase-stats", commit)
    except TransportError:
        return
    raise AssertionError("合成默认身份不应通过企业邮箱域校验")
