import json

import pytest

from knowbase import config, index, store, versioned_feedback, writer
from knowbase.server import context_impl, read_impl
from knowbase.mutations import _compute_revision
from test_knowledge_consumption import seed
from test_versioned_feedback import use_for_task, feedback


def rule(repo):
    body=("## 结论\n已结算订单不能撤回。\n## 解决的问题\n避免结算状态被修改。\n"
          "## 适用条件\n订单完成结算。\n## 不适用条件\n草稿订单。\n"
          "## 可执行动作\n拒绝撤回请求。\n## 关键证据\n需求 PRD-4。\n"
          "## 验证情况\n使用场景测试验证。\n## 未知与待确认\n无。\n")
    result=writer.create(repo,"bizrule","结算订单撤回规则",body,scope="orders",provenance="PRD-4 §2")
    assert not result.get("error"),result
    return result["entity_id"]


def validation(repo,mid,task,*,outcome="helpful",task_result="completed",outcome_id=None):
    session="s-"+task
    result=json.loads(context_impl(session_id=session,event_id="lookup",scope="orders",task_id=task,
                                  current_goal="验证订单规则",host_profile="fixture"))
    if (store.load(repo,mid)[2].parent.name=="staging" and
            config.load_config()["evolution"]["auto_activate_bizrules"]):
        assert mid in {c["id"] for c in result["validation_candidates"]}
    text=read_impl(mid,session_id=session,event_id="validation-read",context_event_id="lookup",host_profile="fixture")
    assert "read_use_id=" in text,text
    meta,body,_=store.load(repo,mid)
    conn=index.connect(repo)
    use=conn.execute("SELECT use_id FROM knowledge_uses WHERE session_id=?",(session,)).fetchone()[0]
    try:
        return versioned_feedback.apply(repo,conn,mid,outcome,"one-tool",
            expected_content_revision=_compute_revision(meta,body),use_id=use,
            task_outcome_id=outcome_id or "out-"+task,task_result=task_result,request_id="req-"+task+(outcome_id or ""))
    finally:
        conn.close()


def test_same_tool_independent_tasks_promote_without_manual_review(memory_repo):
    meta,_,_=seed(memory_repo)
    for n in range(3):
        result=feedback(memory_repo,use_for_task(memory_repo,"task-"+str(n)),by="same-tool",
                        task_result="completed",task_outcome_id="out-"+str(n))
        assert not result.get("error"),result
    assert store.load(memory_repo,meta["id"])[0]["confidence"]=="verified"


def test_bizrule_automatically_activates_then_retires(memory_repo):
    mid=rule(memory_repo)
    for n in range(3):
        result=validation(memory_repo,mid,"test-"+str(n))
        assert not result.get("error"),result
    meta,_,path=store.load(memory_repo,mid)
    assert meta["confidence"]=="verified" and path.parent.name=="bizrules"
    assert not store.mem_path(memory_repo,"bizrule",mid,staging=True).exists()
    replay=validation(memory_repo,mid,"test-2")
    assert replay["idempotent"]
    result=validation(memory_repo,mid,"negative",outcome="incorrect",task_result="failed")
    assert not result.get("error"),result
    assert store.load(memory_repo,mid)[0]["status"]=="stale"
    conn=index.connect(memory_repo)
    assert conn.execute("SELECT staging,status FROM meta WHERE id=?",(mid,)).fetchone()==(0,"stale")
    conn.close()


def test_repeated_task_cannot_promote_candidate(memory_repo):
    mid=rule(memory_repo)
    for n in range(4):
        result=validation(memory_repo,mid,"same-task",outcome_id="result-"+str(n))
        assert not result.get("error"),result
    assert store.load(memory_repo,mid)[2].parent.name=="staging"


def test_plain_read_and_failed_validation_do_not_promote(memory_repo):
    mid=rule(memory_repo)
    result=validation(memory_repo,mid,"no-result",task_result="")
    assert result["error"]=="VALIDATION_RESULT_REQUIRED"
    result=validation(memory_repo,mid,"failed",task_result="failed")
    assert result["error"]=="VALIDATION_NOT_COMPLETED"
    assert store.load(memory_repo,mid)[0]["helpful_count"]==0


def test_activation_move_recovers_after_atomic_move_failure(memory_repo,monkeypatch):
    from pathlib import Path
    mid=rule(memory_repo)
    validation(memory_repo,mid,"one");validation(memory_repo,mid,"two")
    original=Path.replace
    def fail(self,target):
        if self.parent.name=="staging":
            raise OSError("simulated move crash")
        return original(self,target)
    with monkeypatch.context() as m:
        m.setattr(Path,"replace",fail)
        with pytest.raises(OSError,match="simulated"):
            validation(memory_repo,mid,"three")
    result=validation(memory_repo,mid,"three")
    assert not result.get("error"),result
    assert store.load(memory_repo,mid)[2].parent.name=="bizrules"
    assert store.load(memory_repo,mid)[0]["helpful_count"]==3


def test_automatic_activation_can_be_disabled(memory_repo):
    mid=rule(memory_repo)
    cfg=json.loads(config.CONFIG_PATH.read_text());cfg["evolution"]={"auto_activate_bizrules":False}
    config.CONFIG_PATH.write_text(json.dumps(cfg))
    result=validation(memory_repo,mid,"blocked")
    assert result["error"]=="VALIDATION_RESULT_REQUIRED"
    assert store.load(memory_repo,mid)[2].parent.name=="staging"


def test_two_independent_negative_tasks_retire_candidate(memory_repo):
    mid=rule(memory_repo)
    for task in ['bad-one','bad-two']:
        result=validation(memory_repo,mid,task,outcome='not_helpful',task_result='failed')
        assert not result.get('error'),result
    assert store.load(memory_repo,mid)[0]['status']=='stale'


def test_negative_evidence_blocks_positive_promotion(memory_repo):
    mid=rule(memory_repo)
    validation(memory_repo,mid,'negative',outcome='not_helpful',task_result='failed')
    for n in range(3):
        result=validation(memory_repo,mid,'positive-'+str(n))
        assert not result.get('error'),result
    assert store.load(memory_repo,mid)[2].parent.name=='staging'


def test_disabled_evolution_does_not_offer_automatic_validation(memory_repo):
    rule(memory_repo)
    cfg=json.loads(config.CONFIG_PATH.read_text());cfg['evolution']={'enabled':False}
    config.CONFIG_PATH.write_text(json.dumps(cfg))
    result=json.loads(context_impl(session_id='s-disabled',event_id='lookup',scope='orders',
                                  task_id='disabled',current_goal='订单验证',host_profile='fixture'))
    assert 'validation_candidates' not in result
