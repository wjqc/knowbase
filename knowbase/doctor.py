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
            parse_errors: list = []
            cards = list(store.iter_all(repo, include_staging=True, errors=parse_errors))
            # 坏卡（frontmatter/正文解析失败）会被 iter_all 跳过：若从未入库，
            # markdown/meta/fts 三边计数仍然自洽、index_parity 照样 pass，
            # 坏卡对全系统不可见。这里独立成一项检查，显性列出文件与错误。
            if parse_errors:
                detail = "; ".join(
                    f"{p.relative_to(repo) if p.is_relative_to(repo) else p}: "
                    f"{type(e).__name__}: {e}" for p, e in parse_errors[:10])
                add("unparseable_cards", "fail",
                    f"count={len(parse_errors)}; {detail}")
            else:
                add("unparseable_cards", "pass", "count=0")
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
            # Phase 1 自进化清单（只读呈现，不写库）：
            archived = [(m["id"], m.get("archived_at", ""), m.get("archive_reason", ""),
                         m.get("last_evidence_summary")) for m, _, _ in cards if m.get("status") == "archived"]
            add("archive_ledger", "pass", json.dumps(
                [{"id": i, "archived_at": a, "reason": r, "last_evidence": e} for i, a, r, e in archived],
                ensure_ascii=False))
            quarantined = [(m["id"], m.get("adjudication_note", "")) for m, _, _ in cards
                           if m.get("contradiction_quarantine")]
            add("contradiction_quarantine", "warn" if quarantined else "pass", json.dumps(
                [{"id": i, "note": n} for i, n in quarantined], ensure_ascii=False))
            # B 轻量路线：同卡并发冲突隔离区（card_conflicts 表，迁移 v23）。
            if "card_conflicts" in tables:
                open_rows = conn.execute(
                    "SELECT conflict_id,card_id,status,source FROM card_conflicts "
                    "WHERE status='open' ORDER BY created_at").fetchall()
                add("card_conflicts", "warn" if open_rows else "pass", json.dumps(
                    [{"conflict_id": c, "card_id": i, "status": s, "source": src}
                     for c, i, s, src in open_rows], ensure_ascii=False))
            # 双崩溃遗留边界：同一张卡多条 pending intent 时恢复不猜测归属，
            # 这里显性报告，供人工/客户端重试跟进。
            if "write_intents" in tables:
                multi = conn.execute(
                    "SELECT entity_id,COUNT(*) FROM write_intents WHERE status='pending' "
                    "AND entity_id != '' AND kind IN ('update','feedback') "
                    "GROUP BY entity_id HAVING COUNT(*)>1").fetchall()
                add("ambiguous_pending_intents", "warn" if multi else "pass", json.dumps(
                    [{"entity_id": e, "pending": n} for e, n in multi], ensure_ascii=False))
            if "candidates" in tables:
                discarded = conn.execute("SELECT candidate_id,job_id,candidate_key,created_at,updated_at "
                                         "FROM candidates WHERE decision='discarded'").fetchall()
                add("review_discard_ledger", "pass", json.dumps(
                    [{"candidate_id": c, "job_id": j, "candidate_key": k, "created_at": cr, "discarded_at": u}
                     for c, j, k, cr, u in discarded], ensure_ascii=False))
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
            # P4：项目识别绑定（迁移 v27 的 project_bindings 表）。
            try:
                from .scope_binding import list_bindings
                bindings = list_bindings(conn)
                confirmed = [b for b in bindings if b["confirmed"]]
                unconfirmed = [b for b in bindings if not b["confirmed"]]
                add("scope_bindings", "pass", json.dumps(
                    {"confirmed": len(confirmed), "unconfirmed_candidates": len(unconfirmed),
                     "candidates": [{"project_path": b["project_path"], "source": b["source"]}
                                    for b in unconfirmed[:10]]}, ensure_ascii=False))
            except Exception as exc:
                add("scope_bindings", "warn", type(exc).__name__)
            # P4：治理维护状态（只读快照：应归档/应清理/待恢复/未决冲突）。
            try:
                from .maintenance import maintenance_status
                add("maintenance", "pass", json.dumps(
                    maintenance_status(repo, conn), ensure_ascii=False))
            except Exception as exc:
                add("maintenance", "warn", type(exc).__name__)
            # P4：证据包待确认（value_outbox 未 ACK 的事件与最长账龄）。
            try:
                if "value_outbox" in tables:
                    rows = conn.execute(
                        "SELECT state,COUNT(*),MIN(created_at) FROM value_outbox "
                        "GROUP BY state").fetchall()
                    by_state = {s: n for s, n, _ in rows}
                    oldest = min((c for _, _, c in rows if c), default=None)
                    add("pending_ack", "warn" if by_state.get("pending") or by_state.get("leased") else "pass",
                        json.dumps({"by_state": by_state, "oldest_created_at": oldest}, ensure_ascii=False))
                else:
                    add("pending_ack", "pass", "value_outbox 不存在")
            except Exception as exc:
                add("pending_ack", "warn", type(exc).__name__)
            # P4：证据关联覆盖率（P0 的递送/采用链；P0 未合入时表不存在，降级报告）。
            try:
                if {"retrieval_deliveries", "use_retrieval_links", "knowledge_uses"} <= tables:
                    total = conn.execute("SELECT COUNT(DISTINCT use_id) FROM knowledge_uses").fetchone()[0]
                    linked = conn.execute("SELECT COUNT(DISTINCT use_id) FROM use_retrieval_links").fetchone()[0]
                    add("evidence_link_coverage", "pass", json.dumps(
                        {"uses": total, "linked_uses": linked,
                         "coverage": (linked / total) if total else None}, ensure_ascii=False))
                else:
                    add("evidence_link_coverage", "pass", "P0 递送表不存在（P0 未合入），覆盖率不可用")
            except Exception as exc:
                add("evidence_link_coverage", "warn", type(exc).__name__)
            # P4：同步确认（最新 sync_state 行：远端目标与确认版本）。
            try:
                if "sync_state" in tables:
                    row = conn.execute(
                        "SELECT remote,local_revision,remote_revision,status,last_push_at "
                        "FROM sync_state ORDER BY rowid DESC LIMIT 1").fetchone()
                    if row:
                        add("sync_confirmed", "pass", json.dumps(
                            {"remote": row[0], "local_revision": row[1],
                             "remote_revision": row[2], "status": row[3],
                             "last_push_at": row[4]}, ensure_ascii=False))
                    else:
                        add("sync_confirmed", "pass", "无同步记录")
                else:
                    add("sync_confirmed", "pass", "sync_state 不存在")
            except Exception as exc:
                add("sync_confirmed", "warn", type(exc).__name__)
            # P4：worker 租约（P3 的 automation_lease 表；P3 未合入时降级报告；绝不输出 fencing_token）。
            try:
                if "automation_lease" in tables:
                    row = conn.execute(
                        "SELECT owner,lease_until FROM automation_lease ORDER BY rowid DESC LIMIT 1").fetchone()
                    if row:
                        add("worker_lease", "pass", json.dumps(
                            {"owner": row[0], "lease_until": row[1]}, ensure_ascii=False))
                    else:
                        add("worker_lease", "pass", "无租约记录")
                else:
                    add("worker_lease", "pass", "automation_lease 不存在（P3 未合入），worker 未安装")
            except Exception as exc:
                add("worker_lease", "warn", type(exc).__name__)
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
    # P4：显式激活策略（类型/范围/策略版本与总开关）。
    try:
        from .evolution import activation_policy
        ap = activation_policy()
        evo = config.load_config().get("evolution", {})
        add("activation_policy", "pass", json.dumps(
            {"policy_revision": ap["policy_revision"], "allowed_types": ap["allowed_types"],
             "allowed_scopes": ap["allowed_scopes"],
             "master_switch": bool(evo.get("auto_activate_bizrules", True)),
             "evolution_enabled": bool(evo.get("enabled", True))}, ensure_ascii=False))
    except Exception as exc:
        add("activation_policy", "warn", type(exc).__name__)
    # P4：会话状态（当前 build 已加载的宿主与需重连的旧 build 会话）。
    try:
        from .build_identity import build_id
        from .sessions import sessions_by_agent
        grouped = sessions_by_agent(build_id())
        add("session_states", "warn" if grouped["stale"] else "pass", json.dumps(
            {"loaded_current_build": sorted(grouped["current"]),
             "reconnect_required": sorted(grouped["stale"])}, ensure_ascii=False))
    except Exception as exc:
        add("session_states", "warn", type(exc).__name__)
    # P4：执行器能力（P1 的探针模块；P1 未合入时降级报告，不触网、不做隔离探针）。
    try:
        spec = importlib.util.find_spec("knowbase.automation.executor")
        if spec is None:
            add("executor_capability", "pass", "automation.executor 不存在（P1 未合入），执行器能力未知")
        else:
            from knowbase.automation import executor as _executor
            found = _executor.discover_executors() if hasattr(_executor, "discover_executors") else []
            add("executor_capability", "pass", json.dumps(
                {"discovered": found}, ensure_ascii=False)[:1000])
    except Exception as exc:
        add("executor_capability", "warn", type(exc).__name__)
    return checks
