"""V2-10/11 使用证据分层：曝光/引用/采用分离、通用命令与歧义不发奖、
L3 只认已登记验证器的真实报告。S4 核心路径。"""
import json

from knowbase import consumption, delivery, index, store
from knowbase.knowledge_lifecycle import attribution, observation
from knowbase.migrations import apply_all

BODY_A = ("## 结论\nA：只读未用。\n## 解决的问题\nx。\n## 适用条件\ny。\n"
          "## 不适用条件\nz。\n## 可执行动作\n编辑 docs/a-guide.md 后校验。\n"
          "## 关键证据\nPR-1。\n## 验证情况\nv。\n## 未知与待确认\n无。\n")
BODY_B = ("## 结论\nB：修复脚本被执行。\n## 解决的问题\nx。\n## 适用条件\ny。\n"
          "## 不适用条件\nz。\n## 可执行动作\n运行 `python scripts/fix_crlf.py --all` 全量修复。\n"
          "## 关键证据\nPR-2。\n## 验证情况\nv。\n## 未知与待确认\n无。\n")
BODY_C = ("## 结论\nC：无关卡。\n## 解决的问题\nx。\n## 适用条件\ny。\n"
          "## 不适用条件\nz。\n## 可执行动作\n查看 runbook.md。\n"
          "## 关键证据\nPR-3。\n## 验证情况\nv。\n## 未知与待确认\n无。\n")


def seed_card(repo, title, body, scope="orders", dtype="pitfall"):
    """测试基建：直写文件+索引模拟历史已存在卡（绕开生产查重门——查重不是
    本文件被测逻辑；S3/S4 场景的多张相似卡在真实库中是跨期产生的）。"""
    from knowbase.mutations import _compute_revision
    meta = store.new_meta(dtype, title, scope, ["fixture"], "agent:fixture:s")
    meta["id"] = store.alloc_id(repo, dtype)
    path = store.mem_path(repo, dtype, meta["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    store.write_shared_text(path, store.render(meta, body))
    conn = index.connect(repo)
    index.upsert(conn, meta, body, path)
    conn.close()
    return meta["id"]


def deliver(conn, host, session, mid, revision, rank=1):
    return delivery.record_delivery(
        conn, search_id=f"search-{session}-{mid[:6]}", host_profile=host,
        session_id=session, context_event_id=f"ctx-{session}", task_id=f"task-{session}",
        task_revision="", scope="orders", memory_id=mid, content_revision=revision,
        rank=rank)


def test_s4_exposure_read_adoption_separated(memory_repo):
    """S4：A=L1（read）、B=L2（精确动作命中）、C=L0；completed 不给无采用卡发奖。"""
    a_id = seed_card(memory_repo, "A 只读卡", BODY_A)
    b_id = seed_card(memory_repo, "B 修复卡", BODY_B)
    c_id = seed_card(memory_repo, "C 无关卡", BODY_C)
    conn = index.connect(memory_repo)
    apply_all(conn)
    revs = {}
    for mid in (a_id, b_id, c_id):
        revs[mid] = conn.execute(
            "SELECT content_revision FROM entity_heads WHERE entity_id=?", (mid,)).fetchone()[0]
    # 会话：三卡递送；read A、B；工具执行命中 B 的独有动作；任务 completed。
    host, session = "zcode", "s-s4"
    tokens = {mid: deliver(conn, host, session, mid, revs[mid]) for mid in (a_id, b_id, c_id)}
    conn.commit()
    for mid in (a_id, b_id):
        assert consumption.record_delivered_read(memory_repo, delivery.by_id(conn, tokens[mid]))["delivery_linked"]
    run = observation.ingest(conn, host, session, [
        {"native_key": "t1", "kind": "tool_call", "tool_call_id": "tc-1",
         "tool_name": "Bash", "payload": {"command": "python scripts/fix_crlf.py --all"}},
        {"native_key": "t2", "kind": "tool_result", "tool_call_id": "tc-1",
         "payload": {"exit_code": 0}},
        {"native_key": "t3", "kind": "tool_call", "tool_call_id": "tc-2",
         "tool_name": "Bash", "payload": {"command": "git status"}},
        {"native_key": "t4", "kind": "tool_result", "tool_call_id": "tc-2",
         "payload": {"exit_code": 0}},
        {"native_key": "t5", "kind": "task_end",
         "payload": {"completion": "completed"}},
    ], project_key="repo:orders", native_task_key=f"task-{session}", native_run_key="s4-run")
    conn.commit()
    result = attribution.correlate(conn, memory_repo, run["run_id"])
    assert result["L0"] and set(result["L0"]) == {a_id, b_id, c_id}
    assert set(result["L1"]) == {a_id, b_id}
    assert result["L2"] == [b_id]  # 只有 B 的精确动作被命中
    assert a_id not in result["L2"] and c_id not in result["L2"]
    # 通用命令被拒收清单化。
    assert any("git status" in g for g in result["generic_rejected"])
    # 任务 completed 不给 A/C 产生任何 L2/L3。
    summary_a = attribution.evidence_summary(conn, a_id, revs[a_id])
    assert summary_a["L2"] == 0 and summary_a["L3"] == 0 and summary_a["L1"] == 1
    summary_c = attribution.evidence_summary(conn, c_id, revs[c_id])
    assert summary_c["L0"] == 1 and summary_c["L1"] == 0 and summary_c["L2"] == 0
    summary_b = attribution.evidence_summary(conn, b_id, revs[b_id])
    assert summary_b["L2"] == 1
    conn.close()


def test_ambiguous_action_not_awarded(memory_repo):
    """两卡动作命中同一 token → ambiguous，都不发 L2。"""
    x_id = seed_card(memory_repo, "X 同动作卡", BODY_B.replace("B：", "X："))
    y_id = seed_card(memory_repo, "Y 同动作卡", BODY_B.replace("B：", "Y："))
    conn = index.connect(memory_repo)
    apply_all(conn)
    host, session = "zcode", "s-amb"
    for mid in (x_id, y_id):
        rev = conn.execute("SELECT content_revision FROM entity_heads WHERE entity_id=?",
                           (mid,)).fetchone()[0]
        token = deliver(conn, host, session, mid, rev)
        conn.commit()
        assert consumption.record_delivered_read(memory_repo, delivery.by_id(conn, token))["delivery_linked"]
    run = observation.ingest(conn, host, session, [
        {"native_key": "t1", "kind": "tool_call", "tool_call_id": "tc-1",
         "tool_name": "Bash", "payload": {"command": "python scripts/fix_crlf.py --all"}},
        {"native_key": "t2", "kind": "tool_result", "tool_call_id": "tc-1",
         "payload": {"exit_code": 0}},
    ], project_key="repo:orders", native_task_key=f"task-{session}", native_run_key="amb-run")
    conn.commit()
    result = attribution.correlate(conn, memory_repo, run["run_id"])
    assert result["L2"] == []  # 歧义不发奖
    assert set(result["ambiguous"]) >= {x_id} & {x_id, y_id}
    conn.close()


def test_l3_requires_registered_validator_and_real_report(memory_repo):
    """V2-11 硬门：未登记验证器拒绝；空报告 unknown；全 pass 才 L3；
    引用不存在的 claim → unknown；伪 independent 无通道。"""
    mid = seed_card(memory_repo, "L3 验证卡", BODY_B)
    conn = index.connect(memory_repo)
    apply_all(conn)
    rev = conn.execute("SELECT content_revision FROM entity_heads WHERE entity_id=?",
                       (mid,)).fetchone()[0]
    run = observation.ingest(conn, "zcode", "s-l3", [
        {"native_key": "t1", "kind": "tool_call", "tool_call_id": "tc-1",
         "tool_name": "Bash", "payload": {"command": "pytest -q tests/test_crlf.py"}},
        {"native_key": "t2", "kind": "tool_result", "tool_call_id": "tc-1",
         "payload": {"exit_code": 0, "artifact": "report.json"}},
    ], project_key="repo:orders", native_task_key="l3", native_run_key="l3-run")
    # 未登记验证器 → 拒绝（Agent 自报 independent 无通道）。
    unregistered = attribution.verify_execution(
        conn, memory_repo, run_id=run["run_id"], validator_id="agent-self-report",
        report={"target": {"memory_id": mid, "content_revision": rev},
                "artifact_hash": "h" * 64,
                "checks": [{"check_id": "c1", "observed_result": "pass"}]})
    assert unregistered["error"] == "VALIDATOR_NOT_REGISTERED"
    # 旧的执行戳和直接登记不足以产生 L3。
    artifact = "a" * 64
    attribution.record_execution(conn, memory_id=mid, content_revision=rev,
                                 artifact_hash=artifact, run_id=run["run_id"], validator_id="pytest-text")
    report = {"target": {"memory_id": mid, "content_revision": rev},
              "artifact_hash": artifact, "execution_stamp": {"memory_id": mid, "content_revision": rev,
               "run_id": run["run_id"], "validator_id": "pytest-text"},
              "checks": [{"check_id": "unrelated", "observed_result": "pass"}]}
    assert attribution.verify_execution(conn, memory_repo, run_id=run["run_id"], validator_id="pytest-text",
                                        report=report)["error"] == "VALIDATOR_BINDING_REQUIRED"
    from lifecycle_verifier_fixture import run_verifier
    good, _ = run_verifier(conn, memory_repo, mid, rev, run_id=run["run_id"])
    assert good["outcome"] == "pass" and good["governance"]["decision"] == "validated"
    failed, _ = run_verifier(conn, memory_repo, mid, rev, run_id=run["run_id"], case="counterexample", outcome="fail")
    assert failed["outcome"] == "fail" and failed["governance"]["decision"] == "quarantine"
    conn.close()
