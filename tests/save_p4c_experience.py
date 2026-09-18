"""P4-C 经验沉淀脚本：把 5 条 P4-C 经验写入 ~/knowbase。

需手动在 IDE 终端执行（TRAE sandbox 可能拦 ~/knowbase/.lock 写入）。

用法：
    python3 tests/save_p4c_experience.py

如果 sandbox 报错，按 IDE Custom Sandbox Configuration 把 ~/knowbase/.knowbase/cache 加白，
或先在 IDE 里手动 init 一次 knowbase。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# 关键：隔离 KNOWBASE_REPO_PATH / KNOWBASE_CONFIG，让 server.save_impl 走默认 ~/knowbase
for k in ("KNOWBASE_REPO_PATH", "KNOWBASE_CONFIG", "KNOWBASE_V2_INGESTION",
          "KNOWBASE_V2_IDEMPOTENT_INGEST", "KNOWBASE_V2_PARSER_MARKDOWN",
          "KNOWBASE_V2_PARSER_TXT", "KNOWBASE_V2_PARSER_PDF", "KNOWBASE_V2_PARSER_DOCX"):
    os.environ.pop(k, None)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from knowbase import __main__ as cli  # noqa: E402
from knowbase import config, server  # noqa: E402

# 确保真实仓库 init 完毕（幂等）；lock 没权限时跳过（save_impl 不依赖 init）
print(f"repo_path = {config.repo_path()}")
try:
    cli.main(["init"])
except PermissionError as e:
    print(f"[skip init] {e}")


# P4-C 5 条踩坑/经验：(type, title, body, tags, scope)
EXPERIENCES = [
    (
        "pitfall",
        "macOS git init --bare 不会自动创建 bare_dir 父目录",
        """## 现象
P4-C MirrorWriter._init_if_needed 首次执行时，``git init --bare /path/to/bare.git`` 报
``fatal: cannot change to '.knowbase/mirror.git': No such file or directory``。

## 原因
macOS 自带 git 的 ``init --bare`` 不会自动创建 bare_dir 的父目录；Linux 行为视发行版，
多数也不会。

## 正确做法
先 ``bare_dir.parent.mkdir(parents=True, exist_ok=True)``，再 ``bare_dir.mkdir(parents=True, exist_ok=False)``。
Portable 写法，跨平台一致。""",
        ["git", "mirror", "bare-repo", "macos"],
    ),
    (
        "pitfall",
        "bare repo 不能 commit --allow-empty；worktree add -b BRANCH 也会失败",
        """## 现象
P4-C MirrorWriter 在 bare repo 上尝试 ``git commit --allow-empty`` 报
``fatal: this operation must be run in a work tree``。
如果绕过用 ``worktree add -b main WORKTREE``，又会报
``fatal: not a valid object name: 'HEAD'``（HEAD 还没指向任何 commit）。

## 正确顺序
1. ``git -C BARE hash-object -t tree /dev/null`` 拿空 tree SHA；
2. ``git -C BARE commit-tree TREE_SHA -m init`` 拿 init commit SHA；
3. ``git -C BARE update-ref refs/heads/BRANCH SHA``；
4. ``git -C BARE worktree add WORKTREE BRANCH``。

## 关键
- 不要 ``worktree add -b BRANCH``，bare repo 里没有现成分支可 checkout；
- 必须先手工建 init commit + update-ref，让 HEAD 指向有效 object。""",
        ["git", "mirror", "bare-repo", "worktree"],
    ),
    (
        "pitfall",
        "V2 frozen dataclass 字段不能直接赋值",
        """## 现象
V2 域 Document / DocumentVersion / SyncRun 都是 ``@dataclass(frozen=True)``。
P4-C mirror_handler 想给 SyncRun 写 stats：
``run.stats = {...}`` 立刻报 ``dataclasses.FrozenInstanceError: cannot assign to field 'stats'``。

## 正确做法
``from dataclasses import replace; run = replace(run, stats={...})`` 返回新对象。
Document 切换 status 同理：
``doc = replace(doc, status=DocumentStatus.TOMBSTONED)``。

## 测试 helper
frozen dataclass 不能 ``with`` 后修改，必须一开始就把要变的字段构造进对象或用 replace。
Test 阶段把 frozen=True 关掉能临时验证行为，但不要把它当 fix。""",
        ["v2", "dataclass", "frozen", "python"],
    ),
    (
        "pitfall",
        "document 表 UNIQUE(source_id, path) 约束：多文档测试必须 path 不同",
        """## 现象
P4-C test_mirror_writer.py 调试时，3 个文档只看到最后 1 个文件落盘。
原因不是 mirror writer 并发问题，而是 ``_make_doc`` helper 默认
``path='test.md'`` + 同 ``source_id``，触发 ``document.UNIQUE(source_id, path)`` 约束，
``INSERT OR REPLACE`` 把前 2 条全部覆盖。

## helper 修法
加 ``path: str | None = None`` 参数，按 counter 自增：
``path=path or f"test-{counter}.md"``。

## 真实 ingest 注意
同一 source 下不允许两条 doc 同 path，否则会被静默替换（看似成功，实际丢数据）。
inotify / fsnotify 类增量同步需特别处理 rename + 同名场景。""",
        ["v2", "sqlite", "schema", "test-helper"],
    ),
    (
        "decision",
        "仓库方法命名：upsert_outbox → enqueue_outbox；upsert_version → add_version",
        """## 现象
V2 ``sqlite_repo.py`` 真实方法名是 ``enqueue_outbox(ev)`` 和 ``add_version(ver)``，不是直觉的 ``upsert_*``。
P4-C test_mirror_writer.py 第一版 helper 全用了 ``upsert_outbox`` / ``upsert_version``，
跑测试时全部 ``AttributeError: 'V2Repository' object has no attribute 'upsert_outbox'``。

## 教训
写测试 helper 前先 ``grep 'def (upsert_|insert_|add_|save_|record_)' knowbase/v2/repositories/sqlite_repo.py``
确认真实方法名，不要凭其它项目惯例猜测。

## 当前 V2 sqlite_repo 命名约定
- 创建/插入：``upsert_source`` / ``upsert_document`` / ``add_version`` / ``enqueue_outbox`` / ``record_operation`` / ``record_sync_run``
- 关闭：``upsert_organization`` / ``upsert_project`` / ``upsert_principal`` / ``upsert_membership`` / ``upsert_project_membership``（P3-A ACL 用 upsert_*）
- 命名风格不统一（add / upsert / enqueue / record 都有），但 grep 一次就能拿到全表。""",
        ["v2", "naming", "convention", "test-helper"],
    ),
]


def main() -> None:
    print("Saving 5 P4-C experiences to ~/knowbase ...")
    saved: list[str] = []
    failed: list[str] = []
    for type_, title, body, tags in EXPERIENCES:
        try:
            res = server.save_impl(
                type=type_, title=title, body=body, tags=tags,
                scope="global", source="p4c-mirror-writer",
            )
            saved.append(str(res)[:120])
            print(f"  ok  {type_}/{title[:60]}")
        except Exception as e:  # pragma: no cover
            failed.append(f"{type_}/{title}: {e}")
            print(f"  FAIL {type_}/{title[:60]}: {e}")

    print(f"\nDone. saved={len(saved)} failed={len(failed)}")
    if failed:
        print("Failures:")
        for f in failed:
            print(f"  - {f}")
        sys.exit(1)


if __name__ == "__main__":
    main()
