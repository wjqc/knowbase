"""薪火 MCP 服务端：6 个工具，stdio 传输。

所有写工具在 repo 级跨进程锁内完成「分配 id → 写盘 → git → 重建索引」整个事务。
confidence/status 是服务端状态机的输出，任何工具的入参都不包含它们。
"""

from datetime import date
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from . import config, gitops, index, jobs, lifecycle, store
from .adapters import checkpoint as cp_mod
from .adapters import normalizer
from .extraction import recall, sanitizer, validator
from .locking import RepoLock

mcp = FastMCP(
    "knowbase",
    instructions=(
        "薪火：跨 Agent 共享的经验记忆库。"
        "任务开始涉及具体项目/系统/报错时先 memory_search；"
        "任务结束产生踩坑/决策/流程/约束时 memory_save（正文须含八项小节：结论/解决的问题/"
        "适用条件/不适用条件/可执行动作/关键证据/验证情况/未知与待确认）；"
        "业务规则的代码定位写入结构化 code_refs；"
        "整篇文档等原始材料不入 memory_save，走 CLI knowbase import 导入为 source；"
        "按记忆行动后如实 memory_feedback。"
    ),
)


# ---------- 内部工具 ----------

def _repo():
    rp = config.repo_path()
    if not rp.exists() or not (rp / ".git").exists():
        return None, "错误：记忆库未初始化。请先运行 `knowbase init`（显式初始化，服务不会偷偷创建）。"
    return rp, None


def _cfg():
    return config.load_config()


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


# ---------- 6 个 MCP 工具（impl 供测试直接调用） ----------

def save_impl(type: str, title: str, body: str, tags: list | None = None,
              scope: str = "global", relations: list | None = None,
              evidence: list | None = None, source: str | None = None,
              provenance: str | None = None, domain: str | None = None,
              rule_status: str | None = None, code_refs: list | None = None,
              request_id: str = ""):
    """创建新记忆（L1: 使用 commit_adapter 带 intent 持久化和崩溃恢复）。"""
    from . import commit_adapter

    rp, err = _repo()
    if err:
        return err
    if type not in store.TYPES:
        return f"错误：type 须为 {store.TYPES}"
    if type == "reference":
        return ("错误：reference 已不是可发布知识类型（原始材料与可复用知识已分层）。"
                "整篇文档/日志/代码请用 CLI `knowbase import <目录> --scope <项目>` 导入为 source artifact"
                "（保真、不进默认检索）；从中提炼的可复用结论请改用 pitfall/decision/workflow 等类型，"
                "并按八项小节结构撰写：结论/解决的问题/适用条件/不适用条件/可执行动作/关键证据/验证情况/未知与待确认。")
    if not title.strip() or not body.strip():
        return "错误：title 与 body 必填"
    # MCP 入参不具备身份权威：source 只可作为普通 agent 来源，不能声明 human。
    if source and source.lower().startswith("human:"):
        source = None
    src = source or f"agent:{config.agent_name()}:adhoc"
    meta = store.new_meta(type, title, scope, tags or [], src, relations, evidence, code_refs)
    if provenance:
        meta["provenance"] = provenance
    if domain:
        meta["domain"] = domain
    if rule_status:
        meta["rule_status"] = rule_status
    meta["id"] = "(待分配)"

    errs = store.lint(meta, body, rp)
    if errs:
        return "错误：lint 未通过，未保存。\n- " + "\n- ".join(errs)
    warns = store.lint_warnings(meta, body)

    # 锁外预查重（快速路径，锁内会再次检查）
    sim = store.find_similar(rp, title)
    if sim:
        _, smeta, spath = sim
        return (f"已存在高度相似的记忆 [{smeta['id']}] {smeta.get('title','')}（{spath}）。\n"
                f"本次未保存。若为补充/修正请改用 memory_update({smeta['id']}, ...)；"
                f"若为取代旧经验，请在 relations 中声明 {{id: {smeta['id']}, type: supersedes}} 后重试。")

    cfg = _cfg()
    conn = index.connect(rp)
    try:
        # 使用 commit_adapter 执行核心写入（带 intent 持久化和崩溃恢复）
        extra_meta = {}
        if provenance:
            extra_meta["provenance"] = provenance
        if domain:
            extra_meta["domain"] = domain
        if rule_status:
            extra_meta["rule_status"] = rule_status

        result = commit_adapter.adapter_create(
            rp, conn, type, title, body,
            scope=scope, tags=tags or [], source=src,
            relations=relations, evidence=evidence, code_refs=code_refs,
            request_id=request_id,
            lock_timeout=cfg.get("lock_timeout", 10.0),
            **extra_meta,
        )

        # 处理适配器返回的各种情况
        if result.get("error") == "DUPLICATE":
            existing_id = result["existing_id"]
            existing_title = result["existing_title"]
            existing_path = result["existing_path"]
            return (f"已存在高度相似的记忆 [{existing_id}] {existing_title}（{existing_path}）。\n"
                    f"本次未保存。若为补充/修正请改用 memory_update({existing_id}, ...)；"
                    f"若为取代旧经验，请在 relations 中声明 {{id: {existing_id}, type: supersedes}} 后重试。")
        if result.get("error"):
            return f"错误：{result['error']}"
        if result.get("idempotent"):
            # 崩溃恢复命中，返回既有结果
            mid = result["entity_id"]
            meta_result, body_result, path_result = store.load(rp, mid)
            if meta_result:
                staging = "staging" in str(path_result)
                where = (f"staging（提案待人工审核：人工 `knowbase promote {mid}` "
                         f"后移入 {store.TYPE_DIR[type]}/ 生效）") if staging else str(path_result)
                return f"已保存 {mid} → {where}（幂等恢复）"
            return f"已保存 {mid}（幂等恢复）"

        # 创建成功
        mid = result["entity_id"]
        path = Path(result["path"])
        staging = type in ("standard", "preference", "bizrule")

        # 加载刚写入的 meta
        meta, body, _ = store.load(rp, mid)

        # 锁内执行 relation side effects 和 post_save
        notes = []
        modified = []
        gw = None
        with RepoLock(rp, cfg.get("lock_timeout", 10.0)):
            notes, modified = _relation_side_effects(rp, meta)
            index.record_usage(conn, "save", mid, title)
            gw = _post_save(rp, cfg, meta, body, path, staging,
                            f"memory({mid}): {meta['title']} [{src}]", modified)
    finally:
        conn.close()

    pw = _push_after_write(rp, cfg, gw)

    where = (f"staging（提案待人工审核：人工 `knowbase promote {mid}` "
             f"后移入 {store.TYPE_DIR[type]}/ 生效）") if staging else str(path)
    out = [f"已保存 {mid} → {where}"]
    out += notes
    if warns:
        out.append("写作规范建议（不影响入库）：\n- " + "\n- ".join(warns))
    if gw:
        out.append(gw)
    if pw:
        out.append(pw)
    out.append("提示：他人按此记忆解决问题后，请用 memory_feedback 回填结果。")
    return "\n".join(out)


def update_impl(id: str, body: str | None = None, title: str | None = None,
                tags: list | None = None, relations: list | None = None,
                evidence: list | None = None, code_refs: list | None = None,
                expected_content_revision: str = "",
                expected_governance_revision: str = "",
                request_id: str = ""):
    """更新记忆（L1: 使用 commit_adapter 带 CAS 和 intent 持久化）。"""
    from . import commit_adapter

    rp, err = _repo()
    if err:
        return err
    cfg = _cfg()

    # 预检查：加载 meta 验证存在性和类型权限
    meta, old_body, path = store.load(rp, id)
    if not meta:
        return f"错误：未找到 {id}"
    if meta.get("type") in ("standard", "preference", "bizrule") \
            and path.parent.name != "staging":
        return (f"错误：{id} 是已生效的 {meta.get('type')}，Agent 无权直接修改；"
                f"请由人工执行 `knowbase revise {id} --body-file <文件>`。")

    # 构建更新内容用于 lint 预检查
    preview_meta = dict(meta)
    preview_meta["updated"] = date.today().isoformat()
    if title is not None:
        preview_meta["title"] = title.strip()
    if tags is not None:
        preview_meta["tags"] = tags
    if relations is not None:
        preview_meta["relations"] = relations
    if evidence is not None:
        preview_meta["evidence"] = evidence
    if code_refs is not None:
        preview_meta["code_refs"] = code_refs
    new_body = body if body is not None else old_body
    errs = store.lint(preview_meta, new_body, rp)
    if errs:
        return "错误：lint 未通过，未修改。\n- " + "\n- ".join(errs)

    conn = index.connect(rp)
    try:
        # 构建更新参数
        updates = {}
        if title is not None:
            updates["title"] = title.strip()
        if tags is not None:
            updates["tags"] = tags
        if relations is not None:
            updates["relations"] = relations
        if evidence is not None:
            updates["evidence"] = evidence
        if code_refs is not None:
            updates["code_refs"] = code_refs
        if body is not None:
            updates["body"] = body

        result = commit_adapter.adapter_update(
            rp, conn, id,
            expected_content_revision=expected_content_revision,
            expected_governance_revision=expected_governance_revision,
            request_id=request_id,
            lock_timeout=cfg.get("lock_timeout", 10.0),
            **updates,
        )

        # 处理适配器返回
        if result.get("error"):
            if result["error"] == commit_adapter.REVISION_CONFLICT:
                return (f"错误：版本冲突（{id} 已被其他进程修改）。"
                        f"请先 memory_read 获取最新 revision 后重试。")
            if result["error"] == commit_adapter.IDEMPOTENCY_CONFLICT:
                return f"错误：幂等冲突（request_id={request_id} 已用于不同内容）。"
            if result.get("idempotent"):
                return f"已更新 {id}（幂等恢复）"
            return f"错误：{result['error']}"

        # 更新成功，加载新 meta 执行 post-processing
        meta, new_body, path = store.load(rp, id)

        notes = []
        modified = []
        gw = None
        with RepoLock(rp, cfg.get("lock_timeout", 10.0)):
            notes, modified = _relation_side_effects(rp, meta)
            index.record_usage(conn, "update", id)
            gw = _post_save(rp, cfg, meta, new_body, path,
                            path.parent.name == "staging", f"update({id}): {meta['title']}", modified)
    finally:
        conn.close()

    pw = _push_after_write(rp, cfg, gw)
    out = [f"已更新 {id}（内容修改；confidence/status 由服务端状态机管理，如需人工复核请用 CLI: knowbase verify {id}）"]
    out += notes
    if gw:
        out.append(gw)
    if pw:
        out.append(pw)
    return "\n".join(out)


def read_impl(id: str):
    """读取记忆（L1: 返回 content_revision 和 governance_revision 供 CAS 使用）。"""
    rp, err = _repo()
    if err:
        return err
    meta, body, path = store.load(rp, id)
    if not meta:
        # 兼容兜底：旧服务进程的目录扫描范围可能不含新增类型目录，
        # 此时按索引记录的实际路径读取（索引由写入方实时更新）。
        conn = index.connect(rp)
        row = conn.execute("SELECT path FROM meta WHERE id=?", (id,)).fetchone()
        conn.close()
        if row and Path(row[0]).exists():
            meta, body = store.parse(Path(row[0]))
            path = Path(row[0])
    if not meta:
        return f"错误：未找到 {id}"
    conn = index.connect(rp)
    index.bump_hit(conn, id)
    index.record_usage(conn, "read", id)

    # 获取 revision 信息
    row = conn.execute(
        "SELECT content_revision, governance_revision FROM entity_heads WHERE entity_id=?",
        (id,),
    ).fetchone()
    conn.close()

    content_rev = row[0] if row else ""
    gov_rev = row[1] if row else ""

    head = (f"[{meta['id']}] {meta.get('title','')}"
            f" ｜ {meta.get('confidence')}·{meta.get('status')}"
            f" ｜ scope={meta.get('scope')}")
    if content_rev:
        head += f" ｜ content_revision={content_rev}"
    if gov_rev:
        head += f" ｜ governance_revision={gov_rev}"
    head += (f" ｜ 采纳/验证后请回填 memory_feedback(id, helpful/not_helpful/outdated/incorrect)"
             f"——晋升与淘汰只认反馈\n\n")
    refs = []
    for ref in meta.get("code_refs") or []:
        location = f"{ref.get('repo', '')} {ref.get('path', '')}"
        if ref.get("symbol"):
            location += f"#{ref['symbol']}"
        if ref.get("lines") is not None:
            location += f":{ref['lines']}"
        refs.append(location)
    if refs:
        head += "代码位置: " + "；".join(refs) + "\n\n"
    if meta.get("type") == "reference":
        head = ("[knowbase] 该条为 reference 原始材料卡（已退出默认检索，待迁移为 source）："
                "内容未经知识化提炼，采信前须自行验证。\n\n" + head)
    return head + body


def search_impl(query: str, type: str | None = None, scope: str | None = None,
                tag: str | None = None, limit: int = 5, include_inactive: bool = False):
    rp, err = _repo()
    if err:
        return err
    if type == "reference":
        return ("错误：reference（原始材料卡）已退出默认检索。存量 R 卡可 memory_read(id) 单条查看；"
                "新原始材料请用 CLI `knowbase import <目录> --scope <项目>` 导入为 source artifact。")
    _updated, sync_warn = gitops.sync_before_read(rp, _cfg())
    conn = index.connect(rp)
    if scope is None:
        from .hooks import _infer_scope, has_project_context
        inferred = _infer_scope(conn)
        if has_project_context() and inferred is None:
            conn.close()
            return ("错误：当前项目无法解析 scope；请在 ~/.knowbase/config.json 的 "
                    "scope_map 中配置项目路径，或显式传入 scope。")
        scope = inferred or "global"
    results = index.search(conn, query, mtype=type, scope=scope, tag=tag, limit=limit, include_inactive=include_inactive)
    index.record_search(conn, query, scope, results)
    conn.close()
    if not results:
        base = (f"未命中「{query}」。建议：检索词用 ≥3 字的具体名词/报错关键词，"
                "或换英文技术词；也可放宽 scope/type 过滤。")
        return base + (f"\n同步告警：{sync_warn}" if sync_warn else "")
    lines = [f"命中 {len(results)} 条（原地混合召回：词法+dense→RRF×scope×置信度×新鲜度×反馈）："]
    for r in results:
        stg = "（staging 提案）" if r["staging"] else ""
        lines.append(
            f"- [{r['id']}]{stg} {r['title']} ｜ {r['confidence']}·{r['status']}"
            f" ｜ scope={r['scope']} ｜ hit={r['hit_count']} helpful={r['helpful_count']}"
            f" ｜ score={r['score']} ｜ channels={','.join(r.get('channels', []))}"
            f"\n  摘要：{r['snippet'][:80]}\n  详情：memory_read(\"{r['id']}\")"
        )
    lines.append("按命中条目行动后，请 memory_feedback 回填结果（反馈驱动 once→verified 晋升与 active→stale 淘汰）。")
    if sync_warn:
        lines.append(f"同步告警：{sync_warn}")
    return "\n".join(lines)


def feedback_impl(id: str, outcome: str, context: str = "",
                  expected_governance_revision: str = "",
                  request_id: str = ""):
    """记录反馈（L1: 使用 commit_adapter 带 governance_revision 检查）。"""
    from . import commit_adapter

    rp, err = _repo()
    if err:
        return err
    if outcome not in lifecycle.OUTCOMES:
        return f"错误：outcome 须为 {lifecycle.OUTCOMES}"
    cfg = _cfg()
    by = config.agent_name()

    conn = index.connect(rp)
    try:
        result = commit_adapter.adapter_feedback(
            rp, conn, id, outcome, by,
            expected_governance_revision=expected_governance_revision,
            request_id=request_id,
            lock_timeout=cfg.get("lock_timeout", 10.0),
        )

        # 处理适配器返回
        if result.get("error"):
            if result["error"] == commit_adapter.REVISION_CONFLICT:
                return (f"错误：版本冲突（{id} 已被其他进程修改）。"
                        f"请先 memory_read 获取最新 revision 后重试。")
            if result.get("idempotent"):
                events = result.get("events", [])
                return f"已记录反馈：{id} ← {outcome}（by {by}）（幂等恢复）" + (f"\n" + "\n".join(events) if events else "")
            return f"错误：{result['error']}"

        events = result.get("events", [])
        meta, body, path = store.load(rp, id)
        staging = "staging" in str(path)
        index.record_usage(conn, "feedback", id, f"{outcome} {context}".strip())
        gw = _post_save(rp, cfg, meta, body, path, staging,
                        f"feedback({id}): {outcome} by {by}")
    finally:
        conn.close()

    pw = _push_after_write(rp, cfg, gw)
    out = [f"已记录反馈：{id} ← {outcome}（by {by}）"] + events
    if gw:
        out.append(gw)
    if pw:
        out.append(pw)
    return "\n".join(out)


def stats_impl():
    rp, err = _repo()
    if err:
        return err
    conn = index.connect(rp)
    s = index.stats(conn)
    conn.close()
    lines = [
        f"记忆总数 {s['total']}（" + " ".join(f"{k}:{v}" for k, v in sorted(s['by_type'].items())) + "）",
        f"状态分布：" + " ".join(f"{k}:{v}" for k, v in sorted(s['by_status'].items())),
        f"项目分布：" + " ".join(f"{k}:{v}" for k, v in sorted(s['by_scope'].items())),
        f"近 7 天写入/更新 {s['written_last_7d']} 条",
        (f"使用漏斗：search {s['funnel']['search']} → read {s['funnel']['read']}"
         f" ｜ save {s['funnel']['save']} ｜ feedback {s['funnel']['feedback']}"
         f"（helpful {s['funnel']['feedback_helpful']}）"),
        "TOP5：",
    ]
    for t in s["top"]:
        lines.append(f"- [{t['id']}] {t['title']}（hit {t['hit']} / helpful {t['helpful']}）")
    return "\n".join(lines)


# ---------- 任务队列 MCP 工具（M0） ----------

def capture_impl(session_id: str = "", checkpoint: str = "", scope: str = "",
                 transcript_ref: str = "", messages: list | None = None,
                 request_id: str = "") -> str:
    """入队一个对话沉淀任务。

    返回 job_id + 状态；宿主收到后通过 memory_job_claim 领取材料并提炼。
    """
    rp, err = _repo()
    if err:
        return err
    if not session_id:
        return "错误：session_id 必填"
    if not scope:
        return "错误：scope 必填"

    # 构建 checkpoint
    events = []
    if messages:
        for msg in messages:
            if isinstance(msg, dict):
                events.append(normalizer.normalize_event(msg, default_session_id=session_id, default_scope=scope))
    elif transcript_ref:
        events = normalizer.parse_transcript_file(transcript_ref, default_session_id=session_id, default_scope=scope)

    if not events:
        return "错误：未提供可分析的对话内容（messages 或 transcript_ref 至少提供一个）"

    # 脱敏
    cp = cp_mod.build_checkpoint(events, session_id=session_id, scope=scope)

    # 入队
    conn = index.connect(rp)
    try:
        result = jobs.enqueue(
            conn, jobs.KIND_CAPTURE, scope,
            {"checkpoint": cp.__dict__, "transcript_ref": transcript_ref},
            idempotency_key=cp.idempotency_key if not request_id else f"capture:{request_id}",
        )
        jobs.set_preparing(conn, result["job_id"])
        # 设置 awaiting（材料已就绪）
        jobs.set_awaiting(conn, result["job_id"], 1)
    finally:
        conn.close()

    if result["created"]:
        return (f"已入队 {result['job_id']}（state=awaiting_agent）。\n"
                f"宿主领取：memory_job_claim(job_id=\"{result['job_id']}\", "
                f"host_session_id=\"<你的会话ID>\")")
    else:
        return f"任务已存在 {result['job_id']}（state={result['state']}，幂等命中）"


def job_claim_impl(job_id: str = "", scope: str = "", host_session_id: str = "",
                   max_chars: int = 8000) -> str:
    """领取一个批次的提炼材料。

    返回批次材料（脱敏后的对话片段）、相似卡引用、lease_token 和到期时间。
    M3-4: 新增 distillation_guide 提炼指引。
    """
    rp, err = _repo()
    if err:
        return err
    if not job_id and not scope:
        return "错误：job_id 或 scope 至少提供一个"
    if not host_session_id:
        host_session_id = config.agent_name()

    conn = index.connect(rp)
    try:
        # 如果只给了 scope，找一个 awaiting 的任务
        if not job_id:
            pending = jobs.list_jobs(conn, scope=scope, state=jobs.STATE_AWAITING, limit=1)
            if not pending:
                return "无待领取任务"
            job_id = pending[0]["job_id"]

        result = jobs.claim(conn, job_id, host_session_id)

        if "error" in result:
            if result["error"] == jobs.NO_WORK:
                return "无待领取任务"
            return f"领取失败：{result['error']}"

        # 构建材料摘要
        materials = result.get("materials", {})
        checkpoint_data = materials.get("checkpoint", {})
        events = checkpoint_data.get("events", [])

        # 宽召回相似卡
        query_text = " ".join(e.get("text", "")[:200] for e in events[:5])
        similar = recall.wide_recall(conn, query_text, materials.get("scope", ""))

        lines = [
            f"批次 {result['batch_id']}（job={job_id}）",
            f"lease_token: {result['lease_token']}",
            f"expires_at: {result['expires_at']}",
            f"fencing_token: {result['fencing_token']}",
            f"cursor: {result['cursor']}/{result.get('batch_total', '?')}",
            "",
            "== 材料（脱敏后） ==",
        ]
        char_count = 0
        for ev in events:
            entry = f"[{ev.get('role', '?')}] {ev.get('text', '')}"
            if char_count + len(entry) > max_chars:
                lines.append("...（已截断，续租后继续领取）")
                break
            lines.append(entry)
            char_count += len(entry)

        # M3-4: 添加提炼指引
        lines.append("")
        lines.append("== 提炼指引（v1） ==")
        lines.append(_build_distillation_guide(events, similar, materials.get("scope", "")))

        if similar:
            lines.append("")
            lines.append("== 相似卡（宽召回，请逐一 memory_read 判断是否重复） ==")
            for s in similar[:10]:
                lines.append(f"- [{s['id']}] {s['title']}（{s['confidence']}·{s['scope']} score={s['score']}）")

        lines.append("")
        lines.append("提交：memory_job_submit(job_id, batch_id, lease_token, request_id, candidates, segment_results)")
        lines.append("续租：memory_job_renew(job_id, batch_id, lease_token)")
        return "\n".join(lines)
    finally:
        conn.close()


def _build_distillation_guide(events: list[dict], similar: list[dict], scope: str) -> str:
    """M3-4: 构建提炼指引，指导宿主如何分析对话并提炼候选。"""
    lines = [
        "1. 分析对话，识别可复用经验：",
        "   - 一次性 bug 修复、测试数字、文档事实 → 不入库",
        "   - 下次还会踩的坑、还会用的技巧 → 提炼为候选",
        "",
        "2. 每个候选按八项结构撰写：",
        "   - 结论 / 解决的问题 / 适用条件 / 不适用条件",
        "   - 可执行动作 / 关键证据 / 验证情况 / 未知与待确认",
        "",
        "3. 类型选择建议：",
        "   - pitfall: 踩坑经验（最常见）",
        "   - decision: 技术决策（为什么选 A 不选 B）",
        "   - workflow: 工作流程（步骤化操作）",
        "   - bizrule: 业务规则（代码定位写入 code_refs）",
        "",
        "4. 提交格式：",
        '   candidates=[{"candidate_key": "唯一标识", "body": {"conclusion": "...", "problem": "...", "scope": "' + scope + '"}, "evidence": [...], "decision": "accepted|needs_review"}]',
        '   segment_results=[{"segment_id": "...", "decision": "distilled|skipped|no_value", "reason": "..."}]',
        "",
        "5. 若确认无值得复用经验，提交空结果：",
        '   candidates=[], segment_results=[{"segment_id": "...", "decision": "skipped", "reason": "一次性操作"}]',
    ]
    return "\n".join(lines)


def job_renew_impl(job_id: str = "", batch_id: str = "", lease_token: str = "") -> str:
    """续租：延长当前批次的领取期限。"""
    rp, err = _repo()
    if err:
        return err
    if not all([job_id, lease_token]):
        return "错误：job_id 和 lease_token 必填"

    conn = index.connect(rp)
    try:
        result = jobs.renew(conn, job_id, lease_token)
        if "error" in result:
            return f"续租失败：{result['error']}"
        return f"已续租 {job_id}，新到期时间：{result['expires_at']}"
    finally:
        conn.close()


def job_submit_impl(job_id: str = "", batch_id: str = "", lease_token: str = "",
                    request_id: str = "", candidates: list | None = None,
                    segment_results: list | None = None) -> str:
    """提交提炼结果（候选经验或 no_candidate 理由）。"""
    rp, err = _repo()
    if err:
        return err
    if not all([job_id, batch_id, lease_token, request_id]):
        return "错误：job_id/batch_id/lease_token/request_id 必填"

    conn = index.connect(rp)
    try:
        if not candidates and not segment_results:
            # 无候选
            result = jobs.submit_no_candidate(conn, job_id, lease_token, request_id, "no_candidate")
            if "error" in result:
                return f"提交失败：{result['error']}"
            return f"已提交 no_candidate（job={job_id}）"

        # 校验每个候选
        candidates = candidates or []
        validated = []
        warnings = []
        for c in candidates:
            ve = validator.validate_candidate(c, scope="", repo_path=rp)
            if not ve.ok:
                warnings.append(f"候选 {c.get('candidate_key', '?')} 校验未通过：{'; '.join(ve.errors)}")
                continue
            validated.append(c)
            if ve.warnings:
                warnings.append(f"候选 {c.get('candidate_key', '?')} 警告：{'; '.join(ve.warnings)}")

        result = jobs.submit(conn, job_id, batch_id, lease_token, request_id,
                             validated, segment_results)
        if "error" in result:
            return f"提交失败：{result['error']}"

        lines = [
            f"已提交（job={job_id}）",
            f"accepted: {result['accepted']}, needs_review: {result['needs_review']}",
            f"next_cursor: {result['next_cursor']}, state: {result['state']}",
        ]
        if warnings:
            lines.append("校验告警：")
            lines.extend(f"- {w}" for w in warnings)
        return "\n".join(lines)
    finally:
        conn.close()


def job_status_impl(job_id: str = "") -> str:
    """查询任务状态。"""
    rp, err = _repo()
    if err:
        return err
    if not job_id:
        return "错误：job_id 必填"

    conn = index.connect(rp)
    try:
        s = jobs.status(conn, job_id)
        if not s:
            return f"未找到任务 {job_id}"
        return (
            f"任务 {s['job_id']}（{s['kind']}）\n"
            f"scope: {s['scope']}\n"
            f"state: {s['state']}\n"
            f"attempt: {s['attempt']}/{s['max_attempts']}\n"
            f"cursor: {s['cursor']}/{s['total']}\n"
            f"retryable: {s['retryable']}\n"
            f"error: {s['error'] or '无'}\n"
            f"created: {s['created_at']}\n"
            f"updated: {s['updated_at']}"
        )
    finally:
        conn.close()


def import_impl(directory: str, scope: str = "global", mode: str = "extract") -> str:
    """M4-5: 导入文档目录，入队 extract 任务供提炼。

    Args:
        directory: 文档目录路径
        scope: 项目 scope
        mode: 导入模式（目前只支持 "extract"）

    Returns:
        入队结果（job_id 列表）
    """
    from pathlib import Path
    import hashlib
    from .extraction.parsers import parse_document

    rp, err = _repo()
    if err:
        return err

    src_dir = Path(directory).expanduser()
    if not src_dir.is_dir():
        return f"错误：目录不存在 {src_dir}"

    src_dir = src_dir.resolve()
    if src_dir == rp.resolve() or rp.resolve() in src_dir.parents or src_dir in rp.resolve().parents:
        return "错误：导入目录与记忆库不可互相包含"

    # 支持的文件格式
    supported = {".md", ".txt", ".html", ".htm", ".py", ".js", ".ts", ".java", ".go",
                 ".log", ".json", ".yaml", ".yml", ".xml"}
    files = [f for f in sorted(src_dir.rglob("*"))
             if f.is_file() and f.suffix.lower() in supported
             and not any(part.startswith(".") for part in f.relative_to(src_dir).parts)
             and not f.is_symlink() and src_dir in f.resolve().parents]

    if not files:
        return "目录中未找到支持的文档文件"

    # 阶段 1：锁外解析
    parsed_docs = []
    skipped = 0
    for f in files:
        try:
            doc = parse_document(f)
            if doc is None or not doc.text.strip():
                skipped += 1
                continue
            # 检查明文凭据
            if store.SECRET_RE.search(doc.text):
                skipped += 1
                continue
            parsed_docs.append({
                "file": f,
                "doc": doc,
                "title": doc.meta.get("title", f.stem)[:80],
            })
        except Exception:
            skipped += 1
            continue

    if not parsed_docs:
        return f"完成：入队 0 个 extract 任务，跳过 {skipped}。"

    # 阶段 2：锁内入队
    cfg = _cfg()
    enqueued = 0
    job_ids = []
    with RepoLock(rp, cfg.get("lock_timeout", 10.0)):
        conn = index.connect(rp)
        try:
            for item in parsed_docs:
                f = item["file"]
                doc = item["doc"]
                # 构建 materials
                materials = {
                    "source_path": str(f),
                    "source_format": doc.format,
                    "title": item["title"],
                    "text": doc.text[:100000],  # 限制大小
                    "segments": [
                        {
                            "segment_id": seg.segment_id,
                            "text": seg.text[:4000],
                            "locator": seg.locator._asdict() if hasattr(seg.locator, '_asdict') else str(seg.locator),
                            "text_hash": seg.text_hash,
                        }
                        for seg in doc.segments
                    ],
                    "total_segments": len(doc.segments),
                }
                # 幂等键：文件路径 + 内容哈希
                content_hash = hashlib.sha256(doc.text.encode("utf-8")).hexdigest()[:16]
                idem_key = f"extract:{f}:{content_hash}"

                result = jobs.enqueue(
                    conn, jobs.KIND_EXTRACT, scope, materials,
                    idempotency_key=idem_key,
                )
                if result["created"]:
                    jobs.set_preparing(conn, result["job_id"])
                    jobs.set_awaiting(conn, result["job_id"], len(doc.segments))
                    enqueued += 1
                    job_ids.append(result["job_id"])
        finally:
            conn.close()

    lines = [f"完成：入队 {enqueued} 个 extract 任务，跳过 {skipped}。"]
    if job_ids:
        lines.append(f"任务 ID：{', '.join(job_ids[:5])}{'...' if len(job_ids) > 5 else ''}")
        lines.append("使用 memory_job_claim 领取任务，memory_job_submit 提交提炼结果。")
    return "\n".join(lines)


# ---------- 注册为 MCP 工具（显式命名） ----------

mcp.tool(name="memory_save", description="保存一条经验记忆。standard/preference 由 Agent 保存时自动进 staging 待人审")(save_impl)
mcp.tool(name="memory_update", description="修改已有记忆的内容/标签/关系。confidence 与 status 由服务端状态机管理，不接受指定")(update_impl)
mcp.tool(name="memory_read", description="按 id 读取记忆全文（自动累计 hit_count）")(read_impl)
mcp.tool(name="memory_search", description="检索经验记忆：任务开始涉及具体项目/系统/报错时先调用。检索词建议 ≥3 字的具体名词或报错关键词")(search_impl)
mcp.tool(name="memory_feedback", description="按记忆行动后回填结果：helpful/not_helpful/outdated/incorrect。驱动经验晋升与淘汰")(feedback_impl)
mcp.tool(name="memory_stats", description="记忆库统计：数量分布、使用漏斗、TOP 记忆")(stats_impl)
mcp.tool(name="memory_capture", description="入队对话沉淀任务。宿主会话结束或检查点时调用，返回 job_id 供后续领取")(capture_impl)
mcp.tool(name="memory_job_claim", description="领取提炼批次。返回脱敏材料、相似卡引用、lease_token 和到期时间")(job_claim_impl)
mcp.tool(name="memory_job_renew", description="续租当前批次。延长 lease 到期时间")(job_renew_impl)
mcp.tool(name="memory_job_submit", description="提交提炼结果（候选经验或 no_candidate）。宿主完成提炼后调用")(job_submit_impl)
mcp.tool(name="memory_job_status", description="查询任务状态。返回 state/cursor/error 等信息")(job_status_impl)
mcp.tool(name="memory_import", description="M4-5: 导入文档目录，入队 extract 任务供提炼。支持 Markdown/HTML/代码等多种格式")(import_impl)


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
