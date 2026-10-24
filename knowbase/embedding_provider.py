"""Hardened intranet adapter, reusing ZCode ed76fb2's protocol/Canary approach.

No public fallback, ML runtime, implicit truncation, or plaintext credential storage.
"""
import hashlib
import json
import math
import os
import queue
import re
from pathlib import Path
import stat
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

CANARY_TEXTS = [
    "内网服务连接失败如何排查", "docker pull timeout HTTP 500 INVALID_STATE",
    "TopicsValidator 参数校验异常", "结算后订单禁止撤回", "草稿订单允许删除",
    "HTTP 401 authentication failure", "HTTP 403 permission denied", "Chinese English mixed query",
    "只看200响应Content-Encoding", "database unique constraint violation",
    "unrelated weather forecast", "scope and version must match",
]
_NETWORK_SLOTS = threading.BoundedSemaphore(2)


class ProviderError(Exception):
    def __init__(self, category, *, status=None, retry_after=0):
        super().__init__(category)  # Never retain server body, URL, exception text, or secret.
        self.category, self.status, self.retry_after = category, status, retry_after


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def normalize(vector, dimension=None):
    if (not isinstance(vector, list) or not vector or len(vector) > 32768 or
            dimension is not None and len(vector) != dimension or
            any(isinstance(v, bool) or not isinstance(v, (int, float)) or (isinstance(v,int) and abs(v)>10**300) or not math.isfinite(v) for v in vector)):
        raise ProviderError("invalid_vector")
    try:
        norm = math.sqrt(sum(v*v for v in vector))
    except (OverflowError,ValueError):raise ProviderError("invalid_vector") from None
    if not norm or not math.isfinite(norm):
        raise ProviderError("invalid_vector")
    return [v/norm for v in vector]


def cosine(a, b):
    a, b = normalize(a), normalize(b, len(a))
    return sum(x*y for x,y in zip(a,b))


def retry_after(value):
    try:
        return min(86400, max(0, float(value)))
    except (TypeError, ValueError):
        try:
            return min(86400, max(0, parsedate_to_datetime(value).timestamp()-time.time()))
        except (TypeError, ValueError, OverflowError):
            return 0


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ProviderError("redirect_rejected")


class OpenAICompatProvider:
    def __init__(self, settings):
        self.settings = dict(settings)
        root = settings.get("api_root") or settings.get("base_url") or ""
        u = urllib.parse.urlsplit(root)
        hosts=settings.get("allowed_hosts",[])
        try:port=u.port
        except ValueError:raise ProviderError("invalid_endpoint") from None
        if (not isinstance(hosts,list) or any(not isinstance(h,str) for h in hosts) or
                u.scheme not in {"http", "https"} or not u.hostname or u.username or u.password or
                u.query or u.fragment or u.hostname not in hosts):
            raise ProviderError("invalid_endpoint")
        if u.scheme == "http" and not settings.get("allow_insecure_http", False):
            raise ProviderError("insecure_transport")
        self.root = root.rstrip("/")
        self.model = settings.get("model")
        if not isinstance(self.model, str) or not self.model or len(self.model)>256:
            raise ProviderError("invalid_model")
        self.query_prefix = settings.get("query_prefix", "")
        self.document_prefix = settings.get("document_prefix", "")
        if not all(isinstance(s,str) and len(s)<=1024 for s in [self.query_prefix,self.document_prefix]):
            raise ProviderError("invalid_preprocessing")
        self.dimension = settings.get("dimensions")
        if self.dimension is not None and (type(self.dimension) is not int or not 1<=self.dimension<=32768):
            raise ProviderError("invalid_dimension")
        self.timeout = min(10, max(.05, float(settings.get("timeout_seconds", .7))))
        self.batch_timeout = min(30, max(.05, float(settings.get("batch_timeout_seconds",10))))
        self.auth_kind = settings.get("auth_kind", "bearer")
        self.key_env = settings.get("api_key_env", "KNOWBASE_EMBED_API_KEY")
        if self.auth_kind not in {"bearer","none"} or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.key_env):
            raise ProviderError("invalid_auth_config")
        try:
            ctx = ssl.create_default_context(cafile=settings.get("ca_bundle"))
        except (OSError, ssl.SSLError):
            raise ProviderError("tls_config_error") from None
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect(),
                                                   urllib.request.HTTPSHandler(context=ctx))
        self.fingerprint = digest({"root":self.root,"model":self.model,"deployment_id":settings.get("deployment_id"),
            "query_prefix":self.query_prefix,"document_prefix":self.document_prefix,
            "dimensions":self.dimension,"normalization":"unit-v1","protocol":"openai-embeddings-v1",
            "tenant":settings.get("tenant"),"ca_bundle":settings.get("ca_bundle"),
            "chunk_chars":max(128,min(8000,int(settings.get("chunk_chars",6000)))),
            "max_chunks":max(1,min(64,int(settings.get("max_chunks",8)))),"document_profile":"chunk-mean-v1"})
        self.secret_ref=settings.get("secret_ref") or "env:"+self.key_env
        if not isinstance(self.secret_ref,str) or not self.secret_ref.startswith(("env:","file:")):
            raise ProviderError("invalid_secret_reference")
        self.calls = 0
        self.last_usage = None
        self.usage_responses = []

    def _secret(self):
        if self.secret_ref.startswith("env:"):
            name=self.secret_ref[4:]
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*",name):raise ProviderError("invalid_secret_reference")
            value=os.environ.get(name,"")
        else:
            p=Path(self.secret_ref[5:]).expanduser()
            try:
                if not p.is_absolute() or p.is_symlink() or not stat.S_ISREG(p.stat().st_mode) or p.stat().st_size>16384 or p.stat().st_mode & 0o077:
                    raise ProviderError("insecure_secret_file")
                if hasattr(os,"geteuid") and p.stat().st_uid!=os.geteuid():raise ProviderError("insecure_secret_file")
                value=p.read_text().strip()
            except OSError:raise ProviderError("auth_missing") from None
        if "\n" in value or "\r" in value:raise ProviderError("invalid_secret_reference")
        return value

    def auth_revision(self):
        stamp=None
        if self.secret_ref.startswith("file:"):
            try:
                s=Path(self.secret_ref[5:]).expanduser().stat();stamp=(s.st_mtime_ns,s.st_size)
            except OSError:stamp="missing"
        return digest({"reference":self.secret_ref,"file_stamp":stamp,"auth_kind":self.auth_kind})

    def _request(self, route, payload=None, *, deadline=None, timeout_budget=None):
        budget=self.timeout if timeout_budget is None else timeout_budget
        deadline = min(deadline or time.monotonic()+budget, time.monotonic()+budget)
        if deadline <= time.monotonic():
            raise ProviderError("timeout")
        headers = {"Content-Type":"application/json"}
        if self.auth_kind == "bearer":
            key = self._secret()
            if not key:
                raise ProviderError("auth_missing")
            headers["Authorization"] = "Bearer " + key
        req = urllib.request.Request(self.root+route,
            data=json.dumps(payload).encode() if payload is not None else None, headers=headers)
        if not _NETWORK_SLOTS.acquire(blocking=False):
            raise ProviderError("transport_busy")
        done = queue.Queue(maxsize=1)
        self.calls += 1
        def work():
            try:
                with self.opener.open(req, timeout=max(.01,deadline-time.monotonic())) as response:
                    chunks=[];size=0
                    while True:
                        if time.monotonic() >= deadline: raise ProviderError("timeout")
                        chunk=response.read1(65536)
                        if not chunk:break
                        size+=len(chunk)
                        if size>16*1024*1024:raise ProviderError("response_too_large")
                        chunks.append(chunk)
                    data=json.loads(b"".join(chunks))
                    if not isinstance(data,dict) or data.get("error"):raise ProviderError("protocol_error")
                    done.put(data)
            except urllib.error.HTTPError as exc:
                category=({401:"auth_error",403:"auth_error",404:"model_missing",429:"rate_limited",400:"input_rejected"}.get(exc.code,"server_error"))
                done.put(ProviderError(category,status=exc.code,retry_after=retry_after(exc.headers.get("Retry-After"))))
                exc.close()
            except ProviderError as exc:done.put(exc)
            except TimeoutError:done.put(ProviderError("timeout"))
            except urllib.error.URLError as exc:
                category="timeout" if isinstance(exc.reason,TimeoutError) else "tls_error" if isinstance(exc.reason,ssl.SSLError) else "connection_error"
                done.put(ProviderError(category))
            except OSError:done.put(ProviderError("connection_error"))
            except (ValueError, TypeError):done.put(ProviderError("protocol_error"))
            except Exception:done.put(ProviderError("protocol_error"))
            finally:_NETWORK_SLOTS.release()
        threading.Thread(target=work,daemon=True).start()
        try:result=done.get(timeout=max(.001,deadline-time.monotonic()))
        except queue.Empty:raise ProviderError("timeout") from None
        if isinstance(result,ProviderError):raise result
        return result

    def models(self, *, deadline=None):
        data=self._request("/models",deadline=deadline,timeout_budget=3)
        values=data.get("data")
        if not isinstance(values,list):raise ProviderError("protocol_error")
        return [v["id"] for v in values if isinstance(v,dict) and isinstance(v.get("id"),str)][:256]

    def embed(self, texts, *, query=False, deadline=None):
        if not isinstance(texts,list) or not 1<=len(texts)<=64 or any(not isinstance(t,str) or not t.strip() for t in texts):
            raise ProviderError("invalid_input")
        prefix=self.query_prefix if query else self.document_prefix
        prepared=[prefix+t for t in texts]
        if sum(len(t.encode()) for t in prepared)>65536:raise ProviderError("input_too_large")
        payload={"model":self.model,"input":prepared,"encoding_format":"float"}
        if self.dimension is not None:payload["dimensions"]=self.dimension
        data=self._request("/embeddings",payload,deadline=deadline,timeout_budget=self.timeout if query else self.batch_timeout)
        items=data.get("data")
        if not isinstance(items,list) or len(items)!=len(texts):raise ProviderError("protocol_error")
        out={}
        for item in items:
            if not isinstance(item,dict) or type(item.get("index")) is not int:raise ProviderError("protocol_error")
            i=item["index"]
            if not 0<=i<len(texts) or i in out:raise ProviderError("protocol_error")
            out[i]=normalize(item.get("embedding"),self.dimension)
        dimension=len(out[0])
        if any(len(v)!=dimension for v in out.values()):raise ProviderError("dimension_mismatch")
        response_model=data.get("model")
        if response_model is not None and not isinstance(response_model,str):raise ProviderError("protocol_error")
        expected=self.settings.get("response_model")
        if expected and response_model!=expected:raise ProviderError("model_mismatch")
        usage=data.get("usage",{})
        self.last_usage={k:v for k,v in usage.items() if k in {"prompt_tokens","total_tokens"} and type(v) is int and v>=0} if isinstance(usage,dict) else None
        self.usage_responses.append(self.last_usage)
        identity={"provider_fp":self.fingerprint,"dimension":dimension,"response_model":response_model,
                  "deployment_id":self.settings.get("deployment_id"),"normalization":"unit-v1"}
        identity["space_id"]=digest(identity)
        return [out[i] for i in range(len(texts))],identity

    def probe(self, baseline=None, *, deadline=None):
        vectors,identity=self.embed(CANARY_TEXTS,deadline=deadline)
        if baseline:
            if identity["space_id"]!=baseline["space_id"]:raise ProviderError("model_mismatch")
            old=baseline.get("canary",[])
            if len(old)!=len(vectors):raise ProviderError("canary_invalid")
            deviations=[math.sqrt(sum((a-b)**2 for a,b in zip(normalize(x),normalize(y,len(x))))) for x,y in zip(vectors,old)]
            if max(deviations)>baseline["tolerance"]:raise ProviderError("canary_drift")
            return baseline
        # Calibrate numerical noise; no assumption that aliases are immutable versions.
        tolerance=1e-6
        for _ in range(2):
            fresh,current=self.embed(CANARY_TEXTS,deadline=deadline)
            if current["space_id"]!=identity["space_id"]:raise ProviderError("unstable_model")
            tolerance=max(tolerance,4*max(math.sqrt(sum((a-b)**2 for a,b in zip(x,y))) for x,y in zip(vectors,fresh)))
        if tolerance>.01:raise ProviderError("unstable_model")
        return {**identity,"canary":vectors,"tolerance":tolerance,"canary_revision":"fixed-12-v1"}


    def usage_summary(self):
        complete=bool(self.calls and len(self.usage_responses)==self.calls and all(self.usage_responses))
        keys=set.intersection(*(set(v) for v in self.usage_responses)) if complete else set()
        return {"network_calls":self.calls,"reported_usage_complete":complete,
                "reported_usage_totals":{k:sum(v[k] for v in self.usage_responses) for k in keys} if complete else None,
                "currency_cost_actual":None}
