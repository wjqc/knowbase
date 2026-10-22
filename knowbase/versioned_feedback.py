"""反馈的版本/任务结果去重及文件落盘恢复；旧调用保留为未绑定观察。"""

import copy
import hashlib
import json
import uuid
from datetime import date,datetime,timezone

from . import index,materials,store
from .locking import RepoLock
from .mutations import _compute_revision,_compute_governance_revision


def _file_hash(meta,body):
    return hashlib.sha256(store.render(meta,body).encode()).hexdigest()


def _finish(repo,conn,row,*,replaying):
    (receipt,mid,before,after,meta_json,result_json,state,agent,outcome,stamp)=row
    result=json.loads(result_json)
    if state=="applied":
        return {**result,"idempotent":True}
    meta,body,path=store.load(repo,mid)
    if not meta:
        return {"error":"FEEDBACK_RECOVERY_CONFLICT","receipt_id":receipt}
    current=_file_hash(meta,body)
    if current not in (before,after):
        return {"error":"FEEDBACK_RECOVERY_CONFLICT","receipt_id":receipt}
    saved=json.loads(meta_json)
    if current!=after:
        from .commit_adapter import _atomic_write
        _atomic_write(path,store.render(saved,body))
    reread,rbody,rpath=store.load(repo,mid)
    if not reread or _file_hash(reread,rbody)!=after:
        return {"error":"FEEDBACK_READBACK_FAILED","receipt_id":receipt}
    index.upsert(conn,reread,rbody,rpath,rpath.parent.name=="staging",commit=False)
    conn.execute("INSERT INTO feedback_log(ts,memory_id,agent,outcome) VALUES(?,?,?,?)",
                 (stamp,mid,agent,outcome))
    conn.execute("UPDATE feedback_events SET state='applied' WHERE receipt_id=?",(receipt,))
    conn.commit()
    return {**result,"idempotent":replaying}


def _row(conn,where,params):
    return conn.execute("SELECT receipt_id,memory_id,before_hash,after_hash,after_meta_json,result_json,state,"
                        "agent,outcome,created_at FROM feedback_events WHERE "+where,params).fetchone()


def apply(repo,conn,mid,outcome,by,*,expected_content_revision="",expected_governance_revision="",
          use_id="",task_outcome_id="",request_id="",lock_timeout=10.0):
    from .lifecycle import OUTCOMES,PROMOTE_MIN_HELPFUL,PROMOTE_MIN_TOOLS
    if outcome not in OUTCOMES:
        return {"error":"INVALID_OUTCOME"}
    if any(not isinstance(v,str) or len(v)>512 for v in
           (mid,by,expected_content_revision,expected_governance_revision,use_id,task_outcome_id,request_id)):
        return {"error":"INVALID_FEEDBACK_IDENTITY"}
    bound=bool(use_id and task_outcome_id and expected_content_revision)
    if any((use_id,task_outcome_id,expected_content_revision)) and not bound:
        return {"error":"INCOMPLETE_FEEDBACK_BINDING"}
    phash=materials.digest([mid,outcome,by,expected_content_revision,use_id,task_outcome_id])
    with RepoLock(repo,lock_timeout):
        if request_id:
            prior=conn.execute("SELECT payload_hash,receipt_id FROM feedback_requests WHERE request_id=?",(request_id,)).fetchone()
            if prior:
                if prior[0]!=phash:
                    return {"error":"IDEMPOTENCY_CONFLICT"}
                return _finish(repo,conn,_row(conn,"receipt_id=?",(prior[1],)),replaying=True)
        use=None
        task_key=""
        if bound:
            use=conn.execute("SELECT host_profile,session_id,task_id,task_revision,memory_id,content_revision,state "
                             "FROM knowledge_uses WHERE use_id=?",(use_id,)).fetchone()
            if not use or use[4]!=mid or use[5]!=expected_content_revision or use[6] not in ("adopted","referenced"):
                return {"error":"VALIDATED_USE_REQUIRED"}
            task_key=materials.digest(list(use[:4]))
            prior=conn.execute("SELECT outcome FROM feedback_events WHERE memory_id=? AND content_revision=? "
                               "AND task_key=? AND task_outcome_id=? AND binding='version_task'",
                               (mid,expected_content_revision,task_key,task_outcome_id)).fetchone()
            if prior:
                if prior[0]!=outcome:
                    return {"error":"TASK_OUTCOME_CONFLICT"}
                existing=_row(conn,"memory_id=? AND content_revision=? AND task_key=? "
                    "AND task_outcome_id=? AND binding='version_task'",
                    (mid,expected_content_revision,task_key,task_outcome_id))
                if request_id:
                    conn.execute("INSERT INTO feedback_requests VALUES(?,?,?)",(request_id,phash,existing[0]))
                    conn.commit()
                return _finish(repo,conn,existing,replaying=True)
        # 未完成落盘的同卡反馈必须先恢复，不能以另一个请求覆盖其文件。
        pending=_row(conn,"memory_id=? AND state='applying'",(mid,))
        if pending:
            recovered=_finish(repo,conn,pending,replaying=True)
            if recovered.get("error"):
                return recovered
        meta,body,path=store.load(repo,mid)
        if not meta:
            return {"error":"not_found"}
        current=_compute_revision(meta,body)
        gov=_compute_governance_revision(meta)
        if expected_content_revision and current!=expected_content_revision:
            return {"error":"REVISION_CONFLICT","current_content_revision":current}
        if expected_governance_revision and gov!=expected_governance_revision:
            return {"error":"REVISION_CONFLICT","current_governance_revision":gov}
        if bound and (path.parent.name=="staging" or meta.get("status")=="archived"):
            return {"error":"KNOWLEDGE_NOT_ACTIVE"}
        after=copy.deepcopy(meta)
        events=[]
        if bound:
            after["updated"]=date.today().isoformat()
            key="helpful_count" if outcome=="helpful" else "unhelpful_count"
            after[key]=int(after.get(key,0))+1
            if outcome in ("outdated","incorrect") and after.get("status")=="active":
                after["status"]="stale"
                events.append(f"status: active → stale（{outcome}，版本绑定）")
            if outcome=="helpful" and after.get("confidence")=="once" and after.get("status")=="active":
                rows=conn.execute("SELECT agent FROM feedback_events WHERE memory_id=? AND content_revision=? "
                                  "AND outcome='helpful' AND binding='version_task' AND state='applied'",(mid,current)).fetchall()
                total=len(rows)+1;tools={r[0] for r in rows}|{by}
                if total>=PROMOTE_MIN_HELPFUL and len(tools)>=PROMOTE_MIN_TOOLS:
                    after["confidence"]="verified";after["last_verified"]=date.today().isoformat()
                    after["verified_content_revision"]=current
                    events.append(f"confidence: once → verified（本版本helpful×{total}，{len(tools)}个工具标签；不代表人数）")
        else:
            events.append("未绑定使用回执/内容版本/任务结果：仅保留观察，不改变治理状态或验证计数。")
        receipt="FB-"+uuid.uuid4().hex
        binding="version_task" if bound else "legacy_unbound"
        result={"entity_id":mid,"receipt_id":receipt,"content_revision":current,
                "governance_revision":_compute_governance_revision(after),"binding":binding,
                "events":events,"governance_applied":bound}
        stamp=datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn.execute("INSERT INTO feedback_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (receipt,request_id,phash,mid,current,use_id,task_key,task_outcome_id,by,outcome,binding,"applying",
             _file_hash(meta,body),_file_hash(after,body),json.dumps(after,ensure_ascii=False),
             json.dumps(result,ensure_ascii=False),stamp))
        if request_id:
            conn.execute("INSERT INTO feedback_requests VALUES(?,?,?)",(request_id,phash,receipt))
        conn.commit()  # 写前落回执；崩溃后核对before/after，不重复加计数。
        return _finish(repo,conn,_row(conn,"receipt_id=?",(receipt,)),replaying=False)
