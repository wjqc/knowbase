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
            bases_out = []
            notes, modified = _relation_side_effects(repo, current, conn, bases_out=bases_out)
            if not result.get("idempotent"):
                index.record_usage(conn, "save", mid, title)
            git_warning = _post_save(repo, cfg, current, saved_body, path,
                                    path.parent.name == "staging", f"memory({mid}): {title} [{src}]", modified,
                                    bases=[(mid, "none")] + bases_out)
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
    # 自进化：已生效受治理卡允许 Agent 修订，不再以 GOVERNANCE_DENIED 硬拒。
    # 修订即降权——内容变化时 adapter_update 会把 confidence 重置为 once 并
    # 清空验证与计数，verified 的帽子不会被改版后继续保留。治理字段仍由
    # 服务端状态机管理，调用方不可直接指定（MCP 层本就不暴露这些字段，
    # 这里再对直接调用做一道防线）。
    if meta.get("type") in GOVERNED and path.parent.name != "staging":
        reserved = {"status", "confidence", "last_verified", "verified_content_revision",
                    "helpful_count", "unhelpful_count"} & set(updates)
        if reserved:
            return {"error": "GOVERNANCE_FIELDS_RESERVED",
                    "message": "confidence/status 等治理字段由服务端状态机管理，调用方不可指定",
                    "fields": sorted(reserved)}
    preview = {**meta, **{k: v for k, v in updates.items() if k != "body"}}
    errors = store.lint(preview, updates.get("body", old_body), repo)
    if errors:
        return {"error": "LINT_FAILED", "errors": errors}
    cfg = config.load_config()
    conn = index.connect(repo)
    try:
        # 裁决用证据口径：relations 计入内容修订号，声明矛盾的更新本身会改变
        # 修订号并清 last_verified，使新版本暂时无证据；裁决须用声明方更新前的
        # 修订号与 verified 日期比较，否则声明方永远先丢证据、必输。
        # 新建卡无更新前版本，用当前修订号与 verified 日期（新建卡证据为零）。
        from .mutations import _compute_revision
        source_prior = {"content_revision": _compute_revision(meta, old_body),
                        "last_verified": meta.get("last_verified", "")}
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
            bases_out = []
            notes, modified = _relation_side_effects(repo, current, conn,
                                                     source_prior=source_prior, bases_out=bases_out)
            if not result.get("idempotent"):
                index.record_usage(conn, "update", mid)
            git_warning = _post_save(repo, cfg, current, body, path, path.parent.name == "staging",
                                    f"update({mid}): {current['title']}", modified,
                                    bases=[(mid, source_prior["content_revision"])] + bases_out)
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

def _post_save(rp, cfg, meta, body, path, staging, commit_msg, modified=(), bases=None):
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
        git_warn = gitops.commit_paths(rp, gitops.with_base_trailers(commit_msg, bases), owned_paths)
    return git_warn


def _push_after_write(rp, cfg, git_warn):
    """本地写锁释放后执行自动收敛与 push；网络失败不回滚本地提交。"""
    if git_warn or not cfg["git"].get("auto_push"):
        return None
    return gitops.schedule_push(rp, cfg["git"])


def _relation_side_effects(rp, meta, conn=None, source_prior=None, bases_out=None):
    """supersedes：被取代条目自动标 stale；
    contradicts：被矛盾方 relations 自动回指形成双向标记，并按执行证据强度自动裁决
    （conn 为 None 时只双向标记不裁决；裁决仅在关系新建时进行一次）。
    source_prior：声明方更新前的 {"content_revision", "last_verified"}（update 路径
    传入；新建卡无此值，用当前修订号与 verified 日期）。
    bases_out：可选列表，收集本次连带改动的目标卡 (id, 改前 content_revision)，
    供调用方写提交附带 base trailer（B-(1)）。
    返回 (notes, modified)：modified 为被改动的目标记忆，调用方须同步索引。"""
    from .mutations import _compute_revision
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
            if bases_out is not None:
                bases_out.append((rid, _compute_revision(tmeta, tbody)))
            tmeta["status"] = "stale"
            tmeta["stale_since"] = today
            tmeta["updated"] = today
            store.write_shared_text(tpath, store.render(tmeta, tbody))
            modified.append((tmeta, tbody, tpath))
            notes.append(f"{rid} 已因被取代标记为 stale")
        elif rtype == "contradicts":
            rels = tmeta.get("relations") or []
            newly = not any(x.get("id") == meta["id"] and x.get("type") == "contradicts" for x in rels)
            target_prior = None
            if newly:
                # 回指写入本身会改变对方修订号、使其新版本暂时无证据；
                # 裁决前先捕获对方更新前的修订号与 verified 日期（与声明方同理）。
                from .mutations import _compute_revision
                target_prior = {"content_revision": _compute_revision(tmeta, tbody),
                                "last_verified": tmeta.get("last_verified", "")}
                if bases_out is not None:
                    bases_out.append((rid, target_prior["content_revision"]))
                tmeta["relations"] = rels + [{"id": meta["id"], "type": "contradicts"}]
                tmeta["updated"] = today
                store.write_shared_text(tpath, store.render(tmeta, tbody))
                modified.append((tmeta, tbody, tpath))
            if conn is not None and newly:
                sub_notes, sub_modified = _adjudicate_contradiction(
                    rp, conn, meta, tmeta, tbody, tpath, today, source_prior, target_prior)
                notes.extend(sub_notes)
                modified.extend(sub_modified)
                if not sub_notes:
                    notes.append(f"注意：与 {rid} 存在矛盾关系（{tmeta.get('title','')}），已双向标记（当前状态不满足自动裁决条件）")
            else:
                notes.append(f"注意：与 {rid} 存在矛盾关系（{tmeta.get('title','')}），已双向标记")
    return notes, modified


def _adjudicate_contradiction(rp, conn, meta, tmeta, tbody, tpath, today,
                              source_prior=None, target_prior=None):
    """Phase 1：contradicts 自动裁决（仅在关系新建时调用一次）。

    比较双方已采纳执行证据：(helpful 任务数, 最新 verified 日期)。
    relations 计入内容修订号，声明更新与回指写入本身都会使新版本暂时无证据
    并清 verified 日期；因此双方均取关系建立前的修订号与 verified 日期比较，
    否则先写关系的一方必输。新建卡无更新前值时用当前修订号与 verified 日期。
    强者保留、弱者标 stale 并附裁决说明；证据打平或双方均有负面证据时，
    双方同时降 stale 并挂隔离标记——绝不在证据打平的情况下自动选边。
    任一方已 stale/archived 或在 staging 目录时不裁决：stale 方本就不可见，
    staging 卡的复活通道要求非 staging，裁决会制造无法复活的状态。
    返回 (notes, modified)。"""
    from .evolution import evidence_for_revision
    from .mutations import _compute_revision
    notes, modified = [], []
    src_path = store.find_file(rp, meta["id"])
    if (meta.get("status") in ("stale", "archived") or tmeta.get("status") in ("stale", "archived")
            or (src_path and src_path.parent.name == "staging") or tpath.parent.name == "staging"):
        return notes, modified
    _, src_body, _ = store.load(rp, meta["id"])
    if src_body is None or src_path is None:
        return notes, modified
    if source_prior:
        src_rev = source_prior["content_revision"]
        src_verified = source_prior["last_verified"]
    else:
        src_rev = _compute_revision(meta, src_body)
        src_verified = meta.get("last_verified") or ""
    if target_prior:
        tgt_rev = target_prior["content_revision"]
        tgt_verified = target_prior["last_verified"]
    else:
        tgt_rev = _compute_revision(tmeta, tbody)
        tgt_verified = tmeta.get("last_verified") or ""
    src_ev = evidence_for_revision(conn, meta["id"], src_rev)
    tgt_ev = evidence_for_revision(conn, tmeta["id"], tgt_rev)
    src_strength = (src_ev["helpful_tasks"], src_verified)
    tgt_strength = (tgt_ev["helpful_tasks"], tgt_verified)
    both_negative = src_ev["negative_evidence_tasks"] > 0 and tgt_ev["negative_evidence_tasks"] > 0

    def _to_stale(m, body, path):
        m["status"] = "stale"
        m["stale_since"] = today
        m["updated"] = today
        store.write_shared_text(path, store.render(m, body))
        modified.append((m, body, path))

    if both_negative or src_strength == tgt_strength:
        reason = "双方均有负面证据" if both_negative else "执行证据打平"
        for m, body, path, other in ((meta, src_body, src_path, tmeta["id"]),
                                     (tmeta, tbody, tpath, meta["id"])):
            m["contradiction_quarantine"] = True
            m["adjudication_note"] = (f"contradiction adjudication: {reason}，与 {other} 同时降 stale 并隔离，"
                                      "不自动选边；修订后可凭新版本证据重新晋升")
            _to_stale(m, body, path)
        notes.append(f"矛盾裁决：{reason}，双方同时降 stale 并隔离（{meta['id']} / {tmeta['id']}）")
        return notes, modified
    if src_strength > tgt_strength:
        winner, loser, loser_body, loser_path = meta["id"], tmeta, tbody, tpath
        loser_strength, winner_strength = tgt_strength, src_strength
    else:
        winner, loser, loser_body, loser_path = tmeta["id"], meta, src_body, src_path
        loser_strength, winner_strength = src_strength, tgt_strength
    loser["adjudication_note"] = (f"contradiction adjudication: 执行证据弱于 {winner} "
                                  f"（helpful 任务 {loser_strength[0]} vs {winner_strength[0]}），自动标 stale")
    _to_stale(loser, loser_body, loser_path)
    notes.append(f"矛盾裁决：{loser['id']} 执行证据弱于 {winner}，自动标 stale")
    return notes, modified


