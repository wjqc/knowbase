"""Local, privacy-minimal retrieval observations and read-only reporting."""
import json
import sqlite3
import uuid
from datetime import datetime,timezone
from pathlib import Path

from . import config,index


def observe(conn,event_id,origin,detail):
    if not config.load_config().get('stats',{}).get('enabled',True):return
    caller_transaction=conn.in_transaction
    savepoint="retrieval_observation_"+uuid.uuid4().hex
    try:
        if caller_transaction:conn.execute("SAVEPOINT "+savepoint)
        conn.execute('INSERT OR IGNORE INTO retrieval_observations VALUES(?,?,?,?,?,?)',
            (event_id,datetime.now(timezone.utc).isoformat(),origin,detail.get('level','unknown'),
             detail.get('reason') or '',json.dumps(detail,ensure_ascii=False)))
        if caller_transaction:conn.execute("RELEASE "+savepoint)
        else:conn.commit()
    except sqlite3.Error:
        if caller_transaction:
            conn.execute("ROLLBACK TO "+savepoint);conn.execute("RELEASE "+savepoint)
        else:conn.rollback()


def delivery(repo,parent_id,text,tiers=None,dropped_once=0):
    if not parent_id:return
    conn=None
    try:
        conn=sqlite3.connect(index.db_path(repo).resolve().as_uri()+'?mode=rw',uri=True,timeout=.05)
        import re
        detail={'search_id':parent_id,'level':'local',
            'delivered_ids':re.findall(r'\[([A-Z]-(?:[a-f0-9]{12}|\d{4}-\d{4}))\]',text),
            'injected_chars':len(text),'injected_tokens_estimated':(len(text)+1)//2,
            'estimator':'chars-div2-v1','currency_cost_actual':None}
        if tiers is not None:
            detail['injection_tiers']={k:list(v) for k,v in tiers.items()}
            detail['dropped_once']=dropped_once
        observe(conn,uuid.uuid4().hex,'hook_delivery',detail)
    except (sqlite3.Error,OSError):pass
    finally:
        if conn:conn.close()


def shadow_observe(conn, query_hash, diagnostics):
    """V2-03 影子观测：只记录"若启用混合会怎样/为何仍是 L2"的诊断，
    不产生 delivery、usage 或反馈，也不参与任何发布决策。"""
    detail = {'query_hash': query_hash, 'mode': 'shadow',
              'no_delivery': True, 'no_governance': True,
              **{k: v for k, v in (diagnostics or {}).items()
                 if k in ('level', 'reason', 'semantic_status', 'channels',
                          'vector_coverage', 'generation_id')}}
    observe(conn, uuid.uuid4().hex, 'shadow_eval', detail)


def report(repo):
    from collections import Counter
    result={'schema':'knowbase.retrieval-report.v1','levels':{},'degradation_reasons':{},
            'executed_queries':0,'remote_network_calls':0,'injected_tokens_estimated':0,
            'token_estimator':'chars-div2-v1','currency_cost_actual':None,'net_token_savings':None,
            'effective_adoption_rate':None,'shadow':{},'note':'命中、递送、采纳及完成后helpful分别统计；无对照不报告净节省。'}
    path=index.db_path(repo)
    if not path.exists():return {**result,'error':'database_missing'}
    conn=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True);conn.execute('PRAGMA query_only=ON')
    try:
        levels=Counter();reasons=Counter()
        tables={r[0] for r in conn.execute('SELECT name FROM sqlite_master')}
        tier_triples=[]
        if 'retrieval_observations' in tables:
            shadow_reasons={}
            hook_opportunities = {}
            for origin,level,reason,raw in conn.execute('SELECT origin,level,reason,detail_json FROM retrieval_observations'):
                d=json.loads(raw)
                if origin=='shadow_eval':
                    # V2-03：影子观测单列，不进 executed_queries/levels，不产生递送。
                    shadow_reasons[reason or d.get('reason') or (d.get('semantic_status') or 'observed')]=shadow_reasons.get(reason or d.get('reason') or (d.get('semantic_status') or 'observed'),0)+1
                    continue
                if origin=='hook_delivery':
                    result['injected_tokens_estimated']+=d.get('injected_tokens_estimated',0)
                    for tier,ids in (d.get('injection_tiers') or {}).items():
                        if isinstance(ids,list) and ids:
                            tier_triples.append((d.get('search_id'),tier,ids))
                    if d.get('dropped_once'):
                        result['dropped_once_injections']=result.get('dropped_once_injections',0)+d['dropped_once']
                    continue
                if origin=='embedding_worker':
                    result['remote_network_calls']+=d.get('remote_calls',0);continue
                if origin == 'hook_prompt':
                    host = d.get('host_profile') or 'legacy'
                    build = d.get('runtime_build') or 'legacy'
                    cohort = hook_opportunities.setdefault(host, {}).setdefault(build, Counter())
                    skipped = d.get('query_class') or ''
                    if skipped.startswith('skipped:'):
                        cohort[skipped] += 1
                    else:
                        cohort['eligible'] += 1
                        cohort['with_delivery'] += bool(d.get('delivered_count'))
                        cohort['scope:' + (d.get('scope_source') or d.get('scope_reason') or 'legacy')] += 1
                if (d.get('query_class') or '').startswith('skipped:'):continue
                levels[level]+=1
                if reason:reasons[reason]+=1
                result['remote_network_calls']+=d.get('remote_calls',0)
            result.update(levels=dict(levels),degradation_reasons=dict(reasons),executed_queries=sum(levels.values()))
            result['hook_opportunities'] = {host: {build: dict(counts) for build, counts in builds.items()}
                                            for host, builds in hook_opportunities.items()}
            if shadow_reasons:
                result['shadow']={'observations':sum(shadow_reasons.values()),'reasons':dict(shadow_reasons),
                                  'delivered':0,'note':'shadow observations never inject or govern'}
        if tier_triples and 'knowledge_uses' in tables:
            delivered={};adopted={}
            for search_id,tier,ids in tier_triples:
                delivered[tier]=delivered.get(tier,0)+len(ids)
                if not search_id:continue
                if {'use_retrieval_links','retrieval_deliveries'} <= tables:
                    q='SELECT COUNT(DISTINCT u.memory_id) FROM knowledge_uses u JOIN use_retrieval_links l ON l.use_id=u.use_id WHERE l.search_id=? AND u.current_version=1 AND u.state=? AND u.memory_id IN (%s)'%','.join('?'*len(ids))
                    adopted[tier]=adopted.get(tier,0)+conn.execute(q,(search_id,'adopted',*ids)).fetchone()[0]
            result['injection_tiers']=delivered
            result['tier_adoption']={t:{'delivered':delivered[t],'adopted':adopted.get(t),
                'rate':adopted[t]/delivered[t] if delivered[t] and t in adopted else None} for t in delivered}
        if 'knowledge_uses' in tables:
            result['use_receipts']=dict(conn.execute('SELECT state,COUNT(*) FROM knowledge_uses GROUP BY state'))
            # V2-01 观测口径：采用率分母按 task-run/card-revision/delivery 定义；
            # 无 delivery 关联的回执单列，不混入有效采用分母。
            if 'use_retrieval_links' in tables:
                linked=conn.execute('SELECT COUNT(DISTINCT use_id) FROM use_retrieval_links').fetchone()[0]
                total=conn.execute('SELECT COUNT(*) FROM knowledge_uses').fetchone()[0]
                result['use_receipts_delivery_linked']=linked
                result['use_receipts_unlinked']=max(0,total-linked)
                if 'retrieval_deliveries' in tables:
                    rows = conn.execute("SELECT d.host_profile,count(DISTINCT d.delivery_id),"
                        "count(DISTINCT CASE WHEN u.state='read' THEN d.delivery_id END) "
                        "FROM retrieval_deliveries d LEFT JOIN use_retrieval_links l ON d.delivery_id=l.delivery_id "
                        "LEFT JOIN knowledge_uses u ON u.use_id=l.use_id GROUP BY d.host_profile")
                    result['read_attribution'] = {host: {'delivered': total, 'with_linked_read': reads}
                                                  for host, total, reads in rows}
        if 'task_outcomes' in tables:
            result['task_results']=dict(conn.execute('SELECT result,COUNT(*) FROM task_outcomes GROUP BY result'))
        if {'evidence_events', 'task_runs'} <= tables:
            rows = conn.execute(
                "WITH adopted AS (SELECT DISTINCT run_id,subject_revision FROM evidence_events "
                "WHERE event_type='knowledge.adopted' AND level='L2' AND import_state='local' "
                "AND trust_basis IN ('bound_action','exact_action_match','explicit_adoption')) "
                "SELECT r.host_profile,count(*),sum(EXISTS(SELECT 1 FROM evidence_events v "
                "WHERE v.run_id=a.run_id AND v.subject_revision=a.subject_revision "
                "AND v.event_type='validation.completed' AND v.level='L3' "
                "AND v.trust_basis='registered-adapter' AND v.import_state='local' "
                "AND NOT EXISTS(SELECT 1 FROM evidence_events x WHERE x.revokes_event_id=v.event_id))) "
                "FROM adopted a JOIN task_runs r ON r.run_id=a.run_id GROUP BY r.host_profile")
            result['validation_conversion'] = {host: {'adopted_run_cards': total, 'with_trusted_verification': validated}
                                               for host, total, validated in rows}
        if {'knowledge_uses','task_outcomes','feedback_events'} <= tables:
            denominator=conn.execute("SELECT count(DISTINCT o.task_outcome_id) FROM task_outcomes o JOIN knowledge_uses u ON o.use_id=u.use_id "
                "WHERE u.current_version=1 AND u.state IN ('adopted','referenced') AND o.result IN ('completed','failed','abandoned')").fetchone()[0]
            numerator=conn.execute("SELECT count(DISTINCT o.task_outcome_id) FROM task_outcomes o JOIN knowledge_uses u ON o.use_id=u.use_id "
                "JOIN feedback_events f ON f.use_id=u.use_id AND f.task_outcome_id=o.task_outcome_id AND f.memory_id=u.memory_id AND f.content_revision=u.content_revision "
                "WHERE u.current_version=1 AND u.state='adopted' AND o.result='completed' AND f.outcome='helpful' AND f.binding='version_task' AND f.state='applied'").fetchone()[0]
            result.update(observed_used_task_outcomes=denominator,completed_helpful_outcomes=numerator,
                          effective_adoption_rate=numerator/denominator if denominator else None)
            result['verification_sources']=dict(conn.execute('SELECT verification_source,count(*) FROM task_outcomes GROUP BY verification_source'))
        result['adoption_measurement']='Version-bound adopted+completed+helpful outcomes / observed used task outcomes; missing outcomes excluded and not treated as success.'
        if 'executor_runs' in tables:
            result['background_execution']=dict(conn.execute('SELECT state,count(*) FROM executor_runs GROUP BY state'))
            result['executor_invocations_reserved']=conn.execute('SELECT coalesce(sum(calls),0) FROM automation_budget').fetchone()[0]
            result['executor_invocations_started']=conn.execute('SELECT count(*) FROM executor_runs WHERE invoked=1').fetchone()[0]
            result['cached_executor_runs']=conn.execute("SELECT count(*) FROM executor_runs WHERE usage_source='cache'").fetchone()[0]
            result['background_usage_sources']=dict(conn.execute('SELECT usage_source,count(*) FROM executor_runs GROUP BY usage_source'))
            result['background_cost_actual']=None
            result['background_usage_note']='Reserved/started controller invocations include failures, not provider invoices. Cache replays are not new calls; executor_report is not independently billed usage.'
        return result
    finally:conn.close()
