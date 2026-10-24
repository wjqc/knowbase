"""Shared durable breaker and guarded L3 selection; evolved from ZCode cce7aeb.

Automatic callers never invoke this module. Network always runs outside transactions.
"""
import json
import sqlite3
import threading
import time
import uuid
import random
from collections import OrderedDict

from . import config, index
from .embedding_provider import OpenAICompatProvider, ProviderError, cosine, digest
from . import semantic_index as si

_CACHE=OrderedDict()
_CACHE_LOCK=threading.Lock()
_WORKERS=set()
_WORKER_LOCK=threading.Lock()


def provider():return OpenAICompatProvider(si.settings())


def wake(repo):
    options=si.settings()
    if not options.get("enabled") or not options.get("auto_index",True):return
    key=str(repo)
    with _WORKER_LOCK:
        if key in _WORKERS:return
        _WORKERS.add(key)
    def work():
        try:si.build(repo)
        except Exception:pass  # Job lease expires; basic retrieval never depends on this worker.
        finally:
            with _WORKER_LOCK:_WORKERS.discard(key)
    threading.Thread(target=work,daemon=True).start()


def _state(conn,key):
    row=conn.execute("SELECT state,failures,retry_at,checked_at,lease_owner,lease_until,reason FROM retrieval_provider_state WHERE provider_fp=?",(key,)).fetchone()
    return dict(zip(["state","failures","retry_at","checked_at","owner","lease_until","reason"],row)) if row else None


def _failure(conn,key,exc,owner=None):
    current=_state(conn,key)
    if not current:return
    if owner and current["owner"]!=owner:return
    delay=900 if exc.category in {"auth_error","auth_missing","model_missing"} else min(300,10*2**min(current["failures"],5))
    delay=max(delay*random.uniform(.8,1.2),exc.retry_after)
    next_state="quarantined" if exc.category in {"model_mismatch","canary_drift","invalid_vector","dimension_mismatch"} else "open"
    conn.execute("UPDATE retrieval_provider_state SET state=?,failures=failures+1,retry_at=?,lease_owner='',lease_until=0,reason=? "
                 "WHERE provider_fp=?",(next_state,time.time()+delay,exc.category,key));conn.commit()


def _healthy(conn,p,identity,deadline):
    wall=time.time();current=_state(conn,p.fingerprint)
    auth=conn.execute("SELECT auth_revision FROM retrieval_provider_state WHERE provider_fp=?",(p.fingerprint,)).fetchone()
    revision=p.auth_revision()
    if auth and auth[0]!=revision:
        conn.execute("UPDATE retrieval_provider_state SET state='cold',retry_at=0,checked_at=0,auth_revision=? WHERE provider_fp=?",(revision,p.fingerprint));conn.commit()
        current=_state(conn,p.fingerprint)
    if current and current["state"]=="closed" and 0<=wall-current["checked_at"]<900:return True
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("INSERT OR IGNORE INTO retrieval_provider_state(provider_fp,state,auth_revision) VALUES(?,'cold',?)",(p.fingerprint,revision))
        owner=uuid.uuid4().hex
        changed=conn.execute("UPDATE retrieval_provider_state SET lease_owner=?,lease_until=?,state='half_open' "
            "WHERE provider_fp=? AND lease_until<=? AND retry_at<=?",(owner,wall+10,p.fingerprint,wall,wall)).rowcount
        conn.commit()
    except Exception:conn.rollback();raise
    if not changed:return False
    try:
        p.probe(identity,deadline=deadline)
        conn.execute("UPDATE retrieval_provider_state SET state='closed',failures=0,checked_at=?,retry_at=0,lease_owner='',lease_until=0,reason='' "
                     "WHERE provider_fp=? AND lease_owner=?",(time.time(),p.fingerprint,owner));conn.commit()
        return True
    except ProviderError as exc:
        _failure(conn,p.fingerprint,exc,owner)
        return False


def dense(conn, query, allowed_ids, *, deadline, diagnostics):
    options=si.settings()
    if not options.get("enabled"):
        diagnostics["semantic_status"]="not_configured";return {}
    if conn.in_transaction:
        diagnostics["reason"]="transaction_busy";return {}
    local_settings=config.load_config().get("retrieval",{})
    mode=str(local_settings.get("mode","auto")).lower() if isinstance(local_settings,dict) else "raw_fts"
    if mode!="auto":
        diagnostics["semantic_status"]="policy_local";return {}
    old_busy=conn.execute("PRAGMA busy_timeout").fetchone()[0]
    conn.execute("PRAGMA busy_timeout=50")
    p=None;started=time.monotonic()
    try:
        p=provider()
        g=si.generation(conn)
        # A completed shadow generation can activate automatically after evidence arrives.
        if not g or g["identity"]["provider_fp"]!=p.fingerprint:
            row=conn.execute("SELECT generation_id FROM embedding_generations WHERE provider_fp=? AND state='ready' ORDER BY validated_at DESC LIMIT 1",(p.fingerprint,)).fetchone()
            candidate=si.generation(conn,row[0]) if row else None
            if candidate and si.quality(options,candidate["identity"]):
                if _healthy(conn,p,candidate["identity"],deadline):
                    si.activate(conn,candidate["id"],options);g=si.generation(conn)
        if not g or g["identity"]["provider_fp"]!=p.fingerprint:
            diagnostics["reason"]="index_not_ready";return {}
        evidence=si.quality(options,g["identity"])
        if not evidence:
            diagnostics["reason"]="quality_unvalidated";return {}
        if evidence["digest"]!=g["quality_digest"]:
            if not _healthy(conn,p,g["identity"],deadline):
                diagnostics["reason"]="probe_pending";return {}
            si.activate(conn,g["id"],options);g=si.generation(conn)
        items=[v for v in si.manifest(conn,options) if v["id"] in allowed_ids]
        if not items or {v["id"] for v in items}!=allowed_ids:
            diagnostics["reason"]="scope_not_embeddable";return {}
        coverage=si.coverage(conn,g,items);diagnostics["vector_coverage"]=coverage
        if coverage["ratio"]!=1:
            diagnostics["reason"]="index_incomplete";return {}
        if not _healthy(conn,p,g["identity"],deadline):
            state=_state(conn,p.fingerprint)
            diagnostics["reason"]=state["reason"] or "probe_pending";return {}
        if len(query)>512 or deadline-time.monotonic()<.05:
            diagnostics["reason"]="semantic_budget_exhausted";return {}
        key=(p.fingerprint,g["identity"]["space_id"],digest(query))
        with _CACHE_LOCK:
            cached=_CACHE.get(key)
        cache_hit=bool(cached and time.monotonic()-cached[0]<600)
        if cache_hit:qv=cached[1]
        else:
            values,observed=p.embed([query],query=True,deadline=deadline)
            if observed["space_id"]!=g["identity"]["space_id"]:raise ProviderError("model_mismatch")
            qv=values[0]
            with _CACHE_LOCK:
                _CACHE[key]=(time.monotonic(),qv);_CACHE.move_to_end(key)
                while len(_CACHE)>128:_CACHE.popitem(last=False)
        # Late data/config/index changes never replace an already selected revision.
        active=si.generation(conn)
        if not active or active["id"]!=g["id"] or provider().fingerprint!=p.fingerprint:
            diagnostics["reason"]="generation_changed";return {}
        latest={v["id"]:v for v in si.manifest(conn,options)}
        if any(latest.get(v["id"])!=v for v in items):
            diagnostics["reason"]="index_changed";return {}
        scores={}
        for item in items:
            value=si.vector(conn,g["id"],item,g["identity"]["dimension"])
            if value is None:raise ProviderError("index_corrupt")
            score=cosine(qv,value)
            if score>=evidence["dense_min"]:scores[item["id"]]=score
        diagnostics.update(level="L3",semantic_status="healthy",generation_id=g["id"],
            model_epoch=g["identity"]["space_id"],quality_digest=evidence["digest"],cache_hit=cache_hit,
            remote_calls=p.calls,provider_usage=p.usage_summary(),reason=None)
        return dict(sorted(scores.items(),key=lambda v:-v[1])[:100])
    except ProviderError as exc:
        diagnostics["reason"]=exc.category
        if p:
            try:
                if not _state(conn,p.fingerprint):
                    conn.execute("INSERT INTO retrieval_provider_state(provider_fp,state) VALUES(?,'cold')",(p.fingerprint,));conn.commit()
                _failure(conn,p.fingerprint,exc)
            except sqlite3.Error:conn.rollback()
        return {}
    except (sqlite3.Error,ValueError,TypeError,OSError):
        if conn.in_transaction:conn.rollback()
        diagnostics["reason"]="semantic_unavailable";return {}
    finally:
        diagnostics["provider_ms"]=round((time.monotonic()-started)*1000,2)
        if p:
            diagnostics["remote_calls"]=p.calls
            diagnostics["provider_usage"]=p.usage_summary()
        conn.execute("PRAGMA busy_timeout="+str(int(old_busy)))


def snapshot(conn):
    g=si.generation(conn)
    options=si.settings()
    key=provider().fingerprint if options.get("enabled") else None
    states=[dict(zip(["state","reason","retry_at","checked_at"],r)) for r in conn.execute("SELECT state,reason,retry_at,checked_at FROM retrieval_provider_state WHERE provider_fp=?",(key,))]
    items=si.manifest(conn,options)
    last=conn.execute("SELECT origin,level,reason,occurred_at FROM retrieval_observations WHERE origin IN ('mcp_search','mcp_context') ORDER BY occurred_at DESC LIMIT 1").fetchone()
    return {"configured":bool(options.get("enabled")),"last_actual_retrieval":dict(zip(["origin","level","reason","at"],last)) if last else None,
            "health_observation_stale":not states or not 0<=time.time()-states[0]["checked_at"]<900,
            "generation":g["id"] if g else None,
            "model_epoch":g["identity"]["space_id"] if g else None,"coverage":si.coverage(conn,g,items),
            "quality":bool(g and g["identity"]["provider_fp"]==key and si.quality(options,g["identity"])),"provider_state":states[0] if states else None}


def revision(conn):
    """Read-only cache identity; no probe and no worker on automatic paths."""
    try:
        active=conn.execute("SELECT generation_id,revision FROM retrieval_active WHERE singleton=1").fetchone()
        key=provider().fingerprint if si.settings().get("enabled") else None
        state=conn.execute("SELECT state,reason,retry_at,checked_at FROM retrieval_provider_state WHERE provider_fp=?",(key,)).fetchone()
        due=bool(state and state[0]!="closed" and time.time()>=state[2])
        report=si.settings().get("quality_report")
        p=__import__("pathlib").Path(report).expanduser() if report else None
        stamp=(p.stat().st_mtime_ns,p.stat().st_size) if p and p.exists() else None
        return digest({"active":active,"state":state,"due":due,"report":stamp,"embedding":si.settings()})
    except (ProviderError,sqlite3.Error,OSError,ValueError,TypeError):return "unavailable"


def maintenance(repo):
    """A server-lifetime maintenance tick; Hook processes never start this timer."""
    options=si.settings()
    if not options.get("enabled"):return
    wake(repo)
    conn=None
    try:
        conn=index.connect(repo);p=provider();g=si.generation(conn)
        row=conn.execute("SELECT generation_id FROM embedding_generations WHERE provider_fp=? AND state='ready' ORDER BY validated_at DESC,rowid DESC LIMIT 1",(p.fingerprint,)).fetchone()
        candidate=si.generation(conn,row[0]) if row else None
        if candidate and digest(si.manifest(conn,options))==candidate["manifest_hash"]:
            g=candidate
        if g and g["identity"]["provider_fp"]==p.fingerprint:
            evidence=si.quality(options,g["identity"])
            if evidence and _healthy(conn,p,g["identity"],time.monotonic()+2):
                items=si.manifest(conn,options)
                if si.coverage(conn,g,items)["ratio"]==1 and digest(items)==g["manifest_hash"]:
                    current=si.generation(conn)
                    if not current or current["id"]!=g["id"] or current["quality_digest"]!=evidence["digest"]:
                        si.activate(conn,g["id"],options)
    except (ProviderError,sqlite3.Error,ValueError,TypeError,OSError):
        if conn and conn.in_transaction:conn.rollback()
    finally:
        if conn:conn.close()
