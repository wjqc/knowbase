"""V2 异常体系：仅定义专用异常，避免污染 V1。

错误命名规范：
- FlagDisabledError     - 显式 kill switch / flag 关闭
- NotConfiguredError    - 缺少必需配置（如 source/document 缺失）
- ParseError            - 解析失败，附带 parser 名与原因
"""
from __future__ import annotations


class FlagDisabledError(RuntimeError):
    """被 feature flag 拦截（on_disabled='raise' 时抛出）。"""

    def __init__(self, name: str):
        super().__init__(f"feature flag '{name}' is disabled")
        self.flag = name


class NotConfiguredError(ValueError):
    """必需配置缺失或非法。"""


class ParseError(RuntimeError):
    """文档解析失败。"""

    def __init__(self, parser: str, reason: str, *, path: str = ""):
        super().__init__(f"[{parser}] {reason}" + (f" @ {path}" if path else ""))
        self.parser = parser
        self.reason = reason
        self.path = path