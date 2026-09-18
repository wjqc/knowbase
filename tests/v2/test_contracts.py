"""P0-C 6 个 MCP 工具 JSON Schema 契约测试。

校验：
- 入参缺必填/类型错 → ContractViolation
- 6 个工具的契约均可被 mcp_schemas() 导出
- 错误输出前缀识别正确
"""
from __future__ import annotations

import pytest

from knowbase.v2.contracts import (
    ContractViolation,
    ToolContract,
    is_error_output,
    mcp_schemas,
    validate_tool_input,
    validate_tool_output,
)


def test_all_six_tools_have_contracts():
    expected = {"memory_save", "memory_update", "memory_read",
                "memory_search", "memory_feedback", "memory_stats"}
    schemas = {s["name"] for s in mcp_schemas()}
    assert schemas == expected


def test_mcp_schemas_returns_documents():
    docs = mcp_schemas()
    assert len(docs) == 6
    for d in docs:
        assert "name" in d
        assert "description" in d
        assert "inputSchema" in d
        assert d["inputSchema"]["type"] == "object"


# ---------- memory_save ----------

def test_memory_save_minimal_valid():
    validate_tool_input("memory_save", {
        "type": "pitfall", "title": "vpn 死锁", "body": "正文"
    })


def test_memory_save_missing_required():
    with pytest.raises(ContractViolation):
        validate_tool_input("memory_save", {"type": "pitfall", "title": "x"})
    with pytest.raises(ContractViolation):
        validate_tool_input("memory_save", {"type": "pitfall", "body": "正文"})


def test_memory_save_invalid_type():
    with pytest.raises(ContractViolation):
        validate_tool_input("memory_save", {
            "type": "unknown_type", "title": "x", "body": "y"
        })


def test_memory_save_full_payload_ok():
    validate_tool_input("memory_save", {
        "type": "decision", "title": "选型", "body": "正文",
        "tags": ["vpn", "mac"], "scope": "project",
        "relations": [{"id": "M-000123", "type": "supersedes"}],
        "evidence": [{"kind": "log", "ref": "/tmp/a.log"}],
        "source": "agent:claude:adhoc",
        "provenance": "github:issue/123", "domain": "infra",
        "rule_status": "draft",
    })


def test_memory_save_rejects_invalid_relation_id():
    with pytest.raises(ContractViolation):
        validate_tool_input("memory_save", {
            "type": "pitfall", "title": "t", "body": "b",
            "relations": [{"id": "bad-id", "type": "supersedes"}]
        })


# ---------- memory_update ----------

def test_memory_update_requires_id():
    with pytest.raises(ContractViolation):
        validate_tool_input("memory_update", {"body": "x"})


def test_memory_update_allows_partial():
    validate_tool_input("memory_update", {"id": "M-000001", "title": "新标题"})
    validate_tool_input("memory_update", {"id": "M-000001", "tags": ["a"]})


# ---------- memory_read ----------

def test_memory_read_id_pattern():
    with pytest.raises(ContractViolation):
        validate_tool_input("memory_read", {"id": "M-1"})
    validate_tool_input("memory_read", {"id": "M-000001"})


# ---------- memory_search ----------

def test_memory_search_required_query():
    with pytest.raises(ContractViolation):
        validate_tool_input("memory_search", {"limit": 5})


def test_memory_search_limit_bounds():
    with pytest.raises(ContractViolation):
        validate_tool_input("memory_search", {"query": "x", "limit": 100})
    with pytest.raises(ContractViolation):
        validate_tool_input("memory_search", {"query": "x", "limit": 0})


# ---------- memory_feedback ----------

def test_memory_feedback_outcome_enum():
    with pytest.raises(ContractViolation):
        validate_tool_input("memory_feedback", {"id": "M-000001", "outcome": "maybe"})
    validate_tool_input("memory_feedback", {"id": "M-000001", "outcome": "helpful"})


# ---------- memory_stats ----------

def test_memory_stats_no_params():
    validate_tool_input("memory_stats", {})
    with pytest.raises(ContractViolation):
        validate_tool_input("memory_stats", {"extra": 1})


# ---------- output ----------

def test_output_must_be_string():
    validate_tool_output("memory_search", "命中 3 条")
    validate_tool_output("memory_search", "")
    with pytest.raises(ContractViolation):
        validate_tool_output("memory_search", {"x": 1})


def test_is_error_output():
    assert is_error_output("memory_search", "错误：未初始化") is True
    assert is_error_output("memory_search", "未命中「xxx」") is False
    assert is_error_output("memory_read", "错误：未找到 M-000001") is True


def test_unknown_tool_raises():
    with pytest.raises(ContractViolation):
        validate_tool_input("memory_xxx", {})
    with pytest.raises(ContractViolation):
        validate_tool_output("memory_xxx", "x")
