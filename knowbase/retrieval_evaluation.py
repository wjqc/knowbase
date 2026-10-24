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
        if (label.get('status') not in {'reviewed','adjudicated'} or not label.get('labeler') or
                type(label.get('unanswerable')) is not bool or positive==label['unanswerable'] or
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
    deltas=[]
    for row,a,b in zip(rows,left,right):
        relevant=set(row['labels']['relevant_ids'])
        if relevant:deltas.append((len(set(a[:5])&relevant)-len(set(b[:5])&relevant))/len(relevant))
    if not deltas:return None
    rng=random.Random(20261023)
    boot=sorted(sum(rng.choice(deltas) for _ in deltas)/len(deltas) for _ in range(1000))
    return {'metric':'Recall@5','delta':sum(deltas)/len(deltas),'CI95':[boot[24],boot[974]],'positive_n':len(deltas),'method':'paired_query_bootstrap; task-cluster bootstrap still required for formal acceptance'}

def run(repo,source,cache_path,baseline_source, *, dense_min,gate_dense_min):
    rows=dataset(source);bad=validate_labels(rows)
    if bad:return {'state':'NOT_RUN','reason':'human_qrels_incomplete','pending':len(bad)}
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
