import json
import re

from knowbase import context, index, store
from knowbase.server import context_impl, save_impl

CARD = """## 结论
已结算订单不能撤回；状态校验在服务层执行。

## 解决的问题
阻止已结算订单从旧页面被重复撤回。

## 适用条件
订单已结算，且当前用户请求撤回订单。

## 不适用条件
草稿订单和退单流程有单独规则。

## 可执行动作
读取订单状态，在保存前拒绝已结算订单的撤回请求。

## 关键证据
PRD-ORDER 第四条定义了结算后撤回限制。

## 验证情况
在测试项目复现已结算订单的撤回请求，服务层拒绝，数据库状态保持已结算。

## 未知与待确认
跨系统退单流程尚未验证。
"""


def seed(repo, scope="orders", dtype="pitfall"):
    out = save_impl(dtype, "已结算订单撤回的状态校验", CARD, scope=scope,
                    provenance="PRD-ORDER §4" if dtype == "bizrule" else None)
    match = re.search(r"已保存 ([PB]-[\w-]+)", out)
    assert match, out
    return match.group(1)


def run(event, scope="orders", queries=None, stage="subtask_start", **extra):
    return json.loads(context_impl(session_id="s1", event_id=event, scope=scope, stage=stage,
        task_id="t1", task_revision="task-v1", current_goal="订单撤回校验",
        topics=[{"topic_id": "p1", "queries": queries or ["已结算 订单 撤回"], "status": "active"}],
        **extra))


def test_multiple_execution_stages_and_new_failure_use_existing_card(memory_repo):
    mid = seed(memory_repo)
    first = run("e1")
    assert mid in [c["id"] for c in first["candidates"]]
    assert "不能撤回" in first["candidates"][0]["summary"]
    assert run("e2")["retrieval_status"] == "skipped_cache"
    failure = run("e3", stage="new_failure", evidence_refs=[{"tool_call_id": "failure-2"}])
    assert failure["budget_used"]["queries"] > 0
    assert failure["candidates"]


def test_context_event_idempotency_and_scope_binding(memory_repo):
    seed(memory_repo)
    original = run("fixed")
    assert run("fixed") == original
    assert run("fixed", queries=["不同问题"])["error"] == "IDEMPOTENCY_CONFLICT"
    assert run("other", scope="other-project")["error"] == "SCOPE_MISMATCH"
    conn = index.connect(memory_repo)
    assert conn.execute("SELECT COUNT(*) FROM context_events WHERE event_id='fixed'").fetchone()[0] == 1
    conn.close()


def test_rule_in_staging_is_not_default_execution_advice(memory_repo):
    seed(memory_repo, dtype="bizrule")
    assert run("rules")["candidates"] == []


def test_once_card_requires_active_read_not_automatic_advice(memory_repo):
    seed(memory_repo)
    result = context.execute(memory_repo, session_id="auto", event_id="a1", scope="orders",
                             current_goal="订单撤回", automatic=True)
    assert not result["candidates"]
    assert run("manual")["candidates"][0]["confidence"] == "once"


def test_recovery_reinjects_after_context_was_compacted(memory_repo):
    seed(memory_repo)
    assert run("before")["candidates"]
    recovered = run("recover", stage="recover")
    assert recovered["candidates"]


def test_topics_over_budget_remain_deferred(memory_repo):
    result = run("budget", queries=["订单", "权限", "结算", "其他业务"])
    assert result["retrieval_status"] == "deferred_budget"
    assert result["next_topics"][0]["queries"][-1] == "其他业务"
    assert result["budget_used"]["queries"] == 3
    next_result = run("budget-next", queries=["订单", "权限", "结算", "其他业务"])
    assert next_result["budget_used"]["queries"] == 1
    assert not next_result["next_topics"]


def test_index_failure_is_recorded_not_reported_as_no_hit(memory_repo, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("controlled failure")
    monkeypatch.setattr(index, "search", fail)
    result = run("broken")
    assert result["retrieval_status"] == "failed"
    conn = index.connect(memory_repo)
    assert conn.execute("SELECT status FROM context_events WHERE event_id='broken'").fetchone()[0] == "failed"
    conn.close()


def test_same_day_knowledge_change_invalidates_cache(memory_repo):
    mid = seed(memory_repo)
    assert run("old")["candidates"]
    assert run("cached")["retrieval_status"] == "skipped_cache"
    meta, body, path = store.load(memory_repo, mid)
    body = body.replace("不能撤回", "必须先获得人工复核")
    path.write_text(store.render(meta, body))
    conn = index.connect(memory_repo)
    index.upsert(conn, meta, body, path)
    conn.close()
    updated = run("changed")
    assert updated["budget_used"]["queries"]
    assert "人工复核" in updated["candidates"][0]["summary"]


def test_empty_global_start_does_not_lock_later_project_binding(memory_repo):
    first = json.loads(context_impl(session_id="late-project", event_id="start", stage="recover", scope="global"))
    assert first["scope"] == "global"
    bound = json.loads(context_impl(session_id="late-project", event_id="real-task", scope="orders",
                                   stage="task_plan", current_goal="订单撤回"))
    assert bound["scope"] == "orders" and not bound.get("error")
