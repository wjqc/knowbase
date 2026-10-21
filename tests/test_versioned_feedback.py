import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from knowbase import commit_adapter,index,store
from knowbase.server import context_impl,use_impl
from test_knowledge_consumption import seed


def use_for_task(repo,task="task-1",state="adopted"):
    result=json.loads(context_impl(session_id=task,event_id="lookup",scope="orders",task_id=task,
        task_revision="plan-v1",current_goal="SSE 重连",host_profile="fixture"))
    card=result["candidates"][0]
    receipt=json.loads(use_impl(session_id=task,event_id="use",context_event_id="lookup",
        id=card["id"],content_revision=card["content_revision"],state=state,host_profile="fixture"))
    return dict(mid=card["id"],expected_content_revision=card["content_revision"],
                use_id=receipt["use_id"],task_outcome_id="reconnect-validated")


def feedback(repo,args,by="tool-a",outcome="helpful",request_id="",**changes):
    conn=index.connect(repo)
    try:
        return commit_adapter.adapter_feedback(repo,conn,outcome=outcome,by=by,
            **{**args,"request_id":request_id,**changes})
    finally:
        conn.close()


def test_current_version_task_results_promote_but_tool_labels_are_not_humans(memory_repo):
    meta,_,_=seed(memory_repo)
    first=use_for_task(memory_repo)
    r=feedback(memory_repo,first,by="human-fake")
    assert r["binding"]=="version_task"
    assert store.load(memory_repo,meta["id"])[0]["confidence"]=="once"
    second=use_for_task(memory_repo,"task-2")
    r=feedback(memory_repo,second,by="tool-b")
    meta=store.load(memory_repo,meta["id"])[0]
    assert meta["confidence"]=="verified"
    assert meta["verified_content_revision"]==first["expected_content_revision"]
    assert "不代表人数" in "".join(r["events"])


def test_same_task_result_is_deduplicated_across_requests_and_tools(memory_repo):
    seed(memory_repo);args=use_for_task(memory_repo)
    initial=feedback(memory_repo,args,request_id="req-one")
    repeated=feedback(memory_repo,args,by="tool-b",request_id="different-request")
    assert repeated["receipt_id"]==initial["receipt_id"] and repeated["idempotent"]
    assert feedback(memory_repo,args,by="tool-b",outcome="incorrect",request_id="different-request")["error"]=="IDEMPOTENCY_CONFLICT"
    assert feedback(memory_repo,args,outcome="incorrect")["error"]=="TASK_OUTCOME_CONFLICT"
    assert feedback(memory_repo,args,outcome="incorrect",request_id="req-one")["error"]=="IDEMPOTENCY_CONFLICT"
    meta=store.load(memory_repo,args["mid"])[0]
    assert meta["confidence"]=="once" and meta["helpful_count"]==1
    conn=index.connect(memory_repo)
    assert conn.execute("SELECT COUNT(*) FROM feedback_log").fetchone()[0]==1
    conn.close()


def test_old_feedback_and_old_use_do_not_validate_new_content(memory_repo):
    seed(memory_repo);old=use_for_task(memory_repo)
    feedback(memory_repo,old,request_id="old-result")
    conn=index.connect(memory_repo)
    meta,body,_=store.load(memory_repo,old["mid"])
    commit_adapter.adapter_update(memory_repo,conn,old["mid"],body=body+"新版本去重还应检查订阅范围。\n")
    conn.close()
    assert feedback(memory_repo,old,task_outcome_id="late-result")["error"]=="REVISION_CONFLICT"
    current=use_for_task(memory_repo,"new-task")
    feedback(memory_repo,current,by="tool-b")
    meta=store.load(memory_repo,old["mid"])[0]
    assert meta["confidence"]=="once" and meta["helpful_count"]==1
    replay=feedback(memory_repo,old,request_id="old-result")
    assert replay["idempotent"]
    assert store.load(memory_repo,old["mid"])[0]["helpful_count"]==1


def test_editing_verified_card_clears_old_validation(memory_repo):
    seed(memory_repo)
    feedback(memory_repo,use_for_task(memory_repo),by="tool-a")
    feedback(memory_repo,use_for_task(memory_repo,"task-2"),by="tool-b")
    conn=index.connect(memory_repo);meta,body,_=store.load(memory_repo,"P-2026-9001")
    assert meta["confidence"]=="verified"
    commit_adapter.adapter_update(memory_repo,conn,meta["id"],body=body+"更正适用范围。\n")
    conn.close();meta=store.load(memory_repo,meta["id"])[0]
    assert meta["confidence"]=="once" and not meta["last_verified"]
    assert not meta["verified_content_revision"] and meta["helpful_count"]==0


def test_unbound_legacy_calls_are_observations_without_governance(memory_repo):
    seed(memory_repo);args=dict(mid="P-2026-9001")
    for by,outcome in (("human-fake","helpful"),("tool-b","helpful"),("tool-b","incorrect")):
        r=feedback(memory_repo,args,by=by,outcome=outcome)
        assert r["binding"]=="legacy_unbound" and not r["governance_applied"]
    meta=store.load(memory_repo,args["mid"])[0]
    assert meta["confidence"]=="once" and meta["status"]=="active" and meta["helpful_count"]==0
    assert feedback(memory_repo,args,use_id="unknown")["error"]=="INCOMPLETE_FEEDBACK_BINDING"


def test_unverified_use_cannot_produce_validated_result_and_current_negative_stales(memory_repo):
    seed(memory_repo);pending=use_for_task(memory_repo,state="unverified")
    assert feedback(memory_repo,pending)["error"]=="VALIDATED_USE_REQUIRED"
    valid=use_for_task(memory_repo,"checked-task")
    r=feedback(memory_repo,valid,outcome="incorrect")
    assert r["governance_applied"]
    assert store.load(memory_repo,valid["mid"])[0]["status"]=="stale"


@pytest.mark.parametrize("crash_after_file",[False,True])
def test_crash_receipt_recovers_exactly_once(memory_repo,monkeypatch,crash_after_file):
    seed(memory_repo);args=use_for_task(memory_repo)
    target=index if crash_after_file else commit_adapter
    method="upsert" if crash_after_file else "_atomic_write"
    original=getattr(target,method)
    def crash(*a,**k): raise RuntimeError("controlled power loss")
    monkeypatch.setattr(target,method,crash)
    with pytest.raises(RuntimeError,match="power loss"):
        feedback(memory_repo,args,request_id="recover-me")
    monkeypatch.setattr(target,method,original)
    result=feedback(memory_repo,args,request_id="recover-me")
    assert result["idempotent"]
    assert store.load(memory_repo,args["mid"])[0]["helpful_count"]==1
    conn=index.connect(memory_repo)
    assert conn.execute("SELECT COUNT(*) FROM feedback_log").fetchone()[0]==1
    assert conn.execute("SELECT state FROM feedback_events").fetchone()[0]=="applied"
    conn.close()


def test_concurrent_feedback_same_task_result_counts_once(memory_repo):
    seed(memory_repo);args=use_for_task(memory_repo)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(lambda i:feedback(memory_repo,args,request_id=f"retry-{i}"),range(8)))
    assert len({r["receipt_id"] for r in results})==1
    assert store.load(memory_repo,args["mid"])[0]["helpful_count"]==1
