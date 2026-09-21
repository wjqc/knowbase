"""Structured code_refs validation and MCP/index integration tests."""

import json
from datetime import date
from pathlib import Path

import pytest

from knowbase import config, index, store
from knowbase.__main__ import cmd_init
from knowbase.server import read_impl, save_impl, search_impl, update_impl


@pytest.fixture()
def isolated_repo(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    cfg_path = tmp_path / "config.json"
    cfg = config._deep_merge(config.DEFAULTS, {
        "repo_path": str(repo),
        "knowledge_path": "",
        "git": {"auto_commit": False, "auto_push": False, "auto_pull": False},
    })
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", cfg_path)
    monkeypatch.setenv("KNOWBASE_REPO_PATH", str(repo))
    monkeypatch.setenv("KNOWBASE_AGENT_NAME", "pytest-agent")
    assert cmd_init() == 0
    return repo


CARD = """## 结论
bizrule 必须携带 provenance。

## 解决的问题
防止无业务出处的代码推测被当作已生效规则。

## 适用条件
- 使用 memory_save 保存 bizrule

## 不适用条件
- 保存 pitfall 或 workflow

## 可执行动作
保存前校验 provenance 非空。

## 关键证据
save_impl 在 lint 后才写入文件。

## 验证情况
AI 代码逆向梳理于 2026-09-21 main，未人工复核。

## 未知与待确认
业务方尚未确认代码行为是否等同需求真值。
"""


def _meta(code_refs):
    meta = store.new_meta("pitfall", "code refs 校验", "global", ["code", "代码"],
                          "agent:pytest:case", code_refs=code_refs)
    meta["id"] = "P-2026-0001"
    return meta


def test_lint_accepts_valid_code_refs():
    refs = [{
        "repo": "order-center",
        "path": "svc/order/OrderServiceImpl.java",
        "symbol": "OrderServiceImpl#changeCard",
        "lines": "120-180",
        "note": "实名校验入口",
    }]
    assert store.code_refs_errors(_meta(refs)) == []
    assert not any("code_refs" in e for e in store.lint(_meta(refs), CARD))


@pytest.mark.parametrize(("refs", "message"), [
    ([{"path": "svc/Impl.java"}], "repo"),
    ([{"repo": "order-center", "path": "/svc/Impl.java"}], "相对路径"),
    ([{"repo": "order-center", "path": "svc/../Impl.java"}], ".."),
    ([{"repo": "order-center", "path": "svc/Impl.java", "lines": "12:20"}], "lines"),
])
def test_lint_rejects_invalid_code_refs(refs, message):
    errors = store.code_refs_errors(_meta(refs))
    assert errors and any(message in error for error in errors)
    assert any(message in error for error in store.lint(_meta(refs), CARD))


def test_save_search_read_and_update_code_refs(isolated_repo):
    first = [{
        "repo": "knowbase",
        "path": "knowbase/server.py",
        "symbol": "save_impl",
        "lines": "98-170",
        "note": "业务规则保存入口",
    }]
    result = save_impl("pitfall", "bizrule provenance 保存校验", CARD,
                       tags=["bizrule", "业务规则"], scope="knowbase",
                       code_refs=first)
    assert result.startswith("已保存 P-"), result
    mid = result.split()[1]

    hit = search_impl("save_impl", scope="knowbase")
    assert mid in hit
    shown = read_impl(mid)
    assert "代码位置: knowbase knowbase/server.py#save_impl:98-170" in shown

    second = [{
        "repo": "knowbase",
        "path": "knowbase/store.py",
        "symbol": "lint",
        "lines": "284",
    }]
    updated = update_impl(mid, code_refs=second)
    assert updated.startswith("已更新"), updated
    meta, _, _ = store.load(isolated_repo, mid)
    assert meta["code_refs"] == second
    assert mid in search_impl("store.py lint", scope="knowbase")


def test_bizrule_staging_message_points_to_bizrules(isolated_repo):
    result = save_impl(
        "bizrule", "bizrule 的人工激活目标目录", CARD,
        tags=["bizrule", "业务规则"], scope="knowbase",
        provenance="代码逆向梳理待人工确认出处",
        code_refs=[{"repo": "knowbase", "path": "knowbase/store.py", "symbol": "lint"}],
    )
    assert "knowbase promote" in result and "bizrules/" in result


def test_legacy_card_without_code_refs_remains_valid(isolated_repo):
    meta = {
        "id": "P-2026-0001", "type": "pitfall", "title": "旧卡无 code refs",
        "scope": "global", "tags": ["legacy"], "source": "agent:legacy:old",
        "confidence": "once", "status": "active", "last_verified": "",
        "helpful_count": 0, "unhelpful_count": 0, "created": date.today().isoformat(),
        "updated": date.today().isoformat(), "relations": [],
    }
    body = "## 现象\n旧。\n\n## 原因\n旧。\n\n## 正确做法\n旧。\n"
    assert store.lint(meta, body, isolated_repo) == []


def test_code_marker_without_structured_refs_warns():
    meta = _meta([])
    warnings = store.lint_warnings(meta, CARD + "\ncode:knowbase/knowbase/server.py#save_impl:98\n")
    assert any("code_refs" in warning for warning in warnings)
