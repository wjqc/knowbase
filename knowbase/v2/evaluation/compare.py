"""P5-C3: 回归检测 — baseline vs candidate 评测报告对比。

目标：
- 在升级解析器实现 / 模型版本后，重跑同一 golden set
  （baseline 旧版 / candidate 新版），diff 出三类变化：
  - regressions: 旧过 → 新不过（要拦）
  - improvements: 旧不过 → 新过（值得记）
  - unchanged：状态相同
- 在 history 视角下，能拼出连续趋势（多份 RegressionDiff 串联）

设计要点：
- RegressionDiff 是 dataclass(frozen)：纯数据，无 IO
- compare_reports(baseline, candidate) 接收两个 ParserEvalReport，
  按 case_id 对齐（不依赖 by_parser 顺序），区分 4 象限
- case_results 在两侧可能不同（baseline 没跑的 case 视为 "added"）
- format_diff() 输出可读文本：summary → regressions → improvements → added/removed
- had_regression property 让 CI 能一句话 gate
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .parser_runner import ParserCaseResult, ParserEvalReport


# ---------- 单 case 的状态枚举 ----------

# 用字符串而非 enum：JSON 序列化友好，CI 日志可读
STATUS_PASSED = "passed"
STATUS_FAILED = "failed"
STATUS_ADDED = "added"        # 仅 candidate 出现
STATUS_REMOVED = "removed"    # 仅 baseline 出现


@dataclass(frozen=True)
class CaseDelta:
    """One case's status change between baseline and candidate."""
    case_id: str
    parser_name: str
    status: str  # passed / failed / added / removed
    baseline_passed: bool | None = None
    candidate_passed: bool | None = None
    baseline_failures: tuple[str, ...] = ()
    candidate_failures: tuple[str, ...] = ()


@dataclass(frozen=True)
class RegressionDiff:
    """Aggregate diff between two parser eval reports."""
    baseline_name: str
    candidate_name: str
    deltas: tuple[CaseDelta, ...] = ()
    baseline_total: int = 0
    candidate_total: int = 0

    @property
    def regressions(self) -> list[CaseDelta]:
        """Cases that USED to pass and now fail — the core signal."""
        return [d for d in self.deltas
                if d.baseline_passed is True
                and d.candidate_passed is False]

    @property
    def improvements(self) -> list[CaseDelta]:
        return [d for d in self.deltas
                if d.baseline_passed is False
                and d.candidate_passed is True]

    @property
    def added(self) -> list[CaseDelta]:
        return [d for d in self.deltas if d.status == STATUS_ADDED]

    @property
    def removed(self) -> list[CaseDelta]:
        return [d for d in self.deltas if d.status == STATUS_REMOVED]

    @property
    def unchanged(self) -> list[CaseDelta]:
        return [d for d in self.deltas
                if d.baseline_passed == d.candidate_passed
                and d.baseline_passed is not None]

    @property
    def had_regression(self) -> bool:
        return bool(self.regressions)

    def summary(self) -> dict:
        return {
            "baseline": self.baseline_name,
            "candidate": self.candidate_name,
            "baseline_total": self.baseline_total,
            "candidate_total": self.candidate_total,
            "regressions": len(self.regressions),
            "improvements": len(self.improvements),
            "added": len(self.added),
            "removed": len(self.removed),
            "unchanged": len(self.unchanged),
            "had_regression": self.had_regression,
        }


# ---------- 主入口 ----------

def _index_by_case_id(report: ParserEvalReport) -> dict[str, ParserCaseResult]:
    return {r.case.case_id: r for r in report.case_results}


def compare_reports(
    baseline: ParserEvalReport,
    candidate: ParserEvalReport,
) -> RegressionDiff:
    """Diff two ParserEvalReports case-by-case.

    Cases missing from one side get `added` / `removed` status; cases
    present in both get pass/fail reconciliation. Order in the output is
    stable: regressions first, then improvements, added, removed,
    unchanged (alphabetical within each group).
    """
    base_idx = _index_by_case_id(baseline)
    cand_idx = _index_by_case_id(candidate)
    all_ids = sorted(set(base_idx) | set(cand_idx))

    deltas: list[CaseDelta] = []
    for cid in all_ids:
        b = base_idx.get(cid)
        c = cand_idx.get(cid)
        if b is None and c is not None:
            r = c
            deltas.append(CaseDelta(
                case_id=cid,
                parser_name=r.case.parser_name,
                status=STATUS_ADDED,
                baseline_passed=None,
                candidate_passed=r.passed,
                baseline_failures=(),
                candidate_failures=tuple(r.failures),
            ))
        elif c is None and b is not None:
            r = b
            deltas.append(CaseDelta(
                case_id=cid,
                parser_name=r.case.parser_name,
                status=STATUS_REMOVED,
                baseline_passed=r.passed,
                candidate_passed=None,
                baseline_failures=tuple(r.failures),
                candidate_failures=(),
            ))
        else:
            assert b is not None and c is not None  # mypy
            parser_name = b.case.parser_name or c.case.parser_name
            if b.passed and not c.passed:
                status = STATUS_FAILED
            elif not b.passed and c.passed:
                status = STATUS_PASSED
            elif b.passed and c.passed:
                status = STATUS_PASSED
            else:
                status = STATUS_FAILED
            deltas.append(CaseDelta(
                case_id=cid,
                parser_name=parser_name,
                status=status,
                baseline_passed=b.passed,
                candidate_passed=c.passed,
                baseline_failures=tuple(b.failures),
                candidate_failures=tuple(c.failures),
            ))

    return RegressionDiff(
        baseline_name=baseline.golden_set_name,
        candidate_name=candidate.golden_set_name,
        deltas=tuple(deltas),
        baseline_total=baseline.total,
        candidate_total=candidate.total,
    )


# ---------- 格式化 ----------

def format_diff(diff: RegressionDiff) -> str:
    """Human-readable diff for logs / CI / Slack."""
    s = diff.summary()
    lines = [
        f"=== Regression Diff: {s['baseline']} → {s['candidate']} ===",
        f"baseline {s['baseline_total']} cases / candidate {s['candidate_total']} cases",
        f"regressions={s['regressions']}  improvements={s['improvements']}  "
        f"added={s['added']}  removed={s['removed']}  unchanged={s['unchanged']}",
        f"gate: {'FAIL (had_regression)' if s['had_regression'] else 'PASS'}",
    ]
    if diff.regressions:
        lines.append("— regressions (旧过→新不过) —")
        for d in sorted(diff.regressions, key=lambda x: x.case_id):
            lines.append(f"  [{d.parser_name}] {d.case_id}")
            for f in d.candidate_failures:
                lines.append(f"    candidate: - {f}")
    if diff.improvements:
        lines.append("— improvements (旧不过→新过) —")
        for d in sorted(diff.improvements, key=lambda x: x.case_id):
            lines.append(f"  [{d.parser_name}] {d.case_id}")
    if diff.added:
        lines.append("— added (仅新) —")
        for d in sorted(diff.added, key=lambda x: x.case_id):
            tag = "PASS" if d.candidate_passed else "FAIL"
            lines.append(f"  [{d.parser_name}] {d.case_id}  ({tag})")
    if diff.removed:
        lines.append("— removed (仅旧) —")
        for d in sorted(diff.removed, key=lambda x: x.case_id):
            tag = "PASS" if d.baseline_passed else "FAIL"
            lines.append(f"  [{d.parser_name}] {d.case_id}  ({tag})")
    return "\n".join(lines)


__all__ = [
    "CaseDelta",
    "RegressionDiff",
    "STATUS_PASSED",
    "STATUS_FAILED",
    "STATUS_ADDED",
    "STATUS_REMOVED",
    "compare_reports",
    "format_diff",
]
