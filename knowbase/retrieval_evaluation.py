"""Reproducible A-E replay. Offline run never calls a provider or writes knowledge.

Human labels and cache identity are prerequisites, not inferred from retrieval output.
"""
import json
import math
import os
import random
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import index,query_hygiene,semantic_index as si
from .embedding_provider import ProviderError,cosine,digest,normalize
from .retrieval_policy import automatic_match


def dataset(path):
    rows=[json.loads(v) for v in Path(path).read_text().splitlines() if v.strip()]
    if not 1<=len(rows)<=10000 or len({r['id'] for r in rows})!=len(rows):raise ValueError('invalid_dataset')
    return rows


def validate_labels(rows):
    problems=[]
    for r in rows:
        label=r.get('labels',{})
        positive=bool(label.get('relevant_ids'))
        # Execution-derived samples with no relevant ids are not
        # unanswerable by default: explicit 'irrelevant' judgments,
        # per-card 'negative' failures and 'unknown' (no adoption) all
        # legitimately carry relevant_ids=[] with unanswerable=False.
        # Every other row must keep the two fields inconsistent (a
        # relevant answer exists iff the query is answerable). Human
        # rows keep the old rule (no sample_class).
        if (label.get('status') not in {'reviewed','adjudicated','execution_derived'} or not label.get('labeler') or
                type(label.get('unanswerable')) is not bool or
                (positive==label['unanswerable'] and label.get('sample_class') not in {'irrelevant','negative','unknown'}) or
                not label.get('corpus_revision')):
            problems.append(r['id'])
    return problems


def export_cache(repo,source,output, *, provider=None):
    rows=dataset(source);p=provider or __import__('knowbase.retrieval_state',fromlist=['provider']).provider()
    if any(r.get('privacy_review')!='approved' for r in rows):raise ValueError('privacy_review_pending')
    conn=sqlite3.connect(index.db_path(repo).resolve().as_uri()+'?mode=ro',uri=True);conn.execute('PRAGMA query_only=ON')
    try:
        g=si.generation(conn)
        if not g:
            row=conn.execute("SELECT generation_id FROM embedding_generations WHERE state='ready' ORDER BY validated_at DESC LIMIT 1").fetchone()
            g=si.generation(conn,row[0]) if row else None
        if not g or g['identity']['provider_fp']!=p.fingerprint:raise ValueError('index_not_ready')
        p.probe(g['identity'])
        items=si.manifest(conn)
        vectors={item['id']:si.vector(conn,g['id'],item,g['identity']['dimension']) for item in items}
        if not vectors or any(v is None for v in vectors.values()):raise ValueError('index_incomplete')
        queries={};unavailable={}
        for row in rows:
            if row.get('scope','global') not in si.settings().get('allowed_scopes',[]):raise ValueError('scope_not_allowed')
            prepared=query_hygiene.prepare(row['query'])
            for q in dict.fromkeys([row['query']]+([prepared['query']] if prepared['action']=='search' else [])):
                key=digest(p.query_prefix+q)
                if key in queries:continue
                try:
                    values,identity=p.embed([q],query=True)
                    if identity['space_id']!=g['identity']['space_id']:raise ProviderError('model_mismatch')
                    queries[key]=values[0]
                except ProviderError as exc:unavailable[key]=exc.category
        p.probe(g['identity'])
        cache={'schema':'knowbase.embedding-cache.v1','identity':g['identity'],'corpus_digest':digest(items),
               'dataset_digest':digest(rows),'allowed_scopes':si.settings().get('allowed_scopes',[]),'query_prefix':p.query_prefix,'documents':vectors,'queries':queries,
               'unavailable':unavailable,'source_verified_at_export':True,'created_at':si.now()}
        cache['checksum']=digest(cache)
        out=Path(output);out.parent.mkdir(parents=True,exist_ok=True)
        with out.open('x') as f:
            os.chmod(out,0o600);json.dump(cache,f,ensure_ascii=False)
        return {'state':'cached','documents':len(vectors),'queries':len(queries),'unavailable':len(unavailable)}
    finally:conn.close()


def _rank(conn,scores,metadata,scope):
    ranked=[]
    for mid,value in scores.items():
        m=metadata[mid]
        final=value*index._scope_factor(m,scope)*(1.5 if m['confidence']=='verified' else 1)*index._freshness_factor(m)*index._feedback_factor(m)
        ranked.append((mid,final))
    return [mid for mid,_ in sorted(ranked,key=lambda v:(-v[1],v[0]))[:10]]


def _lexical(conn,query):
    terms=query.split()[:16];scores=index._match_ids(conn,terms) or {}
    for mid,score in index._like_ids(conn,[t for t in terms if len(t)<3]).items():scores[mid]=max(scores.get(mid,0),.7*score)
    if not scores:scores=index._like_ids(conn,terms)
    return index._expanded_lexical_ids(conn,query,scores)


def _metrics(rows,rankings):
    recall=[];precision=[];mrr=[];reject=[];noise=[]
    for row,hits in zip(rows,rankings):
        relevant=set(row['labels']['relevant_ids']);negative=row['labels']['unanswerable']
        precision.append(sum(h in relevant for h in hits[:3])/3)
        noise.append(bool(hits) and any(h not in relevant for h in hits[:3]))
        if relevant:
            recall.append(len(set(hits[:5])&relevant)/len(relevant))
            mrr.append(next((1/(i+1) for i,h in enumerate(hits[:10]) if h in relevant),0))
        if negative:reject.append(not hits)
    avg=lambda v:sum(v)/len(v) if v else None
    return {'Recall@5':avg(recall),'Precision@3':avg(precision),'MRR@10':avg(mrr),'correct_rejection':avg(reject),
            'irrelevant_delivery_query_rate':avg(noise),'positive_queries':len(recall),'negative_queries':len(reject),'queries':len(rows)}



def paired_interval(rows, left, right):
    """V2-03：按任务 cluster 重抽样。行携带 cluster/task_id 时按簇重采样，
    避免同任务多条改写把样本量与置信度虚增；无簇键回退逐 query bootstrap
    并在 method 中声明（正式验收仍要求簇键）。"""
    clusters={}
    deltas=[]
    for row,a,b in zip(rows,left,right):
        relevant=set(row['labels']['relevant_ids'])
        if not relevant:continue
        delta=(len(set(a[:5])&relevant)-len(set(b[:5])&relevant))/len(relevant)
        deltas.append(delta)
        # 只有显式簇键（cluster/task_id）才分簇；缺失全部并入同一桶，
        # 簇数<2 时回退 query bootstrap 并在 method 声明，不伪装成独立簇。
        clusters.setdefault(row.get('cluster') or row.get('task_id') or '',[]).append(delta)
    if not deltas:return None
    rng=random.Random(20261023)
    keys=list(clusters)
    if len(keys)>=2:
        boot=[]
        for _ in range(1000):
            picked=[clusters[rng.choice(keys)] for _ in keys]
            flat=[d for group in picked for d in group]
            boot.append(sum(flat)/len(flat))
        boot.sort()
        method=f'task_cluster_bootstrap(clusters={len(keys)}); queries_in_clusters={len(deltas)}'
    else:
        boot=sorted(sum(rng.choice(deltas) for _ in deltas)/len(deltas) for _ in range(1000))
        method='paired_query_bootstrap(no_cluster_keys); task-cluster bootstrap required for formal acceptance'
    return {'metric':'Recall@5','delta':sum(deltas)/len(deltas),'CI95':[boot[24],boot[974]],
            'positive_n':len(deltas),'clusters':len(keys) if len(keys)>=2 else None,'method':method}

# V2-03 激活预算（毫秒）：真实混合路径端到端 p95 上限。离线词法计算延迟
# 不能替代网络/宿主耗时；缺真实测量则 latency 门为 False，保持 L2/影子。
END_TO_END_P95_BUDGET_MS = 1000
MIN_EVALUABLE_SAMPLES = 60
MIN_POSITIVE_SAMPLES = 20
MIN_NEGATIVE_SAMPLES = 20


def freeze_report(comparison, *, dataset_sha256, vectors_sha256, provider_fp, space_id,
                  covered_scopes, deployment_id, dense_min, derived_stats,
                  end_to_end=None, heldout_digest="", task_clusters=None):
    """Freeze a completed comparison into a knowbase.retrieval-evaluation.v2 report.

    V2-03 门（全部机械判定，无人工签核）：
    - quality: hybrid (D) Recall@5 >= lexical (B) Recall@5 AND >= 绝对下限
    - rejection: 真负例存在且 D correct_rejection >= B
    - regression: 配对 D-B Recall@5 CI95 下界 >= 0（簇键时按任务簇重抽样）
    - latency: 真实混合路径 end_to_end_p95_ms <= 预算；只交 p95_lexical_compute_ms
      （离线计算）不能满足此门——end_to_end 为空即 FAIL，保持词法/影子
    - samples: 可判定样本（positive+negative+irrelevant，unknown 不计）
      >= 60 且 positive >= 20、negative >= 20；unknown 单列另报
    verdict is PASS iff all five gates hold. v1 报告仍可读，但不能通过
    semantic_index.quality() 的激活校验。
    """
    metrics = comparison.get('metrics', {}) or {}
    b, d = metrics.get('B', {}) or {}, metrics.get('D', {}) or {}
    paired = (comparison.get('paired_intervals') or {}).get('D-B') or {}
    ci = paired.get('CI95')
    positive = int(derived_stats.get('positive', 0))
    negative = int(derived_stats.get('negative', 0))
    irrelevant = int(derived_stats.get('irrelevant', 0))
    unknown = int(derived_stats.get('unknown', 0))
    evaluable = positive + negative + irrelevant
    e2e_p95 = (end_to_end or {}).get('p95_ms')
    e2e_basis = (end_to_end or {}).get('basis', '')
    gates = {
        'quality': (d.get('Recall@5') is not None and b.get('Recall@5') is not None
                    and d['Recall@5'] >= b['Recall@5']
                    and d['Recall@5'] >= si.ABSOLUTE_MIN_RECALL_AT_5),
        'rejection': ((d.get('negative_queries') or 0) > 0
                      and d.get('correct_rejection') is not None
                      and b.get('correct_rejection') is not None
                      and d['correct_rejection'] >= b['correct_rejection']),
        'regression': (isinstance(ci, list) and len(ci) == 2 and ci[0] >= 0),
        'latency': (e2e_p95 is not None and e2e_basis == 'end_to_end_hybrid'
                    and e2e_p95 <= END_TO_END_P95_BUDGET_MS),
        'samples': (evaluable >= MIN_EVALUABLE_SAMPLES
                    and positive >= MIN_POSITIVE_SAMPLES
                    and negative >= MIN_NEGATIVE_SAMPLES),
    }
    return {'schema': 'knowbase.retrieval-evaluation.v2',
            'purpose': 'task',
            'verdict': 'PASS' if all(gates.values()) else 'FAIL',
            'annotation_status': 'execution_derived',
            'label_provenance': 'execution_derived',
            'evaluable_sample_count': evaluable,
            'evaluable_positive': positive,
            'evaluable_negative': negative,
            'evaluable_irrelevant': irrelevant,
            'derived_unknown': unknown,
            'unknown_note': 'unknown 不计入任何最低样本数，也不作负例；另报覆盖率。',
            'excluded_for_privacy': derived_stats.get('excluded_for_privacy', 0),
            'task_clusters': task_clusters if task_clusters is not None else paired.get('clusters'),
            'heldout_digest': heldout_digest,
            'confidence_interval': {'metric': 'Recall@5', 'delta': paired.get('delta'),
                                    'CI95': ci, 'positive_n': paired.get('positive_n'),
                                    'method': paired.get('method')},
            'hybrid_recall_at_5': d.get('Recall@5'),
            'end_to_end_p95_ms': e2e_p95,
            'latency_basis': e2e_basis or 'offline_compute_only',
            'p95_lexical_compute_ms': comparison.get('p95_lexical_compute_ms'),
            'dataset_sha256': dataset_sha256, 'vectors_sha256': vectors_sha256,
            'retrieval_code': si.code_profile(), 'covered_scopes': covered_scopes,
            'provider_fp': provider_fp, 'space_id': space_id,
            'deployment_id': deployment_id, 'dense_min': dense_min, 'gates': gates}


def measure_end_to_end_p95(repo, queries, *, scope=None, budget_ms=END_TO_END_P95_BUDGET_MS,
                           hybrid_probe=False):
    """真实端到端延迟测量（V2-03 latency 门的取证来源）。

    默认走正式 index.search(origin='mcp_search')（含查询 hygiene、词法、
    激活时的真实远端向量与融合）。hybrid_probe=True 时用"词法 + 真实远端
    query embed + 余弦打分"逐查询实测（不需要 dense 已激活），供冻结报告
    在激活前取得混合路径真实网络耗时。两种模式都只读：不写 delivery、
    usage 或反馈。basis 只在真实远端参与时为 end_to_end_hybrid；否则如实
    标 lexical_only/provider_unavailable，不能满足 latency 门。
    """
    conn = sqlite3.connect(index.db_path(repo).resolve().as_uri() + '?mode=ro', uri=True)
    conn.execute('PRAGMA query_only=ON')
    try:
        times = []
        levels = set()
        vectors = {}
        dimension = None
        if hybrid_probe:
            from . import retrieval_state
            options = si.settings()
            p = retrieval_state.provider()
            row = conn.execute(
                "SELECT generation_id FROM embedding_generations WHERE provider_fp=? "
                "AND state IN ('ready','active') ORDER BY validated_at DESC LIMIT 1",
                (p.fingerprint,)).fetchone()
            g = si.generation(conn, row[0]) if row else None
            if g:
                items = si.manifest(conn, options)
                dimension = g['identity']['dimension']
                vectors = {item['id']: si.vector(conn, g['id'], item, dimension)
                           for item in items}
                p.probe(g['identity'])
        for q in queries[:200]:
            diagnostics = {}
            started = time.monotonic()
            if hybrid_probe and g and vectors and None not in vectors.values():
                from .embedding_provider import normalize
                prepared = query_hygiene.prepare(q)
                text = q if not prepared.get('query') else prepared['query']
                values, _observed = p.embed([text], query=True)
                qv = normalize(values[0], dimension)
                for item_id, value in vectors.items():
                    cosine(qv, value)
            else:
                index.search(conn, q, scope=scope, limit=5, origin='mcp_search',
                             diagnostics=diagnostics)
                levels.add(diagnostics.get('level'))
            times.append((time.monotonic() - started) * 1000)
        if not times:
            return {'p95_ms': None, 'basis': 'no_queries', 'n': 0}
        times.sort()
        p95 = times[math.ceil(.95 * len(times)) - 1]
        if hybrid_probe:
            basis = 'end_to_end_hybrid' if vectors else 'provider_unavailable'
        else:
            basis = 'end_to_end_hybrid' if levels & {'L3'} else 'lexical_only'
        return {'p95_ms': round(p95, 2), 'basis': basis, 'n': len(times),
                'budget_ms': budget_ms, 'mode': 'hybrid_probe' if hybrid_probe else 'production_search',
                'note': 'real network included; read-only, no delivery/usage writes'}
    finally:
        conn.close()


def run(repo,source,cache_path,baseline_source, *, dense_min,gate_dense_min):
    rows=dataset(source);bad=validate_labels(rows)
    if bad:return {'state':'NOT_RUN','reason':'qrels_incomplete','pending':len(bad)}
    cache=json.loads(Path(cache_path).read_text());checksum=cache.pop('checksum',None)
    if (cache.get('schema')!='knowbase.embedding-cache.v1' or checksum!=digest(cache) or cache.get('dataset_digest')!=digest(rows)):
        return {'state':'NOT_RUN','reason':'cache_identity_or_checksum_invalid'}
    conn=sqlite3.connect(index.db_path(repo).resolve().as_uri()+'?mode=ro',uri=True);conn.execute('PRAGMA query_only=ON')
    try:
        if digest(si.manifest(conn,{'allowed_scopes':cache.get('allowed_scopes',[])},verify_sources=False))!=cache['corpus_digest']:return {'state':'NOT_RUN','reason':'corpus_changed'}
        metadata={}
        for raw in conn.execute("SELECT * FROM meta WHERE status='active' AND staging=0 AND type!='reference'"):
            m=index._row_meta(raw);metadata[m['id']]=m
        # A runs the frozen old source in its own process, avoiding import alias collisions.
        script="""import json,sqlite3,sys
from knowbase import index
rows=json.load(sys.stdin);c=sqlite3.connect(sys.argv[1]+'?mode=ro',uri=True);c.execute('PRAGMA query_only=ON')
print(json.dumps([[h['id'] for h in index.search(c,r['query'],scope=r.get('scope') or 'global',limit=10)] for r in rows]))
"""
        with tempfile.TemporaryDirectory(prefix='knowbase-eval-') as temp:
            cp=Path(temp)/'cfg.json';cp.write_text(json.dumps({'stats':{'enabled':False},'git':{'auto_pull':False,'auto_push':False},'embedding':{'enabled':False}}))
            env={**os.environ,'KNOWBASE_CONFIG':str(cp),'KNOWBASE_REPO_PATH':temp,'PYTHONDONTWRITEBYTECODE':'1'}
            raw=subprocess.run([sys.executable,'-B','-c',script,index.db_path(repo).resolve().as_uri()],cwd=baseline_source,
                env=env,input=json.dumps(rows),text=True,capture_output=True,timeout=120)
            if raw.returncode:return {'state':'NOT_RUN','reason':'baseline_failed'}
            rankings={'A':json.loads(raw.stdout),'B':[],'C':[],'D':[],'E':[]}
        times={k:[] for k in 'BCDE'};missing=0
        for row in rows:
            scope=row.get('scope') or 'global';allowed={mid:m for mid,m in metadata.items() if m['scope'] in {scope,'global'}}
            start=time.monotonic();prepared=query_hygiene.prepare(row['query'])
            q=prepared.get('query','');lex={mid:v for mid,v in _lexical(conn,q).items() if mid in allowed} if q else {}
            rankings['B'].append(_rank(conn,index._rrf_scores(lex),allowed,scope));times['B'].append((time.monotonic()-start)*1000)
            dense_by_query=[]
            for text in [row['query'],q]:
                if not text:dense_by_query.append({});continue
                key=digest(cache['query_prefix']+text);v=cache['queries'].get(key)
                if v is None:missing+=1;dense_by_query.append({});continue
                v=normalize(v,cache['identity']['dimension'])
                scores={mid:cosine(v,normalize(vec,cache['identity']['dimension'])) for mid,vec in cache['documents'].items() if mid in allowed}
                dense_by_query.append({mid:s for mid,s in scores.items() if s>=dense_min})
            raw_dense,hy_dense=dense_by_query
            rankings['C'].append(_rank(conn,index._rrf_scores(raw_dense),allowed,scope))
            rankings['D'].append(_rank(conn,index._rrf_scores(lex,hy_dense),allowed,scope))
            gated_lex={mid:s for mid,s in lex.items() if automatic_match(conn,q,mid)}
            gated_dense={mid:s for mid,s in hy_dense.items() if s>=gate_dense_min}
            rankings['E'].append(_rank(conn,index._rrf_scores(gated_lex,gated_dense),allowed,scope))
        if missing:return {'state':'NOT_RUN','reason':'model_inputs_missing','missing':missing,'rankings':rankings}
        return {'schema':'knowbase.retrieval-comparison.v1','state':'COMPLETED_OFFLINE','metrics':{k:_metrics(rows,v) for k,v in rankings.items()},
            'rankings':rankings,'mode':'active_search_proxy_not_real_hook_delivery','dataset_digest':digest(rows),
            'cache_checksum':checksum,'dense_min':dense_min,'gate_dense_min':gate_dense_min,
            'paired_intervals':{'D-B':paired_interval(rows,rankings['D'],rankings['B']),'E-D':paired_interval(rows,rankings['E'],rankings['D'])},
            'by_split':{split:{arm:_metrics([r for r in rows if r.get('split','unassigned')==split],
                [v for r,v in zip(rows,rankings[arm]) if r.get('split','unassigned')==split]) for arm in 'ABCDE'} for split in {r.get('split','unassigned') for r in rows}},
            'p95_lexical_compute_ms':sorted(times['B'])[math.ceil(.95*len(rows))-1],
            'latency_scope':'offline_compute_only; network and host latency NOT_RUN','token_cost_actual':None,
            'release_verdict':'NOT_RUN: requires frozen split, confidence intervals, real latency and regression gates'}
    finally:conn.close()
