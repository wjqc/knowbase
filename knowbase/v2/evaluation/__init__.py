"""V2 真实评测集（P0-B）。

golden_set：固定 query → 期望 id 列表，用于评估 memory_search 的命中率与排序稳定性
runner：装载 fixture + 调 V1 server.search_impl + 产出命中率 / MRR / 零命中比例
fixtures：包含 semantic 同义改写、no-answer 冷僻词、ACL 案例（Phase 3 启用）
"""
from __future__ import annotations

from .golden_set import (
    GoldenCase,
    GoldenSet,
    load_default_golden_set,
)
from .parser_golden_set import (
    ParserCase,
    ParserGoldenSet,
    load_default_parser_golden_set,
)
from .parser_runner import (
    ParserCaseResult,
    ParserEvalReport,
    run_parser_golden_set,
    format_parser_report,
)
from .runner import EvalReport, run_golden_set
from .version_registry import (
    ParserVersion,
    compute_parser_versions,
    stamp_chunk_meta,
    known_parser_names,
)
from .compare import (
    CaseDelta,
    RegressionDiff,
    compare_reports,
    format_diff,
)

__all__ = [
    "GoldenCase",
    "GoldenSet",
    "load_default_golden_set",
    "EvalReport",
    "run_golden_set",
    "ParserCase",
    "ParserGoldenSet",
    "load_default_parser_golden_set",
    "ParserCaseResult",
    "ParserEvalReport",
    "run_parser_golden_set",
    "format_parser_report",
    "ParserVersion",
    "compute_parser_versions",
    "stamp_chunk_meta",
    "known_parser_names",
    "CaseDelta",
    "RegressionDiff",
    "compare_reports",
    "format_diff",
]
