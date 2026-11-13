"""Explicit document commands, separate from card search and extraction jobs."""
import json
import sqlite3
import sys
import time

from . import config
from .document_library import DocumentLibrary


def add_parser(sub):
    parser = sub.add_parser("documents", help="原始文档接入/检索/浏览（不调用模型、不混入知识卡）")
    commands = parser.add_subparsers(dest="documents_cmd", required=True)
    for name in ("sync", "watch"):
        cmd = commands.add_parser(name, help="增量同步目录" if name == "sync" else "前台轮询增量同步（Ctrl-C 停止）")
        cmd.add_argument("directory", nargs="?")
        cmd.add_argument("--saved", action="store_true", help="使用持久登记的目录")
        cmd.add_argument("--scope")
        cmd.add_argument("--retries", type=int, default=2, help="失败后重试次数 0..5")
        cmd.add_argument("--progress", action="store_true", help="向 stderr 输出逐文件进度")
        cmd.add_argument("--exclude", action="append", default=[], help="相对路径 glob，可重复")
        if name == "watch":
            cmd.add_argument("--interval", type=float, default=10)
    cmd = commands.add_parser("register", help="保存本地目录/忽略规则（不会自动启动后台服务）")
    cmd.add_argument("directory")
    cmd.add_argument("--scope", required=True)
    cmd.add_argument("--exclude", action="append", default=[])
    cmd.add_argument("--interval", type=float, default=10)
    commands.add_parser("roots", help="列出持久目录配置和上次同步状态")
    cmd = commands.add_parser("unregister", help="移除监视配置，保留已导入快照")
    cmd.add_argument("root_id")
    commands.add_parser("reindex", help="接入已导入的 source 快照（无需原始目录）")
    cmd = commands.add_parser("list", help="列出文档及删除/解析失败状态")
    cmd.add_argument("--scope", default="")
    cmd = commands.add_parser("search", help="显式全文检索原始文档")
    cmd.add_argument("query")
    cmd.add_argument("--scope", default="")
    cmd.add_argument("--limit", type=int, default=30)
    cmd.add_argument("--offset", type=int, default=0)
    cmd.add_argument("--chunks", action="store_true", help="逐分块而非文件聚合")
    cmd = commands.add_parser("history", help="列出已固定的文档快照")
    cmd.add_argument("document_id")
    cmd = commands.add_parser("read", help="按快照哈希/行号定位提取文本")
    cmd.add_argument("document_id")
    cmd.add_argument("--sha256")
    cmd.add_argument("--historical", action="store_true", help="显式读取属于该文档的历史快照")
    cmd.add_argument("--start-line", type=int)
    cmd.add_argument("--end-line", type=int)
    cmd = commands.add_parser("browse", help="启动仅本机的只读文档浏览器")
    cmd.add_argument("--port", type=int, default=8765)
    cmd.add_argument("--open", action="store_true")


def run(args):
    try:
        repo = config.repo_path()
        if not (repo / ".git").exists():
            raise ValueError("记忆库未初始化，请先运行 knowbase init")
        if args.documents_cmd == "browse":
            from .document_browser import serve
            serve(repo, port=args.port, open_browser=args.open)
            return 0
        library = DocumentLibrary(repo)
        if args.documents_cmd in {"register", "roots", "unregister"}:
            from .document_watch import DirectoryWatch
            watcher = DirectoryWatch(library)
            if args.documents_cmd == "register":
                result = watcher.register(args.directory, scope=args.scope, exclude=args.exclude, interval=args.interval)
            elif args.documents_cmd == "roots":
                result = watcher.roots()
            else:
                result = watcher.remove(args.root_id)
        elif args.documents_cmd in {"sync", "watch"}:
            if args.saved:
                if args.directory or args.scope or args.exclude:
                    raise ValueError("--saved cannot be combined with directory, --scope or --exclude")
                from .document_watch import DirectoryWatch
                result = DirectoryWatch(library).run(once=args.documents_cmd == "sync", retries=args.retries,
                    emit=lambda event: print(json.dumps(event, ensure_ascii=False), flush=True))
                if result["cancelled"]:
                    return 130
                return 2 if any(r.get("errors") for r in result["results"].values()) else 0
            if not args.directory or not args.scope:
                raise ValueError("directory and --scope are required unless --saved is used")
            if args.documents_cmd == "watch" and not 1 <= args.interval <= 3600:
                raise ValueError("watch interval must be between 1 and 3600 seconds")
            from .document_watch import DirectoryWatch
            watcher = DirectoryWatch(library)
            root = {"id": "transient", "directory": args.directory, "scope": args.scope, "exclude": args.exclude}
            emit = (lambda event: print(json.dumps(event, ensure_ascii=False), file=sys.stderr, flush=True)) if args.progress else None
            while True:
                result = watcher.sync_root(root, retries=args.retries, emit=emit)
                print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
                if args.documents_cmd == "sync":
                    return 2 if result["errors"] else 0
                time.sleep(args.interval)
        elif args.documents_cmd == "search":
            result = library.search_page(args.query, scope=args.scope, limit=args.limit, offset=args.offset, group_by_file=not args.chunks)
        elif args.documents_cmd == "history":
            result = library.history(args.document_id)
        elif args.documents_cmd == "list":
            result = library.list_documents(scope=args.scope)
        elif args.documents_cmd == "reindex":
            result = library.reindex()
        else:
            result = library.read(args.document_id, sha256=args.sha256,
                                  start_line=args.start_line, end_line=args.end_line, historical=args.historical)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if isinstance(result, dict) and result.get("errors") else 0
    except KeyboardInterrupt:
        return 130
    except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2
