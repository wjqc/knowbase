"""派生索引层：SQLite FTS5（trigram）+ 统计 + INDEX.md。

知识索引可由 markdown 重建；运行日志与读取统计不可重建，须备份 memory.db。
hit_count 属运行统计，只存本库不入 frontmatter（避免读操作产生 git 提交）。
"""

import json
import hashlib
import math
import os
import re
import sqlite3
import time
from collections import OrderedDict
from datetime import date, datetime, timedelta
from pathlib import Path

from . import store, config

DB_NAME = "memory.db"
FRESH_HALF_LIFE_DAYS = 180
STALE_REVIEW_DAYS = 180


# ---------- 连接与建表 ----------

def db_path(repo: Path) -> Path:
    return Path(repo) / DB_NAME


def connect(repo: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(repo))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS meta(
      id TEXT PRIMARY KEY, type TEXT, title TEXT, scope TEXT, tags TEXT,
      confidence TEXT, status TEXT, last_verified TEXT, updated TEXT,
      helpful_count INTEGER DEFAULT 0, unhelpful_count INTEGER DEFAULT 0,
      hit_count INTEGER DEFAULT 0, path TEXT, staging INTEGER DEFAULT 0);
    CREATE VIRTUAL TABLE IF NOT EXISTS mem_fts USING fts5(
      id UNINDEXED, title, tags, body, tokenize='trigram');
    CREATE TABLE IF NOT EXISTS usage_log(
      ts TEXT, agent TEXT, tool TEXT, memory_id TEXT, detail TEXT);
    CREATE TABLE IF NOT EXISTS feedback_log(
      ts TEXT, memory_id TEXT, agent TEXT, outcome TEXT);
    CREATE TABLE IF NOT EXISTS embedding_index(
      memory_id TEXT PRIMARY KEY, model_version TEXT NOT NULL,
      content_hash TEXT NOT NULL, vector TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS source_state(
      source_path TEXT PRIMARY KEY, source_hash TEXT NOT NULL,
      modified_at TEXT, indexed_at TEXT NOT NULL, status TEXT NOT NULL, error TEXT);
    CREATE TABLE IF NOT EXISTS sync_state(
      remote TEXT PRIMARY KEY, local_revision TEXT, remote_revision TEXT,
      last_fetch_at TEXT, last_push_at TEXT, status TEXT NOT NULL, error TEXT);
    """)
    conn.commit()
    # 运行增量迁移（jobs/candidates/sync_attempts 等新表）
    from . import migrations
    migrations.apply_all(conn)
    return conn


# ---------- 写入与重建 ----------

def _code_refs_text(meta: dict) -> str:
    lines = []
    for ref in meta.get("code_refs") or []:
        if not isinstance(ref, dict) or not ref.get("repo") or not ref.get("path"):
            continue
        value = f"code:{ref['repo']}/{ref['path']}"
        if ref.get("symbol"):
            value += f"#{ref['symbol']}"
        if ref.get("lines") is not None:
            value += f":{ref['lines']}"
        lines.append(value)
    return "\n".join(lines)

def upsert(conn: sqlite3.Connection, meta: dict, body: str, path: Path, staging: bool = False, commit: bool = True):
    indexed_body = body
    code_text = _code_refs_text(meta)
    if code_text:
        indexed_body = f"{body.rstrip()}\n{code_text}\n"
    conn.execute("DELETE FROM mem_fts WHERE id=?", (meta["id"],))
    conn.execute(
        "INSERT INTO mem_fts(id, title, tags, body) VALUES(?,?,?,?)",
        (meta["id"], meta.get("title", ""), " ".join(meta.get("tags", [])), indexed_body),
    )
    vector_text = f"{meta.get('title', '')} {' '.join(meta.get('tags', []))} {indexed_body}"
    content_hash = hashlib.sha256(vector_text.encode("utf-8")).hexdigest()
    model = "char-ngram-v1"  # retained legacy projection; never scored as semantic
    conn.execute(
        "INSERT INTO embedding_index(memory_id,model_version,content_hash,vector,updated_at) "
        "VALUES(?,?,?,?,?) ON CONFLICT(memory_id) DO UPDATE SET "
        "model_version=excluded.model_version,content_hash=excluded.content_hash,"
        "vector=excluded.vector,updated_at=excluded.updated_at "
        "WHERE embedding_index.model_version='char-ngram-v1' "
        "AND embedding_index.content_hash!=excluded.content_hash",
        (meta["id"], model, content_hash, json.dumps(_fallback_vector(vector_text)),
         datetime.now().isoformat(timespec="seconds")),
    )
    conn.execute(
        """INSERT INTO meta(id,type,title,scope,tags,confidence,status,last_verified,updated,
                            helpful_count,unhelpful_count,hit_count,path,staging)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,COALESCE((SELECT hit_count FROM meta WHERE id=?),0),?,?)
           ON CONFLICT(id) DO UPDATE SET
             type=excluded.type, title=excluded.title, scope=excluded.scope, tags=excluded.tags,
             confidence=excluded.confidence, status=excluded.status,
             last_verified=excluded.last_verified, updated=excluded.updated,
             helpful_count=excluded.helpful_count, unhelpful_count=excluded.unhelpful_count,
             path=excluded.path, staging=excluded.staging""",
        (meta["id"], meta.get("type"), meta.get("title", ""), meta.get("scope", ""),
         json.dumps(meta.get("tags", []), ensure_ascii=False), meta.get("confidence", "once"),
         meta.get("status", "active"), meta.get("last_verified", "") or "",
         meta.get("updated", ""), int(meta.get("helpful_count", 0)),
         int(meta.get("unhelpful_count", 0)), meta["id"], str(path), int(staging)),
    )
    # 卡片是内容权威；Git 克隆/同步后也要能读取版本并进行 CAS。
    from .mutations import _compute_revision, _compute_governance_revision
    conn.execute("INSERT INTO entity_heads(entity_id,content_revision,governance_revision,updated_at) "
                 "VALUES(?,?,?,?) ON CONFLICT(entity_id) DO UPDATE SET "
                 "content_revision=excluded.content_revision,governance_revision=excluded.governance_revision,"
                 "updated_at=excluded.updated_at",
                 (meta["id"], _compute_revision(meta, body), _compute_governance_revision(meta),
                  datetime.now().isoformat(timespec="seconds")))
    if commit:
        conn.commit()
        from .semantic_index import schedule
        schedule(conn)


def rebuild(repo: Path) -> int:
    """全量重建（markdown → db + INDEX.md）。返回记忆条数。"""
    repo = Path(repo)
    conn = connect(repo)
    try:
        conn.execute("BEGIN IMMEDIATE")
        old_hits = dict(conn.execute("SELECT id, hit_count FROM meta"))
        conn.execute("DELETE FROM mem_fts")
        conn.execute("DELETE FROM meta")
        n = 0
        for meta, body, path in store.iter_all(repo, include_staging=True):
            upsert(conn, meta, body, path, staging=(path.parent.name == "staging"), commit=False)
            conn.execute("UPDATE meta SET hit_count=? WHERE id=?", (old_hits.get(meta["id"], 0), meta["id"]))
            n += 1
        conn.execute("DELETE FROM embedding_index WHERE generation_id='' AND memory_id NOT IN (SELECT id FROM meta)")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    build_index_md(repo)
    with __import__("contextlib").closing(connect(repo)) as conn:
        from .semantic_index import schedule
        schedule(conn)
    return n


_SOURCE_CACHE=OrderedDict()


def source_current(conn, mid, expected_meta=None):
    row=conn.execute("SELECT m.path,m.scope,m.status,m.confidence,h.content_revision,m.staging,m.type FROM meta m JOIN entity_heads h ON h.entity_id=m.id WHERE m.id=?",(mid,)).fetchone()
    if not row:return False
    if expected_meta and any(expected_meta.get(key)!=row[pos] for key,pos in
            [("scope",1),("status",2),("confidence",3),("content_revision",4),("staging",5),("type",6)]):
        return False
    try:
        path=Path(row[0])
        if path.is_symlink() or path.stat().st_size>1048576:return False
        raw=path.read_bytes();key=(str(path),hashlib.sha256(raw).hexdigest())
        parsed=_SOURCE_CACHE.get(key)
        if parsed is None:
            # utf-8-sig：剥掉可能存在的 BOM，否则带 BOM 的来源文件恒判 source_current=False
            parsed=store.parse_text(raw.decode("utf-8-sig"),str(path));_SOURCE_CACHE[key]=parsed
            while len(_SOURCE_CACHE)>512:_SOURCE_CACHE.popitem(last=False)
        meta,body=parsed
        from .mutations import _compute_revision
        return (meta.get("id")==mid and meta.get("scope")==row[1] and meta.get("status","active")==row[2]
                and meta.get("confidence","once")==row[3] and _compute_revision(meta,body)==row[4])
    except (OSError,UnicodeError,ValueError,TypeError,AttributeError,store.yaml.YAMLError):return False


# ---------- 检索与排序 ----------

def _row_meta(r) -> dict:
    return {
        "id": r[0], "type": r[1], "title": r[2], "scope": r[3],
        "tags": json.loads(r[4] or "[]"), "confidence": r[5], "status": r[6],
        "last_verified": r[7], "updated": r[8], "helpful_count": r[9],
        "unhelpful_count": r[10], "hit_count": r[11], "path": r[12], "staging": r[13],
    }


def _freshness_factor(m: dict) -> float:
    ref = m.get("last_verified") or m.get("updated") or date.today().isoformat()
    try:
        days = max(0, (date.today() - date.fromisoformat(str(ref))).days)
    except ValueError:
        days = 0
    f = 0.5 ** (days / FRESH_HALF_LIFE_DAYS)
    if m.get("status") == "stale":
        f *= 0.5
    return f


def _feedback_factor(m: dict) -> float:
    h, u = int(m.get("helpful_count", 0)), int(m.get("unhelpful_count", 0))
    if u and not h:
        return 0.3  # 只收过负反馈 → 直接沉底
    if h + u == 0:
        return 1.0
    return 0.5 + h / (h + u)  # 0.5 ~ 1.5


def _scope_factor(m: dict, scope: str | None) -> float:
    if scope:
        return 2.0 if m["scope"] == scope else 1.5  # 精确命中项目 / global 通用经验
    return 1.2 if m["scope"] == "global" else 1.0


def _match_ids(conn: sqlite3.Connection, terms: list[str]) -> dict[str, float] | None:
    """≥3 字词走 FTS5 MATCH（trigram），返回 {id: 词法分}；不可用返回 None。

    多词查询按 OR 召回后按命中词数加权：真实排障提问常是"主体 + 现象/动作"
    （如「EasyConnect 断连 排查」），其中任一词命中即应召回，
    命中越多的排得越前。全 AND 会要求单卡包含全部词，导致召回坍塌。
    """
    big = [t.replace('"', "") for t in terms if len(t) >= 3]
    if not big:
        return None
    match = " OR ".join(f'"{t}"' for t in big)
    try:
        rows = conn.execute(
            "SELECT id, rank FROM mem_fts WHERE mem_fts MATCH ? ORDER BY rank",
            (match,),
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    if not rows:
        return None
    bm25 = {r[0]: -r[1] for r in rows}
    # 逐词统计命中数，命中词数越多词法分越高（保留 bm25 的相对次序）
    hit_count: dict[str, int] = {}
    for term in big:
        try:
            sub = conn.execute(
                "SELECT id FROM mem_fts WHERE mem_fts MATCH ?", (f'"{term}"',)
            ).fetchall()
        except sqlite3.OperationalError:
            continue
        for r in sub:
            hit_count[r[0]] = hit_count.get(r[0], 0) + 1
    if not hit_count:
        return bm25
    return {
        mid: bm25.get(mid, 0.0) * (1.0 + 0.5 * (hit_count[mid] - 1))
        for mid in hit_count
    } or None


def _like_ids(conn: sqlite3.Connection, terms: list[str]) -> dict[str, float]:
    """短词 / MATCH 未命中的回退：LIKE 扫描。

    与 _match_ids 同理按命中词数加权，任一词命中即召回。
    """
    if not terms:
        return {}
    scored: dict[str, float] = {}
    for t in terms:
        rows = conn.execute(
            "SELECT id FROM mem_fts WHERE "
            "(instr(lower(title), lower(?)) > 0 OR instr(lower(tags), lower(?)) > 0 "
            "OR instr(lower(body), lower(?)) > 0)",
            (t, t, t),
        ).fetchall()
        for r in rows:
            scored[r[0]] = scored.get(r[0], 0.0) + 1.0
    return scored


def _fallback_vector(text: str, dim: int = 512) -> list[float]:
    """无模型时的字符 n-gram 向量；用于模糊召回，不冒充语义模型。"""
    s = (text or "").lower()
    ascii_terms = re.findall(r"[a-z0-9_.-]{2,}", s)
    cjk_runs = re.findall(r"[\u4e00-\u9fff]+", s)
    tokens = ascii_terms[:]
    for run in cjk_runs:
        tokens.extend(run[i:i + 2] for i in range(max(1, len(run) - 1)))
        tokens.extend(run[i:i + 3] for i in range(max(1, len(run) - 2)))
    vec = [0.0] * dim
    for token in tokens:
        pos = int.from_bytes(hashlib.md5(token.encode("utf-8")).digest()[:4], "big") % dim
        vec[pos] += 1.0
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec] if norm else vec


def _dense_ids(conn: sqlite3.Connection, query: str, allowed_ids: set[str], limit: int = 100) -> dict[str, float]:
    """Retired legacy entry; hash vectors never participate in retrieval."""
    # Legacy vectors remain for migration only; never compare incompatible spaces.
    return {}


_LEGACY_ALIASES = {
    "连不上": ("连接失败", "启动失败", "vpn"),
    "很卡": ("加载慢", "查询慢", "性能"),
    "怎么写": ("生成流程", "编写流程"),
    "问一下": ("征求同意", "确认"),
}


def _load_aliases() -> dict[str, tuple[str, ...]]:
    """Bounded package/local dictionary. Broken input cannot break base retrieval."""
    from importlib.resources import files
    merged = dict(_LEGACY_ALIASES)
    sources = [Path(os.environ.get("KNOWBASE_SYNONYMS", str(Path.home() / ".knowbase" / "synonyms.json")))]
    try:
        sources.insert(0, files("knowbase.data").joinpath("synonyms.json"))
    except (ModuleNotFoundError, TypeError):
        pass
    for source in sources:
        try:
            with source.open("r", encoding="utf-8") as stream:
                raw = stream.read(262145)
            if len(raw) > 262144:
                continue
            data = json.loads(raw)
            if not isinstance(data, dict):
                continue
            for phrase, aliases in list(data.items())[:256]:
                if (not isinstance(phrase, str) or not 1 <= len(phrase) <= 64 or
                        not isinstance(aliases, list)):
                    continue
                values = [a for a in aliases[:6] if isinstance(a, str) and 1 <= len(a) <= 64]
                if values:
                    merged[phrase] = tuple(values)
        except (OSError, ValueError, TypeError, AttributeError, ModuleNotFoundError):
            continue
    return merged


def _expanded_lexical_ids(conn: sqlite3.Connection, query: str,
                          base: dict[str, float] | None, aliases=None) -> dict[str, float]:
    """原查询精确召回 + 受控同义短语扩展；扩展项使用 OR，不改变原词高优先级。"""
    scores = dict(base or {})
    terms = []
    for phrase, values in (aliases if aliases is not None else _load_aliases()).items():
        if phrase in query:
            terms.extend(values)
    # 长中文提问至少保留首个有信息量的 2~4 字片段，如“日报”。
    for run in re.findall(r"[\u4e00-\u9fff]{2,}", query):
        cleaned = re.sub(r"(怎么写|怎么办|如何|一下吗|一下)$", "", run)
        if len(cleaned) >= 2:
            terms.append(cleaned)
    for term in list(dict.fromkeys(terms))[:6]:
        hit = _match_ids(conn, [term]) or _like_ids(conn, [term])
        for mid, score in hit.items():
            scores[mid] = max(scores.get(mid, 0.0), score * 0.7)
    return scores


def _rrf_scores(*rankings: dict[str, float], k: int = 60) -> dict[str, float]:
    fused: dict[str, float] = {}
    for ranking in rankings:
        ordered = sorted(ranking, key=lambda mid: -ranking[mid])
        for rank, mid in enumerate(ordered, 1):
            fused[mid] = fused.get(mid, 0.0) + 1.0 / (k + rank)
    return fused


def search(conn: sqlite3.Connection, query: str, mtype: str | None = None,
           scope: str | None = None, tag: str | None = None, limit: int = 5,
           include_inactive: bool = False, *, origin="local", diagnostics=None, deadline=None) -> list[dict]:
    """排序 = 词法相关度 × scope × confidence × 新鲜度 × 反馈。"""
    started=time.monotonic()
    deadline=deadline or started+1.2
    if diagnostics is None:diagnostics={}
    from .retrieval_policy import local_policy
    policy, aliases = local_policy()
    if diagnostics is not None:
        diagnostics.update(policy, origin=origin)
    if len(query or "") > 8192:
        if diagnostics is not None:
            diagnostics["reason"] = "query_too_large"
        return []
    terms = [t for t in (query or "").split() if t][:16]
    if not terms:
        return []

    before_revision = None
    if origin in {"mcp_search","mcp_context"}:
        before_revision=list(conn.execute("SELECT m.id,m.title,m.scope,m.confidence,m.status,m.staging,h.content_revision FROM meta m LEFT JOIN entity_heads h ON h.entity_id=m.id ORDER BY m.id"))
    # ≥3 字词走 FTS5 MATCH（OR 召回 + 命中词数加权），None 表示不可用/无命中
    lex = _match_ids(conn, terms) or {}
    # 2 字及以下短词（如「死锁」）trigram 无法命中，单独 LIKE 召回后并入词法分。
    # 原实现是在召回后再做「所有短词必须同时出现」的硬闸门，等价于 AND，
    # 会把「主体 + 现象/动作」型提问整体过滤成空结果。
    short_terms = [t for t in terms if len(t) < 3]
    if short_terms:
        for mid, score in _like_ids(conn, short_terms).items():
            lex[mid] = max(lex.get(mid, 0.0), score * 0.7)
    if not lex:
        # trigram 无命中时整体回退 LIKE（错别字/词形变化兜底）
        lex = _like_ids(conn, terms)
    if policy["level"] == "L2":
        lex = _expanded_lexical_ids(conn, query[:8192], lex, aliases)
    eligible = []
    for r in conn.execute("SELECT * FROM meta WHERE status != 'archived'"):
        m = _row_meta(r)
        if m["type"] == "reference":
            continue  # 原始材料（reference）退出默认检索；显式 type=reference 亦不返回（P0 止血）
        if not include_inactive and (m["staging"] or m["status"] != "active"):
            continue
        if mtype and m["type"] != mtype:
            continue
        if scope and m["scope"] not in (scope, "global"):
            continue
        if tag and tag not in m["tags"]:
            continue
        head=conn.execute("SELECT content_revision FROM entity_heads WHERE entity_id=?",(m["id"],)).fetchone()
        m["content_revision"]=head[0] if head else ""
        if origin in {"mcp_search","mcp_context","hook_prompt","hook_inflight"} and not source_current(conn,m["id"],m):
            continue
        eligible.append(m)
    allowed_ids = {m["id"] for m in eligible}
    lex = {mid: score for mid, score in (lex or {}).items() if mid in allowed_ids}
    dense = {}
    if origin in {"mcp_search","mcp_context"}:
        from . import retrieval_state
        dense=retrieval_state.dense(conn,query,allowed_ids,deadline=deadline-.05,diagnostics=diagnostics)
        # Schedule recovery/index progress without blocking this request or any Hook.
        retrieval_state.wake(Path(conn.execute("PRAGMA database_list").fetchone()[2]).parent)
    if before_revision is not None:
        after_revision=list(conn.execute("SELECT m.id,m.title,m.scope,m.confidence,m.status,m.staging,h.content_revision FROM meta m LEFT JOIN entity_heads h ON h.entity_id=m.id ORDER BY m.id"))
        if before_revision!=after_revision:
            fresh={}
            results=search(conn,query,mtype=mtype,scope=scope,tag=tag,limit=limit,
                           include_inactive=include_inactive,origin="local",diagnostics=fresh,deadline=deadline)
            diagnostics.update(level=fresh["level"],channels=["lexical"],reason="index_changed",semantic_status="local_after_revision_change")
            return results
    fused = _rrf_scores(lex,dense)
    if not fused:
        return []

    results = []
    for m in eligible:
        if m["id"] not in fused:
            continue
        score = fused[m["id"]] * _scope_factor(m, scope) \
            * (1.5 if m["confidence"] == "verified" else 1.0) \
            * _freshness_factor(m) * _feedback_factor(m)
        snippet = ""
        row = conn.execute(
            "SELECT snippet(mem_fts, 3, '「', '」', '…', 12) FROM mem_fts WHERE id=?",
            (m["id"],),
        ).fetchone()
        if row:
            snippet = row[0]
        channels = [name for name, ranking in (("lexical", lex), ("dense", dense)) if m["id"] in ranking]
        results.append({**m, "score": round(score, 6), "snippet": snippet,
                        "channels": channels})

    results.sort(key=lambda x: -x["score"])
    # Automatic callers use positive lexical evidence, not an uncalibrated RRF threshold.
    if origin.startswith("hook"):
        from .retrieval_policy import automatic_match
        results = [r for r in results if automatic_match(conn, query, r["id"])]
    diagnostics["total_ms"]=round((time.monotonic()-started)*1000,2)
    diagnostics["channels"]=[name for name,values in (("lexical",lex),("dense",dense)) if values]
    return results[:max(0, min(limit, 100))]


# ---------- 统计与日志 ----------

def record_usage(conn: sqlite3.Connection, tool: str, memory_id: str = "", detail: str = ""):
    if not config.load_config().get("stats", {}).get("enabled", True):
        return
    conn.execute(
        "INSERT INTO usage_log VALUES(?,?,?,?,?)",
        (datetime.now().isoformat(timespec="seconds"), config.agent_name(), tool, memory_id, detail),
    )
    conn.commit()


def bump_hit(conn: sqlite3.Connection, mid: str):
    conn.execute("UPDATE meta SET hit_count = hit_count + 1 WHERE id=?", (mid,))
    conn.commit()


def log_feedback(conn: sqlite3.Connection, mid: str, agent: str, outcome: str):
    conn.execute(
        "INSERT INTO feedback_log VALUES(?,?,?,?)",
        (datetime.now().isoformat(timespec="seconds"), mid, agent, outcome),
    )
    conn.commit()


def helpful_stats(conn: sqlite3.Connection, mid: str) -> tuple[int, int]:
    rows = conn.execute(
        "SELECT agent FROM feedback_log WHERE memory_id=? AND outcome='helpful'", (mid,)
    ).fetchall()
    return len(rows), len({r[0] for r in rows})


def stats(conn: sqlite3.Connection) -> dict:
    by_type = dict(conn.execute("SELECT type, COUNT(*) FROM meta GROUP BY type").fetchall())
    by_scope = dict(conn.execute("SELECT scope, COUNT(*) FROM meta GROUP BY scope").fetchall())
    by_status = dict(conn.execute("SELECT status, COUNT(*) FROM meta GROUP BY status").fetchall())
    week_ago = (datetime.now() - timedelta(days=7)).isoformat(timespec="seconds")
    funnel = {
        "search": conn.execute("SELECT COUNT(*) FROM usage_log WHERE tool='search'").fetchone()[0],
        "read": conn.execute("SELECT COUNT(*) FROM usage_log WHERE tool='read'").fetchone()[0],
        "save": conn.execute("SELECT COUNT(*) FROM usage_log WHERE tool='save'").fetchone()[0],
        "feedback": conn.execute("SELECT COUNT(*) FROM usage_log WHERE tool='feedback'").fetchone()[0],
        "auto_search": conn.execute("SELECT COUNT(*) FROM usage_log WHERE tool='auto_search'").fetchone()[0],
        "feedback_helpful": conn.execute(
            "SELECT COUNT(*) FROM feedback_log WHERE outcome='helpful'").fetchone()[0],
    }
    top = conn.execute(
        "SELECT id, title, hit_count, helpful_count FROM meta ORDER BY hit_count DESC, helpful_count DESC LIMIT 5"
    ).fetchall()
    return {
        "total": sum(by_type.values()), "by_type": by_type, "by_scope": by_scope,
        "by_status": by_status, "written_last_7d": conn.execute(
            "SELECT COUNT(*) FROM meta WHERE updated >= ?", (week_ago[:10],)).fetchone()[0],
        "funnel": funnel,
        "top": [{"id": t[0], "title": t[1], "hit": t[2], "helpful": t[3]} for t in top],
    }


# ---------- INDEX.md（orientation，不是检索入口） ----------

def build_index_md(repo: Path, cfg: dict | None = None) -> Path:
    cfg = cfg or config.load_config()
    repo = Path(repo)
    conn = connect(repo)
    today = date.today()

    counts = {"by_type": {}, "total": 0, "staging": 0}
    groups: dict[str, list[str]] = {}
    staging_lines: list[str] = []
    archived = 0
    references = 0

    for meta, _body, path in store.iter_all(repo, include_staging=True):
        if meta.get("status") == "archived":
            archived += 1
            continue  # 速览不列归档条目（检索同样已排除）
        if meta.get("type") == "reference":
            references += 1
            continue  # 原始材料退出速览与默认检索，待 P3 迁移为 source
        counts["total"] += 1
        counts["by_type"][meta.get("type", "?")] = counts["by_type"].get(meta.get("type", "?"), 0) + 1
        r = conn.execute("SELECT hit_count FROM meta WHERE id=?", (meta["id"],)).fetchone()
        hit = r[0] if r else 0
        flags = f"{meta.get('confidence', '?')}·{meta.get('status', '?')}"
        tags = ",".join((meta.get("tags") or [])[:3])
        line = f"- [{meta['id']}] {meta.get('title', '')}（{flags}" + (f" · {tags}" if tags else "") + "）"
        # once 且超期无命中 → 待复核
        try:
            age = (today - date.fromisoformat(str(meta.get("created")))).days
        except ValueError:
            age = 0
        if meta.get("confidence") == "once" and hit == 0 and age > STALE_REVIEW_DAYS:
            line = "- ⚠️待复核 " + line[2:]
        if path.parent.name == "staging":
            staging_lines.append(line)
            counts["staging"] += 1
        else:
            groups.setdefault(meta.get("scope", "global"), []).append(line)
    conn.close()

    type_summary = " ".join(f"{store.TYPE_DIR[t]}{n}" for t, n in sorted(counts["by_type"].items()))
    lines = [
        "# knowbase 记忆索引（薪火 · 自动生成，勿手改）",
        "",
        f"> 共 {counts['total']} 条（{type_summary}），staging 提案 {counts['staging']} 条"
        f"{'，已归档 ' + str(archived) + ' 条（不列出）' if archived else ''}"
        f"{'，references ' + str(references) + ' 条（原始材料，已退出默认检索待迁移）' if references else ''}。"
        f"生成时间 {datetime.now().isoformat(timespec='seconds')}。",
        "> 本文件仅作速览（orientation）。检索一律走 memory_search，不要整读本文件。",
        "",
    ]
    ordered_scopes = sorted(groups.items(), key=lambda kv: (kv[0] != "global", kv[0]))
    for scope_name, scope_lines in ordered_scopes:
        lines.append(f"## {scope_name}")
        lines.extend(scope_lines)
        lines.append("")
    if staging_lines:
        lines.append("## staging（提案待人工审核）")
        lines.extend(staging_lines)
        lines.append("")

    max_lines = int(cfg.get("index", {}).get("max_lines", 300))
    if len(lines) > max_lines:
        kept = lines[:max_lines - 1]
        kept.append("- …（已截断，完整检索请用 memory_search）")
        lines = kept

    out = repo / "INDEX.md"
    # 本地视图与 .gitattributes 声明一致，固定 LF 避免跨平台抖动
    out.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return out


def record_search(conn, query, scope, results, tool="search", *,
                  query_class=None, raw_len=None, retrieval=None):
    """一条事件保存整次查询及最终返回列表，包括零命中；分数不是概率。

    可选字段（Phase 1 遥测扩展，旧事件按缺失读）：query_class=
    raw/distilled/skipped:<原因>；raw_len=进入卫生层前的原始长度。
    """
    from uuid import uuid4
    payload = {"search_id": str(uuid4()), "query": query, "scope": scope,
               "hits": [{"id": h["id"], "title": h["title"], "rank": i + 1,
                         "score": h["score"], "confidence": h["confidence"],
                         "status": h["status"]} for i, h in enumerate(results)]}
    if query_class is not None:
        payload["query_class"] = query_class
    if raw_len is not None:
        payload["raw_len"] = raw_len
    if retrieval is not None:
        payload["retrieval"] = retrieval
    # A busy metrics store must not turn successful retrieval into a blocked tool.
    if conn.in_transaction:
        savepoint="retrieval_search_log"
        try:
            conn.execute("SAVEPOINT "+savepoint)
            if config.load_config().get("stats",{}).get("enabled",True):
                conn.execute("INSERT INTO usage_log VALUES(?,?,?,?,?)",(datetime.now().isoformat(timespec="seconds"),config.agent_name(),tool,"",json.dumps(payload,ensure_ascii=False)))
                if retrieval is not None:
                    from .retrieval_metrics import observe
                    observe(conn,payload["search_id"],retrieval.get("origin",tool),{**retrieval,"query_class":query_class,"result_count":len(results)})
            conn.execute("RELEASE "+savepoint)
        except sqlite3.Error:
            conn.execute("ROLLBACK TO "+savepoint);conn.execute("RELEASE "+savepoint)
        return payload["search_id"]
    old_busy=conn.execute("PRAGMA busy_timeout").fetchone()[0]
    try:
        conn.execute("PRAGMA busy_timeout=50")
        record_usage(conn, tool, detail=json.dumps(payload, ensure_ascii=False))
        if retrieval is not None:
            from .retrieval_metrics import observe
            observe(conn,payload["search_id"],retrieval.get("origin",tool),
                    {**retrieval,"query_class":query_class,"result_count":len(results)})
    except sqlite3.Error:
        conn.rollback()
    finally:
        conn.execute("PRAGMA busy_timeout="+str(int(old_busy)))
    return payload["search_id"]


def record_source_state(conn, source_path: str, source_hash: str,
                        modified_at: str = "", status: str = "indexed", error: str = ""):
    conn.execute(
        "INSERT INTO source_state(source_path,source_hash,modified_at,indexed_at,status,error) "
        "VALUES(?,?,?,?,?,?) ON CONFLICT(source_path) DO UPDATE SET "
        "source_hash=excluded.source_hash,modified_at=excluded.modified_at,"
        "indexed_at=excluded.indexed_at,status=excluded.status,error=excluded.error",
        (source_path, source_hash, modified_at,
         datetime.now().isoformat(timespec="seconds"), status, error or None),
    )
    conn.commit()


def record_sync_state(conn, remote: str, *, local_revision: str = "",
                      remote_revision: str = "", last_fetch_at: str | None = None,
                      last_push_at: str | None = None, status: str = "ok", error: str = ""):
    conn.execute(
        "INSERT INTO sync_state(remote,local_revision,remote_revision,last_fetch_at,last_push_at,status,error) "
        "VALUES(?,?,?,?,?,?,?) ON CONFLICT(remote) DO UPDATE SET "
        "local_revision=excluded.local_revision,remote_revision=excluded.remote_revision,"
        "last_fetch_at=COALESCE(excluded.last_fetch_at,sync_state.last_fetch_at),"
        "last_push_at=COALESCE(excluded.last_push_at,sync_state.last_push_at),"
        "status=excluded.status,error=excluded.error",
        (remote, local_revision or None, remote_revision or None,
         last_fetch_at, last_push_at, status, error or None),
    )
    conn.commit()


def search_history(conn, limit=100):
    rows = conn.execute("SELECT ts, agent, tool, detail FROM usage_log WHERE tool IN ('search','auto_search') ORDER BY rowid DESC LIMIT ?", (limit,))
    out = []
    for ts, agent, tool, detail in rows:
        try:
            event = json.loads(detail)
            if not isinstance(event, dict):
                raise ValueError()
        except (ValueError, TypeError):
            event = {"query": detail, "hits": None}
        out.append({**event, "ts": ts, "agent": agent, "tool": tool})
    return out
