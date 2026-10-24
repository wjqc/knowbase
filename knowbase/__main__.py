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
import sqlite3
import shutil
import sys
from datetime import date, datetime
from pathlib import Path

from . import config, gitops, index, jobs, lifecycle, sources, store
from .locking import RepoLock

GITIGNORE = "memory.db\nmemory.db-wal\nmemory.db-shm\n.lock\n*.log\n.idea/\n.vscode/\n.DS_Store\nINDEX.md\n.knowbase/local/\n"

REPO_README = """# 薪火（knowbase）经验记忆库

机器高频写入的热记忆层，8 个 AI Agent 共享读写。
- 检索/读写走 MCP 工具（memory_search / read / save / update / feedback / stats）
- INDEX.md 为自动生成的速览，勿手改
- standards/（人员标准）与 preferences/ 只能由人直接写或经 staging/ 提案激活
- memory.db 包含派生索引与不可重建的本地运行日志，请一起备份；reindex 保留运行日志
"""


def _retire_shared_index(repo: Path, cfg: dict) -> str | None:
    """显式 init 升级时退役派生 INDEX；保留物理文件和用户改动。"""
    tracked = bool(gitops._run(repo, "ls-files", "--", "INDEX.md"))
    ignore = repo / ".gitignore"
    previous = ignore.read_text() if ignore.exists() else ""
    missing = [line for line in ("INDEX.md", ".knowbase/local/") if line not in previous.splitlines()]
    if not tracked and not missing:
        return None
    staged = gitops._run(repo, "diff", "--cached", "--name-only").splitlines()
    if staged or (ignore.exists() and gitops._run(repo, "diff", "--", ".gitignore")):
        return "INDEX 退役未执行：保留已有暂存内容/.gitignore 修改，请处理后再次 init"
    if missing:
        ignore.write_text(previous.rstrip() + "\n" + "\n".join(missing) + "\n")
    if tracked:
        gitops._run(repo, "rm", "--cached", "--", "INDEX.md")
        if cfg["git"].get("auto_commit", True):
            gitops._run(repo, "add", "--", ".gitignore")
            staged = gitops._run(repo, "diff", "--cached", "--name-only").splitlines()
            if set(staged) - {".gitignore", "INDEX.md"}:
                return "INDEX 已退役待提交：检测到并行暂存，未卷入其他修改"
            gitops._run(repo, "commit", "-m", "chore: keep generated INDEX and local materials out of shared Git", timeout=15)
        return "INDEX 已退役为本地视图，原文件保留" + ("（待人工提交）" if not cfg["git"].get("auto_commit", True) else "")
    return None


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
        print("✓ git 仓库已存在（保留已有仓库与业务文件）")

    # 4. 派生索引 + INDEX.md（全量重建，天然幂等）
    with RepoLock(rp, 10.0):
        try:
            note = _retire_shared_index(rp, cfg)
            if note:
                print(f"○ {note}")
        except Exception as exc:
            print(f"⚠ INDEX 本地视图迁移未完成：{type(exc).__name__}，原内容保留")
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
    # P4：init 收尾输出接入就绪四项（只读、失败不阻断 init 成功）。
    try:
        from .onboarding import readiness
        ready = readiness(rp)
        print("接入就绪："
              f"配置={'✓' if ready['config_complete'] else '✗'} "
              f"协议={'✓' if ready['protocol_pass'] else '✗'} "
              f"宿主已加载={','.join(ready['hosts_loaded']) or '无'} "
              f"真实任务验证={'✓' if ready['real_task_verified'] else '未验证'}")
        if ready["reconnect_required"]:
            print(f"需重连的旧 build 会话：{','.join(ready['reconnect_required'])}"
                  "（重启该宿主的 knowbase serve 后恢复）")
    except Exception as exc:
        print(f"○ 就绪状态读取失败：{type(exc).__name__}（不影响 init 结果）")
    return 0


def cmd_reindex() -> int:
    rp = config.repo_path()
    with RepoLock(rp, 10.0):
        n = index.rebuild(rp)
    print(f"✓ 已全量重建：{n} 条记忆")
    return 0


def _human_op(rp, fn, mid, label, expect_revision: str = ""):
    """人工操作（verify/archive）的通用流程。

    expect_revision: 可选的 governance_revision CAS 检查。检查必须在
    RepoLock 内执行，与随后的写入处于同一临界区：锁外检查通过后、
    写入执行前状态可被其他写入者改变（TOCTOU），检查结论会失效。
    """
    from .mutations import _compute_governance_revision, _compute_revision, REVISION_CONFLICT

    cfg = config.load_config()

    with RepoLock(rp, cfg.get("lock_timeout", 10.0)):
        # CAS 检查（如果提供了 expect_revision）：与 fn 的写入同临界区。
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
        # verify/archive 只改治理字段，内容修订号不变，写前版本即当前版本
        git_warn = gitops.commit_paths(
            rp, gitops.with_base_trailers(f"{label}({mid}): by human",
                                          [(mid, _compute_revision(meta2, body))]), [path]) \
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
        # 人工激活是治理动作：审核者事件与索引更新同事务（CLI 为人工通道）。
        from .mutations import _compute_revision
        from .value.events import record as _record_value
        _record_value(conn, "knowledge.activated",
                      {"memory_id": mid, "content_revision": _compute_revision(meta, body),
                       "scope": meta.get("scope", "")},
                      source_key=f"knowledge:{mid}:activated", host="cli")
        conn.commit()
        conn.close()
        index.build_index_md(rp, cfg)  # 本地重建，不提交
        # INDEX.md 已退役，不再提交到 Git
        git_warn = gitops.commit_paths(
            rp, gitops.with_base_trailers(
                f"promote({mid}): staging → {store.TYPE_DIR[dtype]}（人工审核通过）",
                [(mid, _compute_revision(meta, body))]),
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
        prior_content_revision = _compute_revision(meta,_old_body)
        if expect_revision and expect_revision != prior_content_revision:
            print("错误：版本冲突（等待写锁期间内容已变化），未修改。")
            return 1
        if title is not None:
            meta["title"] = title.strip()
        if _compute_revision(meta,new_body) != prior_content_revision:
            meta.update(confidence="once",last_verified="",verified_content_revision="",
                        helpful_count=0,unhelpful_count=0)
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
        git_warn = gitops.commit_paths(
            rp, gitops.with_base_trailers(f"revise({mid}): {meta['title']} by human",
                                          [(mid, prior_content_revision)]),
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
        stg = " (staging)" if path.parent.name == "staging" else ""
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

    from .extraction.parsers import importable_extensions

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
    supported = importable_extensions()
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

    载荷构造与 MCP memory_import（server.import_impl）共用同一实现：
    materials.capture_payload 切分并按存储内容计算 text_hash/input_version，
    register_manifest 登记 job_segments。禁止再手搓 dict 或截断正文——
    截断后存储的 text 与全文哈希对不上，证据校验必然失败（旧实现即如此）。
    """
    from . import jobs
    from . import materials as material_store
    from .adapters import normalizer
    from .extraction.parsers import parse_document

    print(f"extract 模式：解析 {len(files)} 个文件并入队提炼任务...")

    # 阶段 1：锁外解析 + 构建载荷（与 MCP 路径同一构造）
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
            event = normalizer.normalize_event({"role": "user", "text": doc.text, "id": str(f)})
            payload = material_store.capture_payload(
                [event], scope=scope, session_id="document:" + str(f))
            payload.update(source_path=str(f), source_format=doc.format,
                           title=doc.meta.get("title", f.stem)[:80], text=doc.text)
            parsed_docs.append({
                "file": f,
                "doc": doc,
                "payload": payload,
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
                payload = item["payload"]
                # 幂等键与 MCP 路径同款：scope + 文件路径 + input_version
                idem_key = f"extract:{scope}:{f}:{payload['input_version']}"

                result = jobs.enqueue(
                    conn, jobs.KIND_EXTRACT, scope, payload,
                    idempotency_key=idem_key,
                )
                if result.get("error"):
                    skipped += 1
                    print(f"  跳过（入队失败 {result['error']}）：{f.name}")
                    continue
                if result["created"]:
                    material_store.register_manifest(conn, result["job_id"], payload)
                    # 设置为 awaiting_agent 状态
                    jobs.set_preparing(conn, result["job_id"])
                    jobs.set_awaiting(conn, result["job_id"], len(payload["segments"]))
                    enqueued += 1
                    job_ids.append(result["job_id"])
                    print(f"  {result['job_id']}  {item['title'][:44]}  ({len(payload['segments'])} segments)")
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


def cmd_doctor(json_output: bool = False, probe_embedding=False) -> int:
    """Read-only health checks; never runs migration, sync, or telemetry."""
    from .doctor import inspect
    checks = inspect(config.repo_path())
    if probe_embedding:
        from .model_setup import inspect_connection
        result=inspect_connection()
        checks.append({"name":"embedding_probe","level":"pass" if result.get("connection_verified") else "warn","message":json.dumps(result,ensure_ascii=False)})
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


def cmd_archive_sweep() -> int:
    """Phase 1：自动归档 stale 满期且近 X 天内无成功复验证据的卡片。"""
    from .lifecycle import auto_archive_sweep
    rp = config.repo_path()
    conn = index.connect(rp)
    try:
        archived = auto_archive_sweep(rp, conn)
        print(json.dumps({"archived": archived}, ensure_ascii=False, indent=2))
    finally:
        conn.close()
    return 0


def cmd_review_sweep() -> int:
    """Phase 1：自动丢弃超期未补充出处的 needs_review 候选。"""
    from .extraction import materialize
    rp = config.repo_path()
    ttl = config.load_config().get("evolution", {}).get("review_candidate_ttl_days", 30)
    conn = index.connect(rp)
    try:
        discarded = materialize.sweep_review_candidates(conn, ttl_days=ttl)
        print(json.dumps({"discarded": discarded}, ensure_ascii=False, indent=2))
    finally:
        conn.close()
    return 0


def cmd_scope_resolve(path: str | None) -> int:
    """P4：解析一条路径的 scope 绑定（只读；未解析时保持 scope_unresolved）。"""
    import os
    from .scope_binding import resolve_binding
    rp = config.repo_path()
    conn = index.connect(rp)
    try:
        result = resolve_binding(conn, path or os.getcwd())
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        conn.close()
    return 0


def cmd_scope_bind(path: str, scope: str) -> int:
    """P4：显式绑定一条路径到 scope（人工确认的映射，优先级最高）。"""
    from .scope_binding import bind_explicit
    rp = config.repo_path()
    conn = index.connect(rp)
    try:
        result = bind_explicit(conn, path, scope)
        conn.commit()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        conn.close()
    return 0


def cmd_maintenance_run() -> int:
    """P4：执行一次定期维护（真实入口：崩溃恢复→归档→review 清理→冲突复验）。"""
    from .maintenance import run_maintenance
    rp = config.repo_path()
    conn = index.connect(rp)
    try:
        report = run_maintenance(rp, conn)
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    finally:
        conn.close()
    return 0


def cmd_maintenance_status() -> int:
    """P4：维护状态快照（只读）。"""
    from .maintenance import maintenance_status
    rp = config.repo_path()
    conn = index.connect(rp)
    try:
        print(json.dumps(maintenance_status(rp, conn), ensure_ascii=False, indent=2))
    finally:
        conn.close()
    return 0


def cmd_onboarding() -> int:
    """P4：接入就绪状态（四项分别输出）+ 本次新提示（提示过的键不再重复）。"""
    from .onboarding import report
    from .sessions import mark_notified
    rp = config.repo_path()
    rep = report(rp)
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    mark_notified([n["key"] for n in rep["new_notices"]])
    return 0


def cmd_executor_probe(isolation: bool = False, scope: str = "probe-orders") -> int:
    """P1 执行器探针：静态探针全部候选，可选对首个 unknown 候选跑隔离探针。

    只输出能力报告 JSON，不输出任何密钥引用值。返回 0 表示至少一个
    执行器 verified；返回 2 表示无候选通过，自动提炼批次保持阻断。
    """
    from .automation import executor as probe

    cfg = config.load_config()
    configured = cfg.get("automation", {}).get("executors", []) or []
    discovered = probe.discover_executors()
    seen = {str(c.get("executor_id")) for c in configured}
    candidates = list(configured) + [c for c in discovered
                                     if str(c.get("executor_id")) not in seen]
    reports = probe.probe_all(candidates)
    isolation_report = None
    chosen = None
    for report in reports:
        if report.status == "verified":
            chosen = report
            break
    if isolation and chosen is None:
        unknowns = [r for r in reports if r.status == "unknown"]
        if unknowns:
            target = unknowns[0]
            target_cfg = next(
                (c for c in candidates
                 if str(c.get("executor_id") or c.get("command")) == target.executor_id),
                None)
            if target_cfg and target_cfg.get("kind", "command") == "command":
                try:
                    from .automation.isolation import isolated_probe
                    isolation_report = isolated_probe(target_cfg, scope=scope)
                    if isolation_report.get("passed"):
                        target.status = "verified"
                        target.reason_code = None
                        chosen = target
                    else:
                        target.status = "blocked"
                        target.reason_code = "isolation_failed"
                except Exception as exc:  # 探针失败不掩盖静态结论
                    isolation_report = {"passed": False, "failure_reasons": ["executor_error"],
                                        "detail": f"{type(exc).__name__}: {str(exc)[:200]}"}
    out = {"executors": [r.to_dict() for r in reports],
           "diagnoses": [probe.diagnose(r) for r in reports],
           "selected": chosen.executor_id if chosen else None,
           "blocked": chosen is None,
           "isolation": isolation_report}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if chosen is not None else 2


def _main(argv=None):
    parser = argparse.ArgumentParser(prog="knowbase", description="薪火：跨 Agent 经验记忆库")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_init = sub.add_parser("init", help="初始化/补全（幂等）")
    p_init.add_argument("--import-from", help="现存文档目录，导入为 source artifact（不产生知识卡）")
    p_init.add_argument("--scope", help="明确项目范围；通用资料用 global")
    p_init.add_argument("--type", choices=store.TYPES, default="workflow",
                        help="（已废弃，兼容保留：导入一律生成 source）")
    p_init.add_argument("--agents", help="接入 claude-code,zcode,trae,codex,cursor,opencode")
    p_init.add_argument("--embedding", choices=["skip","configure"], default="skip")
    p_init.add_argument("--embedding-config", help="只含地址/模型/环境变量引用的JSON，不允许明文密钥")
    p_init.add_argument("--probe-embedding", action="store_true")
    p_init.add_argument("--list-embedding-models", action="store_true")
    p_init.add_argument("--verify-mcp", action="store_true", help="在隔离库检查固定runtime的stdio/build，非真实宿主会话")
    p_init.add_argument("--non-interactive", action="store_true")
    p_init.add_argument("--plan", action="store_true", help="仅显示接入计划，不写库/配置")
    p_dash = sub.add_parser("dashboard", help="生成本地 HTML 知识治理看板")
    p_dash.add_argument("--output", default="knowbase-dashboard.html")
    p_dash.add_argument("--open", action="store_true")
    p_dash.add_argument("--team-data", default="", help="团队快照 JSON（knowbase value snapshot 生成）")
    p_team = sub.add_parser("team", help="各电脑通过共享 Git 自动上报和汇总统计")
    team_sub = p_team.add_subparsers(dest="team_cmd", required=True)
    p_enroll = team_sub.add_parser("enroll", help="首次确认本机成员身份")
    p_enroll.add_argument("--member-id", required=True)
    p_enroll.add_argument("--label", required=True)
    p_enroll.add_argument("--publisher", action="store_true", help="指定本机单独发布 README")
    p_enroll.add_argument("--include-history", action="store_true", help="确认本库历史属于本人后回补，旧日标部分数据")
    p_enroll.add_argument("--source-timezone", default="Asia/Shanghai")
    for name in ("status", "collect", "sync", "install-worker", "remove-worker", "disable"):
        team_sub.add_parser(name)
    p_member = team_sub.add_parser("member", help="动态预登记、改名或退出（历史保留）")
    p_member.add_argument("--member-id", required=True)
    p_member.add_argument("--label", required=True)
    p_member.add_argument("--status", choices=["pending", "active", "retired"], required=True)
    p_recover = team_sub.add_parser("recover", help="检查恢复库来源后，保留旧日桶并开启恢复 epoch")
    p_recover.add_argument("--confirm-local-database", action="store_true", required=True)
    p_rebind = team_sub.add_parser("rebind-device", help="确认原本机数据库后重新绑定稳定设备标识；保留历史与ACK")
    p_rebind.add_argument("--confirm-local-device", action="store_true", required=True)
    p_publisher = team_sub.add_parser("publisher", help="明确转交 README 发布角色；先关闭原发布机")
    p_publisher.add_argument("--enable", action="store_true")
    p_history = sub.add_parser("history", help="查看搜索命中记录 JSON")
    p_history.add_argument("--limit", type=int, default=100)
    p_reindex=sub.add_parser("reindex", help="全量重建索引")
    p_reindex.add_argument("--embed", action="store_true", help="构建内网影子向量代际，质量合格后自动激活")
    p_reindex.add_argument("--deadline-seconds", type=float, default=60)
    p_sem=sub.add_parser("embedding-work", help="处理一个已有索引任务")
    p_sem.add_argument("--deadline-seconds",type=float,default=60)
    p_activate=sub.add_parser("embedding-activate", help="校验后激活/回切现有合格代际")
    p_activate.add_argument("generation")
    sub.add_parser("retrieval-report", help="只读汇总检索等级、降级、成本与采纳证据")
    p_benefit = sub.add_parser("benefit-control", help="同任务集用库/不用库对照，产出净 token 节省与时长对比（Phase 3-B）")
    p_benefit.add_argument("--tasks", required=True, help="任务集 JSON 或派生评测集 JSONL")
    p_benefit.add_argument("--executor", required=True, help="执行器 module:function，签名 executor(task, use_library) -> {input_tokens, output_tokens, seconds}")
    p_benefit.add_argument("--out", required=True, help="对照报告 JSON 输出路径")
    p_accept = sub.add_parser("acceptance-check", help="四项验收门槛机器判定（Phase 3-B），全部由落盘证据判定")
    p_accept.add_argument("--agg-db", required=True, help="汇总分析库路径")
    p_accept.add_argument("--team-id", default="default")
    p_accept.add_argument("--personal-db", default="", help="本机索引库路径（派生评测核验用）")
    p_accept.add_argument("--eval-report", default="", help="冻结评测报告 JSON")
    p_accept.add_argument("--dataset", default="", help="派生评测集 JSONL")
    p_accept.add_argument("--control-report", default="", help="对照报告 JSON")
    p_accept.add_argument("--out", default="", help="验收报告 JSON 输出路径（为空则只打印）")
    p_eval=sub.add_parser("retrieval-evaluate",help="离线五组重放，需要审核标签和模型缓存")
    p_eval.add_argument("--dataset",required=True);p_eval.add_argument("--cache",required=True)
    p_eval.add_argument("--baseline-source",required=True);p_eval.add_argument("--output",required=True)
    p_eval.add_argument("--dense-min",required=True,type=float);p_eval.add_argument("--gate-dense-min",required=True,type=float)
    p_cache=sub.add_parser("retrieval-cache",help="显式向内网模型缓存已批准的评测输入")
    p_cache.add_argument("--dataset",required=True);p_cache.add_argument("--output",required=True)
    p_privacy = sub.add_parser("privacy-scrub", help="只报告或备份后清洗观察/提炼队列中的凭据，不输出命中原文")
    p_privacy.add_argument("--apply", action="store_true")
    p_privacy.add_argument("--backup-dir", help="apply 必填：仓库外的新私有备份目录")
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
    p_metrics=sub.add_parser("metrics",help="只读导出本机隐私最小化计数JSON，不触网或迁移")
    p_metrics.add_argument("--since",default="",help="UTC含起点，ISO日期/时间")
    p_metrics.add_argument("--until",default="",help="UTC不含终点，ISO日期/时间")
    p_metrics.add_argument("--source-label",default="unknown",help="自行填写的设备标签，仅字母数字._-，不证明人数")
    p_metrics.add_argument("--naive-timezone",default="local",help="旧无时区日志解释，建议IANA名如Asia/Shanghai")
    p_doc = sub.add_parser("doctor", help="单库健康检查（memory.db/FTS/Git/治理）")
    p_doc.add_argument("--json", action="store_true", help="以 JSON 列表输出")
    p_doc.add_argument("--probe-embedding", action="store_true", help="显式发合成连接探针，不构建索引")
    p_alerts = sub.add_parser("alerts", help="输出单库异常状态，供调度器通知")
    p_alerts.add_argument("--dry-run", action="store_true",
                          help="不实际派发 webhook/文件，仅 stdout 打印事件清单")
    p_hook = sub.add_parser("hook", help="Agent hook 入口")
    p_hook.add_argument("event", choices=["session-start", "user-prompt", "stop", "tool-before", "tool-after", "tool-failure", "before-compact"])
    p_hook.add_argument("--style", choices=["claude", "zcode", "trae", "codex"], default="claude")
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
    sub.add_parser("archive-sweep", help="自动归档 stale 满期且近 X 天内无成功复验证据的卡片（Phase 1）")
    sub.add_parser("review-sweep", help="自动丢弃超期未补充出处的 needs_review 候选（Phase 1）")
    p_scope = sub.add_parser("scope", help="项目识别绑定（P4）")
    scope_sub = p_scope.add_subparsers(dest="scope_cmd", required=True)
    p_resolve = scope_sub.add_parser("resolve", help="解析一条路径的 scope 绑定（只读）")
    p_resolve.add_argument("path", nargs="?", default=None, help="待解析的本地路径，默认当前目录")
    p_bind = scope_sub.add_parser("bind", help="显式绑定一条路径到 scope（人工确认）")
    p_bind.add_argument("--path", required=True, help="本地路径")
    p_bind.add_argument("--scope", required=True, help="目标 scope")
    p_maint = sub.add_parser("maintenance", help="治理维护（P4）")
    maint_sub = p_maint.add_subparsers(dest="maint_cmd", required=True)
    maint_sub.add_parser("run", help="执行一次定期维护（真实入口）")
    maint_sub.add_parser("status", help="维护状态快照（只读）")
    sub.add_parser("onboarding", help="接入就绪状态：配置/协议/宿主加载/真实任务验证分别输出（P4）")
    p_value = sub.add_parser("value", help="团队价值事件文件汇总（显式导出/导入/快照，不自动上传）")
    value_sub = p_value.add_subparsers(dest="value_cmd", required=True)
    p_value_export = value_sub.add_parser("export", help="导出本机最小事件包（不改变事件状态）")
    p_value_export.add_argument("--output", required=True)
    p_value_export.add_argument("--since", default="", help="UTC 含起点")
    p_value_export.add_argument("--until", default="", help="UTC 不含终点")
    p_value_import = value_sub.add_parser("import", help="汇总机导入事件包并生成接收回执")
    p_value_import.add_argument("package")
    p_value_import.add_argument("--agg-db", default="")
    p_value_import.add_argument("--team-id", default="default")
    p_value_import.add_argument("--receipt-out", default="")
    p_value_receipt = value_sub.add_parser("receipt", help="源端核对回执并确认已接收事件")
    p_value_receipt.add_argument("receipt_file")
    p_value_snapshot = value_sub.add_parser("snapshot", help="从汇总库生成最小团队快照")
    p_value_snapshot.add_argument("--output", required=True)
    p_value_snapshot.add_argument("--agg-db", default="")
    p_value_snapshot.add_argument("--team-id", default="default")
    p_value_snapshot.add_argument("--since", default="")
    p_value_snapshot.add_argument("--until", default="")
    p_value_member = value_sub.add_parser("register-member", help="登记团队成员（名单信息；覆盖率分母现为有归属的已登记实例）")
    p_value_member.add_argument("--agg-db", default="")
    p_value_member.add_argument("--member-id", required=True)
    p_value_member.add_argument("--label", default="")
    p_value_confirm = value_sub.add_parser("confirm-member", help="纠错入口：修正安装实例归属成员（留痕；默认归属走实例 enroll 链）")
    p_value_confirm.add_argument("--agg-db", default="")
    p_value_confirm.add_argument("--installation", required=True)
    p_value_confirm.add_argument("--member-id", required=True)
    p_value_confirm.add_argument("--actor", default="", help="纠错操作人（留痕）")
    p_value_confirm.add_argument("--note", default="", help="纠错说明（留痕）")
    p_value_report = value_sub.add_parser("report", help="确定性生成周/月报告（Markdown，无模型润色）")
    p_value_report.add_argument("--output", required=True)
    p_value_report.add_argument("--agg-db", default="")
    p_value_report.add_argument("--team-id", default="default")
    p_value_report.add_argument("--since", default="")
    p_value_report.add_argument("--until", default="")
    p_value_report.add_argument("--personal", action="store_true", help="附带本机事件窗口摘要")
    p_value_usage = value_sub.add_parser("usage", help="提取本机宿主请求级用量（当前支持 claude transcripts；zcode/trae 明确不支持）")
    p_value_usage.add_argument("--source", default="claude", choices=["claude", "zcode", "trae"])
    p_value_usage.add_argument("--path", default="", help="transcripts 根目录（默认 ~/.claude/projects）")
    p_sync = sub.add_parser("sync-once", help="执行已领取租约的后台同步工作")
    p_sync.add_argument("--remote", required=True)
    p_sync.add_argument("--branch", required=True)
    p_sync.add_argument("--lease-token", required=True)
    p_worker = sub.add_parser("worker", help="P3 后台 supervisor：OS 调度入口，SQLite 租约互斥（提炼位保持阻断）")
    worker_sub = p_worker.add_subparsers(dest="worker_cmd", required=True)
    for _name, _help in (("run", "常驻运行：OS 负责启动/保活，租约互斥，按 next_run_at 唤醒"),
                         ("once", "执行一次到期任务（持租约者）"),
                         ("status", "查看租约与各任务下次运行时间"),
                         ("install", "安装 worker 调度入口并迁移旧 team 定时任务"),
                         ("disable", "停用 worker 调度入口（含旧 team 定时任务）")):
        worker_sub.add_parser(_name, help=_help)
    p_exec = sub.add_parser("executor", help="提炼执行器探针（P1）")
    exec_sub = p_exec.add_subparsers(dest="executor_cmd", required=True)
    p_exec_probe = exec_sub.add_parser("probe", help="静态探针全部候选，可选跑隔离探针")
    p_exec_probe.add_argument("--isolation", action="store_true",
                              help="对首个 unknown 候选跑第一轮真实隔离探针")
    p_exec_probe.add_argument("--scope", default="probe-orders")
    args = parser.parse_args(argv)

    if args.cmd == "sync-once":
        from .refresh import run_once
        result = run_once(config.repo_path(), args.remote, args.branch, args.lease_token)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["state"] in ("completed", "cancelled") else 2

    if args.cmd == "metrics":
        from .metrics import export
        result=export(config.repo_path(),since=args.since,until=args.until,source_label=args.source_label,
                      naive_timezone=args.naive_timezone)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 2 if result.get("error") else 0

    if args.cmd == "init":
        from . import integration
        agents = [v.strip() for v in (args.agents or "").split(",") if v.strip()]
        if not args.agents and not args.non_interactive and sys.stdin.isatty():
            available = integration.discover()
            print("发现 Agent：" + ", ".join(h["id"] for h in available))
            print("可选择 claude-code,zcode,trae,codex,cursor,opencode；Cursor/OpenCode先使用主动MCP。")
            selected = input("接入哪些 Agent（逗号分隔；直接回车跳过）？ ").strip()
            agents = [v.strip() for v in selected.split(",") if v.strip()]
        model_options=None
        from . import model_setup
        if args.embedding_config:
            try:model_options=model_setup.read_options(args.embedding_config)
            except (integration.IntegrationError,OSError):
                print("接入未完成：模型配置文件无效或不可读");return 3
        elif args.embedding=="configure":
            if args.non_interactive or not sys.stdin.isatty():
                print("非交互模型配置需要 --embedding-config；基础检索无需模型")
                return 2
            model_options=model_setup.interactive()
        elif not args.non_interactive and sys.stdin.isatty():
            if input("配置公司内网 Embedding？[y/N] ").lower()=="y":model_options=model_setup.interactive()
        try:
            if not args.plan and (agents or model_options is not None):
                integration.resume()
            changes = integration.plan(agents)
            if model_options is not None:
                secret_change=getattr(model_options,"secret_change",None)
                if secret_change:changes.append(secret_change)
                changes.append(integration.model_change(model_options))
            if args.plan:
                print(json.dumps({"files": [c["path"] for c in changes], "agents": agents,
                    "writes": False, "embedding": "not_configured", "session_effective": "unknown"}, ensure_ascii=False))
                return 0
            code = cmd_init(args.import_from, args.scope, args.type)
            if code:return code
            # Basic init may create the shared config. Re-plan its model diff to preserve it.
            if model_options is not None:
                changes=[c for c in changes if Path(c["path"])!=config.CONFIG_PATH.resolve()]
                changes.append(integration.model_change(model_options))
            if not changes and not agents and model_options is None and not (args.probe_embedding or args.list_embedding_models or args.verify_mcp):
                return 0
            result = integration.apply(changes,allowed_paths=(config.CONFIG_PATH.resolve(),))
            print(json.dumps(result, ensure_ascii=False))
            if args.probe_embedding:print(json.dumps(model_setup.inspect_connection(),ensure_ascii=False))
            if args.list_embedding_models:print(json.dumps(model_setup.inspect_connection(models=True),ensure_ascii=False))
            if args.verify_mcp:
                import asyncio
                print(json.dumps(asyncio.run(integration.probe_protocol()),ensure_ascii=False))
            print("本地检索可用。请在宿主重连MCP并检查Hook开关/信任；配置、协议探针和真实会话生效分别验收。")
            return 0
        except (integration.IntegrationError, OSError) as exc:
            print("接入未完成：" + str(exc) if isinstance(exc, integration.IntegrationError) else "接入未完成：配置I/O失败")
            return 3
    if args.cmd == "privacy-scrub":
        from .privacy import scrub
        try:
            result = scrub(config.repo_path(), apply=args.apply, backup_dir=args.backup_dir)
        except (OSError, ValueError, sqlite3.Error) as exc:
            # Exception arguments may contain source content; return only a fixed code/type.
            result = {"error": type(exc).__name__}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if result.get("error") else 0
    if args.cmd == "dashboard":
        from .dashboard import generate
        team_snapshot = None
        if getattr(args, "team_data", ""):
            from .value.report import load_team_snapshot
            try:
                team_snapshot = load_team_snapshot(args.team_data)
            except (ValueError, OSError) as exc:
                print(f"错误：团队快照不可用（{exc}）；本次仅生成本机看板。")
                return 2
        output = generate(config.repo_path(), Path(args.output).expanduser().resolve(),
                          team_snapshot=team_snapshot)
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
        code=cmd_reindex()
        if code or not args.embed:return code
        from .semantic_index import build
        import time
        result=build(config.repo_path(),deadline=time.monotonic()+max(1,min(3600,args.deadline_seconds)))
        print(json.dumps(result,ensure_ascii=False));return 0 if result["state"] in {"active","ready","no_work"} else 2
    if args.cmd == "embedding-work":
        from .semantic_index import build
        import time
        result=build(config.repo_path(),deadline=time.monotonic()+max(1,min(3600,args.deadline_seconds)))
        print(json.dumps(result,ensure_ascii=False));return 0 if result["state"] in {"active","ready","no_work"} else 2
    if args.cmd == "embedding-activate":
        from .semantic_index import activate
        from .embedding_provider import ProviderError
        conn=index.connect(config.repo_path())
        try:
            from .semantic_index import generation
            from .retrieval_state import provider
            g=generation(conn,args.generation)
            if not g:raise ProviderError("index_not_ready")
            provider().probe(g["identity"])
            print(json.dumps({"generation":activate(conn,args.generation),"state":"active"}));return 0
        except ProviderError as exc:
            print(json.dumps({"state":"local","reason":exc.category}));return 2
        finally:conn.close()
    if args.cmd == "retrieval-evaluate":
        from .retrieval_evaluation import run
        result=run(config.repo_path(),args.dataset,args.cache,args.baseline_source,dense_min=args.dense_min,gate_dense_min=args.gate_dense_min)
        Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2))
        print(json.dumps({"state":result["state"],"reason":result.get("reason")}));return 0 if result["state"]=="COMPLETED_OFFLINE" else 2
    if args.cmd == "retrieval-cache":
        from .retrieval_evaluation import export_cache
        try:result=export_cache(config.repo_path(),args.dataset,args.output)
        except (ValueError,OSError) as exc:result={"state":"NOT_RUN","reason":type(exc).__name__}
        print(json.dumps(result,ensure_ascii=False));return 0 if result["state"]=="cached" else 2
    if args.cmd == "retrieval-report":
        from .retrieval_metrics import report
        print(json.dumps(report(config.repo_path()),ensure_ascii=False,indent=2));return 0
    if args.cmd == "benefit-control":
        import importlib
        from .benefit_control import load_tasks, run_control, write_report
        spec = args.executor
        if ":" in spec:
            mod_name, func_name = spec.split(":", 1)
        else:
            mod_name, _, func_name = spec.rpartition(".")
        try:
            executor = getattr(importlib.import_module(mod_name), func_name)
        except (ImportError, AttributeError) as exc:
            print(f"错误：执行器不可用（{exc}）"); return 2
        try:
            tasks = load_tasks(args.tasks)
            result = run_control(tasks, executor)
            write_report(result, args.out)
        except (ValueError, OSError) as exc:
            print(f"错误：{exc}"); return 2
        print(json.dumps({"tasks": result["totals"]["task_count"],
                          "net_token_savings": result["totals"]["net_token_savings"],
                          "net_seconds": result["totals"]["net_seconds"],
                          "out": args.out}, ensure_ascii=False)); return 0
    if args.cmd == "acceptance-check":
        import sqlite3
        from .acceptance import render_markdown, run_all
        from .value import aggregate
        agg_conn = aggregate.connect(args.agg_db)
        try:
            personal_conn = None
            if args.personal_db:
                personal_conn = sqlite3.connect(
                    f"file:{Path(args.personal_db).expanduser()}?mode=ro", uri=True)
            try:
                result = run_all(agg_conn=agg_conn, team_id=args.team_id,
                                 personal_conn=personal_conn,
                                 eval_report=args.eval_report or None,
                                 dataset=args.dataset or None,
                                 control_report=args.control_report or None)
            finally:
                if personal_conn is not None:
                    personal_conn.close()
            print(json.dumps({"verdict": result["verdict"],
                              "checks": {k: v["passed"] for k, v in result["checks"].items()}},
                             ensure_ascii=False))
            if args.out:
                out = Path(args.out).expanduser()
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(render_markdown(result), encoding="utf-8")
            return 0 if result["verdict"] == "PASS" else 2
        finally:
            agg_conn.close()
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
        if args.event == "before-compact":
            hooks.cmd_before_compact(style=args.style)
            return 0
        if args.event in {"tool-before", "tool-after", "tool-failure"}:
            hooks.cmd_tool_event(style=args.style, event=args.event)
            return 0
        {"session-start": hooks.cmd_session_start,
         "user-prompt": hooks.cmd_user_prompt,
         "stop": hooks.cmd_stop}[args.event](style=args.style)
        return 0
    if args.cmd == "team":
        from .team import service, state, worker
        from .locking import RepoLock
        try:
            action = args.team_cmd
            if action == "enroll":
                result = service.enroll(args.member_id, args.label, publisher=args.publisher,
                                        include_history=args.include_history, source_timezone=args.source_timezone)
            elif action in {"sync", "collect"}:
                result = service.run(network=action == "sync")
            elif action == "member":
                result = service.member(args.member_id, args.label, args.status)
            elif action in {"install-worker", "remove-worker", "disable"}:
                # P3 §6.1：旧 team 定时任务迁为 worker 的兼容别名，每库只有一个调度所有者。
                from .automation import worker as automation_worker
                result = automation_worker.install(remove=action != "install-worker")
                if action == "disable":
                    cfg = config.load_config(); cfg["team"]["enabled"] = False
                    service._save_config(cfg)
            elif action == "rebind-device":
                result = service.rebind_device()
            elif action == "recover":
                result = service.recover()
            else:
                root = service.root_path()
                with RepoLock(root):
                    conn = state.connect(root)
                    try:
                        if action == "publisher":
                            e = state.get(conn, "enrollment")
                            if not e:
                                raise ValueError("team enrollment required")
                            e["publisher"] = args.enable
                            state.put(conn, "enrollment", e); conn.commit()
                        result = service.status(conn)
                    finally:
                        conn.close()
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 1 if result.get("errors") else 0
        except (ValueError, RuntimeError, OSError) as exc:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False))
            return 1
    if args.cmd == "worker":
        from .automation import worker as automation_worker
        try:
            action = args.worker_cmd
            repo = config.repo_path()
            if action == "run":
                result = automation_worker.run_forever(repo)
            elif action == "once":
                result = automation_worker.run_once(repo)
            elif action == "status":
                result = automation_worker.status(repo)
            elif action == "install":
                result = automation_worker.install()
            else:
                result = automation_worker.disable()
            print(json.dumps(result, ensure_ascii=False, indent=2))
            failed = result.get("lease") == "lost" or any(
                task.get("status") == "error" for task in result.get("ran", []))
            return 1 if action == "once" and failed else 0
        except (ValueError, RuntimeError, OSError) as exc:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False))
            return 1
    if args.cmd == "value":
        from .value import aggregate, exchange
        agg_default = config.load_config().get("value", {}).get("aggregate_db",
                                                                "~/.knowbase/value-aggregate.db")
        if args.value_cmd == "export":
            result = exchange.export_events(config.repo_path(), args.output,
                                            since=args.since, until=args.until)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 2 if result.get("error") else 0
        if args.value_cmd == "import":
            agg_path = args.agg_db or agg_default
            try:
                agg_conn = aggregate.connect(agg_path)
            except ValueError as exc:
                print(f"错误：{exc}")
                return 2
            try:
                receipt_out = args.receipt_out or (args.package + ".receipt.json")
                result = exchange.import_package(agg_conn, args.package, team_id=args.team_id,
                                                 receipt_out=receipt_out)
            finally:
                agg_conn.close()
            summary = {k: v for k, v in result.items() if k != "receipt"}
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 2 if result.get("error") else 0
        if args.value_cmd == "receipt":
            receipt = json.loads(Path(args.receipt_file).expanduser().read_text(encoding="utf-8"))
            result = exchange.acknowledge_receipt(config.repo_path(), receipt)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 2 if result.get("error") else 0
        if args.value_cmd == "snapshot":
            agg_conn = aggregate.connect(args.agg_db or agg_default)
            try:
                snap = aggregate.snapshot(agg_conn, team_id=args.team_id,
                                          since=args.since, until=args.until)
            finally:
                agg_conn.close()
            out = aggregate.write_snapshot(snap, args.output)
            print(json.dumps({"output": str(out), "window": snap["window"],
                              "events_total": snap["counts"]["events_total"],
                              "members": snap["members"]}, ensure_ascii=False, indent=2))
            return 0
        if args.value_cmd == "register-member":
            agg_conn = aggregate.connect(args.agg_db or agg_default)
            try:
                print(json.dumps(aggregate.register_member(agg_conn, args.member_id, args.label),
                                 ensure_ascii=False))
            finally:
                agg_conn.close()
            return 0
        if args.value_cmd == "confirm-member":
            agg_conn = aggregate.connect(args.agg_db or agg_default)
            try:
                print(json.dumps(aggregate.confirm_member(agg_conn, args.installation,
                                                          args.member_id, actor=args.actor,
                                                          note=args.note), ensure_ascii=False))
            finally:
                agg_conn.close()
            return 0
        if args.value_cmd == "report":
            from .value import report as value_report
            team = None
            if args.agg_db:
                agg_conn = aggregate.connect(args.agg_db)
                try:
                    team = aggregate.snapshot(agg_conn, team_id=args.team_id,
                                              since=args.since, until=args.until)
                finally:
                    agg_conn.close()
            personal = None
            if args.personal:
                pconn = index.connect(config.repo_path())
                try:
                    personal = value_report.personal_summary(pconn, since=args.since,
                                                             until=args.until)
                finally:
                    pconn.close()
            if not team and not personal:
                print("错误：未指定 --agg-db 或 --personal，报告无内容。")
                return 2
            out = Path(args.output).expanduser()
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(value_report.render_markdown(team=team, personal=personal,
                                                        title=f"knowbase 价值报告（{args.since or '开始'} ~ {args.until or '最新'}）"),
                           encoding="utf-8")
            print(f"✓ 报告已生成 {out}")
            return 0
        if args.value_cmd == "usage":
            if args.source in ("zcode", "trae"):
                print(json.dumps({"status": "not_supported", "source": args.source,
                                  "reason": "H0 探针未发现稳定本机请求级用量来源（UI 统计不作为导出依据）；"
                                            "不估算冒充实测"}, ensure_ascii=False))
                return 0
            from .value import host_usage
            from .value.host_usage import claude as claude_usage
            from .value.events import record as record_value
            base = args.path or claude_usage.DEFAULT_PATH
            if not config.load_config().get("value", {}).get("events_enabled"):
                print(json.dumps({"error": "EVENTS_NOT_ENABLED",
                                  "hint": "value.events_enabled=true 后重试"}, ensure_ascii=False))
                return 2
            records = host_usage.claude.extract_records(base)
            counts = {"emitted": 0, "duplicate": 0, "skipped": 0}
            pconn = index.connect(config.repo_path())
            try:
                pconn.execute("BEGIN IMMEDIATE")
                for record in records:
                    try:
                        result = record_value(pconn, "model.usage",
                                              claude_usage.to_event_payload(record),
                                              source_key=claude_usage.source_key(record),
                                              host="claude-code",
                                              session_id=record["session_id"],
                                              operation_id=record["request_id"],
                                              occurred_at=record["occurred_at"] or None)
                    except ValueError:
                        counts["skipped"] += 1
                        continue
                    if result is None:
                        counts["skipped"] += 1
                    elif result.get("idempotent"):
                        counts["duplicate"] += 1
                    elif result.get("error"):
                        counts["skipped"] += 1
                    else:
                        counts["emitted"] += 1
                pconn.commit()
            finally:
                pconn.close()
            print(json.dumps({"status": "ok", "source": "claude_transcripts",
                              "path": str(base), **counts}, ensure_ascii=False))
            return 0
        return 1
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
    if args.cmd == "executor":
        if args.executor_cmd == "probe":
            return cmd_executor_probe(isolation=args.isolation, scope=args.scope)
        return 1
    if args.cmd == "archive-sweep":
        return cmd_archive_sweep()
    if args.cmd == "review-sweep":
        return cmd_review_sweep()
    if args.cmd == "scope":
        if args.scope_cmd == "resolve":
            return cmd_scope_resolve(args.path)
        if args.scope_cmd == "bind":
            return cmd_scope_bind(args.path, args.scope)
        return 1
    if args.cmd == "maintenance":
        if args.maint_cmd == "run":
            return cmd_maintenance_run()
        if args.maint_cmd == "status":
            return cmd_maintenance_status()
        return 1
    if args.cmd == "onboarding":
        return cmd_onboarding()
    return 1


def main(argv=None):
    from time import monotonic
    from . import telemetry
    arguments=list(sys.argv[1:] if argv is None else argv)
    command=arguments[0] if arguments else ""
    if command=="hook" and len(arguments)>1:
        command="hook."+arguments[1]
    if command=="value" and len(arguments)>1:
        command="value."+arguments[1]
    started=monotonic();status="raised"
    try:
        code=_main(arguments)
        status="ok" if code==0 else "returned_error"
        return code
    finally:
        # metrics导出及serve自身不落新日志；具体MCP调用单独记录。
        if command not in {"privacy-scrub", "doctor", "alerts", "retrieval-report", "retrieval-evaluate", "retrieval-cache"} and not (command == "init" and "--plan" in arguments):
            telemetry.record("cli",command,status,(monotonic()-started)*1000)


if __name__ == "__main__":
    sys.exit(main())
