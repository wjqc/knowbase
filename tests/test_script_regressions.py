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


@pytest.mark.parametrize("script", SCRIPTS)
def test_script_regression(script):
    result = subprocess.run(
        [sys.executable, str(ROOT / "tests" / script)],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
