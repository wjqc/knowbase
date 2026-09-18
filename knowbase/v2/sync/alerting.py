"""P4 收尾：薪火 V2 运维告警（alerting）。

实现 V2 计划 §12「运维与可观察」要求的告警 hook：

- 复用 doctor 检查项，把 doctor 数据集 + 阈值映射成 ``AlertEvent`` 列表
- ``AlertDispatcher`` 注册多个 sink（webhook / 控制台 / 文件），每个
  sink 可独立开关、互不影响（一个 sink 抛异常不影响其它）
- 防抖：同 ``(check_name, level)`` 在 ``cooldown_seconds`` 内不重复派发
  （in-memory 记录；进程重启会重置 — 接受告警风暴换来简单性，避免再加
  SQLite 表/迁移）

设计取舍（V2 计划 §12 没规定细节；本实现选择「先做对，不做花」）：
- 不在告警里附 source_id / 阈值上下文 → 简单 payload = ``{check, level, message}``
- webhook 用 ``urllib.request``（stdlib），不引入 httpx / aiohttp
- 防抖 key = ``(check.name, level)``，cooldown 默认 300s（5 分钟）
- 不强求持久化告警日志；stdout / 文件可选
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .doctor import (
    DEFAULT_CHECKS,
    DoctorCheck,
    DoctorThresholds,
    V2Repository,
    run_checks,
)

log = logging.getLogger("knowbase.v2.alerting")


# ---------- 阈值映射 ----------

@dataclass(frozen=True)
class AlertConfig:
    """告警触发配置。

    Attributes:
        enabled: 全局开关（CLI --no-alerts 可关闭）
        cooldown_seconds: 同一 ``(check, level)`` 防抖窗口
        webhook_url: webhook 目标 URL（空字符串 = 不发 webhook）
        webhook_timeout: webhook POST 超时（秒）
        log_to_file: 可选持久化路径（空 = 仅 stdout）
    """

    enabled: bool = True
    cooldown_seconds: float = 300.0
    webhook_url: str = ""
    webhook_timeout: float = 5.0
    log_to_file: str = ""


# ---------- 告警事件 ----------

@dataclass(frozen=True)
class AlertEvent:
    """单条待派发告警。

    Attributes:
        check: 对应 doctor 项名（例如 ``dead_letters``）
        level: ``warn`` 或 ``fail``（pass 不发）
        message: 原始 message（含数值）
        ts: unix 时间戳（构造瞬间）
    """

    check: str
    level: str  # 'warn' | 'fail'
    message: str
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "level": self.level,
            "message": self.message,
            "ts": self.ts,
        }

    def key(self) -> str:
        return f"{self.check}:{self.level}"


# ---------- 评估：从 doctor 结果筛告警 ----------

def evaluate(checks: Iterable[DoctorCheck]) -> list[AlertEvent]:
    """把 doctor 检查结果中非 pass 的项转成 AlertEvent。"""
    events: list[AlertEvent] = []
    for c in checks:
        if c.level == "pass":
            continue
        events.append(AlertEvent(
            check=c.name, level=c.level, message=c.message,
        ))
    return events


# ---------- Dispatcher ----------

# 一个 sink 的签名：接收 AlertEvent + AlertConfig，可抛异常（被吞）
AlertSink = Callable[[AlertEvent, AlertConfig], None]


def sink_webhook(ev: AlertEvent, cfg: AlertConfig) -> None:
    """POST JSON 到 cfg.webhook_url；无 URL 直接返回。"""
    if not cfg.webhook_url:
        return
    payload = json.dumps({"alert": ev.to_dict()}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        cfg.webhook_url, data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=cfg.webhook_timeout) as resp:
            # 只读 status，不消费 body
            _ = resp.status
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        # webhook 失败不抛（避免拖垮其它 sink）；只记日志
        log.warning("webhook POST 失败 url=%s err=%r", cfg.webhook_url, e)


def sink_console(ev: AlertEvent, cfg: AlertConfig) -> None:
    """stdout 打印一行 JSON（便于 cron 收集 / docker logs）。"""
    print(json.dumps({"alert": ev.to_dict()}, ensure_ascii=False), flush=True)


def sink_logfile(ev: AlertEvent, cfg: AlertConfig) -> None:
    """追加写入 cfg.log_to_file（每行 JSON）。"""
    if not cfg.log_to_file:
        return
    p = Path(cfg.log_to_file)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"alert": ev.to_dict()}, ensure_ascii=False) + "\n")


class AlertDispatcher:
    """多 sink 防抖派发器。

    用法::

        d = AlertDispatcher(cfg, sinks=[sink_webhook, sink_console])
        d.dispatch(events)
    """

    def __init__(
        self,
        cfg: AlertConfig,
        *,
        sinks: list[AlertSink] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.cfg = cfg
        self.sinks: list[AlertSink] = sinks if sinks is not None else [sink_console]
        self._last_sent: dict[str, float] = {}
        self._clock = clock

    def _in_cooldown(self, ev: AlertEvent) -> bool:
        last = self._last_sent.get(ev.key())
        if last is None:
            return False
        return (self._clock() - last) < self.cfg.cooldown_seconds

    def dispatch(self, events: Iterable[AlertEvent]) -> dict[str, int]:
        """派发事件；返回计数 ``{sent, suppressed, failed}``。

        - sent: 实际派发（首次或过 cooldown）
        - suppressed: 防抖吞掉
        - failed: 派发时抛异常（仍计数；不抛出本身）
        """
        sent = suppressed = failed = 0
        for ev in events:
            if self._in_cooldown(ev):
                suppressed += 1
                continue
            for sink in self.sinks:
                try:
                    sink(ev, self.cfg)
                except Exception as e:  # noqa: BLE001 - sink 异常一律吞
                    failed += 1
                    log.warning("sink %s 抛异常 err=%r", getattr(sink, "__name__", sink), e)
            self._last_sent[ev.key()] = self._clock()
            sent += 1
        return {"sent": sent, "suppressed": suppressed, "failed": failed}


# ---------- 总入口（CLI 调用） ----------

def run_alerts(
    repo: V2Repository,
    repo_root: Path,
    *,
    cfg: AlertConfig,
    thresholds: DoctorThresholds | None = None,
    dispatcher: AlertDispatcher | None = None,
) -> dict[str, Any]:
    """跑一轮：doctor → 评估 → 派发；返回统计 dict（CLI 打印用）。

    返回结构::

        {
            "checks": [DoctorCheck, ...],
            "events": [AlertEvent, ...],
            "dispatch": {"sent": N, "suppressed": N, "failed": N},
        }
    """
    checks = run_checks(repo, repo_root, thresholds=thresholds)
    events = evaluate(checks)
    if cfg.enabled and events:
        disp = dispatcher or AlertDispatcher(cfg)
        stats = disp.dispatch(events)
    else:
        stats = {"sent": 0, "suppressed": 0, "failed": 0}
    return {"checks": checks, "events": events, "dispatch": stats}


def load_config_from_knowbase(cfg_dict: dict | None = None) -> AlertConfig:
    """从 knowbase config dict 取 v2.alerts 段；缺失则返回默认（仅 enabled=True）。"""
    if not cfg_dict:
        return AlertConfig()
    section = cfg_dict.get("v2", {}).get("alerts", {})
    if not section:
        return AlertConfig()
    return AlertConfig(
        enabled=bool(section.get("enabled", True)),
        cooldown_seconds=float(section.get("cooldown_seconds", 300.0)),
        webhook_url=str(section.get("webhook_url", "")),
        webhook_timeout=float(section.get("webhook_timeout", 5.0)),
        log_to_file=str(section.get("log_to_file", "")),
    )


# 重新导出 doctor 的入口，便于 v2.sync 一站式导入
__all__ = [
    "AlertConfig",
    "AlertEvent",
    "AlertDispatcher",
    "AlertSink",
    "DEFAULT_CHECKS",
    "evaluate",
    "load_config_from_knowbase",
    "run_alerts",
    "sink_console",
    "sink_logfile",
    "sink_webhook",
]