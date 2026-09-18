"""P4 收尾：V2 alerting 运维告警单元测试。
覆盖：
1. evaluate() 把 doctor 非 pass 项转 AlertEvent；pass 项被过滤
2. AlertDispatcher：单 sink + 单事件 → 派发
3. AlertDispatcher：多 sink + 多事件 → 全派发
4. AlertDispatcher：cooldown 期内重复 key 被 suppressed
5. AlertDispatcher：cooldown 过期后再次派发
6. AlertDispatcher：sink 抛异常被吞（不阻断其它 sink + 计数）
7. sink_webhook：通过本地 http.server 验证 POST 收到
8. sink_webhook：URL 为空时直接返回（不发请求）
9. sink_webhook：URLError 被吞（仅记日志，不抛）
10. sink_logfile：JSON 行追加写文件
11. sink_console：输出到 stdout（capsys）
12. load_config_from_knowbase：读 cfg.v2.alerts 段
13. run_alerts：集成测试（无事件 → stats 0；有事件 → 派发计数正确）
"""
from __future__ import annotations

import http.server
import json
import threading
import time
from pathlib import Path
from typing import Iterator

import pytest

from knowbase.v2.repositories import V2Repository
from knowbase.v2.sync.alerting import (
    AlertConfig,
    AlertDispatcher,
    AlertEvent,
    evaluate,
    load_config_from_knowbase,
    run_alerts,
    sink_console,
    sink_logfile,
    sink_webhook,
)
from knowbase.v2.sync.doctor import DoctorCheck, DoctorThresholds


@pytest.fixture
def repo(tmp_path: Path) -> V2Repository:
    r = V2Repository(tmp_path)
    yield r
    r.close()


def _ev(check: str, level: str = "warn", msg: str = "x") -> AlertEvent:
    return AlertEvent(check=check, level=level, message=msg)


# ====================================================================
# evaluate
# ====================================================================

class TestEvaluate:
    def test_passes_filtered(self):
        events = evaluate([
            DoctorCheck("a", "pass", "ok"),
            DoctorCheck("b", "warn", "w"),
            DoctorCheck("c", "fail", "f"),
        ])
        assert len(events) == 2
        keys = {ev.key() for ev in events}
        assert keys == {"b:warn", "c:fail"}

    def test_empty_input(self):
        assert evaluate([]) == []


# ====================================================================
# AlertDispatcher
# ====================================================================

class _CaptureSink:
    def __init__(self, raise_exc: Exception | None = None) -> None:
        self.calls: list[AlertEvent] = []
        self.raise_exc = raise_exc

    def __call__(self, ev: AlertEvent, cfg: AlertConfig) -> None:
        if self.raise_exc:
            raise self.raise_exc
        self.calls.append(ev)


class TestAlertDispatcher:

    def test_single_sink_single_event(self):
        sink = _CaptureSink()
        d = AlertDispatcher(AlertConfig(cooldown_seconds=0), sinks=[sink])
        stats = d.dispatch([_ev("a")])
        assert stats == {"sent": 1, "suppressed": 0, "failed": 0}
        assert len(sink.calls) == 1

    def test_multiple_sink_all_dispatched(self):
        sink1 = _CaptureSink()
        sink2 = _CaptureSink()
        d = AlertDispatcher(AlertConfig(cooldown_seconds=0), sinks=[sink1, sink2])
        d.dispatch([_ev("a"), _ev("b")])
        assert len(sink1.calls) == 2
        assert len(sink2.calls) == 2

    def test_cooldown_suppresses_repeat(self):
        # 用确定性时钟：手动推时间
        clock = [1000.0]
        sink = _CaptureSink()
        d = AlertDispatcher(
            AlertConfig(cooldown_seconds=60),
            sinks=[sink], clock=lambda: clock[0],
        )
        d.dispatch([_ev("a")])
        clock[0] += 30   # 没过 cooldown
        d.dispatch([_ev("a")])
        clock[0] += 31   # 过了 cooldown（共 +61）
        d.dispatch([_ev("a")])
        assert len(sink.calls) == 2  # 第二次被吞

    def test_different_keys_have_independent_cooldown(self):
        clock = [1000.0]
        sink = _CaptureSink()
        d = AlertDispatcher(
            AlertConfig(cooldown_seconds=60),
            sinks=[sink], clock=lambda: clock[0],
        )
        d.dispatch([_ev("a", "warn"), _ev("a", "fail"), _ev("b", "warn")])
        # 同一 check 不同 level 视为不同 key；不同 check 视为不同 key
        assert len(sink.calls) == 3

    def test_sink_exception_isolated(self):
        bad = _CaptureSink(raise_exc=RuntimeError("boom"))
        good = _CaptureSink()
        d = AlertDispatcher(AlertConfig(cooldown_seconds=0), sinks=[bad, good])
        stats = d.dispatch([_ev("a")])
        assert stats["failed"] == 1
        assert len(good.calls) == 1   # 没被 bad 拖垮

    def test_empty_events(self):
        sink = _CaptureSink()
        d = AlertDispatcher(AlertConfig(cooldown_seconds=0), sinks=[sink])
        stats = d.dispatch([])
        assert stats == {"sent": 0, "suppressed": 0, "failed": 0}
        assert sink.calls == []


# ====================================================================
# sink_webhook
# ====================================================================

class _WebhookHandler(http.server.BaseHTTPRequestHandler):
    received: list[dict] = []

    def do_POST(self):  # noqa: N802
        ln = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(ln).decode("utf-8")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = {"_raw": body}
        self.received.append(payload)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *a, **k):  # 静音 stderr
        pass


@pytest.fixture
def webhook_server() -> Iterator[str]:
    """启动本地 http.server，返回 base URL；fixture 退出时自动停。"""
    _WebhookHandler.received = []
    srv = http.server.HTTPServer(("127.0.0.1", 0), _WebhookHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        port = srv.server_address[1]
        yield f"http://127.0.0.1:{port}/hook"
    finally:
        srv.shutdown()
        srv.server_close()


class TestSinkWebhook:
    def test_post_received(self, webhook_server: str):
        ev = _ev("dead_letters", "fail", "1 dead letter")
        sink_webhook(ev, AlertConfig(webhook_url=webhook_server, webhook_timeout=2))
        assert len(_WebhookHandler.received) == 1
        body = _WebhookHandler.received[0]
        assert body["alert"]["check"] == "dead_letters"
        assert body["alert"]["level"] == "fail"

    def test_empty_url_noop(self, capsys):
        # 没 URL 时直接返回（不发请求，也不会 raise）
        sink_webhook(_ev("x"), AlertConfig(webhook_url=""))

    def test_bad_url_swallows_error(self):
        # 1.0.0.0:1 是 TEST-NET-1，正常发不出去；sink_webhook 应该吞掉
        sink_webhook(_ev("x"),
                     AlertConfig(webhook_url="http://127.0.0.1:1/hook",
                                 webhook_timeout=0.5))


# ====================================================================
# sink_console
# ====================================================================

class TestSinkConsole:
    def test_prints_json(self, capsys):
        ev = _ev("a", "warn", "msg")
        sink_console(ev, AlertConfig())
        out = capsys.readouterr().out.strip()
        parsed = json.loads(out)
        assert parsed["alert"]["check"] == "a"
        assert parsed["alert"]["message"] == "msg"


# ====================================================================
# sink_logfile
# ====================================================================

class TestSinkLogfile:
    def test_appends_json_lines(self, tmp_path: Path):
        log = tmp_path / "alerts.jsonl"
        cfg = AlertConfig(log_to_file=str(log))
        sink_logfile(_ev("a", "warn", "m1"), cfg)
        sink_logfile(_ev("b", "fail", "m2"), cfg)
        lines = log.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["alert"]["check"] == "a"
        assert json.loads(lines[1])["alert"]["check"] == "b"

    def test_empty_path_noop(self):
        sink_logfile(_ev("a"), AlertConfig(log_to_file=""))   # noop


# ====================================================================
# load_config_from_knowbase
# ====================================================================

class TestLoadConfig:
    def test_default_when_missing(self):
        cfg = load_config_from_knowbase(None)
        assert cfg.enabled is True
        assert cfg.cooldown_seconds == 300.0
        assert cfg.webhook_url == ""

    def test_partial_override(self):
        cfg = load_config_from_knowbase({"v2": {"alerts": {
            "cooldown_seconds": 60,
            "webhook_url": "http://x/y",
        }}})
        assert cfg.cooldown_seconds == 60
        assert cfg.webhook_url == "http://x/y"
        # 没覆盖的字段保留默认
        assert cfg.enabled is True
        assert cfg.log_to_file == ""


# ====================================================================
# run_alerts（集成）
# ====================================================================

class TestRunAlerts:
    def test_empty_db_triggers_mirror_and_staleness_warns(
        self, repo: V2Repository, tmp_path: Path,
    ):
        # 全空库预期有两个合法 warn：mirror_git 缺失 + source_sync_state 空
        # （schema_version / pending_outbox / dead_letters 都应 pass）
        sink = _CaptureSink()
        d = AlertDispatcher(AlertConfig(cooldown_seconds=0), sinks=[sink])
        result = run_alerts(
            repo=repo, repo_root=tmp_path,
            cfg=AlertConfig(cooldown_seconds=0), dispatcher=d,
        )
        names = {ev.check for ev in result["events"]}
        assert "mirror_git" in names
        assert "sync_staleness" in names
        # mirror + staleness 两条都被派发
        assert result["dispatch"]["sent"] == 2
        assert len(sink.calls) == 2

    def test_disabled_short_circuits(self, repo: V2Repository, tmp_path: Path):
        # cfg.enabled=False → run_alerts 内部不调 dispatcher（不派发），
        # 但 events 仍被 evaluate 出来。
        sink = _CaptureSink()
        d = AlertDispatcher(AlertConfig(cooldown_seconds=0), sinks=[sink])
        result = run_alerts(
            repo=repo, repo_root=tmp_path,
            cfg=AlertConfig(enabled=False, cooldown_seconds=0), dispatcher=d,
        )
        # 空库至少有两个（mirror / staleness）
        assert len(result["events"]) >= 2
        # 但因为 enabled=False，dispatcher 没被调用
        assert result["dispatch"]["sent"] == 0
        assert sink.calls == []