"""Explicit document commands, separate from card search and extraction jobs."""
import json
import sqlite3
import sys

from . import config
from .document_library import DocumentLibrary


def add_parser(sub):
    parser = sub.add_parser("documents", help="原始文档接入/检索（不调用模型、不混入知识卡）")
    commands = parser.add_subparsers(dest="documents_cmd", required=True)
    cmd = commands.add_parser("sync", help="增量同步目录")
    cmd.add_argument("directory")
    cmd.add_argument("--scope", required=True)
    cmd.add_argument("--progress", action="store_true", help="向 stderr 输出逐文件进度")
    cmd.add_argument("--exclude", action="append", default=[], help="相对路径 glob，可重复")
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


def run(args):
    try:
        repo = config.repo_path()
        if not (repo / ".git").exists():
            raise ValueError("记忆库未初始化，请先运行 knowbase init")
        library = DocumentLibrary(repo)
        if args.documents_cmd == "sync":
            emit = (lambda event: print(json.dumps(event, ensure_ascii=False),
                                        file=sys.stderr, flush=True)) if args.progress else None
            result = library.sync(args.directory, scope=args.scope,
                                  exclude=args.exclude, progress=emit)
        elif args.documents_cmd == "search":
            result = library.search_page(args.query, scope=args.scope, limit=args.limit,
                                         offset=args.offset, group_by_file=not args.chunks)
        elif args.documents_cmd == "history":
            result = library.history(args.document_id)
        elif args.documents_cmd == "list":
            result = library.list_documents(scope=args.scope)
        elif args.documents_cmd == "reindex":
            result = library.reindex()
        else:
            result = library.read(args.document_id, sha256=args.sha256,
                                  start_line=args.start_line, end_line=args.end_line,
                                  historical=args.historical)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if isinstance(result, dict) and result.get("errors") else 0
    except KeyboardInterrupt:
        return 130
    except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2
