collect_ignore = [
    "test_bizrule.py",
    "test_e2e.py",
    "test_governance.py",
    "test_hooks.py",
    "test_import.py",
    "test_retrieval.py",
    "test_query_hygiene_phase1.py",  # 脚本式测试：pytest 进程内导入顺序会破坏其 env 隔离
]


import json
import pytest


@pytest.fixture()
def windows_probe(monkeypatch):
    """Fake Win32 surface so Windows code paths can be exercised on any host."""
    import ctypes
    from types import SimpleNamespace

    _UNSET = object()

    def build(*, handle=0x1234, wait=_UNSET, exit_code=_UNSET, open_error=None, last_error=0, per_pid=None):
        from knowbase.updates import engine
        per_pid = per_pid or {}

        def pick(value, pid, default):
            if value is _UNSET: value = default
            if isinstance(value, dict): return value.get(pid, default)
            return value

        calls = []
        current = [None]

        def OpenProcess(access, inherited, pid):
            calls.append(('OpenProcess', access, inherited, pid))
            current[0] = pid
            return 0 if open_error is not None else handle
        def WaitForSingleObject(h, timeout):
            calls.append(('WaitForSingleObject', h, timeout))
            state = per_pid.get(current[0], {})
            return state.get('wait', pick(wait, current[0], engine._WINDOWS_WAIT_TIMEOUT))
        def GetExitCodeProcess(h, code):
            calls.append(('GetExitCodeProcess', h))
            state = per_pid.get(current[0], {})
            # exit_code=None means "GetExitCodeProcess fails", not "still active".
            value = state.get('exit_code', pick(exit_code, current[0], engine._WINDOWS_STILL_ACTIVE))
            if value is None: return 0
            ctypes.cast(code, ctypes.POINTER(ctypes.c_ulong))[0] = value
            return 1
        def CloseHandle(h):
            calls.append(('CloseHandle', h))
            return 1
        kernel = SimpleNamespace(OpenProcess=OpenProcess, WaitForSingleObject=WaitForSingleObject,
                                 GetExitCodeProcess=GetExitCodeProcess, CloseHandle=CloseHandle)
        monkeypatch.setattr(ctypes, 'WinDLL', lambda *a, **k: kernel, raising=False)
        monkeypatch.setattr(ctypes, 'get_last_error', lambda: last_error, raising=False)
        monkeypatch.setattr(engine, '_WINDOWS_API', engine._WindowsProcessApi())
        return calls
    return build


@pytest.fixture()
def memory_repo(tmp_path, monkeypatch):
    from knowbase import config
    from knowbase.__main__ import cmd_init
    repo = tmp_path / "memory"
    cfg_path = tmp_path / "knowbase.json"
    cfg_path.write_text(json.dumps(config._deep_merge(config.DEFAULTS, {
        "repo_path": str(repo), "knowledge_path": "",
        "git": {"auto_commit": False, "auto_push": False, "auto_pull": False},
    })))
    monkeypatch.setattr(config, "CONFIG_PATH", cfg_path)
    monkeypatch.setenv("KNOWBASE_CONFIG", str(cfg_path))
    monkeypatch.setenv("KNOWBASE_REPO_PATH", str(repo))
    monkeypatch.setenv("KNOWBASE_AGENT_NAME", "pytest-inflight")
    monkeypatch.setenv('KNOWBASE_RUNTIME_ROOT',str(tmp_path/'runtime'))
    assert cmd_init() == 0
    return repo
