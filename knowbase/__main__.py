"""薪火 CLI：显式、幂等、可观察的初始化与人工治理操作。

用法（与 argparse 一致，共 15 个命令）：
  knowbase init [--import-from 目录 --scope 名 --type 类]  初始化/补全（幂等），可顺带存量导入
  knowbase import <目录> [--type 类 --scope 名 --staging]  批量导入 Markdown/PDF/Office/HTML/图片/代码/日志
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
"""

import argparse
import json
import shutil
import sys
from datetime import date, datetime
from pathlib import Path

from . import config, gitops, index, lifecycle, store
from .locking import RepoLock

GITIGNORE = "memory.db\nmemory.db-wal\nmemory.db-shm\n.lock\n*.log\n"

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


def _human_op(rp, fn, mid, label):
    cfg = config.load_config()
    with RepoLock(rp, cfg.get("lock_timeout", 10.0)):
        meta = fn(rp, mid)
        if not meta:
            print(f"错误：未找到 {mid}")
            return 1
        meta2, body, path = store.load(rp, mid)
        conn = index.connect(rp)
        index.upsert(conn, meta2, body, path, path.parent.name == "staging")
        conn.close()
        index.build_index_md(rp, cfg)
        git_warn = gitops.commit_paths(rp, f"{label}({mid}): by human", [path, rp / "INDEX.md"]) \
            if cfg["git"].get("auto_commit", True) else None
    push_warn = None if git_warn or not cfg["git"].get("auto_push") else \
        gitops.schedule_push(rp, cfg["git"])
    print(f"✓ {label} {mid} 完成")
    if git_warn:
        print(f"⚠ {git_warn}")
    if push_warn:
        print(f"⚠ {push_warn}")
    return 0


def cmd_verify(mid: str) -> int:
    return _human_op(config.repo_path(), lifecycle.human_verify, mid, "verify")


def cmd_archive(mid: str) -> int:
    return _human_op(config.repo_path(), lifecycle.human_archive, mid, "archive")


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
        index.build_index_md(rp, cfg)
        git_warn = gitops.commit_paths(
            rp, f"promote({mid}): staging → {store.TYPE_DIR[dtype]}（人工审核通过）",
            [path, target, rp / "INDEX.md"],
        ) if cfg["git"].get("auto_commit", True) else None
    push_warn = None if git_warn or not cfg["git"].get("auto_push") else \
        gitops.schedule_push(rp, cfg["git"])
    print(f"✓ {mid} 已激活 → {target}")
    if git_warn:
        print(f"⚠ {git_warn}")
    if push_warn:
        print(f"⚠ {push_warn}")
    return 0


def cmd_revise(mid: str, body_file: str, title: str | None = None) -> int:
    """可信人工 CLI：修订已生效的 standard/preference/bizrule。"""
    rp = config.repo_path()
    cfg = config.load_config()
    body_path = Path(body_file).expanduser().resolve()
    if not body_path.is_file():
        print(f"错误：正文文件不存在 {body_path}")
        return 1
    new_body = body_path.read_text(encoding="utf-8")
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
        errs = store.lint(meta, new_body)
        if errs:
            print("错误：lint 未通过，未修改。\n- " + "\n- ".join(errs))
            return 1
        path.write_text(store.render(meta, new_body), encoding="utf-8")
        conn = index.connect(rp)
        index.upsert(conn, meta, new_body, path, False)
        conn.close()
        index.build_index_md(rp, cfg)
        git_warn = gitops.commit_paths(rp, f"revise({mid}): {meta['title']} by human",
                                       [path, rp / "INDEX.md"]) \
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


def cmd_import(directory: str, dtype: str, scope: str, staging: bool, source: str) -> int:
    """批量解析支持的文档格式并导入为 once 级记忆。"""
    import re as _re
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
    imported = skipped = 0
    written_paths: list[Path] = []
    effective_staging = staging or dtype in ("standard", "preference", "bizrule")
    with RepoLock(rp, cfg.get("lock_timeout", 10.0)):
        conn = index.connect(rp)
        existing = {(m.get("import_path"), m.get("scope"), m.get("type")): m
                    for m, _, _ in store.iter_all(rp, include_staging=True)}
        for f in files:
            import hashlib
            if f.suffix.lower() == ".md":
                body = f.read_text(encoding="utf-8").strip()
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
            digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
            prior = existing.get((str(f.resolve()), scope, dtype))
            if prior:
                skipped += 1
                reason = "已导入" if prior.get("import_sha256") == digest else "源文已变化，请复核并更新已有条目"
                print(f"  跳过（{reason} [{prior['id']}]）：{f.name}")
                continue
            if store.SECRET_RE.search(body):
                skipped += 1
                print(f"  跳过（疑似明文凭据）：{f.name}")
                continue
            if not body:
                skipped += 1
                continue
            m = _re.search(r"^#\s+(.+)$", body, _re.M)
            title = (m.group(1).strip() if m else parsed_title or f.stem)[:80]
            meta = store.new_meta(dtype, title, scope,
                                  ["import", src_dir.name, f.suffix.lower().lstrip(".")], source)
            meta["import_path"] = str(f.resolve())
            meta["import_sha256"] = digest
            if dtype == "bizrule":
                meta["provenance"] = f"待补出处（导入自 {src_dir.name}/{f.name}）"
            meta["id"] = store.alloc_id(rp, dtype)
            target = rp / ("staging" if effective_staging else store.TYPE_DIR[dtype]) / f"{meta['id']}.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(store.render(meta, body), encoding="utf-8")
            written_paths.append(target)
            index.upsert(conn, meta, body, target, effective_staging)
            index.record_source_state(
                conn, str(f.resolve()), digest,
                datetime.fromtimestamp(f.stat().st_mtime).isoformat(timespec="seconds"),
            )
            imported += 1
            print(f"  {meta['id']}  {title[:44]}")
        if imported:
            index.record_usage(conn, "import", detail=f"{imported} from {src_dir.name}")
        conn.close()
        index.build_index_md(rp, cfg)
        git_warn = gitops.commit_paths(
            rp, f"import({dtype}): {imported} 条 ← {src_dir.name}（scope={scope}，{'staging' if effective_staging else '直接入库'}）",
            written_paths + [rp / "INDEX.md"],
        ) if imported and cfg["git"].get("auto_commit", True) else None
    push_warn = None if git_warn or not imported or not cfg["git"].get("auto_push") else \
        gitops.schedule_push(rp, cfg["git"])
    print(f"完成：导入 {imported}，跳过 {skipped}（重复、空文件或被拦截）")
    if git_warn:
        print(f"⚠ {git_warn}")
    if push_warn:
        print(f"⚠ {push_warn}")
    return 0


def cmd_stats() -> int:
    from .server import stats_impl
    print(stats_impl())
    return 0


def cmd_doctor(json_output: bool = False) -> int:
    """检查现有 Markdown + memory.db + Git 单库运行状态。"""
    import subprocess
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
        status = subprocess.run(["git", "-C", str(rp), "status", "--porcelain", "--branch"],
                                capture_output=True, text=True, timeout=5,
                                env={**__import__('os').environ, "GIT_TERMINAL_PROMPT": "0"})
        lines = status.stdout.splitlines()
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


def main(argv=None):
    parser = argparse.ArgumentParser(prog="knowbase", description="薪火：跨 Agent 经验记忆库")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_init = sub.add_parser("init", help="初始化/补全（幂等）")
    p_init.add_argument("--import-from", help="现存 markdown 目录，导入 staging 待审")
    p_init.add_argument("--scope", help="明确项目范围；通用资料用 global")
    p_init.add_argument("--type", choices=store.TYPES, default="workflow")
    p_dash = sub.add_parser("dashboard", help="生成本地 HTML 知识治理看板")
    p_dash.add_argument("--output", default="knowbase-dashboard.html")
    p_dash.add_argument("--open", action="store_true")
    p_history = sub.add_parser("history", help="查看搜索命中记录 JSON")
    p_history.add_argument("--limit", type=int, default=100)
    sub.add_parser("reindex", help="全量重建索引")
    p_verify = sub.add_parser("verify", help="人工确认有效")
    p_verify.add_argument("id")
    p_arch = sub.add_parser("archive", help="人工归档")
    p_arch.add_argument("id")
    p_pro = sub.add_parser("promote", help="激活 staging 提案")
    p_pro.add_argument("id")
    p_rev = sub.add_parser("revise", help="人工修订已生效的标准/偏好/业务规则")
    p_rev.add_argument("id")
    p_rev.add_argument("--body-file", required=True)
    p_rev.add_argument("--title")
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
    p_imp = sub.add_parser("import", help="批量导入 Markdown/PDF/Office/HTML/图片/代码/日志")
    p_imp.add_argument("directory")
    p_imp.add_argument("--type", choices=store.TYPES, default="pitfall")
    p_imp.add_argument("--scope", default="global")
    p_imp.add_argument("--source", default="human:import")
    p_imp.add_argument("--staging", action="store_true", help="导入到 staging 待人审")
    sub.add_parser("serve", help="启动 MCP 服务（stdio）")
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
        return cmd_verify(args.id)
    if args.cmd == "archive":
        return cmd_archive(args.id)
    if args.cmd == "promote":
        return cmd_promote(args.id)
    if args.cmd == "revise":
        return cmd_revise(args.id, args.body_file, args.title)
    if args.cmd == "list":
        return cmd_list(args.type)
    if args.cmd == "stats":
        return cmd_stats()
    if args.cmd == "doctor":
        return cmd_doctor(json_output=args.json)
    if args.cmd == "alerts":
        return cmd_alerts(dry_run=args.dry_run)
    if args.cmd == "import":
        return cmd_import(args.directory, args.type, args.scope, args.staging, args.source)
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
    return 1


if __name__ == "__main__":
    sys.exit(main())
