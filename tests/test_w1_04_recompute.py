"""W1-4：治理重算接线 + apply_decision 条件写入。

覆盖：投影无变化时二次重算不写盘（文件字节不变，调度无 git churn）；
新 L3 反证到达后重算使目标卡 stale 而无关卡不动；worker handler 与门；
CLI recompute 手动触发。
"""
import json
from pathlib import Path

from knowbase import __main__ as cli, config, index, store, writer
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
    report = validators.parse_pytest_text(
        validators.stamp_report_text(FAIL_REPORT, memory_id=mid, content_revision=revision))
    attribution.record_execution(conn, memory_id=mid, content_revision=revision,
                                 artifact_hash=report["artifact_hash"])
    report["target"] = {"memory_id": mid,
                        "content_revision": revision}
    result = attribution.verify_execution(conn, repo, run_id="",
                                          validator_id="pytest-text", report=report)
    assert not result.get("error"), result
    assert result["outcome"] == "fail"


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
