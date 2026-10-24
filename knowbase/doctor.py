"""Read-only diagnostics: no migrations, index rebuild, network or telemetry writes."""
import hashlib
from contextlib import closing
import json
import sqlite3
import shutil
import importlib.util
from pathlib import Path

from . import config, gitops, index, migrations, store


def inspect(repo):
    checks = []
    def add(name, level, message):
        checks.append({"name": name, "level": level, "message": message})
    path = index.db_path(repo)
    if not path.exists():
        add("memory_db", "fail", "不存在；请运行 knowbase init")
        return checks
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            conn.execute("PRAGMA query_only=ON")
            check = conn.execute("PRAGMA quick_check").fetchone()[0]
            add("integrity", "pass" if check == "ok" else "fail", check)
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
            needed = {"meta", "mem_fts", "schema_migrations", "entity_heads"}
            if not needed <= tables:
                add("schema", "fail", "缺少表：" + ",".join(sorted(needed - tables)))
                return checks
            applied = dict(conn.execute("SELECT version,checksum FROM schema_migrations"))
            expected = {version: hashlib.sha256(sql.encode()).hexdigest()[:16] for version, sql in migrations.MIGRATIONS}
            bad = [str(v) for v, checksum in applied.items() if v in expected and expected[v] != checksum]
            missing = sorted(set(expected) - set(applied))
            add("migrations", "fail" if bad or missing else "pass",
                f"version={max(applied, default=0)}; checksum_mismatch={bad}; pending={missing}")
            meta_n = conn.execute("SELECT count(*) FROM meta").fetchone()[0]
            fts_n = conn.execute("SELECT count(*) FROM mem_fts").fetchone()[0]
            cards = list(store.iter_all(repo, include_staging=True))
            md_n = len(cards)
            missing_fts = conn.execute("SELECT count(*) FROM meta WHERE id NOT IN (SELECT id FROM mem_fts)").fetchone()[0]
            conn.execute("SELECT id FROM mem_fts WHERE mem_fts MATCH 'doctor' LIMIT 1").fetchall()
            add("index_parity", "pass" if md_n == meta_n == fts_n and not missing_fts else "fail",
                f"markdown={md_n} meta={meta_n} fts={fts_n}")
            from .mutations import _compute_revision
            content_mismatch = 0
            for meta, body, _ in cards:
                row = conn.execute("SELECT title,tags,body FROM mem_fts WHERE id=?", (meta["id"],)).fetchone()
                code = index._code_refs_text(meta)
                expected_body = f"{body.rstrip()}\n{code}\n" if code else body
                expected_fts = (meta.get("title", ""), " ".join(meta.get("tags", [])), expected_body)
                head = conn.execute("SELECT content_revision FROM entity_heads WHERE entity_id=?", (meta["id"],)).fetchone()
                content_mismatch += row != expected_fts or not head or head[0] != _compute_revision(meta, body)
            add("content_parity", "fail" if content_mismatch else "pass", f"mismatched_cards={content_mismatch}")
            stale = conn.execute("SELECT count(*) FROM meta WHERE status='stale'").fetchone()[0]
            add("governance", "warn" if stale else "pass", f"stale={stale}")
            from .retrieval_policy import local_policy
            policy, _ = local_policy()
            add("retrieval", "warn" if policy["reason"] else "pass", json.dumps(policy, ensure_ascii=False))
            if {"embedding_generations","retrieval_active","retrieval_provider_state"} <= tables:
                from .retrieval_state import snapshot
                from .embedding_provider import ProviderError
                try:observed=snapshot(conn)
                except (ProviderError,ValueError,TypeError):observed={"configured":True,"quality":False,"reason":"invalid_model_configuration"}
                add("embedding_service", "warn" if observed["configured"] and not observed["quality"] else "pass",json.dumps(observed,ensure_ascii=False))
                add("retrieval_effective", "pass", "Hook最高L2；主动L3以本次请求健康/覆盖/质量判定为准；默认doctor不触网")
            if "embedding_index" in tables:
                models=list(conn.execute("SELECT model_version,count(*) FROM embedding_index WHERE generation_id='' GROUP BY model_version")) if "embedding_generations" in tables else list(conn.execute("SELECT model_version,count(*) FROM embedding_index GROUP BY model_version"))
                add("legacy_vectors","pass",json.dumps({"ignored_in_search":models},ensure_ascii=False))
            if config.load_config().get("inflight", {}).get("enabled"):
                if {"jobs", "job_submissions", "context_events"} <= tables:
                    pending = conn.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('awaiting_agent','validating','blocked')").fetchone()[0]
                    receipts = conn.execute("SELECT COUNT(*) FROM job_submissions WHERE state!='done'").fetchone()[0]
                    add("inflight_queue", "warn" if pending or receipts else "pass", f"pending_jobs={pending}; unapplied_receipts={receipts}")
                    events = dict(conn.execute("SELECT status,COUNT(*) FROM context_events GROUP BY status"))
                    add("inflight_events", "pass", json.dumps(events))
                else:
                    add("inflight_queue", "fail", "required tables missing")
            if "sync_state" in tables:
                row = conn.execute("SELECT status FROM sync_state ORDER BY rowid DESC LIMIT 1").fetchone()
                add("sync", "pass" if row and row[0] == "ok" else "warn", row[0] if row else "unknown")
    except (sqlite3.Error, OSError, ValueError) as exc:
        add("memory_db", "fail", type(exc).__name__)
    try:
        lines = gitops._run(repo, "status", "--porcelain", "--branch", timeout=5).splitlines()
        add("git", "warn" if len(lines)>1 else "pass", f"{lines[0] if lines else 'unknown'}; dirty={max(0,len(lines)-1)}")
    except Exception as exc:
        add("git", "fail", type(exc).__name__)
    missing = [name for name in ("pypdf", "docx", "openpyxl", "pptx", "PIL", "pytesseract") if importlib.util.find_spec(name) is None]
    add("parsers", "warn" if missing else "pass", "missing=" + ",".join(missing))
    add("ocr_binary", "pass" if shutil.which("tesseract") else "warn", "available" if shutil.which("tesseract") else "tesseract missing")
    from .integration import discover
    for host in discover():
        add("agent:" + host["id"], "pass" if host["configured"] else "warn", json.dumps(host, ensure_ascii=False))
    return checks
