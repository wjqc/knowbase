"""Feature Flag 注入框架（P0-A）。

设计目标：
- 不引入额外持久化：flag 默认值与覆盖都来自配置文件（~/.knowbase/config.json → v2 段）
- 可装饰任意函数；禁用时返回稳定占位值（保留 V1 行为），不抛异常，便于灰度回滚
- 支持 kill switch：紧急关闭任意 flag 而无需改代码
- 决策结果可被审计：debug 模式下返回决策依据，供 search_explain 解释使用

flag 命名规范：v2_<模块>，例如 v2_ingestion / hybrid_search / acl_v2 / central_sync / vector_recall。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import wraps
from typing import Any, Callable

FLAG_DEFAULTS = {
    # 摄取
    "v2_ingestion": False,        # V2 多格式摄取总开关（Phase 1 默认关）
    "v2_parser_pdf": False,       # PDF 解析
    "v2_parser_docx": False,      # DOCX 解析
    "v2_parser_markdown": True,   # Markdown 解析（与 V1 行为兼容，默认开）
    "v2_parser_txt": True,
    "v2_idempotent_ingest": False,  # SHA-256 + document_version CAS
    # 检索
    "hybrid_search": False,       # Phase 2 四路召回
    "vector_recall": False,       # Phase 2 本地向量召回
    "reranker": False,            # Phase 2 cross-encoder
    # 治理
    "acl_v2": False,              # Phase 3 组织/项目/角色硬过滤
    "central_sync": False,        # Phase 4 Team Profile 唯一写入者
    # 同步
    "background_fetch": False,    # Phase 4 Lite 后台 fetch
    "outbox_persist": False,      # Phase 4 写后 outbox 持久化重试
    # 调试
    "search_explain": True,       # P0：管理员可看召回通道
    "decision_audit": True,       # P0：所有 flag 决策写入 usage_log
}


@dataclass(frozen=True)
class FlagDecision:
    """单次 flag 决策结果，供 search_explain / 审计复盘。"""
    name: str
    enabled: bool
    source: str          # "default" | "config" | "kill_switch" | "env"
    reason: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "enabled": self.enabled, "source": self.source, "reason": self.reason}


@dataclass
class FlagRegistry:
    """运行时 flag 注册表；线程安全由调用方保证（CLI/MCP 单线程）。"""
    _overrides: dict[str, bool] = field(default_factory=dict)
    _kill_switch: set[str] = field(default_factory=set)
    _audit_log: list[FlagDecision] = field(default_factory=list)

    def load_from_config(self, cfg: dict | None) -> None:
        """从 knowbase 配置（v2 段）装载覆盖值；不识别的 key 忽略。"""
        if not cfg:
            return
        v2_cfg = cfg.get("v2", {}) or {}
        if not isinstance(v2_cfg, dict):
            return
        for key, value in v2_cfg.items():
            if key in FLAG_DEFAULTS and isinstance(value, bool):
                self._overrides[key] = value
            elif key == "kill_switch" and isinstance(value, list):
                self._kill_switch.update(str(x) for x in value)

    def is_enabled(self, name: str) -> bool:
        """单次决策，结果写入审计日志。"""
        if name not in FLAG_DEFAULTS:
            decision = FlagDecision(name=name, enabled=False, source="default",
                                    reason=f"unknown flag, default off")
        elif name in self._kill_switch:
            decision = FlagDecision(name=name, enabled=False, source="kill_switch",
                                    reason="explicit kill switch")
        else:
            env_val = os.environ.get(f"KNOWBASE_{name.upper()}")
            if env_val is not None:
                enabled = env_val.strip().lower() in ("1", "true", "yes", "on")
                decision = FlagDecision(name=name, enabled=enabled, source="env",
                                        reason=f"KNOWBASE_{name.upper()}={env_val}")
            elif name in self._overrides:
                decision = FlagDecision(name=name, enabled=self._overrides[name],
                                        source="config", reason="from config.v2")
            else:
                decision = FlagDecision(name=name, enabled=FLAG_DEFAULTS[name],
                                        source="default", reason="built-in default")
        self._audit_log.append(decision)
        return decision.enabled

    def audit(self) -> list[dict]:
        """返回决策快照（仅 debug 用，每次返回独立列表）。"""
        return [d.to_dict() for d in self._audit_log]

    def clear_audit(self) -> None:
        self._audit_log.clear()


# 全局单例：knowbase 进程内一致；多进程独立
_global = FlagRegistry()


def get_registry() -> FlagRegistry:
    return _global


def configure(cfg: dict | None) -> None:
    """CLI/MCP 入口：启动时载入配置。重复调用安全。"""
    get_registry().load_from_config(cfg)


def is_enabled(name: str) -> bool:
    return get_registry().is_enabled(name)


def audit() -> list[dict]:
    return get_registry().audit()


def gated(name: str, *, fallback: Any = None, on_disabled: str = "fallback") -> Callable:
    """装饰器：flag 关闭时跳过被装饰函数，返回 fallback 或 raise。

    参数:
        name: flag 名
        fallback: 返回值（on_disabled='fallback' 时）
        on_disabled: 'fallback' | 'passthrough' | 'raise'
            - fallback: 返回 fallback 值
            - passthrough: 仍调用原函数（用于调试/排错）
            - raise: 抛 FlagDisabledError，便于上层明确感知
    """
    from .observability.errors import FlagDisabledError  # 局部导入避免循环

    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if is_enabled(name):
                return fn(*args, **kwargs)
            if on_disabled == "passthrough":
                return fn(*args, **kwargs)
            if on_disabled == "raise":
                raise FlagDisabledError(name)
            return fallback

        wrapper.__knowbase_flag__ = name  # type: ignore[attr-defined]
        return wrapper

    return decorator