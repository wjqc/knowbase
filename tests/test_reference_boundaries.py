import json
from pathlib import Path

import pytest

from knowbase import config, sources, store
from knowbase.__main__ import cmd_import, cmd_init, cmd_promote, cmd_revise
from knowbase.server import save_impl, update_impl


@pytest.fixture()
def isolated_repo(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    cfg_path = tmp_path / "config.json"
    cfg = config._deep_merge(config.DEFAULTS, {
        "repo_path": str(repo),
        "knowledge_path": "",
        "git": {"auto_commit": False, "auto_push": False, "auto_pull": False},
    })
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", cfg_path)
    monkeypatch.setenv("KNOWBASE_REPO_PATH", str(repo))
    monkeypatch.setenv("KNOWBASE_AGENT_NAME", "pytest-agent")
    assert cmd_init() == 0
    return repo


def _card(ref: str) -> str:
    return (
        "## 结论\n引用边界校验结论。\n\n"
        "## 解决的问题\n避免共享经验卡依赖他人机器上的文件。\n\n"
        "## 适用条件\n- 所有类型经验卡\n- 含文档或代码引用\n\n"
        "## 不适用条件\n- 临时本地草稿\n\n"
        f"## 可执行动作\n{ref}\n\n"
        "## 关键证据\n路径失效导致复用失败的案例。\n\n"
        "## 验证情况\n2026-09-21 本机验证写入拦截生效。\n\n"
        "## 未知与待确认\n暂未发现绕过路径。\n"
    )


STANDARD_CARD = (
    "## 结论\n共享卡正文必须自足。\n\n"
    "## 解决的问题\n防止卡片依赖外部文件。\n\n"
    "## 适用条件\n- 正式生效的标准\n\n## 不适用条件\n- staging 提案\n\n"
    "## 可执行动作\n写入前自查引用。\n\n"
    "## 关键证据\n团队规范条目。\n\n"
    "## 验证情况\n2026-09-21 治理评审通过。\n\n"
    "## 未知与待确认\n暂无。\n"
)


@pytest.mark.parametrize("ref", [
    "参见 ~/knowledge/spec.md。",
    "参见 /Users/alice/work/docs/spec.pdf。",
    r"参见 C:\Users\alice\docs\spec.docx。",
])
def test_save_rejects_machine_paths(isolated_repo, ref):
    result = save_impl("pitfall", f"拒绝机器路径 {len(ref)}", _card(ref))
    assert result.startswith("错误：lint")
    assert "禁止保存本机路径" in result


def test_save_allows_project_relative_code_reference(isolated_repo):
    result = save_impl(
        "pitfall", "允许项目相对代码定位",
        _card("代码 `code:cmi-sales-assistant/agent-platform/app/service.py:42`。"),
    )
    assert result.startswith("已保存")


def test_document_reference_must_exist_inside_knowbase(isolated_repo):
    rejected = save_impl(
        "pitfall", "拒绝仓外相对文档",
        _card("参见 `../knowledge/spec.md`。"),
    )
    assert "文档引用越出 knowbase 仓库" in rejected

    docs = isolated_repo / "docs"
    docs.mkdir()
    (docs / "runbook.md").write_text("# Runbook\n", encoding="utf-8")
    accepted = save_impl(
        "pitfall", "允许仓内相对文档",
        _card("参见 `docs/runbook.md`。"),
    )
    assert accepted.startswith("已保存")

    code_doc = save_impl(
        "pitfall", "允许代码仓内文档定位",
        _card("实现说明见 `code:cmi-sales-assistant/agent-platform/docs/protocol.md`。"),
    )
    assert code_doc.startswith("已保存")


def test_update_and_revise_enforce_same_boundary(isolated_repo, tmp_path):
    result = save_impl("pitfall", "更新也校验引用", _card("正文自足。"))
    mid = result.split()[1]
    denied = update_impl(mid, body=_card("参见 ~/knowledge/spec.md。"))
    assert "禁止保存本机路径" in denied

    governed = save_impl("standard", "人工修订也校验引用", STANDARD_CARD)
    governed_id = governed.split()[1]
    assert cmd_promote(governed_id) == 0
    revision = tmp_path / "revision.md"
    revision.write_text(
        STANDARD_CARD.replace("写入前自查引用。", "参见 /Users/alice/knowledge/spec.md。"),
        encoding="utf-8",
    )
    assert cmd_revise(governed_id, str(revision)) == 1


def test_import_creates_source_without_machine_path(isolated_repo, tmp_path):
    source = tmp_path / "outside-docs"
    source.mkdir()
    imported = source / "lesson.md"
    imported.write_text("# 导入经验\n\n正文已经完整复制。\n", encoding="utf-8")

    assert cmd_import(str(source), "reference", "demo", False, "human:import") == 0
    manifests = list(sources.iter_manifests(isolated_repo))
    assert len(manifests) == 1
    assert not list((isolated_repo / "references").glob("R-*.md"))
    assert str(source) not in (isolated_repo / sources.MANIFESTS_DIR / "SRC-2026-0001.yaml").read_text(encoding="utf-8")
    obj = isolated_repo / manifests[0]["object"]
    assert "正文已经完整复制" in obj.read_text(encoding="utf-8")
    assert str(source) not in obj.read_text(encoding="utf-8")

    # 本机状态仍保留绝对源路径，用于重复导入幂等；它不是共享内容。
    from knowbase import index
    conn = index.connect(isolated_repo)
    row = conn.execute("SELECT source_hash FROM source_state WHERE source_path=?", (str(imported.resolve()),)).fetchone()
    conn.close()
    assert row is not None
