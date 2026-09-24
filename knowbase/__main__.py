"""薪火 CLI：显式、幂等、可观察的初始化与人工治理操作。

用法（与 argparse 一致，共 15 个命令）：
  knowbase init [--import-from 目录 --scope 名]          初始化/补全（幂等），可顺带导入 source
  knowbase import <目录> --scope 名                       批量导入为 source artifact（不产生知识卡、不进默认检索）
  knowbase reindex                                        全量重建 SQLite 索引与 INDEX.md
  knowbase verify <id>                                    人工确认有效（once→verified / stale 复活）
  knowbase archive <id>                                   人工归档
  knowbase promote <id>                                   人工激活 staging 提案（git mv 到正式目录）
  knowbase revise <id> --body-file <文件>                 人工修订已生效的标准/偏好/业务规则
  knowbase list [type]                                    列出记忆
  knowbase stats                                          统计
  knowbase history [--limit N]                            查看搜索命中记录 JSON
  knowbase dashboard [--output 文件] [--open]             生成本地 HTML 知识治理看板
  knowbase doctor [--json]                                单库健康检查（memory.db/FTS/Git/治理）
  knowbase alerts [--dry-run]                             输出单库异常状态，供调度器通知
  knowbase hook <event> [--style claude|zcode]            Agent hook 入口（session-start/user-prompt/stop）
  knowbase serve                                          启动 MCP 服务（stdio，供各 Agent 配置调用）

原始材料与可复用知识分层（2026-09-21）：import 只产 source（sources/objects + manifests，
保真、按内容哈希去重、不进默认检索）；可复用知识由 memory_save 按八项小节结构提炼。
"""

import argparse
import json
import shutil
import sys
from datetime import date, datetime
from pathlib import Path

from . import config, gitops, index, jobs, lifecycle, sources, store
from .locking import RepoLock

GITIGNORE = "memory.db\nmemory.db-wal\nmemory.db-shm\n.lock\n*.log\n.idea/\n.vscode/\n.DS_Store\n"

REPO_README = """# 薪火（knowbase）经验记忆库

机器高频写入的热记忆层，8 个 AI Agent 共享读写。
- 检索/读写走 MCP 工具（memory_search / read / save / update / feedback / stats）
- INDEX.md 为自动生成的速览，勿手改
- standards/（人员标准）与 preferences/ 只能由人直接写或经 staging/ 提案激活
- memory.db 包含派生索引与不可重建的本地运行日志，请一起备份；reindex 保留运行日志
"""


def cmd_init(import_from=None, scope=None, dtype="workflow") -> int:
    if import_from and not scope:
        print("错误：init --import-from 必须指定 --scope，避免跨项目污染")
        return 1
    cfg = config.load_config()
    rp = config.repo_path(cfg)

    # 1. 配置文件：不存在才写默认值（幂等，不覆盖用户已有配置）
    if not config.CONFIG_PATH.exists():
        config.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        config.CONFIG_PATH.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"✓ 已写配置 {config.CONFIG_PATH}")
    else:
        print(f"✓ 配置已存在 {config.CONFIG_PATH}（不覆盖）")

    # 2. 仓库目录结构：缺什么补什么，已有的不动
    rp.mkdir(parents=True, exist_ok=True)
    for d in store.ALL_DIRS:
        (rp / d).mkdir(exist_ok=True)
    gi = rp / ".gitignore"
    if not gi.exists():
        gi.write_text(GITIGNORE, encoding="utf-8")
    readme = rp / "README.md"
    if not readme.exists():
        readme.write_text(REPO_README, encoding="utf-8")
    print(f"✓ 仓库目录就绪 {rp}")

    # 3. git：新仓库 init + 应用远端（白名单校验）
    new_repo = not gitops.is_repo(rp)
    if new_repo:
        import subprocess
        subprocess.run(["git", "-C", str(rp), "init", "-q"], check=True)
        # 创建 .gitignore：INDEX.md 已退役，不再提交到 Git
        gitignore = rp / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text("# 本地重建的速览索引，不进入共享 Git\nINDEX.md\n", encoding="utf-8")
        url = cfg["git"].get("remote", {}).get("url", "")
        if url:
            prefixes = cfg["git"].get("allowed_remote_prefixes", [])
            if not any(url.startswith(p) for p in prefixes):
                print(f"⚠ remote {url} 不在白名单内，已跳过配置")
            else:
                subprocess.run(["git", "-C", str(rp), "remote", "add", "origin", url], check=True)
                print(f"✓ 已配置远端 {url}")
        print("✓ git 仓库已初始化")
    else:
        print("✓ git 仓库已存在（不动）")

    # 4. 派生索引 + INDEX.md（全量重建，天然幂等）
    with RepoLock(rp, 10.0):
        n = index.rebuild(rp)
    print(f"✓ 索引就绪：{n} 条记忆，INDEX.md 已生成")

    # 4.5 崩溃恢复：扫描未入账的 pending intent
    from . import commit_adapter
    conn = index.connect(rp)
    try:
        recovery = commit_adapter.recover_intents(rp, conn)
        if recovery["recovered"] > 0 or recovery["failed"] > 0:
            print(f"✓ 崩溃恢复：{recovery['recovered']} 个 intent 已恢复，"
                  f"{recovery['failed']} 个需要人工核查")
            for detail in recovery["details"]:
                status = detail.get("status", "unknown")
                reason = detail.get("reason", "")
                if status == "failed" and reason:
                    print(f"  - {detail['intent_id']}: {reason}")
    finally:
        conn.close()

    # 5. knowledge_path 联动检测：存在则提示，不存在绝不创建
    kp = Path(cfg.get("knowledge_path", "")).expanduser() if cfg.get("knowledge_path") else None
    if kp and kp.exists():
        print(f"✓ 检测到冷知识层 {kp}（热→冷蒸馏联动可用，薪火不会写入它）")
    else:
        print("○ 未检测到 knowledge_path（不影响运行；薪火不会创建它）")

    if new_repo:
        gitops.commit_all(rp, "init: 薪火记忆库初始化")
    if import_from:
        return cmd_import(import_from, dtype, scope, True, "human:init-import")
    print("完成。Agent 接入：在各 MCP 配置注册 knowbase serve 即可。")
    return 0


def cmd_reindex() -> int:
    rp = config.repo_path()
    with RepoLock(rp, 10.0):
        n = index.rebuild(rp)
    print(f"✓ 已全量重建：{n} 条记忆")
    return 0


def _human_op(rp, fn, mid, label, expect_revision: str = ""):
    """人工操作（verify/archive）的通用流程。

    expect_revision: 可选的 governance_revision CAS 检查。
    """
    from .mutations import _compute_governance_revision, REVISION_CONFLICT

    cfg = config.load_config()

    # CAS 检查（如果提供了 expect_revision）
    if expect_revision:
        conn = index.connect(rp)
        try:
            row = conn.execute(
                "SELECT governance_revision FROM entity_heads WHERE entity_id=?",
                (mid,),
            ).fetchone()
            if row:
                current_rev = row[0]
            else:
                # 没有 entity_heads 记录，从文件计算
                meta, body, _ = store.load(rp, mid)
                if meta:
                    current_rev = _compute_governance_revision(meta)
                else:
                    print(f"错误：未找到 {mid}")
                    return 1
            if expect_revision != current_rev:
                print(f"错误：版本冲突（governance_revision 不匹配）。")
                print(f"  期望: {expect_revision}")
                print(f"  当前: {current_rev}")
                print(f"请先 memory_read 获取最新 revision 后重试。")
                return 1
        finally:
            conn.close()

    with RepoLock(rp, cfg.get("lock_timeout", 10.0)):
        meta = fn(rp, mid)
        if not meta:
            print(f"错误：未找到 {mid}")
            return 1
        meta2, body, path = store.load(rp, mid)
        conn = index.connect(rp)
        index.upsert(conn, meta2, body, path, path.parent.name == "staging")
        conn.close()
        index.build_index_md(rp, cfg)  # 本地重建，不提交
        # INDEX.md 已退役，不再提交到 Git
        git_warn = gitops.commit_paths(rp, f"{label}({mid}): by human", [path]) \
            if cfg["git"].get("auto_commit", True) else None
    push_warn = None if git_warn or not cfg["git"].get("auto_push") else \
        gitops.schedule_push(rp, cfg["git"])
    print(f"✓ {label} {mid} 完成")
    if git_warn:
        print(f"⚠ {git_warn}")
    if push_warn:
        print(f"⚠ {push_warn}")
    return 0


def cmd_verify(mid: str, expect_revision: str = "") -> int:
    return _human_op(config.repo_path(), lifecycle.human_verify, mid, "verify",
                     expect_revision=expect_revision)


def cmd_archive(mid: str, expect_revision: str = "") -> int:
    return _human_op(config.repo_path(), lifecycle.human_archive, mid, "archive",
                     expect_revision=expect_revision)


def cmd_promote(mid: str) -> int:
    """人工激活 staging 提案：等价于审过的 git mv，等价于通过状态机入口的治理动作。"""
    rp = config.repo_path()
    cfg = config.load_config()
    with RepoLock(rp, cfg.get("lock_timeout", 10.0)):
        meta, body, path = store.load(rp, mid)
        if not meta:
            print(f"错误：未找到 {mid}")
            return 1
        if path.parent.name != "staging":
            print(f"错误：{mid} 不在 staging/（无需 promote）")
            return 1
        dtype = meta.get("type")
        target = store.mem_path(rp, dtype, mid)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(target))
        meta["updated"] = date.today().isoformat()
        target.write_text(store.render(meta, body), encoding="utf-8")
        conn = index.connect(rp)
        index.upsert(conn, meta, body, target, False)
        conn.close()
        index.build_index_md(rp, cfg)  # 本地重建，不提交
        # INDEX.md 已退役，不再提交到 Git
        git_warn = gitops.commit_paths(
            rp, f"promote({mid}): staging → {store.TYPE_DIR[dtype]}（人工审核通过）",
            [path, target],
        ) if cfg["git"].get("auto_commit", True) else None
    push_warn = None if git_warn or not cfg["git"].get("auto_push") else \
        gitops.schedule_push(rp, cfg["git"])
    print(f"✓ {mid} 已激活 → {target}")
    if git_warn:
        print(f"⚠ {git_warn}")
    if push_warn:
        print(f"⚠ {push_warn}")
    return 0


def cmd_revise(mid: str, body_file: str, title: str | None = None,
               expect_revision: str = "") -> int:
    """可信人工 CLI：修订已生效的 standard/preference/bizrule。

    expect_revision: 可选的 content_revision CAS 检查。
    """
    from .mutations import _compute_revision

    rp = config.repo_path()
    cfg = config.load_config()
    body_path = Path(body_file).expanduser().resolve()
    if not body_path.is_file():
        print(f"错误：正文文件不存在 {body_path}")
        return 1
    new_body = body_path.read_text(encoding="utf-8")

    # CAS 检查（如果提供了 expect_revision）
    if expect_revision:
        meta, old_body, _ = store.load(rp, mid)
        if not meta:
            print(f"错误：未找到 {mid}")
            return 1
        conn = index.connect(rp)
        try:
            row = conn.execute(
                "SELECT content_revision FROM entity_heads WHERE entity_id=?",
                (mid,),
            ).fetchone()
            if row:
                current_rev = row[0]
            else:
                current_rev = _compute_revision(meta, old_body)
            if expect_revision != current_rev:
                print(f"错误：版本冲突（content_revision 不匹配）。")
                print(f"  期望: {expect_revision}")
                print(f"  当前: {current_rev}")
                print(f"请先 memory_read 获取最新 revision 后重试。")
                return 1
        finally:
            conn.close()

    with RepoLock(rp, cfg.get("lock_timeout", 10.0)):
        meta, _old_body, path = store.load(rp, mid)
        if not meta:
            print(f"错误：未找到 {mid}")
            return 1
        if meta.get("type") not in ("standard", "preference", "bizrule"):
            print(f"错误：revise 仅用于 standard/preference/bizrule，当前为 {meta.get('type')}")
            return 1
        if path.parent.name == "staging":
            print(f"错误：{mid} 仍在 staging，请使用 memory_update 后再 promote")
            return 1
        if title is not None:
            meta["title"] = title.strip()
        meta["updated"] = date.today().isoformat()
        errs = store.lint(meta, new_body, rp)
        if errs:
            print("错误：lint 未通过，未修改。\n- " + "\n- ".join(errs))
            return 1
        path.write_text(store.render(meta, new_body), encoding="utf-8")
        conn = index.connect(rp)
        index.upsert(conn, meta, new_body, path, False)
        conn.close()
        index.build_index_md(rp, cfg)  # 本地重建，不提交
        # INDEX.md 已退役，不再提交到 Git
        git_warn = gitops.commit_paths(rp, f"revise({mid}): {meta['title']} by human",
                                       [path]) \
            if cfg["git"].get("auto_commit", True) else None
    push_warn = None if git_warn or not cfg["git"].get("auto_push") else \
        gitops.schedule_push(rp, cfg["git"])
    print(f"✓ revise {mid} 完成")
    if git_warn:
        print(f"⚠ {git_warn}")
    if push_warn:
        print(f"⚠ {push_warn}")
    return 0


def cmd_list(dtype: str | None) -> int:
    rp = config.repo_path()
    for meta, _body, path in store.iter_all(rp, include_staging=True):
        if dtype and meta.get("type") != dtype:
            continue
        stg = " (staging)" if "staging" in str(path) else ""
        print(f"[{meta['id']}]{stg} {meta.get('title','')} ｜ {meta.get('confidence')}·{meta.get('status')}")
    return 0


def cmd_import(directory: str, dtype: str, scope: str, staging: bool, source: str,
               mode: str = "source") -> int:
    """批量导入原始材料为 Source Artifact（P0，2026-09-21）。

    只写 sources/（objects 文本快照 + manifests 元数据），不生成知识卡、不进入默认检索。
    --type/--staging 仅为命令兼容保留，一律忽略；可复用知识由 memory_save 按八项小节结构提炼。

    M0.5: 锁外读取解析，锁内复核 hash/归属，避免解析期间文件变化形成错配。
    M4-4: 新增 --mode extract，入队 extract 任务供提炼，不直接导入为 source。
    """
    import hashlib

    src_dir = Path(directory).expanduser()
    if not src_dir.is_dir():
        print(f"错误：目录不存在 {src_dir}")
        return 1
    rp = config.repo_path()
    cfg = config.load_config()
    src_dir = src_dir.resolve()
    if src_dir == rp.resolve() or rp.resolve() in src_dir.parents or src_dir in rp.resolve().parents:
        print("错误：导入目录与记忆库不可互相包含")
        return 1
    supported = {".md", ".txt", ".pdf", ".docx", ".xlsx", ".pptx", ".html", ".htm",
                 ".png", ".jpg", ".jpeg", ".py", ".js", ".ts", ".java", ".log"}
    files = [f for f in sorted(src_dir.rglob("*"))
             if f.is_file() and f.suffix.lower() in supported
             and not any(part.startswith(".") for part in f.relative_to(src_dir).parts)
             and not f.is_symlink() and src_dir in f.resolve().parents]
    if not files:
        print("目录中未找到支持的文档文件")
        return 1

    # M4-4: extract 模式
    if mode == "extract":
        return _cmd_import_extract(files, src_dir, scope, source, rp, cfg)

    print("导入只生成 source artifact（--type/--staging 已忽略）：原始材料保真留存，不产生知识卡、不进入默认检索。")

    # === 阶段 1：锁外读取 + 解析（不持有 RepoLock）===
    parsed_items: list[dict] = []
    skipped = 0
    for f in files:
        import re as _re
        # 锁外读取原始字节并计算 hash
        try:
            raw_bytes = f.read_bytes()
            raw_hash = hashlib.sha256(raw_bytes).hexdigest()
            raw_mtime = f.stat().st_mtime
        except Exception as exc:
            skipped += 1
            print(f"  跳过（读取失败: {exc}）：{f.name}")
            continue

        # 锁外解析
        if f.suffix.lower() == ".md":
            body = raw_bytes.decode("utf-8", errors="replace").strip()
            parsed_title = ""
        else:
            try:
                from .parsers import register_builtin, registry
                register_builtin()
                parser = registry().find(f)
                if parser is None:
                    raise ValueError(f"无可用解析器: {f.suffix}")
                parsed = parser.parse(f)
                body = parsed.text.strip()
                parsed_title = str(parsed.meta.get("title", ""))
            except Exception as exc:
                skipped += 1
                print(f"  跳过（解析失败: {exc}）：{f.name}")
                continue
        if not body:
            skipped += 1
            continue
        if store.SECRET_RE.search(body):
            skipped += 1
            print(f"  跳过（疑似明文凭据）：{f.name}")
            continue
        m = _re.search(r"^#\s+(.+)$", body, _re.M)
        title = (m.group(1).strip() if m else parsed_title or f.stem)[:80]

        parsed_items.append({
            "file": f,
            "body": body,
            "title": title,
            "raw_hash": raw_hash,
            "raw_mtime": raw_mtime,
            "fmt": f.suffix.lower().lstrip("."),
        })

    if not parsed_items:
        print(f"完成：导入 0 个 source artifact，跳过 {skipped}（重复、空文件或被拦截）。")
        return 0

    # === 阶段 2：锁内复核 hash + 落库 ===
    imported = 0
    written_paths: list[Path] = []
    with RepoLock(rp, cfg.get("lock_timeout", 10.0)):
        conn = index.connect(rp)
        for item in parsed_items:
            f = item["file"]
            # 锁内复核：文件是否在解析期间被修改
            try:
                current_mtime = f.stat().st_mtime
                if current_mtime != item["raw_mtime"]:
                    skipped += 1
                    print(f"  跳过（解析期间文件已变化）：{f.name}")
                    continue
            except Exception:
                skipped += 1
                print(f"  跳过（复核失败）：{f.name}")
                continue

            sid, digest, created = sources.import_source(
                rp, item["body"], title=item["title"], scope=scope, fmt=item["fmt"],
                parser_version="markdown-direct" if f.suffix.lower() == ".md" else "parsers-v1",
                imported_by=source, kind=sources.kind_for(f.suffix),
                source_modified_at=datetime.fromtimestamp(item["raw_mtime"]).isoformat(timespec="seconds"),
            )
            if not created:
                skipped += 1
                print(f"  跳过（内容重复，已有 {sid}）：{f.name}")
                continue
            index.record_source_state(
                conn, str(f.resolve()), digest,
                datetime.fromtimestamp(item["raw_mtime"]).isoformat(timespec="seconds"),
            )
            written_paths.append(rp / sources.OBJECTS_DIR / f"{digest}.md")
            written_paths.append(rp / sources.MANIFESTS_DIR / f"{sid}.yaml")
            imported += 1
            print(f"  {sid}  {item['title'][:44]}")
        if imported:
            index.record_usage(conn, "import", detail=f"{imported} source artifacts from {src_dir.name}")
        conn.close()
        git_warn = gitops.commit_paths(
            rp, f"import(source): {imported} artifacts ← {src_dir.name}（scope={scope}，不产生知识卡）",
            written_paths,
        ) if imported and cfg["git"].get("auto_commit", True) else None
    push_warn = None if git_warn or not imported or not cfg["git"].get("auto_push") else \
        gitops.schedule_push(rp, cfg["git"])
    print(f"完成：导入 {imported} 个 source artifact（sources/objects + sources/manifests），跳过 {skipped}（重复、空文件或被拦截）。")
    print("原始材料不进入默认检索；提炼可复用结论请用 memory_save 按八项小节结构提炼。")
    if git_warn:
        print(f"⚠ {git_warn}")
    if push_warn:
        print(f"⚠ {push_warn}")
    return 0


def _cmd_import_extract(files: list[Path], src_dir: Path, scope: str, source: str,
                        rp: Path, cfg: dict) -> int:
    """M4-4: extract 模式 — 解析文档并入队 extract 任务供提炼。

    流程：
    1. 锁外解析文档（使用 parsers.py）
    2. 锁内入队 extract job（每个文档一个 job）
    3. 返回 job_id 列表
    """
    from . import jobs
    from .extraction.parsers import parse_document

    print(f"extract 模式：解析 {len(files)} 个文件并入队提炼任务...")

    # 阶段 1：锁外解析
    parsed_docs: list[dict] = []
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
                print(f"  跳过（疑似明文凭据）：{f.name}")
                continue
            parsed_docs.append({
                "file": f,
                "doc": doc,
                "title": doc.meta.get("title", f.stem)[:80],
            })
        except Exception as exc:
            skipped += 1
            print(f"  跳过（解析失败: {exc}）：{f.name}")
            continue

    if not parsed_docs:
        print(f"完成：入队 0 个 extract 任务，跳过 {skipped}。")
        return 0

    # 阶段 2：锁内入队
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
                import hashlib
                content_hash = hashlib.sha256(doc.text.encode("utf-8")).hexdigest()[:16]
                idem_key = f"extract:{f}:{content_hash}"

                result = jobs.enqueue(
                    conn, jobs.KIND_EXTRACT, scope, materials,
                    idempotency_key=idem_key,
                )
                if result["created"]:
                    # 设置为 awaiting_agent 状态
                    jobs.set_preparing(conn, result["job_id"])
                    jobs.set_awaiting(conn, result["job_id"], len(doc.segments))
                    enqueued += 1
                    job_ids.append(result["job_id"])
                    print(f"  {result['job_id']}  {item['title'][:44]}  ({len(doc.segments)} segments)")
                else:
                    skipped += 1
                    print(f"  跳过（重复任务）：{f.name}")

            if enqueued:
                index.record_usage(conn, "import-extract",
                                  detail=f"{enqueued} extract jobs from {src_dir.name}")
        finally:
            conn.close()

    print(f"完成：入队 {enqueued} 个 extract 任务，跳过 {skipped}。")
    if job_ids:
        print(f"任务 ID：{', '.join(job_ids[:5])}{'...' if len(job_ids) > 5 else ''}")
        print("使用 memory_job_claim 领取任务，memory_job_submit 提交提炼结果。")
    return 0


def cmd_stats() -> int:
    from .server import stats_impl
    print(stats_impl())
    return 0


def cmd_doctor(json_output: bool = False) -> int:
    """检查现有 Markdown + memory.db + Git 单库运行状态。"""
    rp = config.repo_path()
    checks: list[dict] = []
    db = rp / index.DB_NAME
    if not db.exists():
        checks.append({"name": "memory_db", "level": "fail", "message": f"不存在：{db}"})
    else:
        try:
            conn = index.connect(rp)
            meta_n = conn.execute("SELECT COUNT(*) FROM meta").fetchone()[0]
            fts_n = conn.execute("SELECT COUNT(*) FROM mem_fts").fetchone()[0]
            staging_n = conn.execute("SELECT COUNT(*) FROM meta WHERE staging=1").fetchone()[0]
            stale_n = conn.execute("SELECT COUNT(*) FROM meta WHERE status='stale'").fetchone()[0]
            md_n = sum(1 for _ in store.iter_all(rp, include_staging=True))
            conn.execute("SELECT id FROM mem_fts WHERE mem_fts MATCH 'doctor' LIMIT 1").fetchall()
            level = "pass" if meta_n == fts_n == md_n else "fail"
            checks.append({"name": "index_parity", "level": level,
                           "message": f"markdown={md_n} meta={meta_n} fts={fts_n}"})
            checks.append({"name": "governance", "level": "warn" if stale_n else "pass",
                           "message": f"staging={staging_n} stale={stale_n}"})
            emb_n = conn.execute("SELECT COUNT(*) FROM embedding_index").fetchone()[0]
            checks.append({"name": "embedding_index", "level": "pass" if emb_n == meta_n else "fail",
                           "message": f"embedding={emb_n} meta={meta_n}"})
            sync_row = conn.execute(
                "SELECT status,error,last_fetch_at,last_push_at FROM sync_state "
                "ORDER BY COALESCE(last_fetch_at,last_push_at) DESC LIMIT 1"
            ).fetchone()
            if sync_row:
                checks.append({"name": "sync", "level": "pass" if sync_row[0] == "ok" else "warn",
                               "message": f"status={sync_row[0]} fetch={sync_row[2]} push={sync_row[3]} error={sync_row[1] or ''}"})
            else:
                checks.append({"name": "sync", "level": "warn", "message": "尚无远程同步记录"})
        except Exception as exc:
            checks.append({"name": "memory_db", "level": "fail", "message": repr(exc)})
        finally:
            try: conn.close()
            except Exception: pass
    try:
        lines = gitops._run(rp, "status", "--porcelain", "--branch", timeout=5).splitlines()
        dirty = max(0, len(lines) - 1)
        checks.append({"name": "git", "level": "warn" if dirty else "pass",
                       "message": f"{lines[0] if lines else 'unknown branch'}; dirty={dirty}"})
    except Exception as exc:
        checks.append({"name": "git", "level": "fail", "message": repr(exc)})
    import importlib.util
    parser_modules = ("pypdf", "docx", "openpyxl", "pptx", "PIL", "pytesseract")
    missing = [m for m in parser_modules if importlib.util.find_spec(m) is None]
    checks.append({"name": "parsers", "level": "warn" if missing else "pass",
                   "message": "missing=" + (",".join(missing) if missing else "none")})
    checks.append({"name": "ocr_binary", "level": "pass" if shutil.which("tesseract") else "warn",
                   "message": shutil.which("tesseract") or "tesseract 未安装，图片 OCR 不可用"})
    if json_output:
        print(json.dumps(checks, ensure_ascii=False, indent=2))
    else:
        for c in checks:
            print(f"[{c['level'].upper()}] {c['name']}: {c['message']}")
    return 2 if any(c["level"] == "fail" for c in checks) else 0


def cmd_alerts(dry_run: bool = False) -> int:
    """单库告警入口：直接复用 doctor，非正常状态由调度器根据退出码通知。"""
    if dry_run:
        print("[dry-run] 单库 doctor 检查如下：")
    return cmd_doctor(json_output=False)


def cmd_jobs_list(scope: str = "", kind: str = "", state: str = "", limit: int = 50) -> int:
    """列出任务队列。"""
    rp = config.repo_path()
    conn = index.connect(rp)
    try:
        items = jobs.list_jobs(conn, scope=scope, kind=kind, state=state, limit=limit)
        if not items:
            print("无任务")
            return 0
        for item in items:
            err = f" error={item['error']}" if item["error"] else ""
            print(f"[{item['job_id']}] {item['kind']} scope={item['scope']} "
                  f"state={item['state']} attempt={item['attempt']} "
                  f"cursor={item['cursor']}/{item['total']}{err}")
    finally:
        conn.close()
    return 0


def cmd_jobs_status(job_id: str) -> int:
    """查询单个任务状态。"""
    rp = config.repo_path()
    conn = index.connect(rp)
    try:
        s = jobs.status(conn, job_id)
        if not s:
            print(f"未找到任务 {job_id}")
            return 1
        print(f"任务 {s['job_id']}（{s['kind']}）")
        print(f"  scope: {s['scope']}")
        print(f"  state: {s['state']}")
        print(f"  attempt: {s['attempt']}/{s['max_attempts']}")
        print(f"  cursor: {s['cursor']}/{s['total']}")
        print(f"  retryable: {s['retryable']}")
        print(f"  error: {s['error'] or '无'}")
        print(f"  created: {s['created_at']}")
        print(f"  updated: {s['updated_at']}")
    finally:
        conn.close()
    return 0


def cmd_jobs_retry(job_id: str) -> int:
    """重试失败任务。"""
    rp = config.repo_path()
    conn = index.connect(rp)
    try:
        result = jobs.retry(conn, job_id)
        if "error" in result:
            print(f"错误：{result['error']}")
            return 1
        print(f"已重置 {job_id} → queued")
    finally:
        conn.close()
    return 0


def cmd_jobs_expire() -> int:
    """回收过期租约。"""
    rp = config.repo_path()
    conn = index.connect(rp)
    try:
        n = jobs.expire_leases(conn)
        print(f"已回收 {n} 个过期租约")
    finally:
        conn.close()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="knowbase", description="薪火：跨 Agent 经验记忆库")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_init = sub.add_parser("init", help="初始化/补全（幂等）")
    p_init.add_argument("--import-from", help="现存文档目录，导入为 source artifact（不产生知识卡）")
    p_init.add_argument("--scope", help="明确项目范围；通用资料用 global")
    p_init.add_argument("--type", choices=store.TYPES, default="workflow",
                        help="（已废弃，兼容保留：导入一律生成 source）")
    p_dash = sub.add_parser("dashboard", help="生成本地 HTML 知识治理看板")
    p_dash.add_argument("--output", default="knowbase-dashboard.html")
    p_dash.add_argument("--open", action="store_true")
    p_history = sub.add_parser("history", help="查看搜索命中记录 JSON")
    p_history.add_argument("--limit", type=int, default=100)
    sub.add_parser("reindex", help="全量重建索引")
    p_verify = sub.add_parser("verify", help="人工确认有效")
    p_verify.add_argument("id")
    p_verify.add_argument("--expect-revision", default="", help="期望的 governance_revision（可选，用于 CAS 检查）")
    p_arch = sub.add_parser("archive", help="人工归档")
    p_arch.add_argument("id")
    p_arch.add_argument("--expect-revision", default="", help="期望的 governance_revision（可选，用于 CAS 检查）")
    p_pro = sub.add_parser("promote", help="激活 staging 提案")
    p_pro.add_argument("id")
    p_rev = sub.add_parser("revise", help="人工修订已生效的标准/偏好/业务规则")
    p_rev.add_argument("id")
    p_rev.add_argument("--body-file", required=True)
    p_rev.add_argument("--title")
    p_rev.add_argument("--expect-revision", default="", help="期望的 content_revision（可选，用于 CAS 检查）")
    p_list = sub.add_parser("list", help="列出记忆")
    p_list.add_argument("type", nargs="?", choices=store.TYPES)
    sub.add_parser("stats", help="统计")
    p_doc = sub.add_parser("doctor", help="单库健康检查（memory.db/FTS/Git/治理）")
    p_doc.add_argument("--json", action="store_true", help="以 JSON 列表输出")
    p_alerts = sub.add_parser("alerts", help="输出单库异常状态，供调度器通知")
    p_alerts.add_argument("--dry-run", action="store_true",
                          help="不实际派发 webhook/文件，仅 stdout 打印事件清单")
    p_hook = sub.add_parser("hook", help="Agent hook 入口")
    p_hook.add_argument("event", choices=["session-start", "user-prompt", "stop"])
    p_hook.add_argument("--style", choices=["claude", "zcode"], default="claude")
    p_imp = sub.add_parser("import", help="批量导入为 source artifact（不产生知识卡、不进默认检索）")
    p_imp.add_argument("directory")
    p_imp.add_argument("--type", choices=store.TYPES, default="pitfall",
                       help="（已废弃，兼容保留：导入一律生成 source）")
    p_imp.add_argument("--scope", default="global")
    p_imp.add_argument("--source", default="human:import")
    p_imp.add_argument("--staging", action="store_true",
                       help="（已废弃，兼容保留：导入一律生成 source）")
    p_imp.add_argument("--mode", choices=["source", "extract"], default="source",
                       help="导入模式：source=直接导入为 source artifact，extract=入队 extract 任务供提炼")
    sub.add_parser("serve", help="启动 MCP 服务（stdio）")
    p_jobs = sub.add_parser("jobs", help="任务队列管理")
    jobs_sub = p_jobs.add_subparsers(dest="jobs_cmd", required=True)
    p_jobs_list = jobs_sub.add_parser("list", help="列出任务")
    p_jobs_list.add_argument("--scope", default="")
    p_jobs_list.add_argument("--kind", default="")
    p_jobs_list.add_argument("--state", default="")
    p_jobs_list.add_argument("--limit", type=int, default=50)
    p_jobs_status = jobs_sub.add_parser("status", help="查询任务状态")
    p_jobs_status.add_argument("job_id")
    p_jobs_retry = jobs_sub.add_parser("retry", help="重试失败任务")
    p_jobs_retry.add_argument("job_id")
    jobs_sub.add_parser("expire", help="回收过期租约")
    args = parser.parse_args(argv)

    if args.cmd == "init":
        return cmd_init(args.import_from, args.scope, args.type)
    if args.cmd == "dashboard":
        from .dashboard import generate
        output = generate(config.repo_path(), Path(args.output).expanduser().resolve())
        print(f"✓ 看板已生成 {output}")
        if args.open:
            import webbrowser
            webbrowser.open(output.as_uri())
        return 0
    if args.cmd == "history":
        conn = index.connect(config.repo_path())
        try:
            print(json.dumps(index.search_history(conn, max(1, min(args.limit, 10000))), ensure_ascii=False, indent=2))
        finally:
            conn.close()
        return 0
    if args.cmd == "reindex":
        return cmd_reindex()
    if args.cmd == "verify":
        return cmd_verify(args.id, expect_revision=getattr(args, "expect_revision", ""))
    if args.cmd == "archive":
        return cmd_archive(args.id, expect_revision=getattr(args, "expect_revision", ""))
    if args.cmd == "promote":
        return cmd_promote(args.id)
    if args.cmd == "revise":
        return cmd_revise(args.id, args.body_file, args.title,
                          expect_revision=getattr(args, "expect_revision", ""))
    if args.cmd == "list":
        return cmd_list(args.type)
    if args.cmd == "stats":
        return cmd_stats()
    if args.cmd == "doctor":
        return cmd_doctor(json_output=args.json)
    if args.cmd == "alerts":
        return cmd_alerts(dry_run=args.dry_run)
    if args.cmd == "import":
        return cmd_import(args.directory, args.type, args.scope, args.staging, args.source,
                         mode=getattr(args, "mode", "source"))
    if args.cmd == "hook":
        from . import hooks
        {"session-start": hooks.cmd_session_start,
         "user-prompt": hooks.cmd_user_prompt,
         "stop": hooks.cmd_stop}[args.event](style=args.style)
        return 0
    if args.cmd == "serve":
        from .server import main as serve
        serve()
        return 0
    if args.cmd == "jobs":
        if args.jobs_cmd == "list":
            return cmd_jobs_list(args.scope, args.kind, args.state, args.limit)
        if args.jobs_cmd == "status":
            return cmd_jobs_status(args.job_id)
        if args.jobs_cmd == "retry":
            return cmd_jobs_retry(args.job_id)
        if args.jobs_cmd == "expire":
            return cmd_jobs_expire()
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
