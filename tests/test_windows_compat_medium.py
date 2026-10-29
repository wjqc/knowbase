"""Windows/macOS 跨平台中严重度修复回归（审计 M-1…M-9）。

覆盖：
- 散落 JSON/配置读写统一 UTF-8
- os.replace 退避重试（fsutil.replace_with_retry）与唯一临时文件名
- Windows worker_running 读取真实运行状态
- PowerShell 进程枚举强制 UTF-8 输出与失败转 UpdateError
- 执行器管道 UTF-8；Windows 正常结束后清理进程树
- BOM 卡片/来源文件不再被静默跳过
- mapped_scope 识别 Windows 绝对路径
- Trae 在 Windows 的 APPDATA 配置路径不再被 home 边界拒绝
"""
import inspect
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from knowbase import config, fsutil, sessions, store
from knowbase.updates import engine
from knowbase.updates import entry as update_entry
from knowbase.updates import bridge as update_bridge


# ---------- M-2：replace_with_retry 与唯一临时名 ----------

def test_replace_with_retry_backs_off_on_windows(monkeypatch, tmp_path):
    calls = []

    def flaky(src, dst):
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError("file in use")

    monkeypatch.setattr(fsutil.os, "replace", flaky)
    monkeypatch.setattr(fsutil.os, "name", "nt")
    monkeypatch.setattr(fsutil.time, "sleep", lambda s: None)
    fsutil.replace_with_retry(tmp_path / "a", tmp_path / "b")
    assert len(calls) == 3  # 前两次锁定被退避重试克服


def test_replace_with_retry_raises_after_exhaustion(monkeypatch, tmp_path):
    calls = []

    def locked(src, dst):
        calls.append(1)
        raise PermissionError("file in use")

    monkeypatch.setattr(fsutil.os, "replace", locked)
    monkeypatch.setattr(fsutil.os, "name", "nt")
    monkeypatch.setattr(fsutil.time, "sleep", lambda s: None)
    with pytest.raises(PermissionError):
        fsutil.replace_with_retry(tmp_path / "a", tmp_path / "b", attempts=3)
    assert len(calls) == 3


def test_replace_with_retry_fails_fast_on_posix(monkeypatch, tmp_path):
    calls = []

    def denied(src, dst):
        calls.append(1)
        raise PermissionError("real permission problem")

    monkeypatch.setattr(fsutil.os, "replace", denied)
    monkeypatch.setattr(fsutil.os, "name", "posix")
    with pytest.raises(PermissionError):
        fsutil.replace_with_retry(tmp_path / "a", tmp_path / "b")
    assert len(calls) == 1


def test_sessions_write_json_uses_unique_temp_names(monkeypatch, tmp_path):
    sources = []
    real_replace = os.replace
    monkeypatch.setattr(os, "replace",
                        lambda src, dst: sources.append(Path(src).name) or real_replace(src, dst))
    target = tmp_path / "sessions.json"
    sessions._write_json(target, {"agent": "trae-中文"})
    sessions._write_json(target, {"agent": "claude"})
    # 固定临时名会让并发 MCP 进程互相截断/覆盖
    assert sources[0] != sources[1]
    assert not list(tmp_path.glob("*.tmp"))  # 替换后无残留
    assert json.loads(target.read_text(encoding="utf-8")) == {"agent": "claude"}


def test_team_save_config_uses_unique_temp_and_retry(monkeypatch, tmp_path):
    from knowbase.team import service
    cfg_path = tmp_path / "config.json"
    monkeypatch.setattr(config, "CONFIG_PATH", cfg_path)
    sources = []
    real_replace = os.replace
    monkeypatch.setattr(os, "replace",
                        lambda src, dst: sources.append(Path(src).name) or real_replace(src, dst))
    service._save_config({"git": {"remote": {"url": "https://示例.invalid/x.git"}}})
    service._save_config({"git": {"remote": {"url": ""}}})
    assert sources[0] != sources[1]
    assert not list(tmp_path.glob("*.team-tmp"))


def test_commit_adapter_write_goes_through_retry_helper(monkeypatch, tmp_path):
    from knowbase import commit_adapter
    seen = []
    monkeypatch.setattr(commit_adapter, "replace_with_retry",
                        lambda src, dst: seen.append((src, dst)) or os.replace(src, dst))
    target = tmp_path / "card.md"
    commit_adapter._atomic_write(target, "---\nid: T-1\n---\n\n正文\n")
    assert seen and seen[0][1] == target
    assert target.read_bytes().endswith("正文\n".encode("utf-8"))


# ---------- M-3：worker_running 读取真实运行状态 ----------

def _bare_engine(cfg=None):
    updater = engine.Engine.__new__(engine.Engine)
    updater.cfg = cfg or {"manage_worker": True}
    return updater


def test_worker_running_windows_reports_actual_state(monkeypatch):
    monkeypatch.setattr(engine, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(engine, "os", SimpleNamespace(name="nt", environ=os.environ))
    monkeypatch.setattr(engine, "command", lambda *a, **kw: "Running".encode())
    assert _bare_engine().worker_running() is True
    monkeypatch.setattr(engine, "command", lambda *a, **kw: "Ready".encode())
    assert _bare_engine().worker_running() is False  # 已注册 ≠ 正在运行


def test_worker_running_windows_unregistered_task_is_false(monkeypatch):
    monkeypatch.setattr(engine, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(engine, "os", SimpleNamespace(name="nt", environ=os.environ))

    def failed(*a, **kw):
        raise engine.UpdateError("subprocess_failed")

    monkeypatch.setattr(engine, "command", failed)
    assert _bare_engine().worker_running() is False


# ---------- M-4：PowerShell 进程枚举 ----------

def test_windows_process_enumeration_parses_utf8_command_lines(monkeypatch):
    monkeypatch.setattr(engine, "os", SimpleNamespace(name="nt", environ=os.environ))
    payload = json.dumps([{"ProcessId": 7, "CommandLine": r"C:\工具\python.exe -m knowbase serve"}],
                         ensure_ascii=False).encode("utf-8")
    monkeypatch.setattr(engine, "command", lambda *a, **kw: payload)
    rows = _bare_engine().processes()
    assert rows == [(7, r"C:\工具\python.exe -m knowbase serve")]


def test_windows_process_enumeration_forces_utf8_output(monkeypatch):
    monkeypatch.setattr(engine, "os", SimpleNamespace(name="nt", environ=os.environ))
    scripts = []
    monkeypatch.setattr(engine, "command",
                        lambda args, **kw: scripts.append(args[-1]) or b"[]")
    _bare_engine().processes()
    # PS 5.1 管道默认走 OEM 代码页；必须先强制会话输出 UTF-8
    assert "[Console]::OutputEncoding=[Text.Encoding]::UTF8" in scripts[0]


def test_windows_process_enumeration_wraps_parse_failure(monkeypatch):
    monkeypatch.setattr(engine, "os", SimpleNamespace(name="nt", environ=os.environ))
    monkeypatch.setattr(engine, "command", lambda *a, **kw: b"<html>not json")
    with pytest.raises(engine.UpdateError):
        _bare_engine().processes()  # 不能以裸 ValueError/UnicodeDecodeError 冒出


# ---------- M-5/M-6：执行器管道编码与正常结束清理进程树 ----------

def test_executor_pipes_pin_utf8():
    from knowbase.automation import executor
    assert 'encoding="utf-8", errors="replace"' in inspect.getsource(executor._run_version)
    assert 'encoding="utf-8", errors="replace"' in inspect.getsource(executor.CommandExecutorRunner.run_text)


def test_executor_cleans_tree_on_windows_after_normal_exit(monkeypatch):
    from knowbase.automation import executor
    from knowbase import gitops
    taskkill = []

    class FakeProc:
        pid = 424242
        returncode = 0
        def communicate(self, *a, **kw):
            return ("{}", "")
        def poll(self):
            return 0
        def kill(self):
            pass

    monkeypatch.setattr(executor.subprocess, "Popen", lambda *a, **kw: FakeProc())
    monkeypatch.setattr(executor.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)
    monkeypatch.setattr(executor, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(gitops, "_IS_WINDOWS", True)
    monkeypatch.setattr(gitops.subprocess, "run",
                        lambda args, **kw: taskkill.append(args) or SimpleNamespace(returncode=1))
    runner = executor.CommandExecutorRunner.__new__(executor.CommandExecutorRunner)
    runner.binary, runner.noninteractive_argv, runner.timeout, runner.cfg = "x", [], 5, {}
    try:
        runner.run_text(SimpleNamespace(job_id="j", scope="s", protocol_version=1), "中文材料")
    except Exception:
        pass  # finally 已执行；下游解析与本次断言无关
    assert any("taskkill" in str(args[0]) for args in taskkill)  # Windows 正常结束也收尾


def test_isolation_kills_tree_unconditionally():
    from knowbase.automation import isolation
    source = Path(inspect.getsourcefile(isolation)).read_text(encoding="utf-8")
    assert 'if os.name != "nt":\n                _kill_tree' not in source


def test_engine_command_cleans_tree_after_normal_exit_on_windows(monkeypatch):
    taskkill = []

    class FakeProc:
        pid = 424243
        returncode = 0
        def communicate(self, **kw):
            return (b"out", b"")
        def poll(self):
            return 0  # 已正常退出：旧实现会跳过 Windows 收尾

    monkeypatch.setattr(engine, "os", SimpleNamespace(name="nt", environ=os.environ))
    monkeypatch.setattr(engine, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(engine.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)
    monkeypatch.setattr(engine.subprocess, "Popen", lambda args, **kw: FakeProc())
    monkeypatch.setattr(engine.subprocess, "run",
                        lambda args, **kw: taskkill.append(args) or SimpleNamespace(returncode=1))
    assert engine.command(["git", "fetch"]) == b"out"
    assert any("taskkill" in str(args[0]) for args in taskkill)


# ---------- M-7：BOM 容忍 ----------

def test_store_parse_accepts_bom_prefixed_card(tmp_path):
    card = tmp_path / "T-2026-001.md"
    card.write_bytes("\ufeff---\nid: T-2026-001\ntitle: 带BOM的卡\n---\n\n正文\n".encode("utf-8"))
    meta, body = store.parse(card)  # 修复前：BOM 让 startswith("---") 失败，整卡被跳过
    assert meta["id"] == "T-2026-001"
    assert body.strip() == "正文"


def test_index_source_decode_strips_bom():
    from knowbase import index
    source = Path(inspect.getsourcefile(index)).read_text(encoding="utf-8")
    assert 'raw.decode("utf-8-sig")' in source


# ---------- M-8：mapped_scope 识别 Windows 路径 ----------

def test_mapped_scope_matches_windows_absolute_keys(monkeypatch):
    monkeypatch.setattr(config, "os", SimpleNamespace(name="nt"))
    cfg = {"scope_map": {r"C:\Users\q\Proj": "work"}}
    # 盘符大小写与斜杠方向不同也必须命中同一映射
    assert config.mapped_scope("c:/users/q/PROJ", cfg) == "work"
    assert config.mapped_scope(r"C:\Users\q\PROJ", cfg) == "work"


def test_mapped_scope_keeps_posix_alias_resolution(tmp_path):
    cfg = {"scope_map": {str(tmp_path / "alias"): "alias-scope"}}
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "alias"
    link.symlink_to(real)
    assert config.mapped_scope(str(real), cfg) == "alias-scope"


# ---------- M-9：Trae APPDATA 配置根 ----------

def test_apply_accepts_trae_appdata_path_outside_home(tmp_path, monkeypatch):
    from knowbase import integration
    home = tmp_path / "home"
    home.mkdir()
    appdata = tmp_path / "roaming"  # 模拟企业策略把 APPDATA 重定向到 home 之外
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setattr(integration, "sys", SimpleNamespace(platform="win32"))
    changes = integration.plan(["trae"], home=home, managed=True)
    assert any(Path(c["path"]).is_absolute() for c in changes)
    result = integration.apply(changes, home=home)  # 修复前：CONFIG_OUTSIDE_HOME
    assert result["state"] in ("applied", "changed", "unchanged", "configured")
    assert (appdata / "Trae CN/User/mcp.json").exists()


# ---------- M-1：散落读写统一 UTF-8 ----------

def test_shared_json_io_pins_utf8():
    from knowbase import gitops, integration, model_setup
    from knowbase.value import recovery
    gitops_src = Path(gitops.__file__).read_text(encoding="utf-8")
    # gitops：.git 元数据读取（commdir/HEAD/config）显式 UTF-8 + replace
    assert gitops_src.count('read_text(encoding="utf-8", errors="replace")') >= 3
    # integration：updates.json 读取
    assert "read_text(encoding=\"utf-8\")" in Path(integration.__file__).read_text(encoding="utf-8")
    # engine/bridge：原子写与读取
    engine_src = Path(engine.__file__).read_text(encoding="utf-8")
    assert "temp.open('x',encoding='utf-8')" in engine_src
    assert "Path(path).read_text(encoding='utf-8')" in engine_src
    bridge_src = Path(update_bridge.__file__).read_text(encoding="utf-8")
    assert "temp.open('x',encoding='utf-8')" in bridge_src
    assert "drain.read_text(encoding='utf-8')" in bridge_src
    # entry：两处 fdopen 写
    entry_src = Path(update_entry.__file__).read_text(encoding="utf-8")
    assert entry_src.count("os.fdopen(fd,'w',encoding='utf-8')") == 2
    # value/recovery 与 model_setup
    assert "path.read_text(encoding='utf-8')" in Path(recovery.__file__).read_text(encoding="utf-8")
    assert "p.read_text(encoding='utf-8')" in Path(model_setup.__file__).read_text(encoding="utf-8")
    # bootstrap：requirements 写出
    bootstrap_src = (Path(__file__).parent.parent / "scripts" / "bootstrap_auto_update.py").read_text(encoding="utf-8")
    assert "requirements.write_text" in bootstrap_src and "encoding='utf-8'" in bootstrap_src


def test_gitops_sync_target_hash_reads_utf8_git_config(tmp_path):
    from knowbase import gitops
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git/HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (repo / ".git/config").write_text('[user]\n\tname = 中文成员\n', encoding="utf-8")
    digest = gitops.sync_target_hash(repo, {"remote": {}}, "origin", "main")
    assert len(digest) == 64  # 中文 config 不再让读取抛错
