"""Windows/macOS 跨平台高严重度修复回归（审计 H-A…H-G）。

覆盖：
- stdio 固定 UTF-8（bridge/hooks/entry）
- hook 命令按平台拼接/解析（cmd.exe 不认 POSIX 单引号）
- 更新器读取源码文件的编码
- Windows 原地安装的锁退避重试
- 执行器程序名识别（反斜杠路径与 .CMD 后缀）
- 凭据文件权限检查的平台差异
- 共享 Git 仓库内容固定 UTF-8 + LF
"""
import builtins
import hashlib
import importlib.util
import inspect
import io
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from knowbase import config, store
from knowbase.__main__ import cmd_init
from knowbase.updates import bridge, engine
from knowbase.updates import entry as update_entry


# ---------- H-A：stdio 固定 UTF-8 ----------

def test_bridge_utf8_stdio_forces_utf8_and_lf(monkeypatch):
    stdin = io.TextIOWrapper(io.BytesIO(), encoding="cp936")
    stdout = io.TextIOWrapper(io.BytesIO(), encoding="cp936")
    monkeypatch.setattr(bridge, "sys", SimpleNamespace(stdin=stdin, stdout=stdout))
    bridge._utf8_stdio()
    assert stdin.encoding == "utf-8"
    stdout.write("中文\n行2\n")
    stdout.flush()
    assert stdout.buffer.getvalue() == "中文\n行2\n".encode("utf-8")  # 无 \r\n 注入


def test_bridge_backend_pipe_declares_utf8():
    # 后端管道必须与 MCP SDK（固定 UTF-8）一致，不能落到本地代码页。
    source = Path(bridge.__file__).read_text(encoding="utf-8")
    assert "text=True,encoding='utf-8'" in source


def test_hooks_emit_utf8_under_local_codepage(monkeypatch):
    stdin = io.TextIOWrapper(io.BytesIO(), encoding="cp936")
    stdout = io.TextIOWrapper(io.BytesIO(), encoding="cp936")
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)
    from knowbase import hooks
    hooks._emit("中文📈", "zcode")  # cp936 无法编码 emoji；未对齐 UTF-8 会抛 UnicodeEncodeError
    stdout.flush()
    payload = json.loads(stdout.buffer.getvalue().decode("utf-8"))
    assert payload["additionalContext"] == "中文📈"


def test_entry_reads_utf8_payload_under_local_codepage(tmp_path):
    """子进程内把 stdin 构造成 gbk 包装（等价中文 Windows 管道默认代码页）再运行 entry。

    不依赖 PYTHONIOENCODING：宿主注入的 PYTHONUTF8/PYTHONIOENCODING 会互相干扰，
    导致行为随父进程环境漂移；显式包装是确定性的。"""
    root = tmp_path / "runtime"
    root.mkdir()
    (root / "update-drain.json").write_text("{}", encoding="utf-8")
    runner = (
        "import io,runpy,sys\n"
        "raw=sys.stdin.buffer.read()\n"
        "sys.stdin=io.TextIOWrapper(io.BytesIO(raw),encoding='gbk')\n"
        "path=sys.argv[1]\n"
        "sys.argv=['entry.py','hook','session-start']\n"
        "runpy.run_path(path,run_name='__main__')\n"
    )
    payload = json.dumps({"session_id": "sess-1", "transcript_path": "/tmp/项目/会话.jsonl"},
                         ensure_ascii=False)
    result = subprocess.run(
        [sys.executable, "-c", runner, str(Path(update_entry.__file__))],
        input=payload.encode("utf-8"), capture_output=True,
        env={**os.environ, "KNOWBASE_RUNTIME_ROOT": str(root)}, timeout=30)
    assert result.returncode in (0, SystemExit(0).code), result.stderr.decode("utf-8", "replace")
    records = list((root / "deferred-hooks").glob("*.json"))
    assert len(records) == 1
    record = json.loads(records[0].read_text(encoding="utf-8"))
    # 未对齐 UTF-8 时中文路径会被 gbk 解码成乱码
    assert record["transcript_path"] == "/tmp/项目/会话.jsonl"
    assert record["session_id"] == "sess-1"


# ---------- H-B：hook 命令的平台拼接/解析 ----------

def _as_windows(monkeypatch, module):
    monkeypatch.setattr(module, "os", SimpleNamespace(name="nt"))


def test_join_command_uses_cmdline_rules_on_windows(monkeypatch):
    from knowbase import integration
    _as_windows(monkeypatch, integration)
    parts = [r"C:\Program Files\k\python.exe", "-I", "hook", "stop"]
    joined = integration._join_command(parts)
    assert joined == subprocess.list2cmdline(parts)
    assert "'" not in joined  # cmd.exe 不认 POSIX 单引号
    monkeypatch.setattr(integration, "os", SimpleNamespace(name="posix"))
    assert integration._join_command(parts) == " ".join(shlex.quote(x) for x in parts)


def test_split_command_preserves_windows_backslashes(monkeypatch):
    from knowbase import integration
    _as_windows(monkeypatch, integration)
    parts = [r"C:\Program Files\k\python.exe", "-I", "hook", "stop", "--style", "claude"]
    joined = subprocess.list2cmdline(parts)
    assert integration._split_command(joined) == parts
    # 未加引号的 Windows 路径：POSIX 规则会把反斜杠当转义符吃掉——这正是要避免的 bug
    raw = r"C:\Users\q\python.exe -I hook stop"
    assert integration._split_command(raw) == [r"C:\Users\q\python.exe", "-I", "hook", "stop"]
    assert shlex.split(raw) != integration._split_command(raw)


def test_same_command_roundtrip_on_windows(monkeypatch):
    from knowbase import integration
    _as_windows(monkeypatch, integration)
    parts = [r"C:\Users\q k\.knowbase\runtime\venv\Scripts\python.exe", "-I", "hook", "stop"]
    expected = {"type": "command", "command": integration._join_command(parts)}
    assert integration._same_command({"command": subprocess.list2cmdline(parts)}, expected) is True
    assert integration._same_command({"command": subprocess.list2cmdline(parts + ["extra"])}, expected) is False


def test_migrate_hook_rejoins_under_windows_rules(tmp_path, monkeypatch):
    from knowbase import integration
    _as_windows(monkeypatch, integration)
    home = tmp_path
    base = [integration.runtime_command(home, "win32"), *integration.hook_args(home, "stop", "claude", True)]
    expected = {"type": "command", "command": integration._join_command(base), "timeout": 1}
    legacy = {"type": "command",
              "command": integration._join_command(["env", "KNOWBASE_AGENT_NAME=x", *base])}
    assert integration._migrate_hook(legacy, expected, home, "stop", "claude") is True
    # 迁移保留身份 env 前缀，其余按新命令重排
    assert legacy["command"] == integration._join_command(["env", "KNOWBASE_AGENT_NAME=x", *base])
    assert "'" not in legacy["command"]


# ---------- H-C：更新器读取源码文件指定 UTF-8 ----------

def test_source_info_parses_utf8_project_metadata(tmp_path):
    home = tmp_path / "home"
    root = home / ".knowbase/runtime"
    python = root / ("venv/Scripts/python.exe" if os.name == "nt" else "venv/bin/python")
    python.parent.mkdir(parents=True)
    python.touch()
    cp = home / ".knowbase/config.json"
    engine.atomic(cp, {"repo_path": str(home / "knowledge")})
    source = tmp_path / "program"
    pkg = source / "knowbase"
    (pkg / "updates").mkdir(parents=True)
    (pkg / "__init__.py").write_text("__version__='9.9.9'\n", encoding="utf-8")
    (source / "pyproject.toml").write_text(
        '[project]\nname="knowbase"\nversion="9.9.9"\ndescription="跨平台知识库——中文描述"\n'
        'requires-python=">=3.10"\n[tool.knowbase.updates]\nprotocol=1\n', encoding="utf-8")
    for name in ["engine.py", "entry.py", "bridge.py"]:
        (pkg / "updates" / name).write_text("# fixture\n", encoding="utf-8")
    settings = home / ".knowbase/updates.json"
    engine.atomic(settings, {"home": str(home), "runtime_python": str(python),
                             "config_path": str(cp), "source_url": source.as_uri(),
                             "allow_file_source": True, "uv_path": sys.executable, "enabled": True})
    info = engine.Engine(settings).source_info(source)
    assert info["version"] == "9.9.9"


def test_source_info_pins_utf8_when_reading_sources():
    # 中文 Windows 默认编码不是 UTF-8；pyproject 含中文描述时裸 read_text 会解码失败。
    source = inspect.getsource(engine.Engine.source_info)
    assert "read_text(encoding='utf-8')" in source


# ---------- H-D：Windows 安装的锁退避重试 ----------

def _bare_engine():
    updater = engine.Engine.__new__(engine.Engine)
    updater.uv, updater.python = "uv", "python"
    return updater


def test_windows_install_retries_transient_lock(monkeypatch):
    calls = []

    def flaky(args, **kw):
        calls.append(1)
        if len(calls) < 3:
            raise engine.UpdateError("subprocess_failed")
        return b""

    monkeypatch.setattr(engine, "command", flaky)
    monkeypatch.setattr(engine, "os", SimpleNamespace(name="nt", environ=os.environ))
    monkeypatch.setattr(engine.time, "sleep", lambda s: None)
    _bare_engine().install(Path("w.whl"))
    assert len(calls) == 3


def test_windows_install_raises_after_retries_exhausted(monkeypatch):
    calls = []

    def always_fails(args, **kw):
        calls.append(1)
        raise engine.UpdateError("subprocess_failed")

    monkeypatch.setattr(engine, "command", always_fails)
    monkeypatch.setattr(engine, "os", SimpleNamespace(name="nt", environ=os.environ))
    monkeypatch.setattr(engine.time, "sleep", lambda s: None)
    with pytest.raises(engine.UpdateError):
        _bare_engine().install(Path("w.whl"))
    assert len(calls) == 3


def test_posix_install_fails_fast_without_retry(monkeypatch):
    calls = []

    def fails(args, **kw):
        calls.append(1)
        raise engine.UpdateError("subprocess_failed")

    monkeypatch.setattr(engine, "command", fails)
    monkeypatch.setattr(engine, "os", SimpleNamespace(name="posix", environ=os.environ))
    with pytest.raises(engine.UpdateError):
        _bare_engine().install(Path("w.whl"))
    assert len(calls) == 1


def test_bootstrap_command_retries_before_failing(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location(
        "bootstrap_retry_test", Path(__file__).parent.parent / "scripts" / "bootstrap_auto_update.py")
    bootstrap = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bootstrap)
    attempts = []

    def fake_run(args, **kw):
        attempts.append(1)
        return SimpleNamespace(returncode=0 if len(attempts) > 1 else 1, stdout=b"ok")

    monkeypatch.setattr(bootstrap.subprocess, "run", fake_run)
    monkeypatch.setattr(bootstrap.time, "sleep", lambda s: None)
    assert bootstrap.command(["uv", "pip", "install"], retries=2) == b"ok"
    assert len(attempts) == 2
    monkeypatch.setattr(bootstrap.subprocess, "run",
                        lambda args, **kw: SimpleNamespace(returncode=1, stdout=b""))
    with pytest.raises(RuntimeError):
        bootstrap.command(["uv", "pip", "install"], retries=1)


# ---------- H-E：执行器程序名识别 ----------

def test_program_key_handles_windows_paths():
    from knowbase.automation import executor
    assert executor._program_key(r"C:\Users\q\AppData\Roaming\npm\codex.CMD") == "codex"
    assert executor._program_key("/usr/local/bin/claude") == "claude"


def test_probe_recognizes_windows_cmd_shim(tmp_path, monkeypatch):
    from knowbase.automation import executor
    shim = tmp_path / "codex.CMD"
    shim.write_text("#!/bin/sh\n", encoding="utf-8")
    shim.chmod(0o755)
    monkeypatch.setattr(executor, "_run_version", lambda *a, **kw: "1.0.0")
    monkeypatch.setenv("CODEX_API_KEY", "x")
    report = executor.probe_command_executor({"command": str(shim)})
    assert report.headless is True
    assert report.noninteractive_auth is True


def test_command_executor_uses_stem_lookup(tmp_path):
    from knowbase.automation import executor
    shim = tmp_path / "codex.CMD"
    shim.write_text("#!/bin/sh\n", encoding="utf-8")
    shim.chmod(0o755)
    runner = executor.CommandExecutorRunner({"command": str(shim)})
    assert runner.noninteractive_argv == ["exec"]


# ---------- H-F：凭据文件权限检查的平台差异 ----------

def test_secret_file_mode_check_skipped_on_windows(tmp_path, monkeypatch):
    from knowbase import embedding_provider as ep
    secret = tmp_path / "sk.txt"
    secret.write_text("sk-中文-1\n", encoding="utf-8")
    secret.chmod(0o644)
    provider = ep.OpenAICompatProvider.__new__(ep.OpenAICompatProvider)
    provider.secret_ref = "file:" + str(secret)
    # Windows 的 st_mode 权限位恒非 0，检查必然误判 insecure
    monkeypatch.setattr(ep, "os", SimpleNamespace(name="nt"))
    assert provider._secret() == "sk-中文-1"
    # POSIX 行为不变：组/其他可读仍然拒绝
    monkeypatch.setattr(ep, "os", os)
    with pytest.raises(ep.ProviderError):
        provider._secret()


# ---------- H-G：共享仓库内容固定 UTF-8 + LF ----------

def test_write_shared_text_pins_lf_and_utf8(tmp_path, monkeypatch):
    recorded = {}
    real_open = builtins.open

    def spy(file, mode="r", *args, **kwargs):
        recorded.update(newline=kwargs.get("newline"), encoding=kwargs.get("encoding"))
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy)
    store.write_shared_text(tmp_path / "x.md", "a\n中文\nb\n")
    assert recorded == {"newline": "\n", "encoding": "utf-8"}
    assert (tmp_path / "x.md").read_bytes() == "a\n中文\nb\n".encode("utf-8")


def test_sources_object_filename_matches_content_bytes(tmp_path):
    from knowbase import sources
    text = "# 数据\n正文第二行\n"
    sid, sha, created = sources.import_source(
        tmp_path, text, title="中文标题", scope="", fmt="markdown",
        parser_version="t", imported_by="test")
    assert created is True
    raw = (tmp_path / "sources/objects" / f"{sha}.md").read_bytes()
    # 文件名按内存 LF 文本计算 sha；落盘字节必须与之一致（CRLF 会造成名实不符）
    assert hashlib.sha256(raw).hexdigest() == sha
    assert raw == text.encode("utf-8")
    manifest = (tmp_path / "sources/manifests" / f"{sid}.yaml").read_bytes()
    assert b"\r" not in manifest


def test_commit_adapter_atomic_write_keeps_lf(tmp_path):
    from knowbase import commit_adapter
    target = tmp_path / "card.md"
    commit_adapter._atomic_write(target, "---\nid: X\n---\n\n中文正文\n")
    raw = target.read_bytes()
    assert b"\r" not in raw
    assert raw.endswith("中文正文\n".encode("utf-8"))


def test_init_writes_gitattributes_pinned_to_lf(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    cfg_path = tmp_path / "config.json"
    cfg = config._deep_merge(config.DEFAULTS, {
        "repo_path": str(repo), "knowledge_path": "",
        "git": {"auto_commit": False, "auto_push": False, "auto_pull": False},
    })
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", cfg_path)
    monkeypatch.setenv("KNOWBASE_REPO_PATH", str(repo))
    monkeypatch.setenv("KNOWBASE_AGENT_NAME", "pytest-agent")
    assert cmd_init() == 0
    raw = (repo / ".gitattributes").read_bytes()
    assert b"\r" not in raw
    assert b"*.md text eol=lf" in raw
    assert b"\r" not in (repo / "README.md").read_bytes()
