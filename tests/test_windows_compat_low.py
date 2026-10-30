"""Windows/macOS 跨平台低严重度修复回归（审计 L-1…L-8）。

覆盖：
- bootstrap 子进程不弹窗 + 超时整树回收
- launchctl gui/<uid> 不可用时回退 user/<uid>
- hook flag 文件名的 Windows 非法字符清理
- integration.recover 不在 Windows 上恢复 POSIX 权限位
- host_metadata 补 Windows 安装位置探测
- OCR：Vision 可用不再依赖 pytesseract；tesseract lang 可配置
- locking Windows 分支只把锁占用 errno 视为等待
- subprocess errors=replace / os.devnull / machine-id 编码 / INDEX.md LF
"""
import errno
import hashlib
import importlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from knowbase import hooks
from knowbase.team import worker as team_worker
from knowbase.updates import engine
from knowbase.updates import engine as engine_mod


def _load_bootstrap():
    spec = importlib.util.spec_from_file_location(
        "bootstrap_low_test", Path(__file__).parent.parent / "scripts" / "bootstrap_auto_update.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------- L-1：bootstrap 子进程 no_window + 超时整树回收 ----------

def test_bootstrap_command_hides_window_on_windows(monkeypatch):
    bootstrap = _load_bootstrap()
    seen = {}

    class FakeProc:
        returncode = 0
        pid = 0
        def communicate(self, timeout=None):
            return (b"", b"")
        def poll(self):
            return 0
        def kill(self):
            pass

    monkeypatch.setattr(bootstrap, "os", SimpleNamespace(name="nt", environ=os.environ))
    monkeypatch.setattr(bootstrap.subprocess, "Popen",
                        lambda args, **kw: seen.update(kw) or FakeProc())
    bootstrap.command(["git", "fetch"])
    assert seen["creationflags"] == 0x08000000  # CREATE_NO_WINDOW


def test_bootstrap_command_kills_tree_on_timeout(monkeypatch):
    bootstrap = _load_bootstrap()
    killed = []

    class SlowProc:
        returncode = 0
        pid = 424244
        def communicate(self, timeout=None):
            if timeout is not None and timeout < 5:
                raise subprocess.TimeoutExpired(cmd="slow", timeout=timeout)
            return (b"", b"")
        def poll(self):
            return None
        def kill(self):
            killed.append("kill")

    monkeypatch.setattr(bootstrap, "os", SimpleNamespace(name="nt", environ=os.environ))
    monkeypatch.setattr(bootstrap.subprocess, "Popen", lambda args, **kw: SlowProc())
    monkeypatch.setattr(bootstrap.subprocess, "run",
                        lambda args, **kw: killed.append(tuple(args)) or SimpleNamespace(returncode=0))
    with pytest.raises(RuntimeError, match="timed out"):
        bootstrap.command(["uv", "pip", "install"], timeout=1)
    # 修复前 subprocess.run 超时只杀直接子进程，孙进程残留；
    # Windows 上必须 taskkill /T 整树回收，不能只 proc.kill()
    assert any(isinstance(k, tuple) and k[0] == "taskkill" and "/T" in k for k in killed)


# ---------- L-2：launchctl gui/<uid> 回退 user/<uid> ----------

def _darwin_engine(monkeypatch, tmp_path):
    monkeypatch.setattr(engine_mod, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(engine_mod, "os", SimpleNamespace(name="posix", getuid=lambda: 501,
                                                          environ=os.environ))
    updater = engine.Engine.__new__(engine.Engine)
    updater.cfg = {"manage_worker": True}
    updater.home = tmp_path
    plist = tmp_path / "Library/LaunchAgents/com.knowbase.worker.plist"
    plist.parent.mkdir(parents=True)
    plist.write_bytes(b"plist")
    return updater


def test_engine_worker_bootstraps_user_domain_when_gui_missing(monkeypatch, tmp_path):
    updater = _darwin_engine(monkeypatch, tmp_path)
    calls = []

    def fake_command(args, **kw):
        calls.append(args[2])
        if len(calls) == 1:
            raise engine.UpdateError("gui domain unavailable")  # SSH / 无 GUI 会话
        return b""

    monkeypatch.setattr(engine_mod, "command", fake_command)
    updater.worker(True)
    assert calls == ["gui/501", "user/501"]  # 修复前：失败直接冒出，worker 不启动


def test_engine_worker_running_accepts_user_domain(monkeypatch, tmp_path):
    updater = _darwin_engine(monkeypatch, tmp_path)
    runs = []

    def fake_run(args, **kw):
        runs.append(args[2])
        return SimpleNamespace(returncode=0 if len(runs) > 1 else 1)

    monkeypatch.setattr(engine_mod.subprocess, "run", fake_run)
    assert updater.worker_running() is True
    assert runs == ["gui/501/com.knowbase.worker", "user/501/com.knowbase.worker"]
    monkeypatch.setattr(engine_mod.subprocess, "run",
                        lambda args, **kw: SimpleNamespace(returncode=1))
    assert updater.worker_running() is False


def test_team_worker_install_falls_back_to_user_domain(monkeypatch, tmp_path):
    calls = []

    def fake_execute(args, required=True):
        calls.append(tuple(args))
        if args[:2] == ["launchctl", "bootstrap"] and args[2].startswith("gui/"):
            raise RuntimeError("no gui domain")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(team_worker, "_execute", fake_execute)
    monkeypatch.setattr(team_worker, "fixed_python", lambda home=None, platform=None: tmp_path / "py")
    (tmp_path / "py").write_text("")
    monkeypatch.setattr(team_worker.config, "load_config",
                        lambda: {"team": {"data_dir": str(tmp_path / "team"), "interval_seconds": 900}})
    monkeypatch.setattr(team_worker.config, "CONFIG_PATH", tmp_path / "config.json")

    from knowbase.team import service
    monkeypatch.setattr(service, "root_path", lambda cfg: tmp_path / "team")
    monkeypatch.setattr(team_worker, "_backup", lambda root, paths: {p: None for p in paths})
    monkeypatch.setattr(team_worker, "_atomic",
                        lambda p, data: p.parent.mkdir(parents=True, exist_ok=True) or p.write_bytes(data))
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    result = team_worker.install()
    assert result["worker"] == "installed"
    bootstraps = [args[2] for args in calls if tuple(args[:2]) == ("launchctl", "bootstrap")]
    assert bootstraps[0].startswith("gui/") and bootstraps[1].startswith("user/")
    # bootout 覆盖两个域，卸载不残留
    bootouts = [args[2] for args in calls if tuple(args[:2]) == ("launchctl", "bootout")]
    assert any(d.startswith("gui/") for d in bootouts) and any(d.startswith("user/") for d in bootouts)


# ---------- L-3：hook flag 文件名清理 ----------

def test_hook_flag_paths_sanitize_windows_reserved_chars(tmp_path, monkeypatch):
    monkeypatch.setenv("KNOWBASE_HOOK_STATE", str(tmp_path))
    sid = "a:b*c?d<>|e/f"
    path = hooks._flag_path(sid)
    path.write_text("1", encoding="utf-8")  # Windows 非法字符会直接抛 OSError
    assert path.name == "knowbase-hook-a_b_c_d___e_f.flag"
    assert hooks._flag_path(sid) == path  # 同 session 稳定映射
    mid = hooks._mid_capture_flag_path(sid)
    mid.write_text("1", encoding="utf-8")
    assert mid.name == "knowbase-mid-capture-a_b_c_d___e_f.flag"
    assert hooks._safe_session_token("x" * 300) == "x" * 120  # 超长截断


# ---------- L-4：recover 不在 Windows 恢复 POSIX 权限位 ----------

def test_recover_skips_posix_mode_on_windows(tmp_path, monkeypatch):
    from knowbase import integration
    home = tmp_path
    target = home / "cfg.json"
    target.write_text("new", encoding="utf-8")
    bdir = home / ".knowbase/backups/init-1"
    bdir.mkdir(parents=True)
    backup = bdir / "cfg.json.bak"
    backup.write_bytes(b"old")
    journal = {"state": "prepared", "files": [{
        "path": str(target), "mode": 0o444, "backup": str(backup),
        "before_hash": hashlib.sha256(b"old").hexdigest(),
        "after_hash": hashlib.sha256(b"new").hexdigest()}]}
    jp = bdir / "journal.json"
    jp.write_text(json.dumps(journal), encoding="utf-8")

    chmods = []
    # 不能改全局 os.name（pathlib 会随之实例化 WindowsPath）；
    # 只替换 integration 命名空间里的 os。
    monkeypatch.setattr(integration, "os",
                        SimpleNamespace(name="nt", chmod=lambda p, m: chmods.append(m),
                                        fsync=os.fsync, environ=os.environ))
    assert integration.recover(jp) == []
    assert target.read_bytes() == b"old"
    assert 0o444 not in chmods  # 修复前：Windows st_mode 0o444 会把文件设成只读
    assert 0o666 in chmods  # Windows 分支恢复为可写而非 POSIX 权限位


# ---------- L-5：host_metadata Windows 安装位置 ----------

def test_host_metadata_detects_windows_install_dir(tmp_path, monkeypatch):
    from knowbase import integration
    monkeypatch.setattr("shutil.which", lambda name: None)  # 屏蔽宿主机真实 PATH 命中

    def _no_plist(raw):
        raise ValueError("屏蔽宿主机 /Applications 真实应用 bundle")

    monkeypatch.setattr("plistlib.loads", _no_plist)
    base = tmp_path / "Local"
    exe = base / "Programs/Trae CN/Trae CN.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    env = {"LOCALAPPDATA": str(base), "PROGRAMFILES": str(tmp_path / "none")}
    monkeypatch.setattr(integration, "os", SimpleNamespace(name="nt", environ=env))
    monkeypatch.setattr(integration, "sys", SimpleNamespace(platform="win32"))
    meta = integration.host_metadata("trae", home=tmp_path)
    assert meta["installed"] is True  # 修复前：Windows 只能靠 PATH


def test_host_metadata_negative_when_not_installed_anywhere(tmp_path, monkeypatch):
    from knowbase import integration

    def _no_plist(raw):
        raise ValueError("测试宿主机无应用 bundle，屏蔽 /Applications 真实探测")

    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr("plistlib.loads", _no_plist)
    env = {"LOCALAPPDATA": str(tmp_path / "missing"), "PROGRAMFILES": str(tmp_path / "missing")}
    monkeypatch.setattr(integration, "os", SimpleNamespace(name="nt", environ=env))
    monkeypatch.setattr(integration, "sys", SimpleNamespace(platform="win32"))
    meta = integration.host_metadata("cursor", home=tmp_path)
    assert meta["installed"] is False


# ---------- L-6：OCR 引擎选择与语言配置 ----------

def test_ocr_vision_path_does_not_require_pytesseract(monkeypatch, tmp_path):
    from PIL import Image
    from knowbase.parsers import native_ocr, ocr_parser
    img = tmp_path / "pic.png"
    Image.new("RGB", (4, 4), "#ffffff").save(img)
    # sys.modules 置 None 使 `import pytesseract` 抛 ImportError，模拟未安装
    monkeypatch.setitem(sys.modules, "pytesseract", None)
    monkeypatch.setattr(ocr_parser, "_check_tesseract_binary", lambda: None)
    monkeypatch.setattr(native_ocr, "available", lambda: True)
    monkeypatch.setattr(native_ocr, "recognize", lambda p: "中文识别结果")
    doc = ocr_parser.OcrParser().parse(img)  # 修复前：先 import pytesseract 直接失败
    assert doc.text == "中文识别结果"


def test_ocr_tesseract_language_is_configurable(monkeypatch, tmp_path):
    from PIL import Image
    from knowbase.parsers import native_ocr, ocr_parser
    img = tmp_path / "pic.png"
    Image.new("RGB", (4, 4), "#ffffff").save(img)
    monkeypatch.setattr(ocr_parser, "_check_tesseract_binary", lambda: "/usr/bin/tesseract")
    monkeypatch.setattr(native_ocr, "available", lambda: False)
    recorded = {}

    fake = SimpleNamespace(image_to_string=lambda im, lang: recorded.update(lang=lang) or "eng text")
    monkeypatch.setitem(sys.modules, "pytesseract", fake)
    monkeypatch.setenv("KNOWBASE_OCR_LANG", "chi_sim+eng")
    doc = ocr_parser.OcrParser().parse(img)
    assert doc.text == "eng text"
    assert recorded["lang"] == "chi_sim+eng"  # 修复前：固定 lang="eng"，中文图片识别差


# ---------- L-7：locking Windows 分支 errno 细分 ----------

def _windows_locking_module(monkeypatch):
    """独立副本加载 Windows 分支：不 reload 全局 knowbase.locking，
    避免 msvcrt 假实现泄漏污染其他测试。"""
    fake_msvcrt = SimpleNamespace(LK_NBLCK=1, LK_UNLCK=2)
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)
    monkeypatch.setitem(sys.modules, "fcntl", None)  # `import fcntl` -> ImportError
    import knowbase
    spec = importlib.util.spec_from_file_location(
        "locking_windows_copy", Path(knowbase.__file__).parent / "locking.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, fake_msvcrt


def test_windows_try_lock_treats_only_lock_violation_as_busy(monkeypatch):
    locking, fake = _windows_locking_module(monkeypatch)
    fh = SimpleNamespace(fileno=lambda: 0, seek=lambda pos: None)
    fake.locking = lambda fd, mode, n: (_ for _ in ()).throw(OSError(errno.EACCES, "locked"))
    assert locking._try_lock(fh) is False  # 锁被占用 → 等待
    fake.locking = lambda fd, mode, n: (_ for _ in ()).throw(OSError(errno.EDEADLK, "deadlock"))
    assert locking._try_lock(fh) is False
    # 其他 OSError 是真实故障：必须上抛，而不是伪装成等锁超时
    fake.locking = lambda fd, mode, n: (_ for _ in ()).throw(OSError(errno.ENOENT, "handle gone"))
    with pytest.raises(OSError):
        locking._try_lock(fh)


# ---------- L-8：杂项（errors=replace / os.devnull / machine-id / INDEX LF） ----------

def test_subprocess_decoders_use_errors_replace():
    from knowbase import gitops, quarantine
    assert 'encoding="utf-8", errors="replace"' in Path(gitops.__file__).read_text(encoding="utf-8")
    assert 'encoding="utf-8", errors="replace"' in Path(quarantine.__file__).read_text(encoding="utf-8")


def test_transport_uses_platform_devnull():
    from knowbase.team import transport
    source = Path(transport.__file__).read_text(encoding="utf-8")
    assert "core.hooksPath=/dev/null" not in source
    assert "core.hooksPath=\"+os.devnull" in source or "core.hooksPath='+os.devnull" in source


def test_machine_id_read_pins_utf8():
    from knowbase.team import service
    assert 'read_text(encoding="utf-8")' in Path(service.__file__).read_text(encoding="utf-8")


def test_index_md_is_written_with_lf():
    from knowbase import index
    source = Path(index.__file__).read_text(encoding="utf-8")
    assert 'out.write_text("\\n".join(lines) + "\\n", encoding="utf-8", newline="\\n")' in source


# ---------- 补充：审计残留可选项（GIT_EDITOR 可移植性 / stdio LF 行尾） ----------

def test_git_env_noop_editor_is_portable():
    from knowbase import gitops
    env = gitops._git_env()
    # 修复前："true" 依赖 Git for Windows 自带 sh；":" 在 sh 与 cmd 下都是空操作
    assert env["GIT_EDITOR"] == ":"
    assert env["GIT_SEQUENCE_EDITOR"] == ":"


def test_stdio_lf_buffer_normalizes_crlf(monkeypatch):
    """SDK 用 TextIOWrapper(newline=None) 重包 stdout.buffer，Windows 上写出 CRLF；
    _force_lf_stdio 在 buffer 层归一为 LF。用 newline="\\r\\n" 模拟 Windows 翻译，
    使测试在任意平台都能捕获回归。"""
    import io
    from knowbase import server
    chunks = []

    class FakeRaw:
        def write(self, data):
            chunks.append(data)
            return len(data)

        def flush(self):
            pass

        def writable(self):
            return True

        def seekable(self):
            return False

        def close(self):
            pass

        @property
        def closed(self):
            return False

    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(io.BufferedWriter(FakeRaw()), encoding="utf-8"))
    monkeypatch.setattr(server, "os", SimpleNamespace(name="nt", environ=os.environ))
    server._force_lf_stdio()
    # 模拟 SDK：拿 sys.stdout.buffer 再包一层 newline 翻译的 wrapper
    sdk_like = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", newline="\r\n")
    sdk_like.write("msg\n")
    sdk_like.flush()
    assert b"".join(chunks) == b"msg\n"  # 修复前：直落 b"msg\r\n"

    # POSIX 上不做任何替换
    monkeypatch.setattr(server, "os", SimpleNamespace(name="posix", environ=os.environ))
    before = sys.stdout
    server._force_lf_stdio()
    assert sys.stdout is before
