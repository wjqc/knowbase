"""本机任务与知识版本的消费回执；不把检索、阅读或采纳换算成有效反馈。"""

import json
from datetime import datetime, timezone

from . import config, index, materials, store
from .locking import RepoLock
from .mutations import _compute_revision

STATES = {"referenced", "adopted", "irrelevant", "unverified"}


def record(repo, *, session_id, event_id, context_event_id, memory_id,
           content_revision, state, host_profile="", reading=False):
    if state not in (STATES | ({"read"} if reading else set())):
        return {"error":"INVALID_USE_STATE"}
    fields = (session_id,event_id,context_event_id,memory_id,content_revision,host_profile)
    if not all(isinstance(v,str) and len(v)<=512 for v in fields) or not all(fields[:5]):
        names=("session_id","event_id","context_event_id","memory_id","content_revision")
        return {"error":"INVALID_USE_IDENTITY","missing_fields":[name for name,value in zip(names,fields) if not value],
                "required_fields":list(names),"hint":"event_id为本次使用新事件，context_event_id指向实际返回该候选的检索事件；host_profile沿用检索"}
    host = host_profile or config.load_config()["inflight"].get("host_profile") or config.agent_name()
    phash = materials.digest([context_event_id,memory_id,content_revision,state])
    conn = index.connect(repo)
    try:
        with RepoLock(repo,timeout=config.load_config().get("lock_timeout",10)):
            conn.execute("BEGIN IMMEDIATE")
            old=conn.execute("SELECT payload_hash,use_id,current_version FROM knowledge_uses WHERE "
                             "host_profile=? AND session_id=? AND event_id=?",(host,session_id,event_id)).fetchone()
            if old:
                conn.rollback()
                return ({"use_id":old[1],"state":state,"current_version":bool(old[2]),"idempotent":True}
                        if old[0]==phash else {"error":"IDEMPOTENCY_CONFLICT"})
            row=conn.execute("SELECT task_id,result_json FROM context_events WHERE host_profile=? AND "
                             "session_id=? AND event_id=?",(host,session_id,context_event_id)).fetchone()
            if not row:
                return {"error":"CONTEXT_EVENT_NOT_FOUND"}
            receipt=json.loads(row[1])
            if not reading and not any(c.get("id")==memory_id and c.get("content_revision")==content_revision
                                       for c in receipt.get("candidates",[])):
                return {"error":"CANDIDATE_NOT_DELIVERED"}
            meta,body,path=store.load(repo,memory_id)
            current=bool(meta and _compute_revision(meta,body)==content_revision)
            if state in ("adopted","referenced","read"):
                if not current:
                    return {"error":"CONTENT_REVISION_CONFLICT"}
                if not reading and (path.parent.name=="staging" or meta.get("status")!="active"):
                    return {"error":"KNOWLEDGE_NOT_ACTIVE"}
                if reading and meta.get("scope") not in (receipt["scope"],"global"):
                    return {"error":"SCOPE_MISMATCH"}
            use_id="USE-"+materials.digest([host,session_id,event_id])[:24]
            task=receipt.get("task_id") or row[0] or session_id
            task_revision=receipt.get("task_revision","")
            conn.execute("INSERT INTO knowledge_uses VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (use_id,host,session_id,event_id,phash,context_event_id,task,task_revision,
                          receipt["scope"],memory_id,content_revision,state,int(current),
                          datetime.now(timezone.utc).isoformat(timespec="seconds")))
            # 价值事件与业务回执同事务：append 失败则整体回滚，不允许"回执在、事件丢"。
            value_event=None
            if config.load_config().get("value",{}).get("events_enabled"):
                from .value import events as _ve,outbox as _vo,identity as _vi
                ident=_vi.get(conn)
                installation=ident["installation_id"] or _vi.install(conn)
                event=_ve.build_event(
                    "knowledge.read" if state=="read" else "knowledge.used",
                    {"memory_id":memory_id,"content_revision":content_revision,
                     "context_event_id":context_event_id,"use_id":use_id,"state":state,
                     "task_id":task,"scope":receipt["scope"],
                     "task_revision":task_revision,"current_version":bool(current)},
                    host=host,session_id=session_id,operation_id=f"memory_use:{use_id}",
                    task_id=task,installation_id=installation,
                    member_binding_id=ident["member_binding_id"])
                value_event=_vo.append(conn,event,source_key=f"knowledge_uses:{use_id}")
            conn.commit()
            result={"use_id":use_id,"state":state,"task_id":task,"task_revision":task_revision,
                    "id":memory_id,"content_revision":content_revision,"current_version":current,
                    "feedback_recorded":False,"identity_basis":"host_labels"}
            if value_event is not None:
                result["value_event"]=value_event
            return result
    finally:
        conn.close()
