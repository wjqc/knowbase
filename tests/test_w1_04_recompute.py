"""W1-4：治理重算接线 + apply_decision 条件写入。

覆盖：投影无变化时二次重算不写盘（文件字节不变，调度无 git churn）；
新 L3 反证到达后重算使目标卡 stale 而无关卡不动；worker handler 与门；
CLI recompute 手动触发。
"""
import json
import subprocess
from pathlib import Path

import pytest

from knowbase import __main__ as cli, config, gitops, index, store, writer
from knowbase.automation import worker
from knowbase.knowledge_lifecycle import attribution, governance, validators
from knowbase.migrations import apply_all
from knowbase.mutations import _compute_revision

BODY = ("## 结论\n重算只认证据。\n## 解决的问题\nx。\n"
        "## 适用条件\ny。\n## 不适用条件\nz。\n## 可执行动作\n运行 scripts/gov.py。\n"
        "## 关键证据\nPR。\n## 验证情况\nv。\n## 未知与待确认\n无。\n")

FAIL_REPORT = """\
=========================== short test summary info ============================
FAILED tests/test_gov.py::test_rule - assert 0 == 1
========================= 1 failed in 0.01s ==================
"""


def _card_path(repo, mid):
    for sub in ("pitfalls", "bizrules", "standards", "preferences", "worklogs"):
        p = Path(repo) / sub / f"{mid}.md"
        if p.exists():
            return p
        p = Path(repo) / "staging" / f"{mid}.md"
    return Path(repo) / "staging" / f"{mid}.md"


def _seed_negative_evidence(conn, repo, mid):
    meta, body, _ = store.load(repo, mid)
    revision = _compute_revision(meta, body)
    from lifecycle_verifier_fixture import run_verifier
    result, _ = run_verifier(conn, repo, mid, revision, outcome="fail")
    assert result["outcome"] == "fail", result


def test_recompute_is_conditional_write(memory_repo):
    a = writer.create(memory_repo, "bizrule", "治理规则A", BODY, scope="orders",
                      provenance="PRD-1")
    b = writer.create(memory_repo, "pitfall", "旁证卡B", BODY, scope="orders")
    assert not a.get("error") and not b.get("error"), (a, b)
    mid_a, mid_b = a["entity_id"], b["entity_id"]
    conn = index.connect(memory_repo)
    apply_all(conn)
    governance.recompute_all(memory_repo, conn)
    pa, pb = _card_path(memory_repo, mid_a), _card_path(memory_repo, mid_b)
    bytes_a, bytes_b = pa.read_bytes(), pb.read_bytes()
    meta_a, _body, _ = store.load(memory_repo, mid_a)
    assert meta_a.get("quality_state") == "unverified"  # 首轮已投影
    # 无新证据：二次全库重算零写盘（字节不变）。
    governance.recompute_all(memory_repo, conn)
    assert pa.read_bytes() == bytes_a and pb.read_bytes() == bytes_b
    # 新 L3 反证 → A 重算为 stale（字节变化），B 不动。
    _seed_negative_evidence(conn, memory_repo, mid_a)
    governance.recompute_all(memory_repo, conn)
    assert pa.read_bytes() != bytes_a
    assert store.load(memory_repo, mid_a)[0]["status"] == "stale"
    assert pb.read_bytes() == bytes_b
    conn.close()


def test_recompute_handler_and_gate(memory_repo):
    a = writer.create(memory_repo, "bizrule", "治理规则A", BODY, scope="orders",
                      provenance="PRD-1")
    assert not a.get("error"), a
    conn = index.connect(memory_repo)
    apply_all(conn)
    cfg = config.load_config()
    result = worker._h_lifecycle_recompute(memory_repo, conn, cfg)
    assert result["recomputed"] >= 1 and "decisions" in result, result
    assert result["divergent_heads"] == 0
    cfg_off = {"knowledge_lifecycle": {"worker": {"recompute": False}}}
    assert worker._h_lifecycle_recompute(
        memory_repo, conn, cfg_off) == {"skipped": "lifecycle_recompute_disabled"}
    conn.close()


def test_cli_recompute(memory_repo, capsys):
    a = writer.create(memory_repo, "pitfall", "CLI 重算卡", BODY, scope="orders")
    assert not a.get("error"), a
    assert cli.main(["lifecycle", "recompute"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["recomputed"] >= 1 and "divergent_heads" in out, out


def test_worker_commits_governance_and_syncs_divergent_main(memory_repo, tmp_path):
    def git(repo, *args):
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()

    result = writer.create(memory_repo, "pitfall", "待审治理投影", BODY, scope="orders")
    assert result["staging"]
    mid = result["entity_id"]
    _, original_body, path = store.load(memory_repo, mid)
    relative = path.relative_to(memory_repo).as_posix()
    bare, peer = tmp_path / "remote.git", tmp_path / "peer"
    git(tmp_path, "init", "--bare", str(bare))
    git(memory_repo, "symbolic-ref", "HEAD", "refs/heads/main")
    git(memory_repo, "config", "user.name", "governance-test")
    git(memory_repo, "config", "user.email", "governance@test.local")
    git(memory_repo, "config", "commit.gpgsign", "false")
    git(memory_repo, "add", ".")
    git(memory_repo, "commit", "-m", "seed content")
    git(memory_repo, "remote", "add", "origin", bare.as_uri())
    git(memory_repo, "push", "-u", "origin", "main")
    git(tmp_path, "clone", "-b", "main", bare.as_uri(), str(peer))
    (peer / "README.md").write_text("remote stats\n", encoding="utf-8")
    git(peer, "-c", "user.name=peer", "-c", "user.email=peer@test.local",
        "-c", "commit.gpgsign=false", "commit", "-am", "team stats")
    git(peer, "push", "origin", "main")
    peer_head = git(peer, "rev-parse", "HEAD")

    cfg = config.load_config()
    cfg["git"].update(auto_commit=True, auto_pull=True, auto_push=True,
                      allowed_remote_prefixes=[bare.as_uri()])
    config.CONFIG_PATH.write_text(json.dumps(cfg), encoding="utf-8")
    with index.connect(memory_repo) as conn:
        apply_all(conn)
        dirty = memory_repo / "notes.txt"
        dirty.write_text("manual work\n", encoding="utf-8")
        assert worker._h_lifecycle_recompute(memory_repo, conn, cfg) == {
            "blocked": "governance_worktree_dirty",
        }
        assert dirty.read_text(encoding="utf-8") == "manual work\n"
        assert "quality_state" not in store.load(memory_repo, mid)[0]
        dirty.unlink()

        out = worker._h_lifecycle_recompute(memory_repo, conn, cfg)
        assert out["recomputed"] == 1
        assert git(memory_repo, "status", "--porcelain") == ""
        assert git(memory_repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD") == relative
        assert conn.execute("SELECT staging FROM meta WHERE id=?", (mid,)).fetchone()[0] == 1
        assert worker._h_knowledge_sync(memory_repo, conn, cfg) == {"updated": True}
        head = git(memory_repo, "rev-parse", "HEAD")
        assert git(bare, "rev-parse", "refs/heads/main") == head
        assert git(memory_repo, "rev-parse", "HEAD^") == peer_head
        meta, body = store.parse_text(git(bare, "show", f"main:{relative}"))
        assert meta["quality_state"] == "unverified" and body.rstrip() == original_body.rstrip()
        worker._h_lifecycle_recompute(memory_repo, conn, cfg)
        assert git(memory_repo, "rev-parse", "HEAD") == head
        assert git(memory_repo, "status", "--porcelain") == ""


def test_recompute_restores_its_projection_when_git_commit_fails(memory_repo, tmp_path):
    result = writer.create(memory_repo, "pitfall", "提交失败回滚投影", BODY, scope="orders")
    mid = result["entity_id"]
    _, _, path = store.load(memory_repo, mid)
    original = path.read_bytes()
    gitops._run(memory_repo, "add", ".")
    gitops._run(memory_repo, "-c", "user.name=test", "-c", "user.email=test@test.local",
                "-c", "commit.gpgsign=false", "commit", "-m", "seed content")
    hooks = tmp_path / "reject-commit"
    hooks.mkdir()
    hook = hooks / "pre-commit"
    hook.write_bytes(b"#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    gitops._run(memory_repo, "config", "core.hooksPath", str(hooks))
    with index.connect(memory_repo) as conn:
        apply_all(conn)
        with pytest.raises(RuntimeError, match="git commit 失败"):
            governance.recompute_all(memory_repo, conn, cfg={"git": {"auto_commit": True}})
        assert path.read_bytes() == original
        assert gitops._run(memory_repo, "status", "--porcelain") == ""
