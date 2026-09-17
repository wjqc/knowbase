"""派生索引层：SQLite FTS5（trigram）+ 统计 + INDEX.md。

全部数据可由 markdown 全量重建（knowbase reindex），损坏即重建。
hit_count 属运行统计，只存本库不入 frontmatter（避免读操作产生 git 提交）。
"""

import json
import sqlite3
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
    """)
    conn.commit()
    return conn


# ---------- 写入与重建 ----------

def upsert(conn: sqlite3.Connection, meta: dict, body: str, path: Path, staging: bool = False):
    conn.execute("DELETE FROM mem_fts WHERE id=?", (meta["id"],))
    conn.execute(
        "INSERT INTO mem_fts(id, title, tags, body) VALUES(?,?,?,?)",
        (meta["id"], meta.get("title", ""), " ".join(meta.get("tags", [])), body),
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
    conn.commit()


def rebuild(repo: Path) -> int:
    """全量重建（markdown → db + INDEX.md）。返回记忆条数。"""
    repo = Path(repo)
    for suffix in ("", "-wal", "-shm"):
        p = db_path(repo).with_name(DB_NAME + suffix)
        if p.exists():
            p.unlink()
    conn = connect(repo)
    n = 0
    for meta, body, path in store.iter_all(repo, include_staging=True):
        upsert(conn, meta, body, path, staging=("staging" in str(path)))
        n += 1
    conn.close()
    build_index_md(repo)
    return n


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
    """≥3 字词走 FTS5 MATCH（trigram），返回 {id: 词法分}；不可用返回 None。"""
    big = [t.replace('"', "") for t in terms if len(t) >= 3]
    if not big:
        return None
    match = " AND ".join(f'"{t}"' for t in big)
    try:
        rows = conn.execute(
            "SELECT id, rank FROM mem_fts WHERE mem_fts MATCH ? ORDER BY rank LIMIT 200",
            (match,),
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    return {r[0]: -r[1] for r in rows} or None


def _like_ids(conn: sqlite3.Connection, terms: list[str]) -> dict[str, float]:
    """短词 / MATCH 未命中的回退：LIKE 扫描。"""
    conds = " AND ".join(["(title LIKE ? OR tags LIKE ? OR body LIKE ?)"] * len(terms))
    params = [f"%{t}%" for t in terms for _ in range(3)]
    rows = conn.execute(f"SELECT id FROM mem_fts WHERE {conds}", params).fetchall()
    return {r[0]: 1.0 for r in rows}


def search(conn: sqlite3.Connection, query: str, mtype: str | None = None,
           scope: str | None = None, tag: str | None = None, limit: int = 5) -> list[dict]:
    """排序 = 词法相关度 × scope × confidence × 新鲜度 × 反馈。"""
    terms = [t for t in (query or "").split() if t]
    if not terms:
        return []

    lex = _match_ids(conn, terms)
    short_terms = [t for t in terms if len(t) < 3]
    if lex is None:
        lex = _like_ids(conn, terms)
    if not lex:
        # trigram 无命中时整体回退 LIKE（错别字/词形变化兜底）
        lex = _like_ids(conn, terms)
    if not lex:
        return []

    results = []
    for r in conn.execute("SELECT * FROM meta WHERE status != 'archived'"):
        m = _row_meta(r)
        if m["id"] not in lex:
            continue
        if mtype and m["type"] != mtype:
            continue
        if scope and m["scope"] not in (scope, "global"):
            continue
        if tag and tag not in m["tags"]:
            continue
        if short_terms and lex is not None:
            row = conn.execute(
                "SELECT title, tags, body FROM mem_fts WHERE id=?", (m["id"],)
            ).fetchone()
            hay = " ".join(row).lower() if row else ""
            if not all(t.lower() in hay for t in short_terms):
                continue
        score = (lex[m["id"]] or 1.0) * _scope_factor(m, scope) \
            * (1.5 if m["confidence"] == "verified" else 1.0) \
            * _freshness_factor(m) * _feedback_factor(m)
        snippet = ""
        row = conn.execute(
            "SELECT snippet(mem_fts, 3, '「', '」', '…', 12) FROM mem_fts WHERE id=?",
            (m["id"],),
        ).fetchone()
        if row:
            snippet = row[0]
        results.append({**m, "score": round(score, 4), "snippet": snippet})

    results.sort(key=lambda x: -x["score"])
    return results[:limit]


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

    for meta, _body, path in store.iter_all(repo, include_staging=True):
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
        if "staging" in str(path):
            staging_lines.append(line)
            counts["staging"] += 1
        else:
            groups.setdefault(meta.get("scope", "global"), []).append(line)
    conn.close()

    type_summary = " ".join(f"{store.TYPE_DIR[t]}{n}" for t, n in sorted(counts["by_type"].items()))
    lines = [
        "# knowbase 记忆索引（薪火 · 自动生成，勿手改）",
        "",
        f"> 共 {counts['total']} 条（{type_summary}），staging 提案 {counts['staging']} 条。"
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
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out
