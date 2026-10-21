"""两个真实临时 Git 克隆：待审隔离、激活传播及接收方版本恢复。"""

import subprocess

from knowbase import config, index, store, writer
from knowbase.__main__ import cmd_promote
from knowbase.mutations import _compute_revision
from knowbase.server import read_impl
from knowbase.__main__ import _retire_shared_index


def git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_shared_rule_remains_staging_until_human_activation(memory_repo, tmp_path, monkeypatch):
    sections = {"结论": "已结算订单禁止撤回。", "解决的问题": "避免结算后被旧入口改变状态。",
        "适用条件": "订单已结算且用户请求撤回。", "不适用条件": "草稿订单由其他流程处理。",
        "可执行动作": "服务层先校验状态，已结算时拒绝撤回。", "关键证据": "PRD-ORDER 第四条明确了结算后的状态限制。",
        "验证情况": "这是 Git 同步受控测试资产，验证待审隔离和传播，不证明真实业务已实现。",
        "未知与待确认": "实际订单系统的端到端回归尚未执行。"}
    body = "\n\n".join(f"## {heading}\n\n{text}" for heading, text in sections.items())
    rule = {"business_object": "订单", "trigger": "撤回请求", "required_or_forbidden": "已结算时拒绝撤回",
        "exceptions": "草稿另行处理", "applicable_version": "受控 PRD 版本", "business_confirmation": "explicit_source",
        "implementation_status": "not_implemented"}
    saved = writer.create(memory_repo, "bizrule", "已结算订单撤回业务规则", body,
        scope="orders", provenance="PRD-ORDER §4", rule_context=rule)
    assert not saved.get("error"), saved
    mid = saved["entity_id"]
    git(memory_repo, "config", "user.name", "fixture-a")
    git(memory_repo, "config", "user.email", "fixture-a@example.invalid")
    git(memory_repo, "add", "--", ".gitignore", "staging/" + mid + ".md")
    git(memory_repo, "commit", "-m", "fixture: proposed business rule")
    clone = tmp_path / "member-b"
    git(tmp_path, "clone", str(memory_repo), str(clone))
    index.rebuild(clone)
    conn = index.connect(clone)
    assert not index.search(conn, "订单 撤回", scope="orders")
    conn.close()
    assert store.load(clone, mid)[2].parent.name == "staging"

    # 仅隔离夹具模拟可信人工 CLI；生产规则不会被本测试激活。
    assert cmd_promote(mid) == 0
    git(memory_repo, "add", "--", "staging/" + mid + ".md", "bizrules/" + mid + ".md")
    git(memory_repo, "commit", "-m", "fixture: reviewed activation")
    git(clone, "pull", "--ff-only")
    assert index.rebuild(clone) == 1
    meta, received_body, path = store.load(clone, mid)
    assert path.parent.name == "bizrules" and meta["rule_context"] == rule
    conn = index.connect(clone)
    assert mid in [hit["id"] for hit in index.search(conn, "订单 撤回", scope="orders")]
    head = conn.execute("SELECT content_revision FROM entity_heads WHERE entity_id=?", (mid,)).fetchone()[0]
    assert head == _compute_revision(meta, received_body)
    conn.close()
    assert not (clone / "memory.db").is_relative_to(memory_repo)  # 两台独立视图
    assert "memory.db" not in git(clone, "ls-files")
    monkeypatch.setenv("KNOWBASE_REPO_PATH", str(clone))
    assert head in read_impl(mid)


def test_retire_generated_index_preserves_other_user_changes(memory_repo):
    git(memory_repo, "config", "user.name", "fixture-owner")
    git(memory_repo, "config", "user.email", "fixture-owner@example.invalid")
    generated = memory_repo / "INDEX.md"
    business = memory_repo / "user-notes.md"
    business.write_text("initial business notes")
    git(memory_repo, "add", "--", ".gitignore", "user-notes.md")
    git(memory_repo, "add", "-f", "--", "INDEX.md")
    git(memory_repo, "commit", "-m", "fixture: legacy tracked index")
    generated.write_text("new derived index, preserve physical file")
    business.write_text("user modification that must not be committed")
    git(memory_repo, "add", "--", "user-notes.md")
    cfg = config.load_config()
    cfg["git"]["auto_commit"] = True
    assert "未执行" in _retire_shared_index(memory_repo, cfg)
    assert "INDEX.md" in git(memory_repo, "ls-files")
    git(memory_repo, "reset", "--", "user-notes.md")
    assert "已退役" in _retire_shared_index(memory_repo, cfg)
    assert generated.read_text() == "new derived index, preserve physical file"
    assert "INDEX.md" not in git(memory_repo, "ls-files")
    assert "user-notes.md" in git(memory_repo, "diff", "--name-only")
    assert git(memory_repo, "show", "HEAD:user-notes.md") == "initial business notes"
