"""W1-7：admission→publish 的 worker 调度接线（默认关，双级门）。

覆盖：默认配置下 skipped（人工关卡保留）；开 propose 门后 staging 卡自动
立案（publish=False 停在 proposed，staging 源保留）；再开 publish 二级门后
自动发布（staging 删除、正式落盘）；经 tick 全链路调度执行。
"""
import json
from pathlib import Path

from knowbase import config, index, writer
from knowbase.automation import worker
from knowbase.migrations import apply_all

BODY = ("## 结论\n提案链自动跑。\n## 解决的问题\nx。\n"
        "## 适用条件\ny。\n## 不适用条件\nz。\n## 可执行动作\n运行 scripts/adm.py。\n"
        "## 关键证据\nPR。\n## 验证情况\nv。\n## 未知与待确认\n无。\n")


def _enable(repo, *, propose=True, publish=False):
    cfg = json.loads(config.CONFIG_PATH.read_text())
    cfg.setdefault("knowledge_lifecycle", {}).setdefault("worker", {})
    cfg["knowledge_lifecycle"]["worker"]["propose"] = propose
    cfg["knowledge_lifecycle"]["worker"]["publish"] = publish
    config.CONFIG_PATH.write_text(json.dumps(cfg))


def _staging_card(repo):
    # 统一生命周期：类型不参与路由——bizrule 项目内新增去掉 provenance
    # （来源不足）按统一规则落 staging。
    card = writer.create(repo, "bizrule", "自动提案规则", BODY, scope="orders")
    assert not card.get("error"), card
    assert card["staging"] is True, card
    assert writer.update(repo, card["entity_id"], provenance="测试依据 PRD-4")["staging"]
    return card["entity_id"]


def test_propose_disabled_by_default(memory_repo):
    conn = index.connect(memory_repo)
    apply_all(conn)
    cfg = config.load_config()
    assert cfg["knowledge_lifecycle"]["worker"]["propose"] is False  # 默认关
    assert worker._h_lifecycle_propose(
        memory_repo, conn, cfg) == {"skipped": "lifecycle_propose_disabled"}
    conn.close()


def test_propose_gate_publishes_only_with_second_gate(memory_repo):
    mid = _staging_card(memory_repo)
    conn = index.connect(memory_repo)
    apply_all(conn)
    staging = Path(memory_repo) / "staging" / f"{mid}.md"
    formal = Path(memory_repo) / "bizrules" / f"{mid}.md"
    # 一级门开、二级门关：立案停在 proposed，staging 保留。
    _enable(memory_repo, propose=True, publish=False)
    r1 = worker._h_lifecycle_propose(memory_repo, conn, config.load_config())
    assert r1["proposed"] and r1["proposed"][0]["memory_id"] == mid, r1
    assert not r1["published"] and staging.exists() and not formal.exists()
    # 二级门也开：自动发布。
    _enable(memory_repo, propose=True, publish=True)
    r2 = worker._h_lifecycle_propose(memory_repo, conn, config.load_config())
    assert r2["published"] and r2["published"][0]["memory_id"] == mid, r2
    assert formal.exists() and not staging.exists()
    conn.close()


def test_propose_scheduled_by_tick(memory_repo):
    mid = _staging_card(memory_repo)
    _enable(memory_repo, propose=True, publish=False)
    result = worker.tick(memory_repo)
    ran = {r["kind"]: r for r in result.get("ran", [])}
    assert "lifecycle_propose" in ran, ran
    assert ran["lifecycle_propose"]["status"] == "success", ran["lifecycle_propose"]
    conn = index.connect(memory_repo)
    rows = conn.execute(
        "SELECT change_id,state FROM lifecycle_changes WHERE relation='NEW'").fetchall()
    assert rows and rows[0][1] == "proposed", rows
    assert (Path(memory_repo) / "staging" / f"{mid}.md").exists()
    conn.close()
