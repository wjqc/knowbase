"""scope 自动绑定回归（2026-11-04 裁决）。

覆盖：git remote 派生 scope、同名不同库防融合、同仓库多 checkout 归一、
worktree（.git 为文件）、无 remote/非 git 目录按绝对路径、config 显式优先、
目录名精确等值优先于自动绑定、歧义不自动绑、sanitize/URL 形态边界、
hook 未映射回退 global 且不再整场跳过。
"""

import json
import subprocess
from pathlib import Path

import pytest

from knowbase import config, hooks, index, scope_binding

BODY = ("## 结论\nEasyConnect 7.6.7 在 macOS 26.1 上启动即 Rosetta 死锁，内网 vpn 全部连不上。\n\n"
        "## 解决的问题\n避免把启动死锁误判为系统故障反复重装。\n\n"
        "## 适用条件\n- macOS 26.1 arm64\n- EasyConnect 7.6.7\n\n"
        "## 不适用条件\n- Linux 客户端\n\n## 可执行动作\n1. 升级客户端\n2. netstat 验证路由\n\n"
        "## 关键证据\nnetstat -rn | grep ^172 无路由表项。\n\n"
        "## 验证情况\n2026-09-21 macOS 实机复现修复确认。\n\n"
        "## 未知与待确认\nWindows 平台未验证。\n")


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    """隔离的知识库与 config；阻断对生产 config/知识库的任何读取。"""
    root = tmp_path / "kb"
    root.mkdir()
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(
        {"repo_path": str(root), "hooks": {"enabled": True, "inject_min_confidence": "any"}}),
        encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", cfg_path)
    monkeypatch.setenv("KNOWBASE_REPO_PATH", str(root))
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("ZCODE_PROJECT_DIR", raising=False)
    from knowbase.__main__ import cmd_init
    assert cmd_init() == 0  # save_impl 要求仓库已初始化
    conn = index.connect(root)
    yield conn
    conn.close()


def _git(*args, cwd):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True,
                   timeout=30)


def _git_repo(base: Path, name: str, remote: str | None = None, commit: bool = False) -> Path:
    path = base / name
    path.mkdir(parents=True)
    _git("init", cwd=path)
    if remote:
        _git("remote", "add", "origin", remote, cwd=path)
    if commit:
        _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "--allow-empty",
             "-m", "init", cwd=path)
    return path


def test_auto_bind_remote_basename(repo, tmp_path):
    path = _git_repo(tmp_path, "order-center", "http://10.21.20.112:18084/product/cmi/order-center.git")
    result = scope_binding.auto_bind(repo, path)
    assert result["status"] == "resolved" and result["scope"] == "order-center"
    assert result["source"] == "auto_remote"
    rows = scope_binding.list_bindings(repo)
    assert len(rows) == 1 and rows[0]["scope"] == "order-center" and rows[0]["confirmed"]
    # 幂等：重复绑定不产生新行
    scope_binding.auto_bind(repo, path)
    assert len(scope_binding.list_bindings(repo)) == 1


def test_same_basename_different_remotes_no_fusion(repo, tmp_path):
    a = _git_repo(tmp_path, "portal-a", "http://10.21.20.112/product/team-a/portal.git")
    b = _git_repo(tmp_path, "portal-b", "http://10.21.20.112/product/team-b/portal.git")
    ra = scope_binding.auto_bind(repo, a)
    rb = scope_binding.auto_bind(repo, b)
    assert ra["scope"] == "portal"
    # 第二个同名不同库：basename 被占用 → 父目录前缀，绝不融合
    assert rb["scope"] == "team-b-portal"
    assert ra["scope"] != rb["scope"]


def test_same_remote_second_checkout_resolves_identity(repo, tmp_path):
    first = _git_repo(tmp_path, "c1", "http://git.internal/product/cmi/sales.git", commit=True)
    second = _git_repo(tmp_path, "c2", "http://git.internal/product/cmi/sales.git")
    assert scope_binding.auto_bind(repo, first)["scope"] == "sales"
    # 第二处 checkout：git 身份（remote 相同）直接解析，不新增绑定
    resolved = scope_binding.resolve_binding(repo, str(second))
    assert resolved["status"] == "resolved" and resolved["scope"] == "sales"
    assert resolved["source"] == "git_identity"
    assert len(scope_binding.list_bindings(repo)) == 1


def test_worktree_same_project_same_scope(repo, tmp_path):
    main = _git_repo(tmp_path, "main", "http://git.internal/product/x/portal.git", commit=True)
    wt = tmp_path / "wt"
    _git("worktree", "add", str(wt), cwd=main)
    assert scope_binding.auto_bind(repo, main)["scope"] == "portal"
    # worktree 的 .git 是文件；toplevel 指向 worktree 根；同 remote 不算占用
    result = scope_binding.auto_bind(repo, wt)
    assert result["status"] == "resolved" and result["scope"] == "portal"


def test_git_repo_without_remote_binds_by_path(repo, tmp_path):
    path = _git_repo(tmp_path, "local-only")
    result = scope_binding.auto_bind(repo, path)
    assert result["source"] == "auto_path" and result["scope"] == "local-only"


def test_non_git_dir_binds_by_path(repo, tmp_path):
    path = tmp_path / "随便的目录"
    path.mkdir()
    result = scope_binding.auto_bind(repo, path)
    assert result["source"] == "auto_path"
    # 中文目录名 sanitize 后为空 → 兜底 project
    assert result["scope"] == scope_binding._sanitize_scope(path.name) == "project"


def test_config_scope_map_wins(repo, tmp_path):
    path = _git_repo(tmp_path, "anything", "http://git.internal/o/other/zzz.git")
    cfg_path = config.CONFIG_PATH
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["scope_map"] = {str(path): "explicit-scope"}
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    assert hooks._infer_scope(repo, str(path)) == "explicit-scope"
    assert len(scope_binding.list_bindings(repo)) == 0


def test_name_exact_match_beats_autobind(repo, tmp_path):
    repo.execute("INSERT INTO meta(id,type,title,scope,tags,confidence,status,path,staging) "
                 "VALUES('T-1','pitfall','t','knowbase','[]','once','active','',0)")
    repo.commit()
    path = _git_repo(tmp_path, "knowbase", "http://git.internal/product/common/knowbase.git")
    assert hooks._infer_scope(repo, str(path)) == "knowbase"
    # 目录名精确等值优先于自动绑定：不产生绑定行
    assert len(scope_binding.list_bindings(repo)) == 0


def test_ambiguous_resolution_not_autobound(repo, tmp_path, monkeypatch):
    path = _git_repo(tmp_path, "amb", "http://git.internal/a/b/amb.git")
    monkeypatch.setattr(scope_binding, "resolve_binding",
                        lambda conn, p, cfg=None: {"status": "scope_unresolved", "scope": None,
                                                   "source": "", "reason": "ambiguous", "path": p})
    assert hooks._infer_scope(repo, str(path)) is None
    assert len(scope_binding.list_bindings(repo)) == 0


def test_sanitize_edges(repo):
    assert scope_binding._sanitize_scope("My_Proj.V2.git") == "my-proj-v2-git"
    assert scope_binding._sanitize_scope("项目") == "project"
    assert len(scope_binding._sanitize_scope("a" * 100)) == 64


def test_remote_url_forms(repo):
    segs = scope_binding._remote_segments("http://10.21.20.112:18084/product/common/knowbase.git/")
    assert segs[-1] == "knowbase.git" and segs[-2] == "common" and "18084" in segs
    segs = scope_binding._remote_segments("git@10.21.20.112:product/cmi/portal.git")
    assert segs[-1] == "portal.git" and segs[-2] == "cmi"
    # 卡片同名占用 → 父目录前缀
    repo.execute("INSERT INTO meta(id,type,title,scope,tags,confidence,status,path,staging) "
                 "VALUES('T-2','pitfall','t','portal','[]','once','active','',0)")
    repo.commit()
    name, how = scope_binding._derive_auto_name(repo, "git@10.21.20.112:product/cmi/portal.git")
    assert name == "cmi-portal" and how == "parent_prefixed"


def test_hook_unmapped_falls_back_to_global_and_searches(repo, tmp_path):
    from knowbase.server import save_impl
    save_impl("pitfall", "EasyConnect 7.6.7 在 macOS 上启动即死锁", BODY,
              tags=["network", "vpn"], source="agent:zcode:test-scope-autobind")
    project = _git_repo(tmp_path, "fresh-project", "http://git.internal/product/new/fresh-project.git")
    text = hooks.user_prompt("帮我看看内网 vpn 连不上的问题", project_dir=str(project))
    # 不再出现"已跳过自动检索"；global 卡经 (scope=派生 OR global) 召回并注入
    assert "已跳过自动检索" not in text
    assert "[knowbase 自动检索]" in text and "EasyConnect" in text
    bindings = scope_binding.list_bindings(repo)
    assert len(bindings) == 1 and bindings[0]["scope"] == "fresh-project"
    detail = repo.execute("SELECT detail FROM usage_log ORDER BY rowid DESC LIMIT 1").fetchone()[0]
    payload = json.loads(detail)
    assert payload["scope"] == "fresh-project"
    assert payload["retrieval"]["scope_resolved"] is True
    # 第二次：绑定已存在，不再新增（路径祖先匹配，纯 DB 读）
    hooks.user_prompt("内网 vpn 死锁", project_dir=str(project / "sub" / "dir"))
    assert len(scope_binding.list_bindings(repo)) == 1


def test_hook_non_git_dir_uses_global_scope(repo, tmp_path):
    from knowbase.server import save_impl
    save_impl("pitfall", "EasyConnect 7.6.7 在 macOS 上启动即死锁", BODY,
              tags=["network", "vpn"], source="agent:zcode:test-scope-autobind")
    project = tmp_path / "普通目录"
    project.mkdir()
    text = hooks.user_prompt("帮我看看内网 vpn 连不上的问题", project_dir=str(project))
    assert "[knowbase 自动检索]" in text and "EasyConnect" in text
    bindings = scope_binding.list_bindings(repo)
    assert len(bindings) == 1 and bindings[0]["source"] == "auto_path"
    detail = repo.execute("SELECT detail FROM usage_log ORDER BY rowid DESC LIMIT 1").fetchone()[0]
    assert json.loads(detail)["scope"] == "project"  # sanitize("普通目录") 兜底值
