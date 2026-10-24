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
    assert cmd_init() == 0
    return repo
