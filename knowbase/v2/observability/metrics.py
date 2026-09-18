"""基线指标采集（P0-D）。

采集目标（V2 计划 §15.2 必须监控）：
- 检索延迟：search P50 / P95
- 命中率：search → read 转化（funnel）
- 零命中比例：search 无结果占比
- 索引时间：rebuild 耗时
- 写入漏斗：save/update/feedback 计数
- flag 决策审计：每次决策入审计日志

设计：
- 不引入 OpenTelemetry / Prometheus 等重型依赖（P0 阶段只用 stdlib）
- 指标先落内存 + usage_log（与 V1 一致），后续可重定向到外部
- 提供 baseline_report() 一键产出基线报告，供 Phase 6 切换对比
"""
from __future__ import annotations

import statistics
import time
from collections import Counter as _Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

# V1 索引/配置：v2 自身不依赖 V1，仅在跑基线探针时延迟导入。
# 用包级绝对路径（from ... = knowbase）保证 v2 子模块解耦。
try:
    from ... import index as v1_index  # knowbase.index
    from ... import config as v1_config  # knowbase.config
except Exception:  # pragma: no cover - v1 不可用时允许子模块独立被测
    v1_index = None  # type: ignore[assignment]
    v1_config = None  # type: ignore[assignment]


@dataclass
class LatencyRecorder:
    """线程/进程内延迟采样器（内存聚合）。"""
    samples: list[float] = field(default_factory=list)

    @contextmanager
    def measure(self):
        start = time.perf_counter()
        try:
            yield
        finally:
            self.samples.append((time.perf_counter() - start) * 1000.0)  # ms

    def summary(self) -> dict:
        if not self.samples:
            return {"count": 0}
        s = sorted(self.samples)
        n = len(s)
        return {
            "count": n,
            "p50_ms": round(s[max(0, n // 2 - 1)], 2),
            "p95_ms": round(s[min(n - 1, int(n * 0.95))], 2),
            "max_ms": round(s[-1], 2),
            "avg_ms": round(statistics.fmean(s), 2),
        }


@dataclass
class BaselineCollector:
    """一次性采集 V1 当前基线指标；不修改任何持久化数据。"""
    repo: Path
    search_latency: LatencyRecorder = field(default_factory=LatencyRecorder)
    rebuild_latency: float = 0.0
    extra: dict = field(default_factory=dict)

    def run_search_probe(self, queries: Iterable[str]) -> dict:
        """对一组查询采样 search 延迟并统计命中/零命中。"""
        if v1_index is None:
            raise RuntimeError("V1 index 不可用；BaselineCollector 需在 knowbase 仓库内运行")
        conn = v1_index.connect(self.repo)
        hit, miss, zero = 0, 0, 0
        try:
            for q in queries:
                with self.search_latency.measure():
                    res = v1_index.search(conn, q, limit=10)
                if not res:
                    zero += 1
                else:
                    hit += 1
        finally:
            conn.close()
        total = max(1, hit + zero)
        return {
            "queries": hit + zero,
            "hit": hit,
            "zero_hit": zero,
            "zero_hit_rate": round(zero / total, 4),
            "latency": self.search_latency.summary(),
        }

    def run_rebuild_probe(self) -> dict:
        if v1_index is None:
            raise RuntimeError("V1 index 不可用；BaselineCollector 需在 knowbase 仓库内运行")
        conn = v1_index.connect(self.repo)
        try:
            t0 = time.perf_counter()
            v1_index.rebuild(self.repo)
            self.rebuild_latency = (time.perf_counter() - t0) * 1000.0
        finally:
            conn.close()
        return {"rebuild_ms": round(self.rebuild_latency, 2)}

    def run_stats_snapshot(self) -> dict:
        if v1_index is None:
            raise RuntimeError("V1 index 不可用；BaselineCollector 需在 knowbase 仓库内运行")
        conn = v1_index.connect(self.repo)
        try:
            stats = v1_index.stats(conn)
        finally:
            conn.close()
        # 用法漏斗细分
        funnel = stats.get("funnel", {})
        search = funnel.get("search", 0) or 0
        auto_search = funnel.get("auto_search", 0) or 0
        read = funnel.get("read", 0) or 0
        return {
            "total_memories": stats.get("total", 0),
            "by_type": stats.get("by_type", {}),
            "by_scope": stats.get("by_scope", {}),
            "by_status": stats.get("by_status", {}),
            "written_last_7d": stats.get("written_last_7d", 0),
            "funnel": {
                "search": search,
                "auto_search": auto_search,
                "read": read,
                "save": funnel.get("save", 0),
                "feedback": funnel.get("feedback", 0),
                "feedback_helpful": funnel.get("feedback_helpful", 0),
                "search_to_read_rate": round(read / max(1, search), 4),
            },
            "top": stats.get("top", []),
        }

    def baseline_report(self) -> dict:
        """汇总基线报告；调用前先跑 run_* 探针。"""
        agent = v1_config.agent_name() if v1_config is not None else "unknown"
        return {
            "schema_version": 1,
            "agent_name": agent,
            "search_latency_ms": self.search_latency.summary(),
            "rebuild_ms": round(self.rebuild_latency, 2),
            **self.extra,
        }


def default_probe_queries() -> list[str]:
    """默认探针查询集：覆盖明确关键词 / 同义改写 / 短词 / 冷僻词，用于 Phase 6 切换对比。"""
    return [
        "EasyConnect 死锁",
        "EasyConnect",
        "vpn 启动失败",
        "macOS 网络接入",
        "缓存击穿",
        "Redis 雪崩",
        "knowbase 索引",
        "git 推送白名单",
        "Agent 接入",
        "llm 调用",
    ]