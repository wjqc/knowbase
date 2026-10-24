"""反馈的版本/任务结果去重及文件落盘恢复；旧调用保留为未绑定观察。"""

import copy
import hashlib
import json
import uuid
from datetime import date,datetime,timezone

from . import config,index,materials,store
from .locking import RepoLock
from .mutations import _compute_revision,_compute_governance_revision


def _file_hash(meta,body):
    return hashlib.sha256(store.render(meta,body).encode()).hexdigest()


def _finish(repo,conn,row,*,replaying):
    (receipt,mid,before,after,meta_json,result_json,state,agent,outcome,stamp,
     use_id,task_outcome_id,binding)=row
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
    if result.get("auto_activation"):
        target = store.mem_path(repo, saved["type"], mid)
        if path != target:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                return {"error":"ACTIVATION_TARGET_CONFLICT","receipt_id":receipt}
            path.replace(target)  # 同库原子移动；崩溃后 store.load 可从正式目录恢复。
    reread,rbody,rpath=store.load(repo,mid)
    if not reread or _file_hash(reread,rbody)!=after:
        return {"error":"FEEDBACK_READBACK_FAILED","receipt_id":receipt}
    index.upsert(conn,reread,rbody,rpath,rpath.parent.name=="staging",commit=False)
    conn.execute("INSERT INTO feedback_log(ts,memory_id,agent,outcome) VALUES(?,?,?,?)",
                 (stamp,mid,agent,outcome))
    conn.execute("UPDATE feedback_events SET state='applied' WHERE receipt_id=?",(receipt,))
    # 价值事件与状态迁移同事务；崩溃恢复重放时按 source_key 幂等，不重复计一条。
    value_event=None
    if config.load_config().get("value",{}).get("events_enabled"):
        from .value import events as _ve,outbox as _vo,identity as _vi
        ident=_vi.get(conn)
        installation=ident["installation_id"] or _vi.install(conn)
        host=session_id=fb_task_id=""
        if use_id:
            use_row=conn.execute("SELECT host_profile,session_id,task_id FROM knowledge_uses "
                                 "WHERE use_id=?",(use_id,)).fetchone()
            if use_row:
                host,session_id,fb_task_id=use_row
        event=_ve.build_event(
            "feedback.recorded",
            {"memory_id":mid,"content_revision":result.get("content_revision",""),
             "use_id":use_id,"task_outcome_id":task_outcome_id,"outcome":outcome,
             "binding":binding,"receipt_id":receipt,"feedback_source":"agent",
             **({"task_id":fb_task_id} if fb_task_id else {})},
            host=host,session_id=session_id,operation_id=f"memory_feedback:{receipt}",
            task_id=fb_task_id,installation_id=installation,
            member_binding_id=ident["member_binding_id"])
        value_event=_vo.append(conn,event,source_key=f"feedback_events:{receipt}")
        if result.get("auto_activation"):
            _ve.record(conn,"knowledge.activated",
                       {"memory_id":mid,"content_revision":result["content_revision"],
                        "scope":reread.get("scope","")},
                       source_key=f"knowledge:{mid}:{result['content_revision']}:auto_activated",
                       host=host,session_id=session_id,task_id=fb_task_id)
    conn.commit()
    out={**result,"idempotent":replaying}
    if value_event is not None:
        out["value_event"]=value_event
    return out


def _row(conn,where,params):
    return conn.execute("SELECT receipt_id,memory_id,before_hash,after_hash,after_meta_json,result_json,state,"
                        "agent,outcome,created_at,use_id,task_outcome_id,binding "
                        "FROM feedback_events WHERE "+where,params).fetchone()


def apply(repo,conn,mid,outcome,by,*,expected_content_revision="",expected_governance_revision="",
          use_id="",task_outcome_id="",request_id="",lock_timeout=10.0,
          task_result="",verification_source="agent_report"):
    from .lifecycle import OUTCOMES,PROMOTE_MIN_HELPFUL,PROMOTE_MIN_TOOLS
    if outcome not in OUTCOMES:
        return {"error":"INVALID_OUTCOME"}
    from .value.events import TASK_RESULTS,VERIFICATION_SOURCES
    if task_result and task_result not in TASK_RESULTS:
        return {"error":"INVALID_TASK_RESULT","allowed":sorted(TASK_RESULTS)}
    if verification_source not in VERIFICATION_SOURCES:
        return {"error":"INVALID_VERIFICATION_SOURCE","allowed":sorted(VERIFICATION_SOURCES)}
    if task_result and not task_outcome_id:
        return {"error":"TASK_RESULT_REQUIRES_OUTCOME_ID"}
    if any(not isinstance(v,str) or len(v)>512 for v in
           (mid,by,expected_content_revision,expected_governance_revision,use_id,task_outcome_id,request_id)):
        return {"error":"INVALID_FEEDBACK_IDENTITY"}
    bound=bool(use_id and task_outcome_id and expected_content_revision)
    if any((use_id,task_outcome_id,expected_content_revision)) and not bound:
        return {"error":"INCOMPLETE_FEEDBACK_BINDING"}
    phash=materials.digest([mid,outcome,by,expected_content_revision,use_id,task_outcome_id,
                           task_result,verification_source])
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
            if not use or use[4]!=mid or use[5]!=expected_content_revision or use[6] not in ("adopted","referenced","read"):
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
        policy=config.load_config().get("evolution",{})
        staged=path.parent.name=="staging"
        # 自进化统一出口：全部受治理类型（standard/preference/bizrule）的 staged 卡
        # 均可凭绑定 read 回执的真实任务验证积累激活证据；激活阈值见 decide()。
        # 延迟导入：与本文件内其他运行时导入一致，避免模块加载期循环。
        from .writer import GOVERNED
        validation=bool(bound and use[6]=="read" and meta.get("type") in GOVERNED
                        and policy.get("enabled",True) and policy.get("auto_activate_bizrules",True)
                        and task_result in ("completed","failed"))
        if bound and use[6]=="read" and not validation:
            return {"error":"VALIDATION_RESULT_REQUIRED"}
        if validation and outcome=="helpful" and task_result!="completed":
            return {"error":"VALIDATION_NOT_COMPLETED"}
        current=_compute_revision(meta,body)
        gov=_compute_governance_revision(meta)
        if expected_content_revision and current!=expected_content_revision:
            return {"error":"REVISION_CONFLICT","current_content_revision":current}
        if expected_governance_revision and gov!=expected_governance_revision:
            return {"error":"REVISION_CONFLICT","current_governance_revision":gov}
        if bound and ((staged and not validation) or meta.get("status")=="archived"):
            return {"error":"KNOWLEDGE_NOT_ACTIVE"}
        if bound and task_result:
            prior_outcome=conn.execute("SELECT task_id,host_profile,session_id FROM task_outcomes "
                                       "WHERE task_outcome_id=?",(task_outcome_id,)).fetchone()
            if prior_outcome and prior_outcome != (use[2],use[0],use[1]):
                return {"error":"TASK_OUTCOME_ID_CONFLICT"}
        after=copy.deepcopy(meta)
        events=[]
        auto_activation=False
        if bound:
            after["updated"]=date.today().isoformat()
            key="helpful_count" if outcome=="helpful" else "unhelpful_count"
            after[key]=int(after.get(key,0))+1
            if outcome in ("outdated","incorrect") and after.get("status")=="active":
                after["status"]="stale"
                # Phase 1 归档时钟起点：stale 起始日，之后自动归档据此计时。
                after["stale_since"]=date.today().isoformat()
                events.append(f"status: active → stale（{outcome}，版本绑定）")
            from .evolution import decide
            decision=decide(conn,mid,current,meta,use,task_key,outcome,task_result,staged=staged)
            if decision["retire"] and after.get("status")=="active":
                after["status"]="stale"
                after["stale_since"]=date.today().isoformat()
                events.append("status: active → stale（独立任务负面反馈，自动淘汰）")
            if decision["promote"] and after.get("confidence") in ("once","staging"):
                after["confidence"]="verified";after["last_verified"]=date.today().isoformat()
                after["verified_content_revision"]=current
                if after.get("status")=="stale" and after.get("revalidation_pending"):
                    # 严格语义复活：只有修订过的 stale 卡能走到这里；晋升时
                    # status 回 active 并清除修订标记，标记的使命即告完成。
                    # stale 时钟同步清除：复活后的卡不再是 stale 计时对象。
                    after["status"]="active"
                    after.pop("revalidation_pending", None)
                    after.pop("stale_since", None)
                    events.append("status: stale → active（修订后新版本证据达标，自动复活）")
                auto_activation=staged
                events.append(f"confidence: {meta.get('confidence')} → verified（{decision['helpful_tasks']}个独立任务，{decision['sessions']}个会话）")
                if auto_activation:
                    events.append(f"自动激活：staging → {store.TYPE_DIR.get(meta.get('type'), meta.get('type'))}（当前版本验证证据达标）")
            # 兼容既有无任务结果字段的工具反馈；新显式验证统一使用独立任务策略。
            if (not task_result and not staged and policy.get("enabled",True) and outcome=="helpful"
                    and after.get("confidence")=="once" and after.get("status")=="active"
                    and not decision["blocked"]):
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
        if auto_activation:
            result["auto_activation"]=True
        stamp=datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn.execute("INSERT INTO feedback_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (receipt,request_id,phash,mid,current,use_id,task_key,task_outcome_id,by,outcome,binding,"applying",
             _file_hash(meta,body),_file_hash(after,body),json.dumps(after,ensure_ascii=False),
             json.dumps(result,ensure_ascii=False),stamp))
        if request_id:
            conn.execute("INSERT INTO feedback_requests VALUES(?,?,?)",(request_id,phash,receipt))
        # 任务结果账本与反馈回执同事务；Agent 报告不能伪装人工验收（来源字段单列）。
        if bound and task_result:
            outcome_task = use[2] if use else ""
            conn.execute(
                "INSERT INTO task_outcomes(task_outcome_id,task_id,task_run_id,result,"
                "verification_source,host_profile,session_id,use_id,memory_id,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(task_outcome_id) DO UPDATE SET "
                "result=excluded.result,verification_source=excluded.verification_source,"
                "updated_at=excluded.updated_at",
                (task_outcome_id, outcome_task, "", task_result, verification_source,
                 use[0] if use else "", use[1] if use else "", use_id, mid, stamp, stamp))
            from .value.events import record as _record_value
            _record_value(conn,
                "task.completed" if task_result == "completed" else "task.abandoned",
                {"task_id": outcome_task, "task_outcome_id": task_outcome_id,
                 "result": task_result, "verification_source": verification_source,
                 "use_id": use_id, "memory_id": mid},
                source_key=f"task_outcomes:{task_outcome_id}",
                host=use[0] if use else "", session_id=use[1] if use else "",
                operation_id=f"task_outcome:{task_outcome_id}", task_id=outcome_task)
        conn.commit()  # 写前落回执；崩溃后核对before/after，不重复加计数。
        return _finish(repo,conn,_row(conn,"receipt_id=?",(receipt,)),replaying=False)
