"""Golden Set 数据结构与默认 fixture（P0-B）。

设计目标：
- 真实评测：直接调 V1 server.search_impl，对比命中 id 与期望 id
- 覆盖三类场景：
  1. 明确关键词（标题/正文出现过的具体名词、报错字符串）
  2. 同义改写（自然语言不同说法但指向同一条记忆；当前 V1 命中率 0/4）
  3. 零命中（应返回「未命中」且不召回任何记忆）
- ACL 案例占位，Phase 3 启用时填实 id

字段语义：
- query：检索词
- expected_ids：期望命中的记忆 id 列表（按相关度降序，runner 取首个计 MRR）
- category：'exact' | 'semantic' | 'no_answer' | 'acl'（便于分类统计）
- min_rank：期望命中的最低名次（默认 1，可放宽到 5）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


@dataclass(frozen=True)
class GoldenCase:
    query: str
    expected_ids: tuple[str, ...]
    category: str = "exact"          # exact | semantic | no_answer | acl
    min_rank: int = 1                 # 期望命中的最低名次
    note: str = ""


@dataclass
class GoldenSet:
    name: str
    version: int
    cases: list[GoldenCase] = field(default_factory=list)

    def add(self, case: GoldenCase) -> None:
        self.cases.append(case)

    def extend(self, cases: Iterable[GoldenCase]) -> None:
        self.cases.extend(cases)

    def by_category(self, category: str) -> list[GoldenCase]:
        return [c for c in self.cases if c.category == category]

    def __len__(self) -> int:
        return len(self.cases)


def load_default_golden_set() -> GoldenSet:
    """装载默认评测集：与 docs/knowbase-v2-upgrade-plan.md §3.2 对齐。

    注：expected_ids 当前留空，标注「待补」；
    Phase 0-B 的目标不是「跑出高分」，而是「可装载、可运行、可对比」。
    真实 id 来自仓库内现存记忆（可通过 memory_search 实测后回填）。
    """
    return GoldenSet(
        name="knowbase-default",
        version=1,
        cases=[
            # === 明确关键词（V1 命中率高，应作为基线）===
            GoldenCase(
                query="EasyConnect 死锁",
                expected_ids=(),  # 待回填
                category="exact",
                note="V1 §3.2 报告：8/8 命中",
            ),
            GoldenCase(
                query="vpn 启动失败",
                expected_ids=(),
                category="exact",
            ),
            GoldenCase(
                query="Redis 雪崩",
                expected_ids=(),
                category="exact",
            ),
            GoldenCase(
                query="knowbase 索引",
                expected_ids=(),
                category="exact",
            ),
            GoldenCase(
                query="git 推送白名单",
                expected_ids=(),
                category="exact",
            ),

            # === 同义改写（V1 命中率 0/4，是 Phase 2 重点改善项）===
            GoldenCase(
                query="Agent 怎么接入 knowbase",
                expected_ids=(),
                category="semantic",
                note="自然语言改写，V1 FTS5 不命中",
            ),
            GoldenCase(
                query="缓存击穿怎么避免",
                expected_ids=(),
                category="semantic",
                note="同义改写，期望命中 Redis 雪崩/击穿类记忆",
            ),
            GoldenCase(
                query="客户端连不上服务端怎么办",
                expected_ids=(),
                category="semantic",
                note="应命中 vpn/网络接入类记忆",
            ),
            GoldenCase(
                query="怎么回退到上一个版本",
                expected_ids=(),
                category="semantic",
                note="应命中 git/回滚类记忆",
            ),

            # === 零命中（必须有「未命中」返回）===
            GoldenCase(
                query="量子纠缠态坍缩",
                expected_ids=(),
                category="no_answer",
                note="与 knowbase 业务无关，期望 0 命中",
            ),
            GoldenCase(
                query="zzznonexistentkeyword123",
                expected_ids=(),
                category="no_answer",
            ),

            # === ACL（Phase 3 启用；占位以保证 runner 不漏统计）===
            GoldenCase(
                query="内部项目账号",
                expected_ids=(),
                category="acl",
                note="Phase 3 实装 ACL 后回填；当前期望：V1 不做硬过滤，可能误命中",
            ),
        ],
    )
