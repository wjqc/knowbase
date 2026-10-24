"""共享知识卡写入服务：MCP save/update 和候选应用共用校验、intent、索引和 Git。"""

from datetime import date
from pathlib import Path

from . import config, gitops, index, store, commit_adapter
from .locking import RepoLock

GOVERNED = frozenset({"standard", "preference", "bizrule"})


def _readback(conn, meta, body, staging):
    from .mutations import _compute_revision
    head = conn.execute("SELECT content_revision,governance_revision FROM entity_heads WHERE entity_id=?",
                        (meta["id"],)).fetchone()
    code_text = index._code_refs_text(meta)
    expected_body = f"{body.rstrip()}\n{code_text}\n" if code_text else body
    fts = conn.execute("SELECT title,tags,body FROM mem_fts WHERE id=?", (meta["id"],)).fetchone()
    record = conn.execute("SELECT scope,staging FROM meta WHERE id=?", (meta["id"],)).fetchone()
    if (not head or head[0] != _compute_revision(meta, body)
            or fts != (meta.get("title", ""), " ".join(meta.get("tags", [])), expected_body)
            or record != (meta.get("scope"), int(staging))):
        return {"error": "READBACK_MISMATCH", "entity_id": meta["id"], "retryable": True}
    return {"content_revision": head[0], "governance_revision": head[1]}


def create(repo, dtype: str, title: str, body: str, *, scope: str, tags=None,
           relations=None, evidence=None, code_refs=None, source_refs=None,
           provenance=None, domain=None, rule_status=None, rule_context=None,
           source=None, request_id: str = "", push: bool = True) -> dict:
    if dtype not in store.TYPES or dtype == "reference":
        return {"error": "INVALID_TYPE", "message": "reference 已退出知识类型；原始材料使用 knowbase import"}
    if not title.strip() or not body.strip():
        return {"error": "INVALID_CARD", "message": "title 与 body 必填"}
    if source and source.lower().startswith("human:"):
        source = None
    src = source or f"agent:{config.agent_name()}:adhoc"
    meta = store.new_meta(dtype, title, scope, tags or [], src, relations, evidence, code_refs)
    extra = {k: v for k, v in {"provenance": provenance, "domain": domain, "rule_status": rule_status,
                               "rule_context": rule_context, "source_refs": source_refs}.items() if v is not None}
    meta.update(extra)
    errors = store.lint(meta, body, repo)
    if errors:
        return {"error": "LINT_FAILED", "errors": errors}
    cfg = config.load_config()
    conn = index.connect(repo)
    try:
        result = commit_adapter.adapter_create(repo, conn, dtype, title, body, scope=scope,
            tags=tags or [], source=src, relations=relations, evidence=evidence, code_refs=code_refs,
            request_id=request_id, lock_timeout=cfg.get("lock_timeout", 10.0), **extra)
        if result.get("error"):
            return result
        mid = result["entity_id"]
        with RepoLock(repo, cfg.get("lock_timeout", 10.0)):
            # 锁内重读快照：adapter 的锁已释放，两段锁之间其他写入者可能
            # 已更新同一张卡；若用锁外读到的快照回写索引，会把索引降级
            # 到旧版本（两段锁竞态），必须在临界区内取最新状态。
            current, saved_body, path = store.load(repo, mid)
            if not current or path is None:
                return {"error": "READBACK_FAILED", "entity_id": mid, "retryable": True}
            notes, modified = _relation_side_effects(repo, current)
            if not result.get("idempotent"):
                index.record_usage(conn, "save", mid, title)
            git_warning = _post_save(repo, cfg, current, saved_body, path,
                                    path.parent.name == "staging", f"memory({mid}): {title} [{src}]", modified)
        checked = _readback(conn, current, saved_body, path.parent.name == "staging")
        if checked.get("error"):
            return checked
        result.update(checked)
        result.update(path=str(path),
                      staging=path.parent.name == "staging", notes=notes,
                      warnings=store.lint_warnings(current, saved_body), git_warning=git_warning,
                      local_state="local_saved", sync_state="pending" if cfg["git"].get("auto_push") else "disabled")
    finally:
        conn.close()
    if push:
        warning = _push_after_write(repo, cfg, git_warning)
        result["push_warning"] = warning
        if not warning and not git_warning and cfg["git"].get("auto_push"):
            result["sync_state"] = "synced"
    return result


def update(repo, mid: str, *, request_id="", expected_content_revision="",
           expected_governance_revision="", push=True, **updates) -> dict:
    meta, old_body, path = store.load(repo, mid)
    if not meta:
        return {"error": "not_found"}
    if meta.get("type") in GOVERNED and path.parent.name != "staging":
        return {"error": "GOVERNANCE_DENIED", "message": "已生效规则须由人工 knowbase revise 修订"}
    preview = {**meta, **{k: v for k, v in updates.items() if k != "body"}}
    errors = store.lint(preview, updates.get("body", old_body), repo)
    if errors:
        return {"error": "LINT_FAILED", "errors": errors}
    cfg = config.load_config()
    conn = index.connect(repo)
    try:
        result = commit_adapter.adapter_update(repo, conn, mid, request_id=request_id,
            expected_content_revision=expected_content_revision,
            expected_governance_revision=expected_governance_revision,
            lock_timeout=cfg.get("lock_timeout", 10.0), **updates)
        if result.get("error"):
            return result
        with RepoLock(repo, cfg.get("lock_timeout", 10.0)):
            # 锁内重读快照（同 create：防两段锁竞态把索引降级到旧版本）。
            current, body, path = store.load(repo, mid)
            if not current:
                return {"error": "READBACK_FAILED", "retryable": True}
            notes, modified = _relation_side_effects(repo, current)
            if not result.get("idempotent"):
                index.record_usage(conn, "update", mid)
            git_warning = _post_save(repo, cfg, current, body, path, path.parent.name == "staging",
                                    f"update({mid}): {current['title']}", modified)
        checked = _readback(conn, current, body, path.parent.name == "staging")
        if checked.get("error"):
            return checked
        result.update(checked)
        result.update(entity_id=mid, path=str(path),
                      staging=path.parent.name == "staging", notes=notes, git_warning=git_warning,
                      local_state="local_saved", sync_state="pending" if cfg["git"].get("auto_push") else "disabled")
    finally:
        conn.close()
    if push:
        result["push_warning"] = _push_after_write(repo, cfg, git_warning)
    return result

def _post_save(rp, cfg, meta, body, path, staging, commit_msg, modified=()):
    """锁内本地事务：同步索引 → INDEX → git commit。远程 push 必须在锁外。"""
    conn = index.connect(rp)
    index.upsert(conn, meta, body, path, staging)
    for target_meta, target_body, target_path in modified:
        index.upsert(conn, target_meta, target_body, target_path,
                     target_path.parent.name == "staging")
    conn.close()
    index.build_index_md(rp, cfg)  # 本地重建，不提交
    git_warn = None
    if cfg["git"].get("auto_commit", True):
        # INDEX.md 已退役，不再提交到 Git（本地重建供速览）
        owned_paths = [path] + [item[2] for item in modified]
        git_warn = gitops.commit_paths(rp, commit_msg, owned_paths)
    return git_warn


def _push_after_write(rp, cfg, git_warn):
    """本地写锁释放后执行自动收敛与 push；网络失败不回滚本地提交。"""
    if git_warn or not cfg["git"].get("auto_push"):
        return None
    return gitops.schedule_push(rp, cfg["git"])


def _relation_side_effects(rp, meta):
    """supersedes：被取代条目自动标 stale；
    contradicts：被矛盾方 relations 自动回指形成双向标记（状态不动，由人裁决）。
    返回 (notes, modified)：modified 为被改动的目标记忆，调用方须同步索引。"""
    notes, modified = [], []
    today = date.today().isoformat()
    for r in meta.get("relations") or []:
        rid, rtype = r.get("id"), r.get("type")
        if not rid or rid == meta["id"]:
            continue
        tmeta, tbody, tpath = store.load(rp, rid)
        if not tmeta:
            notes.append(f"警告：关系指向的 {rid} 不存在")
            continue
        if rtype == "supersedes" and tmeta.get("status") == "active":
            tmeta["status"] = "stale"
            tmeta["updated"] = today
            tpath.write_text(store.render(tmeta, tbody), encoding="utf-8")
            modified.append((tmeta, tbody, tpath))
            notes.append(f"{rid} 已因被取代标记为 stale")
        elif rtype == "contradicts":
            rels = tmeta.get("relations") or []
            if not any(x.get("id") == meta["id"] and x.get("type") == "contradicts" for x in rels):
                tmeta["relations"] = rels + [{"id": meta["id"], "type": "contradicts"}]
                tmeta["updated"] = today
                tpath.write_text(store.render(tmeta, tbody), encoding="utf-8")
                modified.append((tmeta, tbody, tpath))
            notes.append(f"注意：与 {rid} 存在矛盾关系（{tmeta.get('title','')}），已双向标记，建议人工裁决")
    return notes, modified


