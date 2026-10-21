import json
from concurrent.futures import ThreadPoolExecutor

from knowbase import index,store
from knowbase.server import context_impl,read_impl,use_impl


def seed(repo):
    meta=store.new_meta("pitfall","SSE 重连消息序号核对","orders",["SSE"],"agent:fixture")
    meta["id"]="P-2026-9001"
    body="## 结论\nSSE 重连要核对消息序号并去重。\n## 适用条件\n订单订阅。\n"
    path=store.mem_path(repo,"pitfall",meta["id"])
    path.write_text(store.render(meta,body))
    conn=index.connect(repo);index.upsert(conn,meta,body,path);conn.close()
    return meta,body,path


def delivered(repo,session="task-session",host="fixture-host"):
    seed(repo)
    result=json.loads(context_impl(session_id=session,event_id="retrieval",scope="orders",
        task_id="fix-reconnect",task_revision="plan-v2",current_goal="SSE 重连",
        host_profile=host))
    assert result["candidates"]
    return result["candidates"][0]


def record(card,**changes):
    args=dict(session_id="task-session",event_id="use-1",context_event_id="retrieval",
              id=card["id"],content_revision=card["content_revision"],state="adopted",
              host_profile="fixture-host")
    args.update(changes)
    return json.loads(use_impl(**args))


def test_read_reference_adoption_and_feedback_are_distinct(memory_repo):
    card=delivered(memory_repo)
    text=read_impl(card["id"],session_id="task-session",event_id="read-1",
                   context_event_id="retrieval",host_profile="fixture-host")
    assert "read_use_id=USE-" in text
    result=record(card)
    assert result["task_id"]=="fix-reconnect" and result["task_revision"]=="plan-v2"
    assert result["feedback_recorded"] is False
    conn=index.connect(memory_repo)
    assert set(r[0] for r in conn.execute("SELECT state FROM knowledge_uses"))=={"read","adopted"}
    assert conn.execute("SELECT COUNT(*) FROM feedback_log").fetchone()[0]==0
    conn.close()


def test_unknown_candidate_session_host_and_version_are_rejected(memory_repo):
    card=delivered(memory_repo)
    assert record(card,session_id="other")["error"]=="CONTEXT_EVENT_NOT_FOUND"
    assert record(card,host_profile="other")["error"]=="CONTEXT_EVENT_NOT_FOUND"
    assert record(card,id="P-2026-0000")["error"]=="CANDIDATE_NOT_DELIVERED"
    assert record(card,content_revision="wrong")["error"]=="CANDIDATE_NOT_DELIVERED"
    assert record(card,state="helpful")["error"]=="INVALID_USE_STATE"


def test_changed_content_cannot_be_adopted_as_delivered_version(memory_repo):
    card=delivered(memory_repo)
    meta,body,path=store.load(memory_repo,card["id"])
    path.write_text(store.render(meta,body+"新版本要求重新读取。\n"))
    assert record(card)["error"]=="CONTENT_REVISION_CONFLICT"
    result=record(card,state="unverified")
    assert result["state"]=="unverified" and result["current_version"] is False


def test_use_replay_concurrent_event_is_one_record_and_payload_conflicts(memory_repo):
    card=delivered(memory_repo)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(lambda _:record(card),range(8)))
    assert len({r["use_id"] for r in results})==1
    assert sum(bool(r.get("idempotent")) for r in results)==7
    assert record(card,state="irrelevant")["error"]=="IDEMPOTENCY_CONFLICT"
    conn=index.connect(memory_repo)
    assert conn.execute("SELECT COUNT(*) FROM knowledge_uses").fetchone()[0]==1
    conn.close()
