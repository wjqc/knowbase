"""W1-3：首个注册验证器（pytest-text 解析器 + verify CLI + L3 治理效力）。

覆盖：真实 pytest 文本解析（pass/fail/skip、汇总计数）；CLI verify 全链落
L3（negative polarity）；trusted fail 经治理重算把卡打成 stale（反证门）；
全 pass 在 unknown 任务键下诚实停在 unverified（晋升需任务身份，不伪造）；
未登记 validator 拒绝；空报告按 EMPTY_REPORT 不放行。
"""
import json
from pathlib import Path

from knowbase import __main__ as cli, index, store, writer
from knowbase.knowledge_lifecycle import attribution, governance
from knowbase.knowledge_lifecycle import validators
from knowbase.migrations import apply_all
from knowbase.mutations import _compute_revision

BODY = ("## 结论\n结算规则以脚本为准。\n## 解决的问题\nx。\n"
        "## 适用条件\ny。\n## 不适用条件\nz。\n## 可执行动作\n运行 scripts/settle.py。\n"
        "## 关键证据\nPR。\n## 验证情况\nv。\n## 未知与待确认\n无。\n")

PYTEST_TEXT = """\
============================= test session starts ==============================
platform darwin -- Python 3.13.0, pytest-8.3.5, pluggy-1.5.0
------------------------------- test session ----------------------------------
collected 3 items

tests/test_settle.py F.s                                                   [100%]

================================== FAILURES ===================================
_________________________________ test_discount ______________________________
>       assert settle(100) == 85
E       assert 90 == 85
=========================== short test summary info ============================
FAILED tests/test_settle.py::test_discount - assert 90 == 85
PASSED tests/test_settle.py::test_settle_basic
SKIPPED tests/test_settle.py::test_offline: 离线用例
========================= 1 failed, 1 passed, 1 skipped in 0.02s =================
"""

PYTEXT_ALL_PASS = """\
=========================== short test summary info ============================
PASSED tests/test_settle.py::test_discount
PASSED tests/test_settle.py::test_settle_basic
========================= 2 passed in 0.01s ==================
"""


def _make_card(repo):
    card = writer.create(repo, "bizrule", "折扣结算规则", BODY, scope="orders",
                         provenance="PRD-9")
    assert not card.get("error"), card
    return card["entity_id"]


def _head_of(conn, mid):
    return conn.execute(
        "SELECT content_revision FROM entity_heads WHERE entity_id=?", (mid,)).fetchone()[0]


def test_parse_pytest_text_real_output():
    report = validators.parse_pytest_text(PYTEST_TEXT)
    assert report["parser"] == "pytest-text-v1"
    assert len(report["artifact_hash"]) == 64 and report["expected_hash"] == ""
    by_check = {c["check_id"]: c["observed_result"] for c in report["checks"]}
    assert by_check == {
        "tests/test_settle.py::test_discount": "fail",
        "tests/test_settle.py::test_settle_basic": "pass",
        "tests/test_settle.py::test_offline": "skip",
    }
    assert report["counts"] == {"passed": 1, "failed": 1, "summary_lines": 3}
    # claim_map 注入。
    mapped = validators.parse_pytest_text(
        PYTEXT_ALL_PASS, claim_map={"tests/test_settle.py::test_discount": "C-disc"})
    assert mapped["checks"][0]["claim_id"] == "C-disc"


def test_cli_verify_fail_path_quarantines_via_recompute(memory_repo, tmp_path):
    mid = _make_card(memory_repo)
    report_file = tmp_path / "pytest-fail.txt"
    report_file.write_text(PYTEST_TEXT, encoding="utf-8")
    assert cli.main(["lifecycle", "verify", "--memory-id", mid,
                     "--report-file", str(report_file)]) == 0
    conn = index.connect(memory_repo)
    apply_all(conn)
    rows = conn.execute(
        "SELECT level,polarity,trust_basis,import_state FROM evidence_events "
        "WHERE event_type='validation.completed'").fetchall()
    assert rows == [("L3", "negative", "registered-adapter", "local")], rows
    # trusted fail → 反证门 → quarantine 投影为 stale。
    applied = governance.apply_decision(memory_repo, conn, mid)
    assert applied["decision"] == "quarantine", applied
    meta, _body, _ = store.load(memory_repo, mid)
    assert meta["status"] == "stale" and meta.get("quarantine_reason")
    conn.close()


def test_cli_verify_pass_path_honest_unverified_without_task_identity(memory_repo, tmp_path):
    mid = _make_card(memory_repo)
    report_file = tmp_path / "pytest-pass.txt"
    report_file.write_text(PYTEXT_ALL_PASS, encoding="utf-8")
    assert cli.main(["lifecycle", "verify", "--memory-id", mid,
                     "--report-file", str(report_file)]) == 0
    conn = index.connect(memory_repo)
    apply_all(conn)
    row = conn.execute(
        "SELECT polarity FROM evidence_events WHERE event_type='validation.completed'"
    ).fetchone()
    assert row == ("positive",)
    decision = governance.decide(conn, mid, _head_of(conn, mid))
    # 无 run（无任务身份）→ 独立性 0 → 不伪造晋升：诚实 unverified。
    assert decision["independent_tasks"] == 0
    assert decision["decision"] == "unverified", decision
    conn.close()


def test_unregistered_validator_rejected(memory_repo):
    mid = _make_card(memory_repo)
    conn = index.connect(memory_repo)
    apply_all(conn)
    meta, body, _ = store.load(memory_repo, mid)
    report = validators.parse_pytest_text(PYTEXT_ALL_PASS)
    report["target"] = {"memory_id": mid,
                        "content_revision": _compute_revision(meta, body)}
    result = attribution.verify_execution(conn, memory_repo, run_id="",
                                          validator_id="not-registered",
                                          report=report)
    assert result.get("error") == "VALIDATOR_NOT_REGISTERED", result
    # 空报告（无 summary 行的纯点号输出）→ EMPTY_REPORT，不产 L3。
    empty = validators.parse_pytest_text("...\n2 passed in 0.01s\n")
    result2 = attribution.verify_execution(conn, memory_repo, run_id="",
                                           validator_id="pytest-text", report={
                                               **empty,
                                               "target": report["target"]})
    assert result2.get("error") == "EMPTY_REPORT", result2
    assert conn.execute("SELECT COUNT(*) FROM evidence_events").fetchone()[0] == 0
    conn.close()
