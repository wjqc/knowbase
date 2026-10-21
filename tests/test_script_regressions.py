import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = (
    "test_bizrule.py",
    "test_e2e.py",
    "test_governance.py",
    "test_hooks.py",
    "test_import.py",
    "test_retrieval.py",
)

# 这些脚本按设计打真实记忆库（golden set / 回归红线），不能改判定标准，
# 但允许在真实库的临时副本上运行，避免在真实库上产生 WAL/锁文件。
REAL_REPO_SCRIPTS = frozenset({"test_retrieval.py"})


def _env_for(script: str, tmp_path: Path) -> dict:
    env = dict(os.environ)
    if script not in REAL_REPO_SCRIPTS:
        return env

    sys.path.insert(0, str(ROOT))
    from knowbase import config

    src_db = config.repo_path() / "memory.db"
    if not src_db.exists():
        pytest.skip("真实记忆库不存在，无法跑 golden set")

    # 在线一致性快照，不能遗漏仍在 WAL 中的元数据/迁移。
    with sqlite3.connect(f"file:{src_db}?mode=ro", uri=True) as source:
        with sqlite3.connect(tmp_path / "memory.db") as destination:
            source.backup(destination)
    env["KNOWBASE_REPO_PATH"] = str(tmp_path)
    return env


@pytest.mark.parametrize("script", SCRIPTS)
def test_script_regression(script, tmp_path):
    result = subprocess.run(
        [sys.executable, str(ROOT / "tests" / script)],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
        env=_env_for(script, tmp_path),
    )
    assert result.returncode == 0, result.stdout + result.stderr
