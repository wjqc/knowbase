"""P4-B 指数退避策略。

V2 计划 §8.3 要求：失败按指数退避重试，达 max_attempts 进入死信。

设计：
- 基础等待 = base_seconds * factor^attempts
- 上限 = max_seconds（防止无限增长）
- 抖动 = ±jitter 比例（防止 thundering herd）
- 同步 API：延迟可被预测（测试时禁用 jitter）
"""
from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass(frozen=True)
class BackoffPolicy:
    """指数退避策略。

    Attributes:
        base_seconds: 基础等待（首次失败后）
        max_seconds: 最大等待（避免无限增长）
        factor: 增长系数（>=1.0）
        jitter: 随机抖动比例（0 表示无抖动；0.1 表示 ±10%）
        deterministic_seed: 测试用固定随机种子（None 则用系统随机）
    """

    base_seconds: float = 1.0
    max_seconds: float = 300.0
    factor: float = 2.0
    jitter: float = 0.1
    deterministic_seed: int | None = None

    def delay_for(self, attempts: int) -> float:
        """返回第 `attempts` 次失败后的等待秒数。

        - attempts=0 → base_seconds
        - attempts=1 → base_seconds * factor
        - 以此类推，直至 max_seconds
        - 叠加 jitter 比例随机扰动
        """
        if attempts < 0:
            attempts = 0
        raw = self.base_seconds * (self.factor ** attempts)
        capped = min(self.max_seconds, raw)
        if self.jitter <= 0:
            return capped
        rng = random.Random(self.deterministic_seed) if self.deterministic_seed is not None else random
        jitter_amount = capped * self.jitter
        return max(0.0, capped + rng.uniform(-jitter_amount, jitter_amount))

    def next_attempt_at_iso(self, attempts: int, now_iso: str) -> str:
        """返回 ISO-8601 时间戳 + delay_for(attempts) 秒。

        now_iso 形如 "2026-09-18T04:00:00Z"。
        """
        from datetime import datetime, timedelta, timezone
        # 解析 Z 后缀
        ts = datetime.strptime(now_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        delay = self.delay_for(attempts)
        return (ts + timedelta(seconds=delay)).strftime("%Y-%m-%dT%H:%M:%SZ")
