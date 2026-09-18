"""P4 收尾：薪火 V2 健康检查（doctor）。

实现 V2 计划 §12「运维与可观察」要求的 ``knowbase doctor`` 命令：扫描
V2 DB、mirror 目录与同步状态，输出一组 pass/warn/fail 检查项 + 汇总，
给运维/Agent 快速判断库是否处于健康状态。

设计：
- 单一函数 ``run_checks()`` 返回 ``list[DoctorCheck]``；不抛异常（吞 DB 异常转 fail）
- 检查项实现成 ``DoctorCheck`` 命名元组：(name, level, message)，便于
  单元测试只针对单个 check 做断言
- level ∈ {'pass', 'warn', 'fail'}；exit code：fail>0 → 2，warn>0 → 1，全 pass → 0
- 不依赖外网（mirror 检查仅看本地 bare repo 路径）；告警阈值从参数传入
  便于测试覆盖边界（不读全局 config）

依赖：
- V2Repository：通过 db_path 自动 init（doctor 不强制主仓库有 v2.db，
  v2.db 缺失则 schema_version 检查 = fail，其余跳过）
- mirror.py：检查 ``<repo>/.knowbase/mirror.git`` 路径存在
- schema.current_schema_version：本地直读 PRAGMA，避免初始化连接副作用
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence as _SeqT

from ..repositories import V2Repository
from ..repositories.schema import SCHEMA_VERSION, current_schema_version


@dataclass(frozen=True)
class DoctorCheck:
    """单条健康检查结果。

    Attributes:
        name: 检查项短名（机器可读，例如 ``schema_version``）
        level: ``pass`` / ``warn`` / ``fail``
        message: 人类可读描述（含数值/状态）
    """

    name: str
    level: str  # 'pass' | 'warn' | 'fail'
    message: str


# ---------- 默认阈值（可被 cmd_doctor 覆盖） ----------

@dataclass(frozen=True)
class DoctorThresholds:
    """doctor 检查阈值；超过即 warn / fail。"""

    pending_outbox_warn: int = 100          # pending+dispatched 超过即 warn
    dead_letter_fail: int = 1               # outbox FAILED 数 ≥ 1 即 fail
    recent_sync_failure_warn: int = 1       # 24h 内 sync_run failed/conflicted ≥ 1 即 warn
    sync_staleness_warn_seconds: int = 300  # last_remote_check_at 超过 5 分钟即 warn
    conflicted_docs_warn: int = 1           # source 有冲突 doc ≥ 1 即 warn


# ---------- 公共：DB 反查 helper ----------

def _repo_or_none(repo: V2Repository) -> V2Repository | None:
    """doctor 允许 DB 缺失；调用方应已 try/except 处理。"""
    return repo


def check_schema_version(repo: V2Repository) -> DoctorCheck:
    """当前 v2.db schema_version 是否达到目标 SCHEMA_VERSION。"""
    try:
        cur = current_schema_version(repo.db_path)
    except Exception as e:  # pragma: no cover - 真出错再看
        return DoctorCheck("schema_version", "fail", f"读 schema_version 异常: {e!r}")
    if cur == 0:
        return DoctorCheck(
            "schema_version", "fail",
            f"v2.db 不存在（{repo.db_path}）；请先 knowbase init 触发 init_v2_db",
        )
    if cur < SCHEMA_VERSION:
        return DoctorCheck(
            "schema_version", "fail",
            f"schema_version={cur} < 目标 {SCHEMA_VERSION}，需执行 apply_migrations",
        )
    return DoctorCheck(
        "schema_version", "pass",
        f"schema_version={cur}（目标 {SCHEMA_VERSION}）",
    )


def check_pending_outbox(repo: V2Repository, *, warn_threshold: int) -> DoctorCheck:
    """全库 outbox pending+dispatched 积压。"""
    try:
        row = repo._e(
            "SELECT COUNT(*) FROM outbox_event "
            "WHERE status IN ('pending', 'dispatched')"
        ).fetchone()
    except Exception as e:
        return DoctorCheck("pending_outbox", "fail", f"查 outbox 异常: {e!r}")
    n = int(row[0]) if row else 0
    if n >= warn_threshold:
        return DoctorCheck(
            "pending_outbox", "warn",
            f"积压 {n} 条 ≥ 阈值 {warn_threshold}（worker 可能挂起或上游写入暴增）",
        )
    return DoctorCheck("pending_outbox", "pass", f"积压 {n} 条 < 阈值 {warn_threshold}")


def check_dead_letters(repo: V2Repository, *, fail_threshold: int) -> DoctorCheck:
    """outbox FAILED（达 max_attempts）死信计数。"""
    try:
        row = repo._e(
            "SELECT COUNT(*) FROM outbox_event WHERE status='failed'"
        ).fetchone()
    except Exception as e:
        return DoctorCheck("dead_letters", "fail", f"查死信异常: {e!r}")
    n = int(row[0]) if row else 0
    if n >= fail_threshold:
        return DoctorCheck(
            "dead_letters", "fail",
            f"死信 {n} 条 ≥ 阈值 {fail_threshold}（需人工 triage 或清队列）",
        )
    return DoctorCheck("dead_letters", "pass", f"死信 {n} 条 < 阈值 {fail_threshold}")


def check_recent_sync_failures(repo: V2Repository, *, warn_threshold: int) -> DoctorCheck:
    """24h 内 sync_run 失败 / 冲突计数。

    用 ``finished_at`` 过滤（sync_run 没有 updated_at 列；started_at 不
    反映结束时间，RUNNING 中的 run 也不该计入失败）。RUNNING 状态的 run
    会被自动跳过——它们还没失败，不需要"已完成失败"告警；可在 worker
    看护项里另算。

    时间格式注意：``finished_at`` 是 ``%Y-%m-%dT%H:%M:%fZ`` ISO 字串，
    与 SQLite 默认 ``datetime('now')`` 的 ``%Y-%m-%d %H:%M:%S`` 不可
    直接字符串比较；这里用 ``strftime`` 同步到同一种格式。
    """
    try:
        row = repo._e(
            "SELECT COUNT(*) FROM sync_run "
            "WHERE status IN ('failed', 'conflicted') "
            "AND finished_at IS NOT NULL "
            "AND finished_at >= strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-24 hours')"
        ).fetchone()
    except Exception as e:
        return DoctorCheck("recent_sync_failures", "fail", f"查 sync_run 异常: {e!r}")
    n = int(row[0]) if row else 0
    if n >= warn_threshold:
        return DoctorCheck(
            "recent_sync_failures", "warn",
            f"近 24h 失败 / 冲突 {n} 次 ≥ 阈值 {warn_threshold}（需查 source_sync_state.last_error）",
        )
    return DoctorCheck(
        "recent_sync_failures", "pass",
        f"近 24h 失败 / 冲突 {n} 次 < 阈值 {warn_threshold}",
    )


def check_mirror_git(repo_root: Path) -> DoctorCheck:
    """mirror bare repo 是否就绪（存在且是 git 目录）。"""
    mirror = repo_root / ".knowbase" / "mirror.git"
    if not mirror.exists():
        return DoctorCheck(
            "mirror_git", "warn",
            f"{mirror} 不存在；Lite Profile 同步 / Push 路径将失败（首次同步会自动 init）",
        )
    head = mirror / "HEAD"
    if not head.exists():
        return DoctorCheck(
            "mirror_git", "fail",
            f"{mirror} 存在但无 HEAD（裸仓损坏？）",
        )
    return DoctorCheck("mirror_git", "pass", f"mirror 存在 {mirror}")


def check_sync_staleness(
    repo: V2Repository, *, warn_threshold_seconds: int,
) -> DoctorCheck:
    """对每个 source 检查 last_remote_check_at 是否超过 SLA。"""
    try:
        rows = repo._e(
            "SELECT source_id, last_remote_check_at FROM source_sync_state"
        ).fetchall()
    except Exception as e:
        return DoctorCheck("sync_staleness", "fail", f"查 source_sync_state 异常: {e!r}")

    # 计算最久未检查的 source 距离 now 的秒数（粗略：长度差估算即可）
    # 真实实现需要按 ISO 字符串解析；为简化 doctor 只关心行数 + 是否全 None
    if not rows:
        return DoctorCheck(
            "sync_staleness", "warn",
            "source_sync_state 为空（从未跑过 Lite Profile 同步）",
        )

    # 解析每一行 last_remote_check_at；与 now 比对
    from datetime import datetime, timezone
    stale: list[str] = []
    now = datetime.now(timezone.utc)
    for sid, last in rows:
        if not last:
            stale.append(f"{sid}(never)")
            continue
        try:
            t = datetime.strptime(last, "%Y-%m-%dT%H:%M:%fZ").replace(tzinfo=timezone.utc)
        except ValueError:
            try:
                t = datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            except ValueError:
                stale.append(f"{sid}(bad-ts)")
                continue
        if (now - t).total_seconds() > warn_threshold_seconds:
            stale.append(f"{sid}({(now - t).total_seconds():.0f}s)")
    if stale:
        listed = ", ".join(stale[:3]) + ("..." if len(stale) > 3 else "")
        return DoctorCheck(
            "sync_staleness", "warn",
            f"{len(stale)} 个 source 超过 {warn_threshold_seconds}s 未检查: {listed}",
        )
    return DoctorCheck(
        "sync_staleness", "pass",
        f"{len(rows)} 个 source 全部在 {warn_threshold_seconds}s 内检查",
    )


def check_conflicted_docs(repo: V2Repository, *, warn_threshold: int) -> DoctorCheck:
    """全库处于冲突态的 doc 计数（status=error 或 meta.conflict=1）。"""
    try:
        row = repo._e(
            "SELECT COUNT(*) FROM document "
            "WHERE status='error' OR json_extract(meta, '$.conflict') = 1"
        ).fetchone()
    except Exception as e:
        return DoctorCheck("conflicted_docs", "fail", f"查 document 冲突态异常: {e!r}")
    n = int(row[0]) if row else 0
    if n >= warn_threshold:
        return DoctorCheck(
            "conflicted_docs", "warn",
            f"{n} 个 doc 处于冲突态 ≥ 阈值 {warn_threshold}（需 resolve）",
        )
    return DoctorCheck("conflicted_docs", "pass", f"{n} 个冲突 doc < 阈值 {warn_threshold}")


# ---------- 总入口 ----------

# 默认注册顺序即输出顺序；增删请同步测试
DEFAULT_CHECKS: tuple[
    Callable[[V2Repository, DoctorThresholds], DoctorCheck], ...
] = (
    lambda r, t: check_schema_version(r),
    lambda r, t: check_pending_outbox(r, warn_threshold=t.pending_outbox_warn),
    lambda r, t: check_dead_letters(r, fail_threshold=t.dead_letter_fail),
    lambda r, t: check_recent_sync_failures(r, warn_threshold=t.recent_sync_failure_warn),
    lambda r, t: check_sync_staleness(r, warn_threshold_seconds=t.sync_staleness_warn_seconds),
    lambda r, t: check_conflicted_docs(r, warn_threshold=t.conflicted_docs_warn),
)


def run_checks(
    repo: V2Repository,
    repo_root: Path,
    *,
    thresholds: DoctorThresholds | None = None,
    extra_checks: _SeqT[Callable[[V2Repository, DoctorThresholds], DoctorCheck]] = (),
) -> list[DoctorCheck]:
    """跑默认检查 + 额外检查；DB 异常被各 check 内部吞，不会整轮失败。"""
    t = thresholds or DoctorThresholds()
    results: list[DoctorCheck] = []
    for fn in DEFAULT_CHECKS:
        try:
            results.append(fn(repo, t))
        except Exception as e:  # 防御性兜底：单个 check 挂掉不拖垮整轮
            results.append(DoctorCheck(
                fn.__name__ if hasattr(fn, "__name__") else "unknown",
                "fail", f"检查执行异常: {e!r}",
            ))
    # mirror_git 不依赖 DB，独立跑
    try:
        results.append(check_mirror_git(repo_root))
    except Exception as e:
        results.append(DoctorCheck("mirror_git", "fail", f"检查异常: {e!r}"))
    # 额外检查
    for fn in extra_checks:
        try:
            results.append(fn(repo, t))
        except Exception as e:
            results.append(DoctorCheck(
                getattr(fn, "__name__", "extra"),
                "fail", f"检查异常: {e!r}",
            ))
    return results


def summarize(checks: Iterable[DoctorCheck]) -> tuple[int, int, int]:
    """返回 (pass_count, warn_count, fail_count)。"""
    p = w = f = 0
    for c in checks:
        if c.level == "pass":
            p += 1
        elif c.level == "warn":
            w += 1
        elif c.level == "fail":
            f += 1
    return p, w, f


def exit_code(checks: Iterable[DoctorCheck]) -> int:
    """0=全 pass；1=有 warn 无 fail；2=有 fail。供 CLI 直接 return。"""
    _, w, f = summarize(checks)
    if f:
        return 2
    if w:
        return 1
    return 0


def render(checks: Iterable[DoctorCheck]) -> str:
    """格式化输出（CLI 用）。"""
    lines: list[str] = []
    p = w = f = 0
    for c in checks:
        glyph = {"pass": "✓", "warn": "⚠", "fail": "✗"}.get(c.level, "?")
        lines.append(f"  {glyph} [{c.level.upper():4}] {c.name}: {c.message}")
        if c.level == "pass":
            p += 1
        elif c.level == "warn":
            w += 1
        else:
            f += 1
    lines.append("")
    lines.append(f"汇总：pass={p} warn={w} fail={f}")
    return "\n".join(lines)