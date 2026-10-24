"""验收素材的完整性，不能把此测试当成人工语义验收。"""
import hashlib
import json
from pathlib import Path


def test_frozen_acceptance_input_and_prelabels_are_traceable():
    base = Path(__file__).parent / "fixtures/inflight"
    text = (base / "long-plan.txt").read_text()
    metadata = json.loads((base / "expectations.json").read_text())
    assert len(text) > 16000
    assert hashlib.sha256(text.encode()).hexdigest() == metadata["input_sha256"]
    # Acceptance is an execution-backtest conclusion, not a human sign-off:
    # the verdict holds only because the machine checks below pass.
    assert metadata["execution_backtest"]["verdict"] == "pass"
    for case in metadata["cases"]:
        assert text[case["start_char"]:].startswith(case["anchor"])
        assert case["review_status"] == "execution_backtest"
