"""P5-C3 测试：compare_reports() 回归检测。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

import pytest

from knowbase.v2.evaluation.compare import (
    CaseDelta,
    RegressionDiff,
    STATUS_ADDED,
    STATUS_FAILED,
    STATUS_PASSED,
    STATUS_REMOVED,
    compare_reports,
    format_diff,
)
from knowbase.v2.evaluation.parser_golden_set import ParserCase
from knowbase.v2.evaluation.parser_runner import (
    ParserCaseResult,
    ParserEvalReport,
)


# ---- 辅助构造器 -----------------------------------------------------------

def _case(case_id: str, parser: str = "md", ext: str = ".md") -> ParserCase:
    return ParserCase(
        case_id=case_id,
        parser_name=parser,
        extension=ext,
        input_text="# hi",
    )


def _result(case_id: str, *, passed: bool, failures: list[str] | None = None,
            parser: str = "md") -> ParserCaseResult:
    case = _case(case_id, parser=parser)
    return ParserCaseResult(
        case=case,
        passed=passed,
        failures=failures or [],
        parsed_text="",
        parsed_meta={},
    )


def _report(name: str, results: list[ParserCaseResult]) -> ParserEvalReport:
    r = ParserEvalReport(golden_set_name=name)
    r.case_results = list(results)
    r.total = len(results)
    r.passed = sum(1 for x in results if x.passed)
    for res in results:
        bucket = r.by_parser.setdefault(res.case.parser_name, {"total": 0, "passed": 0})
        bucket["total"] += 1
        if res.passed:
            bucket["passed"] += 1
    return r


# ---- compare_reports 核心象限 ---------------------------------------------

class TestCompareIdentical:
    def test_identical_reports_no_regression(self):
        results = [_result("a", passed=True), _result("b", passed=False, failures=["x"])]
        d = compare_reports(_report("v1", results), _report("v2", results))
        assert d.had_regression is False
        assert d.regressions == []
        assert d.improvements == []
        assert d.added == []
        assert d.removed == []
        assert len(d.unchanged) == 2

    def test_summary_keys(self):
        results = [_result("a", passed=True)]
        d = compare_reports(_report("v1", results), _report("v2", results))
        s = d.summary()
        assert s["baseline"] == "v1"
        assert s["candidate"] == "v2"
        assert s["had_regression"] is False
        assert s["baseline_total"] == 1
        assert s["candidate_total"] == 1


class TestRegressionDetection:
    def test_pass_to_fail_is_regression(self):
        b = _report("b", [_result("a", passed=True)])
        c = _report("c", [_result("a", passed=False, failures=["new fail"])])
        d = compare_reports(b, c)
        assert d.had_regression is True
        assert len(d.regressions) == 1
        assert d.regressions[0].case_id == "a"
        assert d.regressions[0].candidate_failures == ("new fail",)

    def test_fail_to_pass_is_improvement(self):
        b = _report("b", [_result("a", passed=False, failures=["old"])])
        c = _report("c", [_result("a", passed=True)])
        d = compare_reports(b, c)
        assert d.had_regression is False
        assert len(d.improvements) == 1
        assert d.improvements[0].case_id == "a"

    def test_pass_stays_pass(self):
        b = _report("b", [_result("a", passed=True)])
        c = _report("c", [_result("a", passed=True)])
        d = compare_reports(b, c)
        assert d.regressions == []
        assert d.improvements == []
        assert len(d.unchanged) == 1
        assert d.unchanged[0].baseline_passed is True
        assert d.unchanged[0].candidate_passed is True

    def test_fail_stays_fail(self):
        b = _report("b", [_result("a", passed=False, failures=["x"])])
        c = _report("c", [_result("a", passed=False, failures=["x"])])
        d = compare_reports(b, c)
        assert d.regressions == []
        assert d.improvements == []
        assert len(d.unchanged) == 1


class TestAddedRemoved:
    def test_added_case_status(self):
        b = _report("b", [_result("a", passed=True)])
        c = _report("c", [_result("a", passed=True), _result("b-new", passed=False)])
        d = compare_reports(b, c)
        assert len(d.added) == 1
        assert d.added[0].case_id == "b-new"
        assert d.added[0].status == STATUS_ADDED
        assert d.added[0].baseline_passed is None
        assert d.added[0].candidate_passed is False

    def test_removed_case_status(self):
        b = _report("b", [_result("a", passed=True), _result("b-old", passed=True)])
        c = _report("c", [_result("a", passed=True)])
        d = compare_reports(b, c)
        assert len(d.removed) == 1
        assert d.removed[0].case_id == "b-old"
        assert d.removed[0].baseline_passed is True
        assert d.removed[0].candidate_passed is None

    def test_added_passing_case(self):
        b = _report("b", [])
        c = _report("c", [_result("a-new", passed=True)])
        d = compare_reports(b, c)
        assert d.added[0].candidate_passed is True
        # not a regression (no baseline entry)
        assert d.had_regression is False


class TestCaseDelta:
    def test_frozen(self):
        d = CaseDelta(case_id="x", parser_name="md", status=STATUS_PASSED)
        with pytest.raises(Exception):
            d.case_id = "y"  # type: ignore[misc]

    def test_default_failures_empty_tuple(self):
        d = CaseDelta(case_id="x", parser_name="md", status=STATUS_PASSED)
        assert d.baseline_failures == ()
        assert d.candidate_failures == ()


class TestRegressionDiff:
    def test_frozen(self):
        d = RegressionDiff(baseline_name="b", candidate_name="c")
        with pytest.raises(Exception):
            d.baseline_name = "z"  # type: ignore[misc]

    def test_mixed_scenario(self):
        """All 4 status types in one diff."""
        b = _report("b", [
            _result("regression", passed=True),
            _result("improvement", passed=False, failures=["old"]),
            _result("stable", passed=True),
            _result("removed", passed=True),
        ])
        c = _report("c", [
            _result("regression", passed=False, failures=["new"]),
            _result("improvement", passed=True),
            _result("stable", passed=True),
            _result("added", passed=False, failures=["n"]),
        ])
        d = compare_reports(b, c)
        assert len(d.regressions) == 1
        assert d.regressions[0].case_id == "regression"
        assert len(d.improvements) == 1
        assert d.improvements[0].case_id == "improvement"
        assert len(d.added) == 1
        assert d.added[0].case_id == "added"
        assert len(d.removed) == 1
        assert d.removed[0].case_id == "removed"
        assert len(d.unchanged) == 1
        assert d.unchanged[0].case_id == "stable"

    def test_empty_reports(self):
        d = compare_reports(_report("b", []), _report("c", []))
        assert d.deltas == ()
        assert d.had_regression is False
        assert d.summary()["unchanged"] == 0


# ---- format_diff ----------------------------------------------------------

class TestFormatDiff:
    def test_passing_diff_no_regression_section(self):
        b = _report("b", [_result("a", passed=True)])
        c = _report("c", [_result("a", passed=True)])
        d = compare_reports(b, c)
        out = format_diff(d)
        assert "PASS" in out
        assert "regressions=0" in out
        assert "regressions (旧过→新不过)" not in out

    def test_regression_section_present(self):
        b = _report("b", [_result("a", passed=True)])
        c = _report("c", [_result("a", passed=False, failures=["x missing"])])
        d = compare_reports(b, c)
        out = format_diff(d)
        assert "FAIL (had_regression)" in out
        assert "regressions (旧过→新不过)" in out
        assert "a" in out
        assert "x missing" in out

    def test_improvement_section_present(self):
        b = _report("b", [_result("a", passed=False, failures=["old"])])
        c = _report("c", [_result("a", passed=True)])
        d = compare_reports(b, c)
        out = format_diff(d)
        assert "improvements (旧不过→新过)" in out

    def test_added_section_present(self):
        b = _report("b", [])
        c = _report("c", [_result("n", passed=True)])
        d = compare_reports(b, c)
        out = format_diff(d)
        assert "added (仅新)" in out
        assert "(PASS)" in out

    def test_removed_section_present(self):
        b = _report("b", [_result("o", passed=False, failures=["x"])])
        c = _report("c", [])
        d = compare_reports(b, c)
        out = format_diff(d)
        assert "removed (仅旧)" in out
        assert "(FAIL)" in out


# ---- 端到端：跑真 golden set 两次，期望 0 regression ----------------------

class TestEndToEndStableRun:
    def test_default_set_is_stable_across_runs(self):
        from knowbase.v2.evaluation.parser_runner import run_parser_golden_set
        b = run_parser_golden_set()
        c = run_parser_golden_set()
        d = compare_reports(b, c)
        assert d.had_regression is False, format_diff(d)
        assert d.regressions == []
        assert d.improvements == []
