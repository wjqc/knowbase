"""P4-B 同步 Worker。

实现 V2 计划 §8.3「同步恢复语义」：
- worker 使用 lease + heartbeat + fencing token，避免多个实例重复接管
- 事件至少一次投递（消费者必须幂等）
- 失败按指数退避重试，达 max_attempts 进入死信（poison document 隔离）
- 优雅停机：完成当前 in-flight 任务后退出

主循环流程：
    while not stop:
        events = repo.list_outbox_due(limit=batch_size)
        for ev in events:
            spawn _process_one(ev)   # 异步 + heartbeat
        if no events: sleep(poll_interval)
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from ..domain.models import OutboxEvent, OutboxStatus
from ..repositories.sqlite_repo import V2Repository
from .backoff import BackoffPolicy


# ---------- 时区工具 ----------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_plus(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


# ---------- 类型 ----------

# 异步 handler：成功 → 正常返回；失败 → raise 异常
OutboxHandler = Callable[[OutboxEvent], Awaitable[None]]


# ---------- 配置 ----------

@dataclass
class WorkerConfig:
    """Worker 行为配置。

    Attributes:
        lease_seconds: lease 时长（秒）。worker 持有 lease_until 后，
            其他 worker 在 lease 过期前不能 claim。
        heartbeat_seconds: heartbeat 周期（秒）。worker 每 N 秒续约 lease_until。
        batch_size: 一次拉取多少 outbox 事件。
        poll_interval: 空闲时轮询间隔（秒）。
        max_outbox_attempts: outbox 失败最大尝试次数，达到后进入死信 (FAILED)。
        handler_timeout_seconds: 单个 handler 最大执行时间（防止 handler 卡死）。
        backoff: 指数退避策略。
        concurrency: 并发处理上限（最多同时跑多少个 handler）。
        owner_prefix: worker owner 标识前缀，便于诊断；最终 owner = f"{owner_prefix}-{id(self)}"
    """

    lease_seconds: int = 30
    heartbeat_seconds: int = 10
    batch_size: int = 10
    poll_interval: float = 0.1
    max_outbox_attempts: int = 5
    handler_timeout_seconds: float = 60.0
    backoff: BackoffPolicy = field(default_factory=BackoffPolicy)
    concurrency: int = 4
    owner_prefix: str = "sync-worker"


# ---------- Worker ----------

class SyncWorker:
    """异步 outbox consumer。

    使用：
        async def my_handler(ev: OutboxEvent) -> None:
            ...  # 处理事件（成功→返回；失败→raise）

        worker = SyncWorker(repo, handler=my_handler)
        await worker.run()  # 阻塞直到 stop()
    """

    def __init__(
        self,
        repo: V2Repository,
        *,
        handler: OutboxHandler,
        config: WorkerConfig | None = None,
        owner: str | None = None,
    ):
        self.repo = repo
        self.handler = handler
        self.config = config or WorkerConfig()
        self.owner = owner or f"{self.config.owner_prefix}-{id(self):x}"
        self._stop_event = asyncio.Event()
        # ev_id -> heartbeat task
        self._heartbeats: dict[str, asyncio.Task] = {}
        # 当前 in-flight handler task
        self._inflight: set[asyncio.Task] = set()
        # 并发上限（任何调用 _process_one 的路径都受其约束）
        self._semaphore = asyncio.Semaphore(self.config.concurrency)
        # 统计
        self.processed = 0
        self.succeeded = 0
        self.failed = 0
        self.deadlettered = 0
        self.skipped = 0  # claim 失败（被抢占）
        self.errors: list[dict[str, Any]] = []

    # ---------- 生命周期 ----------

    async def run(self) -> None:
        """主循环，直到 stop() 被调用。"""
        while not self._stop_event.is_set():
            try:
                due = await self._fetch_due()
            except Exception as e:
                self.errors.append({"phase": "fetch", "error": str(e)})
                await self._sleep_or_stop(self.config.poll_interval)
                continue

            if not due:
                await self._sleep_or_stop(self.config.poll_interval)
                continue

            # 限流由 _process_one 内部的 self._semaphore 负责
            tasks = [asyncio.create_task(self._process_one(ev)) for ev in due]
            self._inflight.update(tasks)
            try:
                await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                self._inflight.difference_update(tasks)

    def stop(self) -> None:
        """请求停机。run() 在当前 batch 完成后退出。"""
        self._stop_event.set()

    async def drain(self, timeout: float | None = None) -> None:
        """等待所有 in-flight handler + heartbeat 完成后退出。"""
        if self._inflight:
            if timeout is not None:
                await asyncio.wait_for(
                    asyncio.gather(*self._inflight, return_exceptions=True),
                    timeout=timeout,
                )
            else:
                await asyncio.gather(*self._inflight, return_exceptions=True)
        # 取消所有 heartbeat
        for hb in list(self._heartbeats.values()):
            hb.cancel()
        if self._heartbeats:
            await asyncio.gather(*self._heartbeats.values(), return_exceptions=True)
        self._heartbeats.clear()

    # ---------- 单事件处理 ----------

    async def _fetch_due(self) -> list[OutboxEvent]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, lambda: self.repo.list_outbox_due(limit=self.config.batch_size)
        )

    async def _process_one(self, ev: OutboxEvent) -> None:
        # 注册自己到 in-flight（让 drain 等到直接调用 _process_one 的情况）
        task = asyncio.current_task()
        if task is not None:
            self._inflight.add(task)
        try:
            # 限流：最多 concurrency 个并发
            async with self._semaphore:
                await self._do_process(ev)
        finally:
            if task is not None:
                self._inflight.discard(task)

    async def _do_process(self, ev: OutboxEvent) -> None:
        """单个事件的完整处理流程：claim → handler → ack/nack。"""
        lease_until = _now_plus(self.config.lease_seconds)
        # claim 必须在仓库线程执行
        loop = asyncio.get_running_loop()
        claimed = await loop.run_in_executor(
            None, lambda: self.repo.claim_outbox(ev.id, self.owner, lease_until)
        )
        if not claimed:
            self.skipped += 1
            return

        self.processed += 1
        # 启动 heartbeat
        hb = asyncio.create_task(self._heartbeat(ev.id))
        self._heartbeats[ev.id] = hb
        try:
            # 跑 handler（带超时）；handler 可为 sync 或 async 函数
            try:
                result = self.handler(ev)
                if asyncio.iscoroutine(result):
                    await asyncio.wait_for(
                        result, timeout=self.config.handler_timeout_seconds
                    )
            except asyncio.TimeoutError:
                raise TimeoutError(
                    f"handler timeout after {self.config.handler_timeout_seconds}s"
                )
            # ack
            ok = await loop.run_in_executor(None, self.repo.ack_outbox, ev.id)
            if ok:
                self.succeeded += 1
            else:
                # lease 已不在（被抢占），不应发生；记录异常
                self.errors.append({
                    "ev_id": ev.id, "phase": "ack", "error": "lease lost"
                })
                self.failed += 1
        except Exception as e:
            await self._on_failure(ev.id, e)
        finally:
            hb.cancel()
            try:
                await hb
            except (asyncio.CancelledError, Exception):
                pass
            self._heartbeats.pop(ev.id, None)

    async def _on_failure(self, ev_id: str, error: Exception) -> None:
        """handler 失败 → nack + 指数退避（attempts+1）。

        - 达 max_attempts → FAILED（死信，不阻塞其它事件）
        - 未达 → PENDING + next_attempt_at = now + backoff(attempts)

        注意：必须用 ev_id 重新从 DB 拉取最新 attempts，因为 ``ev`` 对象可能已被
        前几次失败过期（直接重试同一对象时 in-memory attempts 是旧值）。
        """
        loop = asyncio.get_running_loop()
        ev = await loop.run_in_executor(None, self.repo.get_outbox, ev_id)
        if ev is None or ev.status != OutboxStatus.PENDING:
            # 已被并发改动（ack 或 FAILED），忽略
            return
        next_attempts = ev.attempts + 1
        if next_attempts >= self.config.max_outbox_attempts:
            # 死信：直接走 nack（max_attempts），repo 会设为 FAILED
            await loop.run_in_executor(
                None,
                lambda: self.repo.nack_outbox(
                    ev.id, str(error), next_attempt_at=None,
                    max_attempts=self.config.max_outbox_attempts,
                ),
            )
            self.deadlettered += 1
        else:
            next_at = self.config.backoff.next_attempt_at_iso(
                next_attempts, _now_iso()
            )
            await loop.run_in_executor(
                None,
                lambda: self.repo.nack_outbox(
                    ev.id, str(error), next_attempt_at=next_at,
                    max_attempts=self.config.max_outbox_attempts,
                ),
            )
            self.failed += 1
        self.errors.append({
            "ev_id": ev.id, "phase": "handler",
            "error": str(error), "type": type(error).__name__,
        })

    async def _heartbeat(self, ev_id: str) -> None:
        """周期续约 lease_until。"""
        loop = asyncio.get_running_loop()
        try:
            while True:
                await asyncio.sleep(self.config.heartbeat_seconds)
                new_until = _now_plus(self.config.lease_seconds)
                ok = await loop.run_in_executor(
                    None, self.repo.renew_outbox_lease, ev_id, self.owner, new_until
                )
                if not ok:
                    # 不再持有 lease（被抢占或已 ack）；退出
                    return
        except asyncio.CancelledError:
            return

    # ---------- 内部 ----------

    async def _sleep_or_stop(self, seconds: float) -> None:
        """sleep 但可被 stop() 提前唤醒。"""
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
