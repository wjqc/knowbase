"""薪火 MCP 服务端：6 个工具，stdio 传输。

所有写工具在 repo 级跨进程锁内完成「分配 id → 写盘 → git → 重建索引」整个事务。
confidence/status 是服务端状态机的输出，任何工具的入参都不包含它们。
"""

from datetime import date
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from . import config, gitops, index, lifecycle, store
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
    index.build_index_md(rp, cfg)
    git_warn = None
    if cfg["git"].get("auto_commit", True):
        owned_paths = [path, rp / "INDEX.md"] + [item[2] for item in modified]
        git_warn = gitops.commit_paths(rp, commit_msg, owned_paths)
    return git_warn


def _push_after_write(rp, cfg, git_warn):
    """兼容旧调用名：只排队，不在 MCP 写请求中执行网络 push。"""
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
              rule_status: str | None = None, code_refs: list | None = None):
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

    sim = store.find_similar(rp, title)
    if sim:
        _, smeta, spath = sim
        return (f"已存在高度相似的记忆 [{smeta['id']}] {smeta.get('title','')}（{spath}）。\n"
                f"本次未保存。若为补充/修正请改用 memory_update({smeta['id']}, ...)；"
                f"若为取代旧经验，请在 relations 中声明 {{id: {smeta['id']}, type: supersedes}} 后重试。")

    cfg = _cfg()
    staging = type in ("standard", "preference", "bizrule")
    with RepoLock(rp, cfg.get("lock_timeout", 10.0)):
        # 查重必须在写锁内再次执行，封住两个进程同时通过预检查的窗口。
        sim = store.find_similar(rp, title)
        if sim:
            _, smeta, spath = sim
            return (f"已存在高度相似的记忆 [{smeta['id']}] {smeta.get('title','')}（{spath}）。\n"
                    f"本次未保存。若为补充/修正请改用 memory_update({smeta['id']}, ...)；"
                    f"若为取代旧经验，请在 relations 中声明 {{id: {smeta['id']}, type: supersedes}} 后重试。")
        mid = store.alloc_id(rp, type)
        meta["id"] = mid
        path = store.mem_path(rp, type, mid, staging=staging)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(store.render(meta, body), encoding="utf-8")
        notes, modified = _relation_side_effects(rp, meta)
        conn0 = index.connect(rp)
        index.record_usage(conn0, "save", mid, title)
        conn0.close()
        gw = _post_save(rp, cfg, meta, body, path, staging,
                        f"memory({mid}): {meta['title']} [{src}]", modified)
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
                evidence: list | None = None, code_refs: list | None = None):
    rp, err = _repo()
    if err:
        return err
    cfg = _cfg()
    with RepoLock(rp, cfg.get("lock_timeout", 10.0)):
        meta, old_body, path = store.load(rp, id)
        if not meta:
            return f"错误：未找到 {id}"
        if meta.get("type") in ("standard", "preference", "bizrule") \
                and path.parent.name != "staging":
            return (f"错误：{id} 是已生效的 {meta.get('type')}，Agent 无权直接修改；"
                    f"请由人工执行 `knowbase revise {id} --body-file <文件>`。")
        meta["updated"] = date.today().isoformat()
        if title is not None:
            meta["title"] = title.strip()
        if tags is not None:
            meta["tags"] = tags
        if relations is not None:
            meta["relations"] = relations
        if evidence is not None:
            meta["evidence"] = evidence
        if code_refs is not None:
            meta["code_refs"] = code_refs
        new_body = body if body is not None else old_body
        errs = store.lint(meta, new_body, rp)
        if errs:
            return "错误：lint 未通过，未修改。\n- " + "\n- ".join(errs)
        path.write_text(store.render(meta, new_body), encoding="utf-8")
        notes, modified = _relation_side_effects(rp, meta)
        conn0 = index.connect(rp)
        index.record_usage(conn0, "update", id)
        conn0.close()
        gw = _post_save(rp, cfg, meta, new_body, path,
                        path.parent.name == "staging", f"update({id}): {meta['title']}", modified)
    pw = _push_after_write(rp, cfg, gw)
    out = [f"已更新 {id}（内容修改；confidence/status 由服务端状态机管理，如需人工复核请用 CLI: knowbase verify {id}）"]
    out += notes
    if gw:
        out.append(gw)
    if pw:
        out.append(pw)
    return "\n".join(out)


def read_impl(id: str):
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
    conn.close()
    head = (f"[{meta['id']}] {meta.get('title','')}"
            f" ｜ {meta.get('confidence')}·{meta.get('status')}"
            f" ｜ scope={meta.get('scope')}"
            f" ｜ 采纳/验证后请回填 memory_feedback(id, helpful/not_helpful/outdated/incorrect)"
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


def feedback_impl(id: str, outcome: str, context: str = ""):
    rp, err = _repo()
    if err:
        return err
    if outcome not in lifecycle.OUTCOMES:
        return f"错误：outcome 须为 {lifecycle.OUTCOMES}"
    cfg = _cfg()
    by = config.agent_name()
    with RepoLock(rp, cfg.get("lock_timeout", 10.0)):
        conn = index.connect(rp)
        meta, events = lifecycle.apply_feedback(rp, id, outcome, by, conn)
        if meta is None:
            conn.close()
            return f"错误：未找到 {id}"
        _, body, path = store.load(rp, id)
        staging = "staging" in str(path)
        index.upsert(conn, meta, body, path, staging)
        index.record_usage(conn, "feedback", id, f"{outcome} {context}".strip())
        conn.close()
        gw = _post_save(rp, cfg, meta, body, path, staging,
                        f"feedback({id}): {outcome} by {by}")
    pw = _push_after_write(rp, cfg, gw)
    out = [f"已记录反馈：{id} ← {outcome}（by {by}）"] + events
    if gw:
        out.append(gw)
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


# ---------- 注册为 MCP 工具（显式命名） ----------

mcp.tool(name="memory_save", description="保存一条经验记忆。standard/preference 由 Agent 保存时自动进 staging 待人审")(save_impl)
mcp.tool(name="memory_update", description="修改已有记忆的内容/标签/关系。confidence 与 status 由服务端状态机管理，不接受指定")(update_impl)
mcp.tool(name="memory_read", description="按 id 读取记忆全文（自动累计 hit_count）")(read_impl)
mcp.tool(name="memory_search", description="检索经验记忆：任务开始涉及具体项目/系统/报错时先调用。检索词建议 ≥3 字的具体名词或报错关键词")(search_impl)
mcp.tool(name="memory_feedback", description="按记忆行动后回填结果：helpful/not_helpful/outdated/incorrect。驱动经验晋升与淘汰")(feedback_impl)
mcp.tool(name="memory_stats", description="记忆库统计：数量分布、使用漏斗、TOP 记忆")(stats_impl)


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
