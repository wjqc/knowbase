"""Generation-indexed real vectors in the existing SQLite embedding_index.

Namespaced primary keys preserve the legacy schema/readers. Old readers cannot match
these keys to valid card IDs; old rebuilds may remove them, forcing safe local fallback.
"""
import json
import math
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import config, index, jobs
from .embedding_provider import OpenAICompatProvider, ProviderError, digest, normalize


def now():return datetime.now(timezone.utc).isoformat(timespec="seconds")


def settings():
    value=config.load_config().get("embedding")
    return value if isinstance(value,dict) else {}


def manifest(conn, options=None, *, verify_sources=True):
    options=options or settings();scopes=options.get("allowed_scopes",[])
    if not isinstance(scopes,list):raise ProviderError("invalid_scope_policy")
    rows=[]
    for mid,scope,title,tags,body,rev in conn.execute(
        "SELECT m.id,m.scope,f.title,f.tags,f.body,h.content_revision FROM meta m "
        "JOIN mem_fts f ON f.id=m.id JOIN entity_heads h ON h.entity_id=m.id "
        "WHERE m.status='active' AND m.staging=0 AND m.type!='reference' ORDER BY m.id"):
        if scope not in scopes:continue
        if verify_sources and not index.source_current(conn,mid):raise ProviderError("source_index_stale")
        text=f"{title} {tags} {body}"
        rows.append({"id":mid,"scope":scope,"text":text,"hash":digest(text),"revision":rev})
    return rows


def code_profile():
    root=Path(__file__).parent
    return digest({name:(root/name).read_text() for name in ["index.py","query_hygiene.py","retrieval_policy.py"]})


def quality(options, identity):
    """Validate a frozen evaluation report, never a boolean 'approved' config flag.

    Since Phase 2 the only accepted provenance is execution-derived: the
    report must carry enough derived samples and a paired confidence
    interval whose lower bound shows no regression. Human annotation counts
    and named reviewers are no longer enabling conditions; a report that
    only carries them fails this gate. The hybrid arm must additionally
    clear an absolute Recall@5 floor, so a weak lexical baseline cannot
    make a poor hybrid look good by relative comparison alone.
    """
    p=options.get("quality_report")
    if not p:return None
    try:
        path=Path(p).expanduser()
        if path.is_symlink() or path.stat().st_size>1048576:return None
        report=json.loads(path.read_text())
        if not isinstance(report,dict) or not isinstance(report.get("gates"),dict):return None
        threshold=report.get("dense_min")
        ci=report.get("confidence_interval") or {}
        ci95=ci.get("CI95")
        if (report.get("schema")!="knowbase.retrieval-evaluation.v1" or report.get("verdict")!="PASS" or
                report.get("annotation_status")!="execution_derived" or
                report.get("derived_sample_count",0)<DERIVED_MIN_SAMPLES or
                not (isinstance(ci95,list) and len(ci95)==2 and ci95[0]>=0) or
                type(report.get("hybrid_recall_at_5")) not in {int,float} or not math.isfinite(report["hybrid_recall_at_5"]) or report["hybrid_recall_at_5"]<ABSOLUTE_MIN_RECALL_AT_5 or
                not re.fullmatch(r"[a-f0-9]{64}",str(report.get("dataset_sha256",""))) or
                not re.fullmatch(r"[a-f0-9]{64}",str(report.get("vectors_sha256",""))) or
                report.get("retrieval_code")!=code_profile() or
                not set(options.get("allowed_scopes",[]))<=set(report.get("covered_scopes",[])) or
                report.get("provider_fp")!=identity["provider_fp"] or report.get("space_id")!=identity["space_id"] or
                not options.get("deployment_id") or type(threshold) not in {int,float} or not math.isfinite(threshold) or not -1<=threshold<=1):
            return None
        if not all(report.get("gates",{}).get(k) is True for k in ["quality","rejection","regression","latency"]):return None
        return {"digest":digest(report),"dense_min":threshold}
    except (OSError,ValueError,TypeError,KeyError):return None


# Minimum execution-derived samples for a frozen report to enable dense
# retrieval. Mirrors the retired 60-query human floor, now counting derived
# samples (positive + negative + irrelevant) instead of hand labels.
DERIVED_MIN_SAMPLES=60


# Absolute floor for the hybrid arm's Recall@5. A report whose hybrid arm
# sits below this fails the quality gate even when it beats the lexical
# arm, so a weak lexical baseline cannot launder a poor hybrid through.
ABSOLUTE_MIN_RECALL_AT_5=0.5


def generation(conn, gid=None):
    row=conn.execute("SELECT generation_id,identity_json,manifest_hash,state,expected_count,completed_count,quality_digest "
        "FROM embedding_generations WHERE generation_id=?",(gid,)).fetchone() if gid else conn.execute(
        "SELECT g.generation_id,g.identity_json,g.manifest_hash,g.state,g.expected_count,g.completed_count,g.quality_digest "
        "FROM embedding_generations g JOIN retrieval_active a ON a.generation_id=g.generation_id WHERE a.singleton=1").fetchone()
    if not row:return None
    try:
        identity=json.loads(row[1])
        if (not isinstance(identity,dict) or not {"provider_fp","space_id","dimension","canary","tolerance"}<=set(identity) or
                type(identity["dimension"]) is not int or not 1<=identity["dimension"]<=32768 or
                not isinstance(identity["canary"],list) or len(identity["canary"])!=12 or
                type(identity["tolerance"]) not in {int,float} or not math.isfinite(identity["tolerance"]) or not 0<identity["tolerance"]<=.01):
            return None
    except (ValueError,TypeError):return None
    return {"id":row[0],"identity":identity,"manifest_hash":row[2],"state":row[3],
            "expected":row[4],"completed":row[5],"quality_digest":row[6]}



def vector(conn,gid,item,dimension):
    row=conn.execute("SELECT vector,vector_checksum,content_hash,content_revision,dimension,model_version "
                     "FROM embedding_index WHERE generation_id=? AND entity_id=?",(gid,item["id"])).fetchone()
    if not row or row[2]!=item["hash"] or row[3]!=item["revision"] or row[4]!=dimension:return None
    try:
        v=json.loads(row[0])
        if digest(v)!=row[1]:return None
        # Reject fake char/model labels even when the dimension happens to match.
        g=conn.execute("SELECT space_id FROM embedding_generations WHERE generation_id=?",(gid,)).fetchone()
        if not g or row[5]!=g[0]:return None
        return normalize(v,dimension)
    except (ValueError,ProviderError):return None


def coverage(conn,g,items):
    if not g:return {"eligible":len(items),"valid":0,"ratio":0 if items else None}
    count=sum(vector(conn,g["id"],item,g["identity"]["dimension"]) is not None for item in items)
    return {"eligible":len(items),"valid":count,"ratio":count/len(items) if items else None}


def activate(conn,gid, options=None):
    if conn.in_transaction:raise ProviderError("transaction_busy")
    conn.execute("BEGIN IMMEDIATE")
    try:
        g=generation(conn,gid);options=options or settings()
        if not g or g["state"] not in {"ready","active","retired"}:raise ProviderError("index_not_ready")
        if OpenAICompatProvider(settings()).fingerprint != g["identity"]["provider_fp"]:
            raise ProviderError("configuration_changed")
        evidence=quality(options,g["identity"])
        if not evidence:raise ProviderError("quality_unvalidated")
        current=manifest(conn,options)
        if digest(current)!=g["manifest_hash"] or not current:raise ProviderError("index_changed")
        if coverage(conn,g,current)["ratio"]!=1:raise ProviderError("index_incomplete")
        old=generation(conn)
        if old and old["id"]!=gid:conn.execute("UPDATE embedding_generations SET state='retired' WHERE generation_id=?",(old["id"],))
        conn.execute("UPDATE embedding_generations SET state='active',quality_digest=?,validated_at=? WHERE generation_id=?",(evidence["digest"],now(),gid))
        conn.execute("INSERT INTO retrieval_active VALUES(1,?,1) ON CONFLICT(singleton) DO UPDATE SET generation_id=excluded.generation_id,revision=revision+1",(gid,))
        conn.commit()
        return gid
    except Exception:conn.rollback();raise


def schedule(conn, *, force=False):
    options=settings()
    if not options.get("enabled") or (not force and not options.get("auto_index",True)) or conn.in_transaction:return None
    try:
        provider=OpenAICompatProvider(options)
        revision=digest(manifest(conn,options))
        # Existing jobs/leases carry work; no second index queue/runtime.
        result=jobs.enqueue(conn,jobs.KIND_EMBED,"global",{"provider_fp":provider.fingerprint,"corpus":revision},
                            idempotency_key="embed:"+provider.fingerprint+":"+revision,max_attempts=100)
        active=generation(conn)
        if result.get("state")=="succeeded" and active and coverage(conn,active,manifest(conn,options))["ratio"]!=1:
            conn.execute("UPDATE jobs SET state='queued',lease_owner='',lease_until='',next_run_at=? WHERE job_id=? AND kind=?",(now(),result["job_id"],jobs.KIND_EMBED));conn.commit()
        return result
    except (ProviderError,ValueError,TypeError,sqlite3.Error):return None


def _chunks(text,options):
    size=max(128,min(8000,int(options.get("chunk_chars",6000))))
    chunks=[text[i:i+size] for i in range(0,len(text),size)]
    if len(chunks)>max(1,min(64,int(options.get("max_chunks",8)))):raise ProviderError("document_too_large")
    return chunks  # join(chunks)==text: no truncation.


def build(repo, *, provider=None, deadline=None):
    """Lease a queued embed job; network outside all DB/repo transactions."""
    options=settings()
    try:provider=provider or OpenAICompatProvider(options)
    except (ProviderError,ValueError,TypeError):return {"state":"not_configured","reason":"invalid_or_missing_provider"}
    conn=index.connect(repo);job_id=None;owner=uuid.uuid4().hex
    deadline=deadline or time.monotonic()+60
    try:
        schedule(conn,force=True)
        row=conn.execute("SELECT job_id FROM jobs WHERE kind=? AND state IN ('queued','retry_wait','claimed') "
                         "AND (lease_until IS NULL OR lease_until='' OR lease_until<?) AND next_run_at<=? ORDER BY created_at LIMIT 1",
                         (jobs.KIND_EMBED,now(),now())).fetchone()
        if not row:return {"state":"no_work"}
        job_id=row[0]
        conn.execute("BEGIN IMMEDIATE")
        until=datetime.fromtimestamp(time.time()+max(120,deadline-time.monotonic()+5),timezone.utc).isoformat(timespec="seconds")
        changed=conn.execute("UPDATE jobs SET state='claimed',lease_owner=?,lease_until=?,fencing_token=fencing_token+1,attempt=attempt+1 "
            "WHERE job_id=? AND state IN ('queued','retry_wait','claimed') AND (lease_until IS NULL OR lease_until='' OR lease_until<?)",(owner,until,job_id,now())).rowcount
        conn.commit()
        if not changed:return {"state":"leased"}
        # Reject obsolete work/config before model calls.
        spool=conn.execute("SELECT input_json FROM job_spool WHERE job_id=?",(job_id,)).fetchone()
        if not spool:
            # job 与 spool 由 enqueue 同事务写入；spool 缺失说明数据已损坏，
            # 重试永远无法恢复：显性失败并记录原因，不让 TypeError 冒泡崩溃。
            conn.execute("UPDATE jobs SET state='failed',error='job_spool_missing',lease_owner='',lease_until='',updated_at=? "
                "WHERE job_id=? AND lease_owner=?",(now(),job_id,owner));conn.commit()
            return {"state":"failed","reason":"job_spool_missing"}
        payload=json.loads(spool[0])
        if payload["provider_fp"]!=provider.fingerprint:
            raise ProviderError("configuration_changed")
        items=manifest(conn,options);corpus=digest(items)
        if corpus!=payload["corpus"]:raise ProviderError("index_changed")
        prior=conn.execute("SELECT generation_id FROM embedding_generations WHERE provider_fp=? ORDER BY created_at DESC,rowid DESC LIMIT 1",(provider.fingerprint,)).fetchone()
        previous_generation=generation(conn,prior[0]) if prior else None
        if prior and not previous_generation:raise ProviderError("index_metadata_invalid")
        identity=provider.probe(previous_generation["identity"] if previous_generation else None,deadline=deadline)
        pending=conn.execute("SELECT generation_id FROM embedding_generations WHERE space_id=? AND manifest_hash=? AND state='building' ORDER BY created_at DESC LIMIT 1",(identity["space_id"],corpus)).fetchone()
        gid=pending[0] if pending else digest({"space":identity["space_id"],"corpus":corpus,"nonce":uuid.uuid4().hex})
        conn.execute("INSERT OR IGNORE INTO embedding_generations(generation_id,provider_fp,space_id,identity_json,manifest_hash,state,expected_count,created_at) VALUES(?,?,?,?,?,'building',?,?)",
            (gid,provider.fingerprint,identity["space_id"],json.dumps(identity),corpus,len(items),now()));conn.commit()
        embedded=reused=0
        for item in items:
            if time.monotonic()>=deadline:raise ProviderError("timeout")
            if vector(conn,gid,item,identity["dimension"]) is not None:
                reused+=1;continue
            old=conn.execute("SELECT e.generation_id FROM embedding_index e JOIN embedding_generations g ON e.generation_id=g.generation_id "
                "WHERE e.entity_id=? AND e.content_hash=? AND e.content_revision=? AND g.space_id=? AND g.state IN ('active','ready','retired') LIMIT 1",
                (item["id"],item["hash"],item["revision"],identity["space_id"])).fetchone()
            previous=vector(conn,old[0],item,identity["dimension"]) if old else None
            if previous is not None:
                v=previous;reused+=1
            else:
                parts=_chunks(item["text"],options);vs=[]
                for part in parts:
                    values,observed=provider.embed([part],deadline=deadline)
                    if observed["space_id"]!=identity["space_id"]:raise ProviderError("model_mismatch")
                    vs.append(values[0])
                v=normalize([sum(row[i] for row in vs)/len(vs) for i in range(identity["dimension"])]);embedded+=1
            conn.execute("BEGIN IMMEDIATE")
            current={v["id"]:v for v in manifest(conn,options)}.get(item["id"])
            lease=conn.execute("SELECT lease_owner FROM jobs WHERE job_id=?",(job_id,)).fetchone()
            if current!=item or not lease or lease[0]!=owner:
                conn.rollback();raise ProviderError("index_changed")
            conn.execute("INSERT OR REPLACE INTO embedding_index(memory_id,model_version,content_hash,vector,updated_at,generation_id,entity_id,content_revision,dimension,vector_checksum) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (gid+":"+item["id"],identity["space_id"],item["hash"],json.dumps(v),now(),gid,item["id"],item["revision"],len(v),digest(v)))
            conn.execute("UPDATE embedding_generations SET completed_count=(SELECT count(*) FROM embedding_index WHERE generation_id=?) WHERE generation_id=?",(gid,gid));conn.commit()
        provider.probe(identity,deadline=deadline)
        if OpenAICompatProvider(settings()).fingerprint!=provider.fingerprint:raise ProviderError("configuration_changed")
        if digest(manifest(conn,settings()))!=corpus:raise ProviderError("index_changed")
        conn.execute("UPDATE embedding_generations SET state='ready',validated_at=? WHERE generation_id=? AND state='building'",(now(),gid));conn.commit()
        state="ready"
        if quality(options,identity):activate(conn,gid,options);state="active"
        prune(conn)
        conn.execute("UPDATE jobs SET state='succeeded' ,result=?,lease_owner='',lease_until='',updated_at=? WHERE job_id=? AND lease_owner=?",
                     (json.dumps({"generation":gid,"state":state}),now(),job_id,owner));conn.commit()
        from .retrieval_metrics import observe
        observe(conn,"embed:"+job_id+":"+owner,"embedding_worker",{"level":"maintenance","generation":gid,"remote_calls":provider.calls,"provider_usage":provider.usage_summary()})
        return {"state":state,"generation":gid,"embedded":embedded,"reused":reused,"provider_usage":provider.usage_summary(),"quality": "approved" if state=="active" else "external_pending"}
    except ProviderError as exc:
        conn.rollback()
        if job_id:
            from datetime import timedelta
            retry=(datetime.now(timezone.utc)+timedelta(seconds=max(30,exc.retry_after))).isoformat(timespec="seconds")
            state="failed" if exc.category in {"configuration_changed","index_changed"} else "retry_wait"
            conn.execute("UPDATE jobs SET state=?,error=?,lease_owner='',lease_until='',next_run_at=?,updated_at=? WHERE job_id=? AND lease_owner=?",
                         (state,exc.category,retry,now(),job_id,owner));conn.commit()
        if job_id:
            from .retrieval_metrics import observe
            observe(conn,"embed:"+job_id+":"+owner,"embedding_worker",{"level":"maintenance","reason":exc.category,"remote_calls":provider.calls,"provider_usage":provider.usage_summary()})
        return {"state":"retry_wait","reason":exc.category}
    finally:conn.close()


def prune(conn):
    """Bound completed vector generations; keep manifests for audit, never delete logs."""
    active=generation(conn)
    keep={active["id"]} if active else set()
    for status in ["retired","ready"]:
        row=conn.execute("SELECT generation_id FROM embedding_generations WHERE state=? ORDER BY validated_at DESC,rowid DESC LIMIT 1",(status,)).fetchone()
        if row:keep.add(row[0])
    for gid,state in conn.execute("SELECT generation_id,state FROM embedding_generations WHERE state IN ('retired','ready')").fetchall():
        if gid not in keep:
            conn.execute("DELETE FROM embedding_index WHERE generation_id=?",(gid,))
    conn.commit()
