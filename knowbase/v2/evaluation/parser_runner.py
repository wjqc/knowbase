"""Parser golden set runner (P5-C1).

Runs each `ParserCase` against the current built-in parser registry,
compares parsed output against expected fields, produces a
`ParserEvalReport` with per-case pass/fail and aggregate stats.

Design:
- Pure function; no DB / no filesystem beyond a tmp file per case.
- Each case writes input_text to a tmp file with `extension` suffix, calls
  `registry().find(tmp_path)` to dispatch, then invokes `parser.parse()`.
- Failures collected individually (don't stop on first mismatch) so the
  runner returns a complete picture for diff/CI.

Why not patch fixtures into the parser directly:
- Keeps tests honest about the actual file/IO path the parser takes.
- Catches regression where `supports()` extension logic changes.
"""
from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from knowbase.v2.ingestion.parsers import ParserRegistry, register_builtin, registry

from .parser_golden_set import (
    ParserCase,
    ParserGoldenSet,
    load_default_parser_golden_set,
)


@dataclass
class ParserCaseResult:
    case: ParserCase
    passed: bool
    failures: list[str] = field(default_factory=list)
    parsed_text: str = ""
    parsed_meta: dict = field(default_factory=dict)


@dataclass
class ParserEvalReport:
    golden_set_name: str
    total: int = 0
    passed: int = 0
    by_parser: dict[str, dict] = field(default_factory=dict)
    case_results: list[ParserCaseResult] = field(default_factory=list)

    @property
    def failed(self) -> int:
        return self.total - self.passed

    def summary(self) -> dict:
        return {
            "golden_set": self.golden_set_name,
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "pass_rate": round(self.passed / max(1, self.total), 4),
            "by_parser": self.by_parser,
        }


# ---------- 期望比对 ----------


def _check_case(parsed_text: str, parsed_meta: dict, case: ParserCase) -> list[str]:
    """Return list of failure messages (empty == all pass)."""
    fails: list[str] = []

    for key in case.expected_meta_keys:
        if key not in parsed_meta:
            fails.append(f"meta missing key: {key!r}")

    for key, substr in case.expected_meta_contains:
        val = parsed_meta.get(key)
        if val is None:
            fails.append(f"meta missing key for contains: {key!r}")
            continue
        if substr not in str(val):
            fails.append(f"meta[{key!r}]={val!r} missing substring {substr!r}")

    if case.expected_headings:
        headings = parsed_meta.get("headings") or []
        if list(headings) != list(case.expected_headings):
            fails.append(
                f"headings mismatch: got={list(headings)} "
                f"expected={list(case.expected_headings)}"
            )

    for needle in case.expected_text_contains:
        if needle not in parsed_text:
            fails.append(f"text missing substring: {needle!r}")

    for bad in case.expected_text_excludes:
        if bad in parsed_text:
            fails.append(f"text contains forbidden substring: {bad!r}")

    return fails


# ---------- 主入口 ----------


def run_parser_golden_set(
    golden_set: ParserGoldenSet | None = None,
    *,
    registry_: ParserRegistry | None = None,
    case_filter: Iterable[str] | None = None,
) -> ParserEvalReport:
    """Run all cases; default registry if `registry_` not supplied."""
    if golden_set is None:
        golden_set = load_default_parser_golden_set()
    if registry_ is None:
        registry_ = registry()
    if not list(registry_.parsers()):
        # Lazy-register builtins if caller hasn't; matches
        # IngestionService's auto_register behavior.
        register_builtin()

    allowed = set(case_filter) if case_filter else None
    report = ParserEvalReport(golden_set_name=golden_set.name)

    for case in golden_set.cases:
        if allowed and case.case_id not in allowed:
            continue

        # 写临时文件驱动 supports/parse
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=case.extension, encoding="utf-8",
            delete=False,
        ) as f:
            f.write(case.input_text)
            tmp = Path(f.name)
        try:
            parser = registry_.find(tmp)
            if parser is None:
                fails = [f"no parser registered for {case.extension!r}"]
                parsed_text = ""
                parsed_meta = {}
            else:
                if parser.name != case.parser_name:
                    fails = [
                        f"parser dispatch mismatch: registry returned "
                        f"{parser.name!r}, case expected {case.parser_name!r}"
                    ]
                else:
                    fails = []
                try:
                    parsed = parser.parse(tmp)
                    parsed_text = parsed.text
                    parsed_meta = parsed.meta
                except Exception as e:  # ParseError or any other
                    fails.append(f"parse raised {type(e).__name__}: {e}")
                    parsed_text = ""
                    parsed_meta = {}
                # 期望比对（仅当 dispatcher 一致 + parse 成功）
                if not fails:
                    fails.extend(_check_case(parsed_text, parsed_meta, case))
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

        result = ParserCaseResult(
            case=case,
            passed=not fails,
            failures=fails,
            parsed_text=parsed_text,
            parsed_meta=parsed_meta,
        )
        report.case_results.append(result)
        report.total += 1
        if result.passed:
            report.passed += 1

        bucket = report.by_parser.setdefault(
            case.parser_name, {"total": 0, "passed": 0}
        )
        bucket["total"] += 1
        if result.passed:
            bucket["passed"] += 1

    return report


def format_parser_report(report: ParserEvalReport) -> str:
    """Human-readable report for logs / CI."""
    s = report.summary()
    lines = [
        f"=== Parser Golden Set: {s['golden_set']} ===",
        f"总用例 {s['total']} ｜ 通过 {s['passed']}（pass_rate={s['pass_rate']}）"
        f" ｜ 失败 {s['failed']}",
        "— by parser —",
    ]
    for pname, b in sorted(s["by_parser"].items()):
        pr = b["passed"] / max(1, b["total"])
        lines.append(f"  {pname}: {b['passed']}/{b['total']} (pass_rate={pr:.2%})")
    if report.case_results:
        failures = [r for r in report.case_results if not r.passed]
        if failures:
            lines.append("— failures —")
            for r in failures:
                lines.append(f"  [{r.case.parser_name}] {r.case.case_id}:")
                for f in r.failures:
                    lines.append(f"    - {f}")
    return "\n".join(lines)


__all__ = [
    "ParserCaseResult",
    "ParserEvalReport",
    "run_parser_golden_set",
    "format_parser_report",
]
