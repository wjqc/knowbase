"""P0-D V1 基线指标测试 + observability 错误测试。"""
from __future__ import annotations

import pytest

from knowbase.v2.observability.errors import (
    FlagDisabledError,
    NotConfiguredError,
    ParseError,
)
from knowbase.v2.observability.metrics import LatencyRecorder, default_probe_queries


def test_flag_disabled_error_carries_flag_name():
    e = FlagDisabledError("v2_ingestion")
    assert "v2_ingestion" in str(e)
    assert e.flag == "v2_ingestion"


def test_not_configured_error():
    with pytest.raises(NotConfiguredError):
        raise NotConfiguredError("v2.db_path not set")


def test_parse_error_with_path():
    e = ParseError(parser="pdf", reason="corrupt", path="/tmp/a.pdf")
    msg = str(e)
    assert "pdf" in msg and "corrupt" in msg and "/tmp/a.pdf" in msg
    assert e.parser == "pdf" and e.path == "/tmp/a.pdf"


def test_latency_recorder_summary():
    rec = LatencyRecorder()
    for v in [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]:
        rec.samples.append(v)
    s = rec.summary()
    assert s["count"] == 10
    assert s["max_ms"] == 100.0
    # 至少 p50/p95/avg/max 应存在
    for k in ("p50_ms", "p95_ms", "avg_ms", "max_ms"):
        assert k in s
        assert isinstance(s[k], (int, float))


def test_latency_recorder_empty():
    rec = LatencyRecorder()
    s = rec.summary()
    assert s == {"count": 0}


def test_default_probe_queries_have_known_count():
    qs = default_probe_queries()
    assert len(qs) >= 8
    # V1 §3.2 报告里 8/8 命中的关键词必须出现在 probe 集中
    text = " ".join(qs)
    for keyword in ("EasyConnect", "vpn", "Redis", "knowbase"):
        assert keyword in text
