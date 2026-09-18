"""把 Phase 4-B 同步 Worker 收尾产生的 5 条经验保存到 ~/knowbase（隔离环境变量，不污染 V2 测试 tmp）。

- 踩坑（P-2026-0010）：SQLite 跨线程 "Recursive use of cursors not allowed"
- 决策（D-2026-0005）：_on_failure 用 ev_id 重新拉取最新 attempts（避免 in-memory 过期）
- 决策（D-2026-0006）：Semaphore 放 __init__ 成员；_process_one 自注册 _inflight
- 流程（W-2026-0006）：P4-B SyncWorker 异步 outbox consumer 实施
- 流程（W-2026-0007）：Phase 4-B 验证（P4-B 20/20，V2 264/264）
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# 关键：隔离 KNOWBASE_REPO_PATH / KNOWBASE_CONFIG，让 server.save_impl 走默认 ~/knowbase
for k in ("KNOWBASE_REPO_PATH", "KNOWBASE_CONFIG", "KNOWBASE_V2_INGESTION",
          "KNOWBASE_V2_IDEMPOTENT_INGEST", "KNOWBASE_V2_PARSER_MARKDOWN",
          "KNOWBASE_V2_PARSER_TXT", "KNOWBASE_V2_PARSER_PDF", "KNOWBASE_V2_PARSER_DOCX"):
    os.environ.pop(k, None)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from knowbase import __main__ as cli  # noqa: E402
from knowbase import config, server  # noqa: E402

# 确保真实仓库 init 完毕（幂等）；lock 没权限时跳过（save_impl 不依赖 init）
print(f"repo_path = {config.repo_path()}")
try:
    cli.main(["init"])
except PermissionError as e:
    print(f"[skip init] {e}")


P1 = (
    "pitfall",
    "SQLite 跨线程：check_same_thread=False 仍需应用层锁，否则「Recursive use of cursors not allowed」（Phase 4-B）",
    """## 现象
P4-B SyncWorker 是 asyncio 协程，但 DB 操作（sqlite_repo）是同步 API。在 worker 内
通过 ``loop.run_in_executor(None, repo.xxx)`` 把同步调用丢到默认线程池：

- 单线程测试时一切正常；
- 一旦引入并发（concurrency > 1、或多个事件并发处理），抛
  ``sqlite3.ProgrammingError: Recursive use of cursors not allowed``；
- 或偶发 ``database is locked``。

## 原因
1. ``sqlite3.connect(..., check_same_thread=False)`` **只**关闭「跨线程访问检查」，但
   SQLite 本身「同一连接同一时刻只能有一个 cursor 在执行」的限制不变；
2. 默认 ``executor`` 是个 ``ThreadPoolExecutor``，多个事件 → 多个线程同时拿同一个
   ``self._conn`` 执行 SQL → cursor 冲突；
3. ``isolation_level=None``（autocommit）不会改变这个限制。

## 正确做法
1. ``__init__`` 显式建锁：
   ```python
   self._lock = threading.RLock()
   ```
2. 集中 helper 走锁，避免散落 `with self._lock:`：
   ```python
   def _e(self, sql: str, params: tuple = ()):
       with self._lock:
           return self._conn.execute(sql, params)
   ```
3. 所有 SQL 调用统一走 ``self._e(sql, params)``（60+ 处批量替换）。
4. ``__init__`` 里 2 条 PRAGMA 单线程 init 可保留 ``self._conn.execute``（无并发风险）；
   helper 自身的 ``self._conn.execute`` 也保留（已经在锁内）。

## 取舍
- 没把 repo 改成 async：async 仓储会让 SQLite 测试 fixture 复杂化、单元测试难写；
  ``run_in_executor`` + 应用层锁是「同步仓储 + 异步 worker」最简单稳定的桥。
- 用 ``RLock`` 而非 ``Lock``：允许同一线程在持有锁时再次获取（避免在 helper 内嵌套调
  仓储其他方法时死锁）。""",
    ["sqlite", "threading", "executor", "rlock"],
)


P2 = (
    "decision",
    "_on_failure 必须用 ev_id 重新拉取最新 attempts（不要信 in-memory ev.attempts）",
    """## 背景
P4-B 测试 ``test_max_attempts_moves_to_failed_deadletter`` 一开始失败：测试用同一个
``OutboxEvent`` 对象连续失败 3 次（max_outbox_attempts=3），期望 ``deadlettered == 1``，
但实际 ``deadlettered == 0``。

## 现象
``_on_failure(ev, error)`` 用 ``ev.attempts + 1`` 算下一次 attempts；测试连续 3 次都传
同一个 ev 对象，in-memory ``ev.attempts`` 永远是 0（从未更新），所以 3 次都算
``next_attempts = 1 < 3``，进入正常 nack 退避，never 死信。

## 决策
1. 改签名 ``_on_failure(self, ev_id: str, error: Exception)``，不再接收 ev 对象；
2. 内部用 ``self.repo.get_outbox(ev_id)`` 重新从 DB 拉取最新 ev（DB 里的 attempts
   在每次 nack 时已 +1）；
3. 拉不到（已被并发 ack）或 status != PENDING → 直接 return，避免与 ack 竞态。

## 影响
- 任何「重试同一对象」的测试都得到正确死信判定；
- 防止 in-memory 与 DB 状态不一致；
- ack / nack 仍然是唯一的「attempts + 1」写入点，attempts 永远递增单调。

## 反例
继续传 ev 对象 + 维护一个 ``self._attempts`` 计数器 = 重蹈 in-memory 与 DB 不一致
覆辙，且无法在多 worker 抢占场景下工作。""",
    ["sync", "outbox", "deadletter", "state-machine"],
)


P3 = (
    "decision",
    "Semaphore 放在 SyncWorker.__init__ 成员；_process_one 用 current_task() 自注册 _inflight",
    """## 背景
P4-B 一开始两个测试接连失败：
1. ``test_concurrency_limits_inflight_handlers``：测试直接
   ``asyncio.create_task(worker._process_one(e))`` × 6，期望 peak ≤ 2，实际 peak = 6；
2. ``test_drain_waits_for_inflight``：测试同样直接调 ``_process_one``，期望 drain 后
   ``handler_done.is_set() is True``，实际 handler 还在跑。

## 原因
1. ``asyncio.Semaphore(self.config.concurrency)`` 在 ``run()`` 内、每次 batch 重建；
   任何不走 ``run()`` 的调用路径（测试、其他入口）都不受限流约束；
2. ``self._inflight`` 也在 ``run()`` 内填充；``drain`` 只等 ``self._inflight``，直接调
   用 ``_process_one`` 创建的 task 永远不会进入集合，drain 提前返回。

## 决策
1. ``Semaphore`` 提升到 ``__init__`` 成员 ``self._semaphore``；
2. ``_process_one`` 内部 ``async with self._semaphore:`` 强制限流（任何调用方都受限）；
3. ``_process_one`` 用 ``asyncio.current_task()`` 拿到自己的 task，注册到 ``_inflight``，
   ``finally`` 清理；这样 ``drain`` 能等任何路径调用的 task。

## 取舍
- 不给 ``_process_one`` 加内部 ``register / unregister`` 装饰器（依赖具体 task API，
  兼容性差）；
- 直接用 ``current_task()`` 是 asyncio 1.7+ 标准 API，简洁可靠。

## 影响
- ``run()`` 不再创建 per-batch Semaphore，代码更简单；
- 测试可直接调 ``_process_one`` 模拟各种并发场景；
- ``drain`` 行为可预测：等所有 ``_process_one`` 调用产生的 task。""",
    ["asyncio", "worker", "concurrency", "drain"],
)


P4 = (
    "workflow",
    "P4-B SyncWorker 异步 outbox consumer 实施（Phase 4-B 核心）",
    """## 步骤
### 1. BackoffPolicy（`v2/sync/backoff.py`）
- frozen dataclass：`base_seconds=1.0, max_seconds=300.0, factor=2.0, jitter=0.1, deterministic_seed=None`
- `delay_for(attempts)`：`base * factor^attempts`，`min(max_seconds, raw)`，叠加 ±jitter
  比例随机扰动；`attempts < 0` 自动 clamp 到 0；
- `next_attempt_at_iso(attempts, now_iso)`：解析 Z 后缀 ISO 时间 + delay_for(attempts)；
- `deterministic_seed` 让测试可重现（避免随机抖动让单测 flaky）。

### 2. WorkerConfig（`v2/sync/worker.py`）
- lease_seconds=30 / heartbeat_seconds=10 / batch_size=10 / poll_interval=0.1
- max_outbox_attempts=5 / handler_timeout_seconds=60
- backoff=BackoffPolicy() / concurrency=4 / owner_prefix="sync-worker"

### 3. SyncWorker 生命周期
- `__init__`：建 `_stop_event`、`_heartbeats` 字典、`_inflight` 集合、`_semaphore`；
- `run()`：循环 `_fetch_due()` → 创建 `_process_one(ev)` tasks → `gather(..., return_exceptions=True)`；
- `stop()`：置 `_stop_event`，`run()` 在当前 batch 完成后退出；
- `drain(timeout=None)`：等 `_inflight` + 取消所有 `_heartbeats`。

### 4. 单事件流程 `_process_one` → `_do_process`
- 注册自己到 `_inflight`（用 current_task）；
- `async with self._semaphore:` 限流；
- claim_outbox（CAS：lease_until 过期或空 → 抢占，原子 +1 attempts）；
- 启动 heartbeat 任务，每 `heartbeat_seconds` 续 lease_until；
- `wait_for(handler(ev), timeout=handler_timeout_seconds)`；
- 成功 → ack_outbox（CAS：仍持有 lease → SUCCEEDED）；
- 失败 → `_on_failure(ev_id, error)`（用 ev_id 重新拉取，避免 in-memory 过期）。

### 5. _on_failure
- DB 拉取最新 ev；status != PENDING 直接 return；
- `next_attempts = ev.attempts + 1`；
- `next_attempts >= max_outbox_attempts` → `nack_outbox(max_attempts=...)` 走死信
  分支（FAILED），`deadlettered += 1`；
- 否则 `nack_outbox(next_attempt_at=backoff.next_attempt_at_iso(...))` 进入退避，
  `failed += 1`；
- 记 `errors[]`：`{ev_id, phase, error, type}`。

### 6. 跨线程安全（关键）
- sqlite_repo `_e()` helper + RLock 串行化所有 SQL；
- worker 内所有 DB 调用 `loop.run_in_executor(None, repo.xxx)`；
- `claim_outbox` / `ack_outbox` / `nack_outbox` / `renew_outbox_lease` / `get_outbox`
  / `list_outbox_due` 全部走 `_e`。""",
    ["sync", "outbox", "worker", "lease", "heartbeat"],
)


P5 = (
    "workflow",
    "Phase 4-B 验证：P4-B 20/20，V2 264/264，G4 同步原语完成（Phase 4-B 收尾实测）",
    """## 步骤
1. `tests/v2/test_sync_worker.py` — 20/20 全过：
   - TestBackoffPolicy (7)：零次/指数增长/上限/负数 clamp/jitter/ISO 时间/deterministic_seed；
   - TestWorkerSuccess (2)：handler 成功 ack + 幂等 handler 只跑一次；
   - TestWorkerFailure (3)：失败 nack + 退避 + 达 max 死信 + handler 超时；
   - TestWorkerConcurrency (2)：限流 + 失败不阻塞其它；
   - TestWorkerHeartbeat (2)：heartbeat 续约 + 仅 owner 可续；
   - TestWorkerGracefulStop (2)：run 在 batch 后停 + drain 等 in-flight；
   - TestMultiWorkerSafety (1)：两 worker 互斥；
   - TestWorkerErrors (1)：错误记录。

2. V2 域全套 **264/264** 全过（= P3 后 208 + P4-A 36 + P4-B 20）；零回归。

3. V1 测试 `test_hooks.py` / `test_import.py` 模块级 INTERNALERROR（sys.exit(1)），
   与 P4-B 改动无关（环境/CLI hook 集成问题），不阻塞 P4-B 通过判定。

## 关键决策
- SQLite 跨线程用 `_e()` helper + RLock 集中加锁；
- Semaphore 放 `__init__` 成员；`_process_one` 用 current_task() 自注册 `_inflight`；
- `_on_failure` 用 ev_id 重新拉取避免 in-memory 过期；
- BackoffPolicy `deterministic_seed` 让测试可重现。

## 产出
- P4-B 完成，可进入 P4-C：Git Mirror Writer（唯一写入者）；
- outbox 状态机闭环：PENDING → claim → handler → SUCCEEDED / FAILED；
- lease + heartbeat + fencing token 在仓储层 + worker 层两边都验证。

## 度量
- 20/20 测试执行 ~2.85s（含 lease / heartbeat 真实等待）；
- V2 全套 ~7.08s 264 项；
- 锁争用未观测到显著延迟（executor 默认 8 线程，锁等待概率低）。""",
    ["v2-upgrade", "sync", "worker", "evaluation"],
)


for typ, title, body, tags in (P1, P2, P3, P4, P5):
    src = "human:文剑"
    out = server.save_impl(typ, title, body, tags=tags, source=src)
    print("=" * 60)
    print(f"[{typ}] {title}")
    print(out[:500])
    print("…\n")
