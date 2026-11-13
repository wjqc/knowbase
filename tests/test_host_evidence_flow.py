import json
import re

from knowbase import config, hooks, index, retrieval_metrics, store, writer
from knowbase.adapters import host_events
from knowbase.automation import worker
from knowbase.knowledge_lifecycle import governance
from knowbase.server import feedback_impl, read_impl, search_impl
from lifecycle_verifier_fixture import configure_verifier


BODY = """## 结论
执行规则回归标记脚本后，产物符合约定。
## 解决的问题
复核规则回归标记。
## 适用条件
隔离的规则验证项目。
## 不适用条件
其他项目。
## 可执行动作
运行 `python scripts/apply.py`。
## 关键证据
PR-1。
## 验证情况
用固定断言核对产物。
## 未知与待确认
无。
"""


def test_host_delivery_plain_read_and_automatic_verification(memory_repo, tmp_path, monkeypatch):
    project = tmp_path / "orders"
    project.mkdir()
    cfg = config.load_config()
    cfg["scope_map"] = {str(project): "orders"}
    cfg["hooks"]["inject_min_confidence"] = "any"
    assert not cfg["inflight"]["enabled"]
    config.CONFIG_PATH.write_text(json.dumps(cfg))
    monkeypatch.setenv("KNOWBASE_AGENT_NAME", "zcode")
    card = writer.create(memory_repo, "pitfall", "规则回归标记", BODY, scope="orders", provenance="PR-1")
    mid, revision = card["entity_id"], card["content_revision"]
    task = {"session_id": "session-a", "task_id": "order-fix", "cwd": str(project)}
    prompt = "规则回归标记"
    text = hooks.user_prompt(prompt, project, {"host_profile": "zcode", "session_id": "session-a"}, task)
    token = re.search(r'delivery_id="(DEL-[a-f0-9]+)"', text)[1]
    conn = index.connect(memory_repo)
    project_key = conn.execute("SELECT project_key FROM host_task_anchors").fetchone()[0]
    configure_verifier(memory_repo, mid, revision, project=project_key, auto=True, workspace=project)
    # Before the read, a path-only tool event is insufficient and must not end observation.
    call = {**task, "tool_name": "Read", "tool_use_id": "early", "tool_input": {"path": "scripts/apply.py"}}
    host_events.handle(call, event="tool-before", style="zcode")
    host_events.handle({**call, "tool_response": {"exit_code": 0}}, event="tool-after", style="zcode")
    initial = worker._h_lifecycle_correlate(memory_repo, conn, config.load_config())
    assert initial["L0"] == 1 and initial["L2"] == 0

    reply = read_impl(mid)  # legacy one-argument call, linked by the actual result event
    assert "read_use_id=" not in reply
    host_events.handle({**task, "tool_name": "mcp__knowbase__memory_read", "tool_input": {"id": mid},
                        "tool_response": {"content": [{"type": "text", "text": reply}]}},
                       event="tool-after", style="zcode")
    assert "read_use_id=USE-" in read_impl(mid, delivery_id=token)
    assert conn.execute("SELECT count(*) FROM use_retrieval_links").fetchone()[0] == 1
    observed = worker._h_lifecycle_correlate(memory_repo, conn, config.load_config())
    assert observed["L1"] == 1 and observed["L2"] == 0
    assert conn.execute("SELECT count(*) FROM validator_executions").fetchone()[0] == 0

    tool = {**task, "tool_name": "Bash", "tool_use_id": "apply", "tool_input": {"command": "python scripts/apply.py && cat result.txt"}}
    host_events.handle(tool, event="tool-before", style="zcode")
    host_events.handle({**tool, "tool_response": {"exit_code": 0}}, event="tool-after", style="zcode")
    result = worker._h_lifecycle_correlate(memory_repo, conn, config.load_config())
    assert result["L2"] == result["validated"] == 1, result
    assert store.load(memory_repo, mid)[0]["quality_state"] == "validated"
    assert governance.decide(conn, mid, revision)["independent_tasks"] == 1
    assert conn.execute("SELECT count(*) FROM feedback_log").fetchone()[0] == 0
    assert worker._h_lifecycle_correlate(memory_repo, conn, config.load_config())["correlated"] == 0
    conn.close()
    report = retrieval_metrics.report(memory_repo)
    assert next(iter(report["hook_opportunities"]["zcode"].values()))["with_delivery"] == 1
    assert report["read_attribution"]["zcode"]["with_linked_read"] == 1
    assert report["validation_conversion"]["zcode"]["with_trusted_verification"] == 1


def test_search_tool_result_binds_receipt_to_host_task(memory_repo, tmp_path, monkeypatch):
    monkeypatch.setenv("KNOWBASE_AGENT_NAME", "zcode")
    cfg = config.load_config()
    cfg["scope_map"] = {str(tmp_path): "orders"}
    config.CONFIG_PATH.write_text(json.dumps(cfg))
    mid = writer.create(memory_repo, "pitfall", "规则回归标记", BODY, scope="orders", provenance="PR-1")["entity_id"]
    task = {"session_id": "s", "cwd": str(tmp_path), "task_id": "task-a"}
    hooks.user_prompt("请继续", tmp_path, {"host_profile": "zcode", "session_id": "s"}, task)
    result = search_impl("规则回归标记", scope="orders")
    token = re.search(r'delivery_id="(DEL-[a-f0-9]+)"', result)[1]
    host_events.handle({**task, "tool_name": "mcp__knowbase__memory_search",
                        "tool_response": {"content": [{"type": "text", "text": result}]}},
                       event="tool-after", style="zcode")
    assert "read_use_id=" not in read_impl(mid, delivery_id="DEL-unknown")
    reply = read_impl(mid, delivery_id=token)
    use_id = re.search(r"read_use_id=(USE-[a-f0-9]+)", reply)[1]
    feedback = feedback_impl(use_id=use_id, outcome="incorrect", task_result="failed")
    assert "已记录反馈" in feedback
    with index.connect(memory_repo) as conn:
        assert conn.execute("SELECT task_id,session_id FROM knowledge_uses").fetchall() == [("task-a", "s")]
        assert conn.execute("SELECT f.binding,f.outcome,f.content_revision,o.task_id,o.result "
                            "FROM feedback_events f JOIN task_outcomes o USING(task_outcome_id) WHERE f.use_id=?",
                            (use_id,)).fetchone() == ("version_task", "incorrect",
                            re.search(r"content_revision=([a-f0-9]+)", reply)[1], "task-a", "failed")
    assert store.load(memory_repo, mid)[0]["status"] == "active"
