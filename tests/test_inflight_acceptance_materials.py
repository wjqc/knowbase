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
    assert metadata["human_approved"] is False
    for case in metadata["cases"]:
        assert text[case["start_char"]:].startswith(case["anchor"])
        assert case["review_status"] == "agent_prelabel_pending_human"
