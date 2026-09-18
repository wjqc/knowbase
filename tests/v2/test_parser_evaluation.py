"""Tests for knowbase.v2.evaluation parser golden set + runner (P5-C1).

Coverage:
- ParserCase / ParserGoldenSet dataclass behavior
- load_default_parser_golden_set() returns expected cases
- run_parser_golden_set() against builtins: 7/7 pass
- _check_case() individual rules (meta keys / headings / contains / excludes)
- Failure modes: missing parser, parse exception, wrong meta key
- format_parser_report() includes summary + by_parser + failures
"""
from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from knowbase.v2.evaluation.parser_golden_set import (
    ParserCase,
    ParserGoldenSet,
    load_default_parser_golden_set,
)
from knowbase.v2.evaluation.parser_runner import (
    ParserCaseResult,
    ParserEvalReport,
    _check_case,
    format_parser_report,
    run_parser_golden_set,
)


# ---------- ParserCase / ParserGoldenSet ----------


class TestParserCase:
    def test_is_frozen(self):
        c = ParserCase(case_id="x", parser_name="markdown", extension=".md",
                       input_text="hi")
        with pytest.raises(FrozenInstanceError):
            c.case_id = "y"  # type: ignore[misc]

    def test_defaults(self):
        c = ParserCase(case_id="x", parser_name="markdown", extension=".md",
                       input_text="hi")
        assert c.expected_meta_keys == ()
        assert c.expected_meta_contains == ()
        assert c.expected_headings == ()
        assert c.expected_text_contains == ()
        assert c.expected_text_excludes == ()
        assert c.note == ""


class TestParserGoldenSet:
    def test_add_and_extend(self):
        s = ParserGoldenSet(name="t", version=1)
        s.add(ParserCase(case_id="a", parser_name="x", extension=".x",
                         input_text=""))
        s.extend([ParserCase(case_id="b", parser_name="x", extension=".x",
                             input_text="")])
        assert len(s) == 2

    def test_by_parser(self):
        s = ParserGoldenSet(name="t", version=1)
        s.add(ParserCase(case_id="a", parser_name="markdown", extension=".md",
                         input_text=""))
        s.add(ParserCase(case_id="b", parser_name="code", extension=".py",
                         input_text=""))
        assert len(s.by_parser("markdown")) == 1
        assert len(s.by_parser("code")) == 1
        assert s.by_parser("unknown") == []


class TestLoadDefault:
    def test_returns_non_empty_set(self):
        gs = load_default_parser_golden_set()
        assert gs.name
        assert gs.version >= 1
        assert len(gs) >= 4

    def test_covers_core_parsers(self):
        gs = load_default_parser_golden_set()
        names = {c.parser_name for c in gs.cases}
        assert "markdown" in names
        assert "code" in names
        assert "html" in names
        assert "log" in names

    def test_case_ids_unique(self):
        gs = load_default_parser_golden_set()
        ids = [c.case_id for c in gs.cases]
        assert len(ids) == len(set(ids))


# ---------- _check_case ----------


class TestCheckCase:
    def test_pass_when_all_match(self):
        case = ParserCase(
            case_id="t", parser_name="markdown", extension=".md",
            input_text="", expected_meta_keys=("a", "b"),
            expected_meta_contains=(("a", "v"),),
            expected_headings=("h1",),
            expected_text_contains=("hello",),
            expected_text_excludes=("bad",),
        )
        fails = _check_case(
            "hello world", {"a": "value", "b": 2, "headings": ("h1",)}, case
        )
        assert fails == []

    def test_missing_meta_key(self):
        case = ParserCase(case_id="t", parser_name="x", extension=".x",
                          input_text="", expected_meta_keys=("missing",))
        fails = _check_case("", {"other": 1}, case)
        assert any("missing key" in f for f in fails)

    def test_meta_contains_substring_fail(self):
        case = ParserCase(
            case_id="t", parser_name="x", extension=".x", input_text="",
            expected_meta_contains=(("lang", "python"),),
        )
        fails = _check_case("", {"lang": "javascript"}, case)
        assert any("substring" in f for f in fails)

    def test_meta_contains_missing_key(self):
        case = ParserCase(
            case_id="t", parser_name="x", extension=".x", input_text="",
            expected_meta_contains=(("lang", "python"),),
        )
        fails = _check_case("", {}, case)
        assert any("missing key for contains" in f for f in fails)

    def test_headings_mismatch(self):
        case = ParserCase(
            case_id="t", parser_name="markdown", extension=".md",
            input_text="", expected_headings=("h1", "h2"),
        )
        fails = _check_case("", {"headings": ("h1",)}, case)
        assert any("headings mismatch" in f for f in fails)

    def test_text_contains_fail(self):
        case = ParserCase(
            case_id="t", parser_name="x", extension=".x", input_text="",
            expected_text_contains=("must-have",),
        )
        fails = _check_case("plain text without it", {}, case)
        assert any("missing substring" in f for f in fails)

    def test_text_excludes_fail(self):
        case = ParserCase(
            case_id="t", parser_name="x", extension=".x", input_text="",
            expected_text_excludes=("forbidden",),
        )
        fails = _check_case("contains forbidden word here", {}, case)
        assert any("forbidden" in f for f in fails)


# ---------- run_parser_golden_set ----------


class TestRunParserGoldenSet:
    def test_default_set_all_pass(self):
        report = run_parser_golden_set()
        s = report.summary()
        assert s["total"] >= 4
        assert s["failed"] == 0
        assert s["pass_rate"] == 1.0

    def test_by_parser_aggregates(self):
        report = run_parser_golden_set()
        for pname, b in report.by_parser.items():
            assert b["total"] == b["passed"], f"{pname} has failures"

    def test_case_results_attached(self):
        report = run_parser_golden_set()
        assert len(report.case_results) == report.total
        for r in report.case_results:
            assert isinstance(r, ParserCaseResult)
            assert r.case.case_id

    def test_case_filter_runs_subset(self):
        report = run_parser_golden_set(case_filter=["md_simple"])
        assert report.total == 1
        assert report.case_results[0].case.case_id == "md_simple"

    def test_unknown_case_filter_returns_empty(self):
        report = run_parser_golden_set(case_filter=["does-not-exist"])
        assert report.total == 0
        assert report.passed == 0

    def test_passed_property(self):
        report = run_parser_golden_set()
        assert report.failed == report.total - report.passed


# ---------- format_parser_report ----------


class TestFormatParserReport:
    def test_includes_summary(self):
        report = run_parser_golden_set()
        text = format_parser_report(report)
        assert "Parser Golden Set" in text
        assert "pass_rate" in text
        assert "by parser" in text

    def test_includes_failures_section(self):
        # Build a report with a deliberate failure
        gs = ParserGoldenSet(name="t", version=1, cases=[
            ParserCase(
                case_id="fail", parser_name="markdown", extension=".md",
                input_text="# title\n",
                expected_meta_contains=(("format", "NOT-MATCHING"),),
            ),
        ])
        report = run_parser_golden_set(gs)
        text = format_parser_report(report)
        assert "failures" in text
        assert "fail" in text
        assert "substring" in text

    def test_no_failure_section_when_all_pass(self):
        report = run_parser_golden_set()
        text = format_parser_report(report)
        # Should not list individual failure entries (the "— failures —"
        # header is only emitted when failures exist)
        assert "— failures —" not in text or "failures_count" not in text
        # Just confirm no per-case failure line for default cases
        for r in report.case_results:
            assert r.passed, f"{r.case.case_id} should pass"


# ---------- Failure injection (custom registry / missing parser) ----------


class _DummyRegistry:
    """Registry-like object that raises when .find() is called, to drive
    the 'no parser registered' branch."""
    def parsers(self):
        return ()
    def find(self, path):
        return None


class _BoomParser:
    name = "markdown"
    def supports(self, path):
        return True
    def parse(self, path):
        raise RuntimeError("boom")


class _BoomRegistry:
    def parsers(self):
        return (_BoomParser(),)
    def find(self, path):
        return _BoomParser()


class TestFailureModes:
    def test_no_parser_for_extension(self):
        gs = ParserGoldenSet(name="t", version=1, cases=[
            ParserCase(
                case_id="nope", parser_name="markdown", extension=".xyz",
                input_text="x",
            ),
        ])
        report = run_parser_golden_set(gs, registry_=_DummyRegistry())  # type: ignore[arg-type]
        assert report.total == 1
        assert report.passed == 0
        assert "no parser registered" in report.case_results[0].failures[0]

    def test_parse_exception_captured(self):
        gs = ParserGoldenSet(name="t", version=1, cases=[
            ParserCase(
                case_id="boom", parser_name="markdown", extension=".md",
                input_text="x",
            ),
        ])
        report = run_parser_golden_set(gs, registry_=_BoomRegistry())  # type: ignore[arg-type]
        assert report.total == 1
        assert report.passed == 0
        assert any("parse raised" in f for f in report.case_results[0].failures)

    def test_dispatch_mismatch(self):
        # Parser returned has different name than case expects
        gs = ParserGoldenSet(name="t", version=1, cases=[
            ParserCase(
                case_id="mismatch", parser_name="OTHER_NAME",
                extension=".md", input_text="# x\n",
            ),
        ])
        report = run_parser_golden_set(gs, registry_=_BoomRegistry())  # type: ignore[arg-type]
        assert report.passed == 0
        assert any("dispatch mismatch" in f for f in report.case_results[0].failures)
