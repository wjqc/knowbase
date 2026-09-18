"""6 个 MCP 工具的 JSON Schema 契约（P0-C）。

约束：
- 仅「形状」契约：必填/可选/类型/枚举；不做语义校验
- 输入 schema 直接对应 server.py impl 函数签名
- 输出 schema 统一为 string（V1 现状：所有 MCP 工具返字符串文本）
- 错误结构：以「错误：xxx」前缀返回，与 V1 行为一致；不抛异常给 MCP 客户端

升级规则：
- 修改 server.py impl 签名时，必须同步本文件；CI 用 validate_tool_input 阻断破坏性变更
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import jsonschema


# 6 个工具类型常量（与 V1 store.TYPES 对齐）
TYPES_ENUM = [
    "pitfall", "decision", "workflow",
    "standard", "preference", "bizrule", "reference",
]

SCOPES_ENUM = ["global", "project", "agent"]

RELATION_TYPES = ["supersedes", "contradicts", "extends", "derived_from"]

OUTCOMES_ENUM = ["helpful", "not_helpful", "outdated", "incorrect"]

SOURCE_PATTERN = r"^(human|agent):[a-zA-Z0-9_.-]+:.+"


@dataclass(frozen=True)
class ToolContract:
    name: str
    description: str
    input_schema: dict
    output_schema: dict
    error_prefix: str = "错误"


# ---------- 6 个工具的入参/出参 schema ----------

_memory_save_input = {
    "type": "object",
    "additionalProperties": False,
    "required": ["type", "title", "body"],
    "properties": {
        "type": {"type": "string", "enum": TYPES_ENUM},
        "title": {"type": "string", "minLength": 1, "maxLength": 200},
        "body": {"type": "string", "minLength": 1},
        "tags": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "scope": {"type": "string", "enum": SCOPES_ENUM, "default": "global"},
        "relations": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "type"],
                "properties": {
                    "id": {"type": "string", "pattern": r"^M-\d{6}$"},
                    "type": {"type": "string", "enum": RELATION_TYPES},
                },
            },
        },
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["kind", "ref"],
                "properties": {
                    "kind": {"type": "string", "enum": ["log", "doc", "url", "file"]},
                    "ref": {"type": "string", "minLength": 1},
                },
            },
        },
        "source": {"type": "string", "pattern": SOURCE_PATTERN},
        "provenance": {"type": "string"},
        "domain": {"type": "string"},
        "rule_status": {"type": "string", "enum": ["draft", "active", "deprecated"]},
    },
}

_memory_update_input = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id"],
    "properties": {
        "id": {"type": "string", "pattern": r"^M-\d{6}$"},
        "body": {"type": "string"},
        "title": {"type": "string", "minLength": 1, "maxLength": 200},
        "tags": {"type": "array", "items": {"type": "string"}},
        "relations": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "type"],
                "properties": {
                    "id": {"type": "string", "pattern": r"^M-\d{6}$"},
                    "type": {"type": "string", "enum": RELATION_TYPES},
                },
            },
        },
        "evidence": {"type": "array"},
    },
}

_memory_read_input = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id"],
    "properties": {
        "id": {"type": "string", "pattern": r"^M-\d{6}$"},
    },
}

_memory_search_input = {
    "type": "object",
    "additionalProperties": False,
    "required": ["query"],
    "properties": {
        "query": {"type": "string", "minLength": 1},
        "type": {"type": "string", "enum": TYPES_ENUM},
        "scope": {"type": "string", "enum": SCOPES_ENUM},
        "tag": {"type": "string"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 5},
        "include_inactive": {"type": "boolean", "default": False},
    },
}

_memory_feedback_input = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "outcome"],
    "properties": {
        "id": {"type": "string", "pattern": r"^M-\d{6}$"},
        "outcome": {"type": "string", "enum": OUTCOMES_ENUM},
        "context": {"type": "string"},
    },
}

_memory_stats_input = {
    "type": "object",
    "additionalProperties": False,
    "properties": {},
}

# 6 个输出 schema：统一字符串（V1 行为约定）
_string_output = {"type": "string"}


MCP_CONTRACTS: dict[str, ToolContract] = {
    "memory_save": ToolContract(
        name="memory_save",
        description="保存一条经验记忆。standard/preference 由 Agent 保存时自动进 staging 待人审",
        input_schema=_memory_save_input,
        output_schema=_string_output,
    ),
    "memory_update": ToolContract(
        name="memory_update",
        description="修改已有记忆的内容/标签/关系。confidence 与 status 由服务端状态机管理，不接受指定",
        input_schema=_memory_update_input,
        output_schema=_string_output,
    ),
    "memory_read": ToolContract(
        name="memory_read",
        description="按 id 读取记忆全文（自动累计 hit_count）",
        input_schema=_memory_read_input,
        output_schema=_string_output,
    ),
    "memory_search": ToolContract(
        name="memory_search",
        description="检索经验记忆：任务开始涉及具体项目/系统/报错时先调用。检索词建议 ≥3 字的具体名词或报错关键词",
        input_schema=_memory_search_input,
        output_schema=_string_output,
    ),
    "memory_feedback": ToolContract(
        name="memory_feedback",
        description="按记忆行动后回填结果：helpful/not_helpful/outdated/incorrect。驱动经验晋升与淘汰",
        input_schema=_memory_feedback_input,
        output_schema=_string_output,
    ),
    "memory_stats": ToolContract(
        name="memory_stats",
        description="记忆库统计：数量分布、使用漏斗、TOP 记忆",
        input_schema=_memory_stats_input,
        output_schema=_string_output,
    ),
}


def mcp_schemas() -> list[dict]:
    """导出 MCP 客户端发现用的 JSON Schema 描述（不强制校验，仅文档）。"""
    return [
        {
            "name": c.name,
            "description": c.description,
            "inputSchema": c.input_schema,
        }
        for c in MCP_CONTRACTS.values()
    ]


class ContractViolation(ValueError):
    """契约违反异常：仅供上层捕获与降级，不抛给 MCP 客户端。"""


def validate_tool_input(name: str, payload: dict) -> None:
    """校验 MCP 工具入参；不符合契约时抛 ContractViolation。"""
    if name not in MCP_CONTRACTS:
        raise ContractViolation(f"未知工具: {name}")
    schema = MCP_CONTRACTS[name].input_schema
    try:
        jsonschema.validate(instance=payload or {}, schema=schema)
    except jsonschema.ValidationError as e:
        raise ContractViolation(f"[{name}] 入参不合法: {e.message}") from e


def validate_tool_output(name: str, output: Any) -> None:
    """校验 MCP 工具出参（V1 全部为 str；空字符串亦合法）。"""
    if name not in MCP_CONTRACTS:
        raise ContractViolation(f"未知工具: {name}")
    schema = MCP_CONTRACTS[name].output_schema
    if schema.get("type") == "string" and not isinstance(output, str):
        raise ContractViolation(f"[{name}] 出参须为字符串，实际 {type(output).__name__}")


def is_error_output(name: str, output: str) -> bool:
    """判断 MCP 工具输出是否为「错误：xxx」结构（V1 约定）。"""
    return isinstance(output, str) and output.startswith(MCP_CONTRACTS[name].error_prefix + "：")


def to_jsonable(contract_dict: dict) -> str:
    """用于 MCP 协议导出（ensure_ascii=False 保中文）。"""
    return json.dumps(contract_dict, ensure_ascii=False, indent=2)
