"""Golden Set 运行器（P0-B）。

通过 V1 server.search_impl 跑评测，输出命中率 / MRR / 零命中比例 / 分类统计。
设计原则：
- 不修改任何持久化数据（只读 + 调 search_impl）
- 不依赖 V2 摄取/检索（保证 V0/V1 兼容基线）
- 失败 case 全量保留，供人工逐条修复 fixture
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .golden_set import GoldenCase, GoldenSet, load_default_golden_set


# 解析 server.search_impl 输出的「命中 N 条」结构
_HIT_PREFIX = re.compile(r"^命中 (\d+) 条")
_NO_HIT = "未命中「"


def _parse_search_output(text: str) -> list[str]:
    """从 V1 search_impl 返回文本中抽取命中 id 列表（保持顺序）。"""
    if not text or text.startswith(_NO_HIT) or text.startswith("错误"):
        return []
    ids: list[str] = []
    for line in text.splitlines():
        # 形如 "- [M-000001]（staging 提案）xxx ｜ ..."
        m = re.match(r"^-?\s*\[(M-\d{6})\]", line)
        if m:
            ids.append(m.group(1))
    return ids


@dataclass
class CaseResult:
    case: GoldenCase
    predicted: list[str]
    hit: bool
    reciprocal_rank: float
    note: str = ""


@dataclass
class EvalReport:
    golden_set_name: str
    total: int = 0
    hit: int = 0
    zero_hit: int = 0
    reciprocal_rank_sum: float = 0.0
    by_category: dict[str, dict] = field(default_factory=dict)
    failures: list[CaseResult] = field(default_factory=list)

    def summary(self) -> dict:
        mrr = (self.reciprocal_rank_sum / max(1, self.hit + self.zero_hit)) if self.total else 0.0
        return {
            "golden_set": self.golden_set_name,
            "total": self.total,
            "hit": self.hit,
            "zero_hit": self.zero_hit,
            "hit_rate": round(self.hit / max(1, self.total), 4),
            "zero_hit_rate": round(self.zero_hit / max(1, self.total), 4),
            "mrr": round(mrr, 4),
            "by_category": self.by_category,
            "failures_count": len(self.failures),
        }


def run_golden_set(
    golden_set: GoldenSet | None = None,
    *,
    repo: Path,
    search_fn=None,
    case_filter: Iterable[str] | None = None,
) -> EvalReport:
    """对 golden_set 中的每条 case 调用 search_fn(query) 评估。

    search_fn 默认为 knowbase.server.search_impl；
    case_filter 限制只跑某些 category（如 ["semantic"] 单独看 V1 短板）。
    """
    if golden_set is None:
        golden_set = load_default_golden_set()
    if search_fn is None:
        # 延迟导入避免循环
        from .. import server as v1_server
        from . import config as _repo_setup
        # _repo_setup 留作未来扩展；当前 search_impl 内部已自行处理 repo
        search_fn = v1_server.search_impl

    allowed = set(case_filter) if case_filter else None
    report = EvalReport(golden_set_name=golden_set.name)

    for case in golden_set.cases:
        if allowed and case.category not in allowed:
            continue
        out = search_fn(case.query)
        predicted = _parse_search_output(out)
        rr = 0.0
        hit = False
        for rank, exp in enumerate(case.expected_ids, start=1):
            if exp in predicted and rank <= case.min_rank + 5:
                rr = 1.0 / rank
                hit = True
                break
        result = CaseResult(case=case, predicted=predicted, hit=hit, reciprocal_rank=rr)
        report.total += 1
        if hit:
            report.hit += 1
            report.reciprocal_rank_sum += rr
        elif case.category == "no_answer" and not predicted:
            # 零命中 case：未命中即「正确」
            report.zero_hit += 1  # 计入 zero_hit 但同时标记为「期望零命中正确」
            result.note = "期望零命中且实际零命中"
        else:
            report.zero_hit += 1
            if case.category not in ("no_answer",):
                report.failures.append(result)

        cat = report.by_category.setdefault(case.category, {"total": 0, "hit": 0, "rr_sum": 0.0})
        cat["total"] += 1
        if hit:
            cat["hit"] += 1
            cat["rr_sum"] += rr

    return report


def format_report(report: EvalReport) -> str:
    """可读化报告，便于人工 review。"""
    s = report.summary()
    lines = [
        f"=== Golden Set: {s['golden_set']} ===",
        f"总用例 {s['total']} ｜ 命中 {s['hit']}（hit_rate={s['hit_rate']}）"
        f" ｜ 零命中 {s['zero_hit']}（zero_hit_rate={s['zero_hit_rate']}）"
        f" ｜ MRR={s['mrr']} ｜ 失败 {s['failures_count']}",
        "— by category —",
    ]
    for cat, c in sorted(s["by_category"].items()):
        hr = (c["hit"] / max(1, c["total"]))
        lines.append(f"  {cat}: {c['hit']}/{c['total']} (hit_rate={hr:.2%})")
    if report.failures:
        lines.append("— failures (top 10) —")
        for f in report.failures[:10]:
            lines.append(f"  - [{f.case.category}] q='{f.case.query}' predicted={f.predicted[:3]} "
                         f"expected={list(f.case.expected_ids)}")
    return "\n".join(lines)
