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


def report(repo):
    from collections import Counter
    result={'schema':'knowbase.retrieval-report.v1','levels':{},'degradation_reasons':{},
            'executed_queries':0,'remote_network_calls':0,'injected_tokens_estimated':0,
            'token_estimator':'chars-div2-v1','currency_cost_actual':None,'net_token_savings':None,
            'effective_adoption_rate':None,'note':'命中、递送、采纳及完成后helpful分别统计；无对照不报告净节省。'}
    path=index.db_path(repo)
    if not path.exists():return {**result,'error':'database_missing'}
    conn=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True);conn.execute('PRAGMA query_only=ON')
    try:
        levels=Counter();reasons=Counter()
        tables={r[0] for r in conn.execute('SELECT name FROM sqlite_master')}
        tier_triples=[]
        if 'retrieval_observations' in tables:
            for origin,level,reason,raw in conn.execute('SELECT origin,level,reason,detail_json FROM retrieval_observations'):
                d=json.loads(raw)
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
                if (d.get('query_class') or '').startswith('skipped:'):continue
                levels[level]+=1
                if reason:reasons[reason]+=1
                result['remote_network_calls']+=d.get('remote_calls',0)
            result.update(levels=dict(levels),degradation_reasons=dict(reasons),executed_queries=sum(levels.values()))
        if tier_triples and 'knowledge_uses' in tables:
            delivered={};adopted={}
            for search_id,tier,ids in tier_triples:
                delivered[tier]=delivered.get(tier,0)+len(ids)
                if not search_id:continue
                q='SELECT COUNT(DISTINCT memory_id) FROM knowledge_uses WHERE event_id=? AND current_version=1 AND state=? AND memory_id IN (%s)'%','.join('?'*len(ids))
                adopted[tier]=adopted.get(tier,0)+conn.execute(q,(search_id,'adopted',*ids)).fetchone()[0]
            result['injection_tiers']=delivered
            result['tier_adoption']={t:{'delivered':delivered[t],'adopted':adopted.get(t,0),
                'rate':adopted.get(t,0)/delivered[t] if delivered[t] else None} for t in delivered}
        if 'knowledge_uses' in tables:
            result['use_receipts']=dict(conn.execute('SELECT state,COUNT(*) FROM knowledge_uses GROUP BY state'))
        if 'task_outcomes' in tables:
            result['task_results']=dict(conn.execute('SELECT result,COUNT(*) FROM task_outcomes GROUP BY result'))
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
        return result
    finally:conn.close()
