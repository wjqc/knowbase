"""薪火 CLI：显式、幂等、可观察的初始化与人工治理操作。

用法：
  knowbase init                  初始化/补全配置与仓库（幂等，绝不破坏已有数据）
  knowbase reindex               全量重建 SQLite 索引与 INDEX.md
  knowbase verify <id>           人工确认有效（once→verified / stale 复活）
  knowbase archive <id>          人工归档
  knowbase promote <id>          人工激活 staging 提案（git mv 到正式目录）
  knowbase list [type]           列出记忆
  knowbase stats                 统计
  knowbase doctor                V2 健康检查（schema/积压/死信/mirror…）
  knowbase alerts                V2 运维告警（基于 doctor + 多 sink 派发）
  knowbase serve                 启动 MCP 服务（stdio，供各 Agent 配置调用）
"""

import argparse
import json
import shutil
import sys
from datetime import date
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
    with RepoLock(rp, 10.0):
        meta = fn(rp, mid)
    if not meta:
        print(f"错误：未找到 {mid}")
        return 1
    cfg = config.load_config()
    _body_path = store.find_file(rp, mid)
    meta2, body, path = store.load(rp, mid)
    conn = index.connect(rp)
    index.upsert(conn, meta2, body, path, "staging" in str(path))
    conn.close()
    index.build_index_md(rp, cfg)
    if cfg["git"].get("auto_commit", True):
        gitops.commit_all(rp, f"{label}({mid}): by human")
    print(f"✓ {label} {mid} 完成")
    return 0


def cmd_verify(mid: str) -> int:
    return _human_op(config.repo_path(), lifecycle.human_verify, mid, "verify")


def cmd_archive(mid: str) -> int:
    return _human_op(config.repo_path(), lifecycle.human_archive, mid, "archive")


def cmd_promote(mid: str) -> int:
    """人工激活 staging 提案：等价于审过的 git mv，等价于通过状态机入口的治理动作。"""
    rp = config.repo_path()
    meta, body, path = store.load(rp, mid)
    if not meta:
        print(f"错误：未找到 {mid}")
        return 1
    if "staging" not in str(path):
        print(f"错误：{mid} 不在 staging/（无需 promote）")
        return 1
    dtype = meta.get("type")
    target = store.mem_path(rp, dtype, mid)
    with RepoLock(rp, 10.0):
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(target))
        meta["updated"] = date.today().isoformat()
        target.write_text(store.render(meta, body), encoding="utf-8")
    cfg = config.load_config()
    conn = index.connect(rp)
    index.upsert(conn, meta, body, target, False)
    conn.close()
    index.build_index_md(rp, cfg)
    if cfg["git"].get("auto_commit", True):
        gitops.commit_all(rp, f"promote({mid}): staging → {store.TYPE_DIR[dtype]}（人工审核通过）")
    print(f"✓ {mid} 已激活 → {target}")
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
    """批量导入现存 markdown 文档为 once 级记忆（原文即 body，结构可后续 AI 提炼 + memory_update 转正）。"""
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
    files = [f for f in sorted(src_dir.rglob("*.md"))
             if not any(part.startswith(".") for part in f.relative_to(src_dir).parts)
             and not f.is_symlink() and src_dir in f.resolve().parents]
    if not files:
        print("目录中未找到 .md 文件")
        return 1
    imported = skipped = 0
    with RepoLock(rp, cfg.get("lock_timeout", 10.0)):
        conn = index.connect(rp)
        existing = {(m.get("import_path"), m.get("scope"), m.get("type")): m
                    for m, _, _ in store.iter_all(rp, include_staging=True)}
        for f in files:
            import hashlib
            body = f.read_text(encoding="utf-8").strip()
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
            title = (m.group(1).strip() if m else f.stem)[:80]
            meta = store.new_meta(dtype, title, scope, ["import", src_dir.name], source)
            meta["import_path"] = str(f.resolve())
            meta["import_sha256"] = digest
            if dtype == "bizrule":
                staging = True  # 业务规则强制人审：无出处的规则是危险品
                meta["provenance"] = f"待补出处（导入自 {src_dir.name}/{f.name}）"
            meta["id"] = store.alloc_id(rp, dtype)
            target = rp / ("staging" if staging else store.TYPE_DIR[dtype]) / f"{meta['id']}.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(store.render(meta, body), encoding="utf-8")
            index.upsert(conn, meta, body, target, staging)
            imported += 1
            print(f"  {meta['id']}  {title[:44]}")
        if imported:
            index.record_usage(conn, "import", detail=f"{imported} from {src_dir.name}")
        conn.close()
    index.build_index_md(rp, cfg)
    if imported and cfg["git"].get("auto_commit", True):
        gitops.commit_all(rp, f"import({dtype}): {imported} 条 ← {src_dir.name}（scope={scope}，{'staging' if staging else '直接入库'}）")
    if imported and cfg["git"].get("auto_push"):
        gitops.push(rp, cfg["git"]["remote"].get("url", ""), cfg["git"].get("allowed_remote_prefixes", []))
    print(f"完成：导入 {imported}，跳过 {skipped}（重复、空文件或被拦截）")
    return 0


def cmd_stats() -> int:
    from .server import stats_impl
    print(stats_impl())
    return 0


def cmd_doctor(json_output: bool = False) -> int:
    """V2 健康检查（schema / outbox 积压 / 死信 / 24h 失败 / mirror / 冲突态）。

    对``v2.db`` 缺失或不可访问的情况：返回 1 条 schema_version fail + 1 条
    ``init_error`` fail，不抛异常（doctor 必须能跑、不能拖垮运维）。
    """
    from .v2.repositories import V2Repository
    from .v2.repositories.schema import V2_DB_FILENAME
    from .v2.sync import (
        DoctorCheck,
        DoctorThresholds,
        doctor_exit_code,
        render_doctor,
        run_doctor_checks,
    )

    rp = config.repo_path()
    db_path = rp / ".knowbase" / V2_DB_FILENAME
    if not db_path.exists():
        # 主仓库未 init：所有 DB check 退化为 fail，mirror 仍按真实路径检查
        checks: list[DoctorCheck] = [
            DoctorCheck(
                "schema_version", "fail",
                f"v2.db 不存在（{db_path}）；先 knowbase init",
            ),
            DoctorCheck(
                "init_error", "fail",
                "V2Repository 不可用（v2.db 缺失）；其他 DB check 跳过",
            ),
            # mirror 检查仍跑（独立于 DB）
        ]
        # 手动跑 mirror_git 一次（不依赖 V2Repository）
        from .v2.sync.doctor import check_mirror_git
        checks.append(check_mirror_git(rp))
    else:
        repo = V2Repository(rp)
        try:
            thresholds = DoctorThresholds()
            checks = run_doctor_checks(repo, rp, thresholds=thresholds)
        except (PermissionError, OSError) as e:
            # sandbox 拦截 / 权限不足：把整轮退化为一组 fail，仍能给出汇总
            checks = [
                DoctorCheck("schema_version", "fail", f"读 v2.db 异常: {e!r}"),
                DoctorCheck("init_error", "fail", "V2Repository 不可访问"),
            ]
            from .v2.sync.doctor import check_mirror_git
            checks.append(check_mirror_git(rp))
        finally:
            repo.close()

    if json_output:
        print(json.dumps(
            [{"name": c.name, "level": c.level, "message": c.message} for c in checks],
            ensure_ascii=False, indent=2,
        ))
    else:
        print(render_doctor(checks))
    return doctor_exit_code(checks)


def cmd_alerts(dry_run: bool = False) -> int:
    """V2 运维告警（基于 doctor + AlertDispatcher 派发，含 webhook / stdout / 文件）。

    - 不传 dry_run：按 cfg.v2.alerts 派发（含 webhook POST / log_file 追加）
    - --dry-run：跳过派发，只在 stdout 打印将要发的告警（默认带 1 个 console sink，
      但因 cooldown 仍生效，重复跑将看到 suppressed 计数）

    对 v2.db 不可访问 / 缺失：fallback 到 schema_version fail + 派发它。
    """
    from .v2.repositories import V2Repository
    from .v2.repositories.schema import V2_DB_FILENAME
    from .v2.sync import (
        AlertConfig,
        AlertDispatcher,
        AlertEvent,
        DoctorCheck,
        load_alert_config,
        sink_console,
    )

    cfg_dict = config.load_config()
    alert_cfg = load_alert_config(cfg_dict)
    if dry_run:
        dispatcher = AlertDispatcher(
            AlertConfig(
                enabled=True,
                cooldown_seconds=alert_cfg.cooldown_seconds,
                webhook_url="",  # 不发 webhook
                log_to_file="",  # 不落盘
            ),
            sinks=[sink_console],
        )
    else:
        dispatcher = AlertDispatcher(alert_cfg)

    rp = config.repo_path()
    db_path = rp / ".knowbase" / V2_DB_FILENAME
    if not db_path.exists():
        checks: list[DoctorCheck] = [
            DoctorCheck("schema_version", "fail",
                        f"v2.db 不存在（{db_path}）；先 knowbase init"),
        ]
        events = [AlertEvent(check="schema_version", level="fail",
                             message=checks[0].message)]
        stats = dispatcher.dispatch(events) if alert_cfg.enabled else {
            "sent": 0, "suppressed": 0, "failed": 0,
        }
        result = {"checks": checks, "events": events, "dispatch": stats}
    else:
        repo = V2Repository(rp)
        try:
            from .v2.sync import run_alerts
            result = run_alerts(repo_root=rp, repo=repo,
                                cfg=alert_cfg, dispatcher=dispatcher)
        except (PermissionError, OSError) as e:
            checks = [DoctorCheck("schema_version", "fail",
                                  f"读 v2.db 异常: {e!r}")]
            events = [AlertEvent(check="schema_version", level="fail",
                                 message=checks[0].message)]
            stats = dispatcher.dispatch(events) if alert_cfg.enabled else {
                "sent": 0, "suppressed": 0, "failed": 0,
            }
            result = {"checks": checks, "events": events, "dispatch": stats}
        finally:
            repo.close()

    # 输出（两种模式都打：dry_run 不靠 dispatcher 派发也能看到事件清单）
    checks = result["checks"]
    events = result["events"]
    stats = result["dispatch"]
    if dry_run:
        print(f"[dry-run] {len(events)} 个告警待派发（已跳过实际 sink）：")
        for ev in events:
            print(f"  {ev.level.upper()} {ev.check}: {ev.message}")
    else:
        print(f"checks={len(checks)} events={len(events)} sent={stats['sent']} "
              f"suppressed={stats['suppressed']} failed={stats['failed']}")
    # 任一事件存在且有 fail 级 → 非零退出（方便 cron 触发）
    return 0 if not events or stats["failed"] == 0 else 1


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
    p_list = sub.add_parser("list", help="列出记忆")
    p_list.add_argument("type", nargs="?", choices=store.TYPES)
    sub.add_parser("stats", help="统计")
    p_doc = sub.add_parser("doctor", help="V2 健康检查（schema/积压/死信/mirror）")
    p_doc.add_argument("--json", action="store_true", help="以 JSON 列表输出")
    p_alerts = sub.add_parser("alerts", help="V2 运维告警（基于 doctor + 多 sink 派发）")
    p_alerts.add_argument("--dry-run", action="store_true",
                          help="不实际派发 webhook/文件，仅 stdout 打印事件清单")
    p_hook = sub.add_parser("hook", help="Agent hook 入口")
    p_hook.add_argument("event", choices=["session-start", "user-prompt", "stop"])
    p_hook.add_argument("--style", choices=["claude", "zcode"], default="claude")
    p_imp = sub.add_parser("import", help="批量导入现存 markdown 文档为 once 级记忆")
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
