import json

from knowbase import commit_adapter, index, store, telemetry
from knowbase.server import feedback_impl, metrics_impl, stats_impl
from test_knowledge_consumption import seed
from test_versioned_feedback import feedback, use_for_task


def test_trae_shortcut_funnel_and_rejections(memory_repo, monkeypatch):
    from knowbase.server import context_impl, use_impl

    monkeypatch.setenv("KNOWBASE_AGENT_NAME", "trae")
    seed(memory_repo)
    context = json.loads(context_impl(session_id="trae-s1", event_id="lookup", scope="orders",
                                      task_id="sse-task", current_goal="SSE 重连", host_profile="trae"))
    card = context["candidates"][0]
    use = json.loads(use_impl(session_id="trae-s1", event_id="use", context_event_id="lookup",
                              id=card["id"], content_revision=card["content_revision"],
                              state="adopted", host_profile="trae"))
    assert use["delivery_linked"]
    assert store.load(memory_repo, card["id"])[0]["helpful_count"] == 0
    observed = telemetry.observe(feedback_impl, "memory_feedback")
    assert "TASK_RESULT_REQUIRED" in observed(use_id=use["use_id"], outcome="helpful")
    assert "VALIDATED_USE_REQUIRED" in observed(use_id="bad-receipt", outcome="helpful", task_result="completed")
    first = observed(use_id=use["use_id"], outcome="helpful", task_result="completed")
    replay = observed(use_id=use["use_id"], outcome="helpful")
    assert "已记录反馈" in first and "幂等恢复" in replay
    assert card["content_revision"] in first
    assert "TASK_OUTCOME_CONFLICT" in observed(use_id=use["use_id"], outcome="helpful", task_result="failed")
    meta = store.load(memory_repo, card["id"])[0]
    assert (meta["helpful_count"], meta["confidence"]) == (1, "once")
    with index.connect(memory_repo) as c:
        assert c.execute("SELECT COUNT(*) FROM feedback_events").fetchone()[0] == 1
        assert c.execute("SELECT result FROM task_outcomes").fetchone() == ("completed",)
    result = json.loads(metrics_impl())["counts"]["feedback_funnel"]
    cohort = result["linked_delivery_cohort"]["trae"]
    assert cohort["referenced_or_adopted"] == cohort["adopted"] == cohort["with_bound_feedback"] == 1
    assert cohort["read"] == 0
    attempts = result["call_attempts"]["trae"]["memory_feedback"]
    assert attempts == {"attempts": 5, "ok": 2, "rejected": 3, "reasons": {
        "TASK_RESULT_REQUIRED": 1, "TASK_OUTCOME_CONFLICT": 1, "VALIDATED_USE_REQUIRED": 1}}
    assert "TASK_RESULT_REQUIRED=1" in stats_impl()
    assert "bad-receipt" not in json.dumps(result)


def test_shortcut_reuses_explicit_result_without_creating_another_vote(memory_repo):
    seed(memory_repo)
    use = use_for_task(memory_repo)
    first = feedback(memory_repo, use, task_result="completed")
    replay = feedback(memory_repo, {"mid": "", "use_id": use["use_id"]})
    assert replay["idempotent"] and replay["receipt_id"] == first["receipt_id"]


def test_shortcut_never_attaches_old_receipt_to_new_content(memory_repo):
    seed(memory_repo)
    use = use_for_task(memory_repo)
    with index.connect(memory_repo) as c:
        _, body, _ = store.load(memory_repo, use["mid"])
        commit_adapter.adapter_update(memory_repo, c, use["mid"], body=body + "\n补充验证条件。")
    assert "版本冲突" in feedback_impl(use_id=use["use_id"], outcome="helpful", task_result="completed")
    with index.connect(memory_repo) as c:
        assert c.execute("SELECT COUNT(*) FROM feedback_events").fetchone()[0] == 0
