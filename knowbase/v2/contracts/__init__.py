"""V2 MCP/API 契约包（P0-C）。

提供：
- mcp_schemas()：6 个 MCP 工具的入参/出参 JSON Schema（仅描述，不强制）
- validate_tool_input / validate_tool_output：供 server 中间件 / 集成测试使用
- ToolContract：单工具契约描述（name/required/optional/returns/errors）

约定：契约不绑定 Pydantic，仅依赖 stdlib + jsonschema，避免给 V1 引入新依赖。
"""
from __future__ import annotations

from .schemas import (
    ContractViolation,
    ToolContract,
    is_error_output,
    mcp_schemas,
    validate_tool_input,
    validate_tool_output,
)

__all__ = [
    "ContractViolation",
    "ToolContract",
    "is_error_output",
    "mcp_schemas",
    "validate_tool_input",
    "validate_tool_output",
]
