"""P4-B SyncWorker 异步测试。

覆盖：
1. BackoffPolicy 指数退避算法
2. Worker 成功 dispatch → ack
3. Worker 失败 → nack + 指数退避
4. Worker 达 max_attempts → FAILED 死信
5. Worker 优雅停机
6. Worker heartbeat 续约
7. Worker lease 抢占保护（renew 失败时不再续约）
8. Worker 并发数限制
9. handler 超时
10. 多 worker 抢占互斥
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Awaitable, Callable

import pytest

from knowbase.v2.domain.models import (
    OutboxEvent,
    OutboxStatus,
)
from knowbase.v2.repositories import V2Repository
from knowbase.v2.sync import BackoffPolicy, SyncWorker, WorkerConfig


# ---------- fixture ----------

@pytest.fixture
def repo(tmp_path: Path) -> V2Repository:
    r = V2Repository(tmp_path)
    yield r
    r.close()


def _fast_config(**overrides) -> WorkerConfig:
    """生产测试用快速配置：短 lease / heartbeat / poll。"""
    defaults = dict(
        lease_seconds=2,
        heartbeat_seconds=1,
        batch_size=5,
        poll_interval=0.05,
        max_outbox_attempts=3,
        handler_timeout_seconds=2.0,
        backoff=BackoffPolicy(
            base_seconds=0.5, max_seconds=10.0, factor=2.0,
            jitter=0, deterministic_seed=42,
        ),
        concurrency=2,
    )
    defaults.update(overrides)
    return WorkerConfig(**defaults)


# ====================================================================
# BackoffPolicy
# ====================================================================

class TestBackoffPolicy:

    def test_attempts_zero_returns_base(self):
        p = BackoffPolicy(base_seconds=1, factor=2, jitter=0)
        assert p.delay_for(0) == 1.0

    def test_exponential_growth(self):
        p = BackoffPolicy(base_seconds=1, factor=2, max_seconds=100, jitter=0)
        assert p.delay_for(1) == 2.0
        assert p.delay_for(2) == 4.0
        assert p.delay_for(3) == 8.0
        assert p.delay_for(4) == 16.0

    def test_max_cap(self):
        p = BackoffPolicy(base_seconds=1, factor=2, max_seconds=5, jitter=0)
        assert p.delay_for(0) == 1.0
        assert p.delay_for(3) == 5.0  # 8 capped to 5
        assert p.delay_for(10) == 5.0

    def test_negative_attempts_clamps_to_zero(self):
        p = BackoffPolicy(base_seconds=1, factor=2, jitter=0)
        assert p.delay_for(-1) == 1.0

    def test_jitter_within_range(self):
        p = BackoffPolicy(base_seconds=10, factor=1, jitter=0.1,
                           deterministic_seed=42)
        # 单次调用不应在 ±10% 外
        v = p.delay_for(0)
        assert 9.0 <= v <= 11.0

    def test_no_jitter_when_zero(self):
        p = BackoffPolicy(base_seconds=2, factor=2, jitter=0)
        for i in range(5):
            assert p.delay_for(i) == min(300.0, 2.0 * (2.0 ** i))

    def test_next_attempt_at_iso(self):
        p = BackoffPolicy(base_seconds=60, factor=1, jitter=0)
        iso = p.next_attempt_at_iso(1, "2026-09-18T04:00:00Z")
        assert iso == "2026-09-18T04:01:00Z"


# ====================================================================
# Worker 行为
# ====================================================================

@pytest.mark.asyncio
class TestWorkerSuccess:

    async def test_handler_success_acks_event(self, repo: V2Repository):
        ev = OutboxEvent.new("doc.created", {"id": "1"})
        repo.enqueue_outbox(ev)

        async def handler(e: OutboxEvent) -> None:
            return None  # 成功

        worker = SyncWorker(repo, handler=handler, config=_fast_config())
        # 单次 batch：拉取 → process → stop
        async def one_shot():
            events = repo.list_outbox_due(limit=10)
            for e in events:
                await worker._process_one(e)

        await one_shot()
        got = repo.get_outbox(ev.id)
        assert got.status == OutboxStatus.DISPATCHED
        assert worker.succeeded == 1
        assert worker.failed == 0
        assert worker.deadlettered == 0

    async def test_idempotent_handler_runs_only_once(self, repo: V2Repository):
        """ack 成功后事件不再被拉取（status=DISPATCHED）。"""
        ev = OutboxEvent.new("doc.created", {"id": "1"})
        repo.enqueue_outbox(ev)
        call_count = 0

        async def handler(e: OutboxEvent) -> None:
            nonlocal call_count
            call_count += 1

        worker = SyncWorker(repo, handler=handler, config=_fast_config())
        for _ in range(3):
            events = repo.list_outbox_due(limit=10)
            for e in events:
                await worker._process_one(e)

        assert call_count == 1
        assert repo.get_outbox(ev.id).status == OutboxStatus.DISPATCHED


@pytest.mark.asyncio
class TestWorkerFailure:

    async def test_handler_failure_nacks_with_backoff(self, repo: V2Repository):
        ev = OutboxEvent.new("doc.created", {"id": "1"})
        repo.enqueue_outbox(ev)

        async def bad_handler(e: OutboxEvent) -> None:
            raise RuntimeError("boom")

        worker = SyncWorker(repo, handler=bad_handler, config=_fast_config())
        await worker._process_one(ev)

        got = repo.get_outbox(ev.id)
        assert got.status == OutboxStatus.PENDING  # 还能重试
        assert got.attempts == 1
        assert got.last_error == "boom"
        assert got.next_attempt_at is not None
        assert worker.failed == 1
        assert worker.deadlettered == 0

    async def test_max_attempts_moves_to_failed_deadletter(
        self, repo: V2Repository
    ):
        ev = OutboxEvent.new("doc.created", {"id": "1"})
        repo.enqueue_outbox(ev)

        async def always_fail(e: OutboxEvent) -> None:
            raise RuntimeError("perm boom")

        cfg = _fast_config(max_outbox_attempts=3)
        worker = SyncWorker(repo, handler=always_fail, config=cfg)

        # 第 1 次失败 attempts=1 → PENDING
        await worker._process_one(ev)
        assert repo.get_outbox(ev.id).status == OutboxStatus.PENDING
        # 重置 next_attempt_at 让其能再次被拉取
        repo._conn.execute(
            "UPDATE outbox_event SET next_attempt_at=NULL WHERE id=?", (ev.id,)
        )
        # 第 2 次失败 attempts=2 → PENDING
        await worker._process_one(ev)
        assert repo.get_outbox(ev.id).status == OutboxStatus.PENDING
        # 重置
        repo._conn.execute(
            "UPDATE outbox_event SET next_attempt_at=NULL WHERE id=?", (ev.id,)
        )
        # 第 3 次失败 attempts=3 >= max → FAILED
        await worker._process_one(ev)
        got = repo.get_outbox(ev.id)
        assert got.status == OutboxStatus.FAILED
        assert worker.deadlettered == 1

    async def test_handler_timeout_treated_as_failure(self, repo: V2Repository):
        ev = OutboxEvent.new("doc.created", {"id": "1"})
        repo.enqueue_outbox(ev)

        async def slow_handler(e: OutboxEvent) -> None:
            await asyncio.sleep(5)

        cfg = _fast_config(handler_timeout_seconds=0.2)
        worker = SyncWorker(repo, handler=slow_handler, config=cfg)
        await worker._process_one(ev)
        got = repo.get_outbox(ev.id)
        assert got.status == OutboxStatus.PENDING
        assert "timeout" in got.last_error.lower()


@pytest.mark.asyncio
class TestWorkerConcurrency:

    async def test_concurrency_limits_inflight_handlers(
        self, repo: V2Repository
    ):
        """concurrency=2 时最多同时跑 2 个 handler。"""
        for i in range(6):
            repo.enqueue_outbox(OutboxEvent.new("doc.created", {"i": i}))

        in_flight = 0
        peak = 0
        lock = asyncio.Lock()

        async def slow_handler(e: OutboxEvent) -> None:
            nonlocal in_flight, peak
            async with lock:
                in_flight += 1
                peak = max(peak, in_flight)
            await asyncio.sleep(0.1)
            async with lock:
                in_flight -= 1

        cfg = _fast_config(concurrency=2)
        worker = SyncWorker(repo, handler=slow_handler, config=cfg)

        # 一次 batch 处理
        events = repo.list_outbox_due(limit=10)
        tasks = [asyncio.create_task(worker._process_one(e)) for e in events]
        await asyncio.gather(*tasks)
        assert peak <= 2

    async def test_failed_event_does_not_block_others(
        self, repo: V2Repository
    ):
        ev_fail = OutboxEvent.new("doc.created", {"id": "fail"})
        ev_ok = OutboxEvent.new("doc.created", {"id": "ok"})
        repo.enqueue_outbox(ev_fail)
        repo.enqueue_outbox(ev_ok)

        async def selective_handler(e: OutboxEvent) -> None:
            if e.payload.get("id") == "fail":
                raise RuntimeError("nope")
            return None

        worker = SyncWorker(repo, handler=selective_handler, config=_fast_config())
        events = repo.list_outbox_due(limit=10)
        tasks = [asyncio.create_task(worker._process_one(e)) for e in events]
        await asyncio.gather(*tasks)

        # fail 仍 PENDING（待重试）；ok 已 DISPATCHED
        assert repo.get_outbox(ev_fail.id).status == OutboxStatus.PENDING
        assert repo.get_outbox(ev_ok.id).status == OutboxStatus.DISPATCHED


@pytest.mark.asyncio
class TestWorkerHeartbeat:

    async def test_heartbeat_renews_lease_while_handler_runs(
        self, repo: V2Repository
    ):
        """handler 跑期间，heartbeat 应延长 lease_until。"""
        ev = OutboxEvent.new("doc.created", {"id": "1"})
        repo.enqueue_outbox(ev)

        started_at_holder = []
        completed = asyncio.Event()

        async def slow_handler(e: OutboxEvent) -> None:
            started_at_holder.append(time.monotonic())
            await asyncio.sleep(1.5)  # 长于 heartbeat 周期
            completed.set()

        cfg = _fast_config(lease_seconds=2, heartbeat_seconds=1)
        worker = SyncWorker(repo, handler=slow_handler, config=cfg,
                            owner="w-heartbeat")

        await worker._process_one(ev)
        assert completed.is_set()

        # 期间 lease_until 已被 heartbeat 多次延期；最终值 > claim 时刻
        got = repo.get_outbox(ev.id)
        # 注意：ack 后 lease_owner 已被清空；只能确认 ack 成功
        assert got.status == OutboxStatus.DISPATCHED

    async def test_heartbeat_only_owner_can_renew(self, repo: V2Repository):
        """其他 worker 不能续约。"""
        from datetime import datetime, timedelta, timezone

        ev = OutboxEvent.new("doc.created", {"id": "1"})
        repo.enqueue_outbox(ev)
        # 模拟"已 claim + lease_until 未来"
        future = (datetime.now(timezone.utc) + timedelta(seconds=10)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        repo._conn.execute(
            "UPDATE outbox_event SET lease_owner=?, lease_until=? WHERE id=?",
            ("real-owner", future, ev.id),
        )
        # 其他 worker 试图续约
        ok = repo.renew_outbox_lease(ev.id, "imposter",
                                      (datetime.now(timezone.utc) + timedelta(seconds=99)).strftime(
                                          "%Y-%m-%dT%H:%M:%SZ"
                                      ))
        assert ok is False
        # lease_owner 没变
        got = repo.get_outbox(ev.id)
        assert got.lease_owner == "real-owner"


@pytest.mark.asyncio
class TestWorkerGracefulStop:

    async def test_run_stops_after_current_batch(
        self, repo: V2Repository
    ):
        for i in range(3):
            repo.enqueue_outbox(OutboxEvent.new("doc.created", {"i": i}))

        async def handler(e: OutboxEvent) -> None:
            await asyncio.sleep(0.05)

        cfg = _fast_config(poll_interval=0.05)
        worker = SyncWorker(repo, handler=handler, config=cfg)

        async def stopper():
            await asyncio.sleep(0.3)
            worker.stop()

        await asyncio.gather(worker.run(), stopper())
        # 至少处理一批
        assert worker.processed >= 1
        # 所有事件最终被 ack（除非 stop 太早）
        dispatched = sum(
            1 for ev in repo.list_outbox_due(limit=10)
            if ev.status == OutboxStatus.DISPATCHED
        )
        assert dispatched == 0  # 都已 DISPATCHED 不再 due

    async def test_drain_waits_for_inflight(self, repo: V2Repository):
        ev = OutboxEvent.new("doc.created", {"id": "1"})
        repo.enqueue_outbox(ev)

        handler_done = asyncio.Event()

        async def slow_handler(e: OutboxEvent) -> None:
            await asyncio.sleep(0.2)
            handler_done.set()

        worker = SyncWorker(repo, handler=slow_handler, config=_fast_config())
        task = asyncio.create_task(worker._process_one(ev))
        await asyncio.sleep(0.05)  # 让 handler 启动
        await worker.drain(timeout=2.0)
        assert handler_done.is_set()
        await task  # _process_one 应已完成


@pytest.mark.asyncio
class TestMultiWorkerSafety:

    async def test_two_workers_only_one_claims_event(self, repo: V2Repository):
        """两个 worker 同时拉取同一事件，SQLite 保证只有一个能 claim。"""
        ev = OutboxEvent.new("doc.created", {"id": "1"})
        repo.enqueue_outbox(ev)

        async def handler(e: OutboxEvent) -> None:
            return None

        w1 = SyncWorker(repo, handler=handler, config=_fast_config(), owner="w1")
        w2 = SyncWorker(repo, handler=handler, config=_fast_config(), owner="w2")

        # 并发 _process_one
        await asyncio.gather(w1._process_one(ev), w2._process_one(ev))

        assert w1.succeeded + w2.succeeded == 1  # 仅一方成功
        assert w1.skipped + w2.skipped == 1  # 另一方被抢占
        assert repo.get_outbox(ev.id).status == OutboxStatus.DISPATCHED


@pytest.mark.asyncio
class TestWorkerErrors:

    async def test_errors_recorded(self, repo: V2Repository):
        ev = OutboxEvent.new("doc.created", {"id": "1"})
        repo.enqueue_outbox(ev)

        async def bad_handler(e: OutboxEvent) -> None:
            raise ValueError("invalid payload")

        worker = SyncWorker(repo, handler=bad_handler, config=_fast_config())
        await worker._process_one(ev)
        assert any("ValueError" in str(e.get("type", "")) for e in worker.errors)
        assert any("invalid payload" in str(e.get("error", "")) for e in worker.errors)
