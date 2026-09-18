"""P0-A Feature Flag 单元测试。"""
from __future__ import annotations

import pytest

from knowbase.v2 import features
from knowbase.v2.features import (
    FLAG_DEFAULTS,
    FlagDecision,
    FlagRegistry,
    configure,
    gated,
    is_enabled,
)
from knowbase.v2.observability.errors import FlagDisabledError


@pytest.fixture(autouse=True)
def _reset_flag_state(monkeypatch):
    """每个用例前后清空全局注册表 + env，避免污染。"""
    features._global = FlagRegistry()
    for k in FLAG_DEFAULTS:
        monkeypatch.delenv(f"KNOWBASE_{k.upper()}", raising=False)
    yield
    features._global = FlagRegistry()


def test_default_value_when_no_override():
    reg = FlagRegistry()
    # 已知 flag 取默认值
    assert reg.is_enabled("v2_parser_markdown") is True
    assert reg.is_enabled("v2_ingestion") is False
    # 未知 flag 默认关闭
    assert reg.is_enabled("totally_unknown_flag") is False


def test_config_override_loads_known_keys_only():
    reg = FlagRegistry()
    reg.load_from_config({"v2": {"v2_ingestion": True, "v2_parser_pdf": True,
                                  "v2_parser_markx_typo": True,  # 未知 key
                                  "kill_switch": ["v2_ingestion", "v2_parser_pdf"]}})
    # 配置覆盖（kill_switch 后置生效；先记录再统一决策）
    assert reg.is_enabled("v2_parser_pdf") is False  # kill_switch 关闭
    # v2_ingestion 也被 kill_switch 关闭
    assert reg.is_enabled("v2_ingestion") is False
    # kill_switch 决定应记录
    audit = reg.audit()
    assert any(d["source"] == "kill_switch" for d in audit)


def test_kill_switch_overrides_everything(monkeypatch):
    monkeypatch.setenv("KNOWBASE_V2_INGESTION", "true")
    reg = FlagRegistry()
    reg.load_from_config({"v2": {"v2_ingestion": True}})
    reg._kill_switch.add("v2_ingestion")
    assert reg.is_enabled("v2_ingestion") is False
    assert reg.audit()[-1]["source"] == "kill_switch"


def test_env_overrides_config(monkeypatch):
    monkeypatch.setenv("KNOWBASE_V2_INGESTION", "true")
    reg = FlagRegistry()
    reg.load_from_config({"v2": {"v2_ingestion": False}})
    assert reg.is_enabled("v2_ingestion") is True
    assert reg.audit()[-1]["source"] == "env"


def test_env_truthy_and_falsy(monkeypatch):
    monkeypatch.setenv("KNOWBASE_V2_INGESTION", "yes")
    reg = FlagRegistry()
    assert reg.is_enabled("v2_ingestion") is True
    monkeypatch.setenv("KNOWBASE_V2_INGESTION", "0")
    reg.clear_audit()
    assert reg.is_enabled("v2_ingestion") is False


def test_audit_records_every_decision():
    reg = FlagRegistry()
    reg.is_enabled("v2_ingestion")
    reg.is_enabled("v2_parser_markdown")
    reg.is_enabled("totally_unknown")
    audit = reg.audit()
    assert len(audit) == 3
    assert all(isinstance(d, FlagDecision) or isinstance(d, dict) for d in audit)
    assert audit[0]["name"] == "v2_ingestion"


def test_gated_decorator_returns_fallback_when_disabled():
    @gated("v2_ingestion", fallback="V1")
    def new_behavior():
        return "V2"

    # 默认 v2_ingestion=False，应返回 V1
    assert new_behavior() == "V1"


def test_gated_decorator_calls_when_enabled(monkeypatch):
    monkeypatch.setenv("KNOWBASE_V2_INGESTION", "true")

    @gated("v2_ingestion", fallback="V1")
    def new_behavior():
        return "V2"

    assert new_behavior() == "V2"


def test_gated_passthrough_mode():
    @gated("v2_ingestion", on_disabled="passthrough")
    def fn():
        return "V2"

    assert fn() == "V2"  # 即使 flag 关闭，passthrough 仍执行


def test_gated_raise_mode():
    @gated("v2_ingestion", on_disabled="raise")
    def fn():
        return "V2"

    with pytest.raises(FlagDisabledError) as exc:
        fn()
    assert exc.value.flag == "v2_ingestion"


def test_configure_helper_loads_globally():
    configure({"v2": {"v2_ingestion": True}})
    assert is_enabled("v2_ingestion") is True
