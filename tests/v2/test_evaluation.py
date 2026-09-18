"""P0-B Golden Set 单元测试。"""
from __future__ import annotations

import pytest

from knowbase.v2.evaluation import (
    EvalReport,
    GoldenCase,
    GoldenSet,
    load_default_golden_set,
    run_golden_set,
)


def test_default_golden_set_has_three_categories():
    gs = load_default_golden_set()
    assert isinstance(gs, GoldenSet)
    cats = {c.category for c in gs.cases}
    assert {"exact", "semantic", "no_answer"}.issubset(cats)
    assert len(gs) >= 8


def test_golden_case_validation():
    case = GoldenCase(query="x", expected_ids=("M-000001",), category="exact")
    assert case.min_rank == 1
    assert case.category == "exact"


def test_runner_with_fake_search_fn_records_results():
    gs = GoldenSet(name="t", version=1, cases=[
        GoldenCase(query="hit", expected_ids=("M-000001",), category="exact"),
        GoldenCase(query="miss", expected_ids=("M-000002",), category="exact"),
        GoldenCase(query="none", expected_ids=(), category="no_answer"),
    ])

    def fake_search(q):
        return {
            "hit": "命中 1 条\n- [M-000001] t1",
            "miss": "未命中「miss」",
            "none": "未命中「none」",
        }[q]

    report = run_golden_set(gs, search_fn=fake_search, repo=None)
    summary = report.summary()
    assert summary["total"] == 3
    assert summary["hit"] == 1
    assert summary["zero_hit"] == 2  # miss + none(no_answer 期望零命中)
    assert summary["hit_rate"] == pytest.approx(0.3333, abs=0.01)
    # miss 失败入列
    assert any(f.case.query == "miss" for f in report.failures)


def test_runner_supports_category_filter():
    gs = GoldenSet(name="t", version=1, cases=[
        GoldenCase(query="a", expected_ids=("M-000001",), category="exact"),
        GoldenCase(query="b", expected_ids=(), category="no_answer"),
    ])

    def fake(q):
        return ""

    report = run_golden_set(gs, search_fn=fake, repo=None, case_filter=["no_answer"])
    assert report.total == 1
    assert report.by_category == {"no_answer": {"total": 1, "hit": 0, "rr_sum": 0.0}}


def test_format_report_contains_key_metrics():
    gs = GoldenSet(name="t", version=1, cases=[
        GoldenCase(query="x", expected_ids=("M-000001",), category="exact"),
    ])

    def fake(q):
        return "命中 1 条\n- [M-000001] t1"

    report = run_golden_set(gs, search_fn=fake, repo=None)
    from knowbase.v2.evaluation.runner import format_report
    text = format_report(report)
    assert "Golden Set: t" in text
    assert "hit_rate=" in text
    assert "MRR=" in text


def test_default_golden_set_covers_v1_known_weak_spots():
    """V1 §3.2 报告：同义改写 0/4 命中；本 fixture 必须保留这些 case 供 V2 对比。"""
    gs = load_default_golden_set()
    semantic = gs.by_category("semantic")
    assert len(semantic) >= 4
    queries = {c.query for c in semantic}
    # 至少包含「Agent 怎么接入」「缓存击穿」这两条已知的 V1 短板
    assert any("Agent" in q for q in queries)
    assert any("缓存击穿" in q or "雪崩" in q for q in queries)
