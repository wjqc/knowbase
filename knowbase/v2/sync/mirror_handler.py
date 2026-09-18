"""P4-C 镜像 outbox handler：消费 document.* 事件 → MirrorWriter → sync_run 记录。

设计（V2 计划 §8.1 + §8.3）：
- handler 是同步函数（被 P4-B SyncWorker 在 run_in_executor 内调用）
- 订阅 topic 前缀：``document.stored`` / ``document.version_activated`` / ``document.tombstoned``
- 每次事件 → 一次 sync_run（status=RUNNING → SUCCEEDED/FAILED/CONFLICTED）
- 幂等：handler 内部通过 writer.commit_doc 的 content_hash 比对实现
- 失败：handler raise → SyncWorker 走 _on_failure 退避 / 死信
"""
from __future__ import annotations

from dataclasses import replace

from ..domain.models import OutboxEvent, SyncRun
from ..repositories.sqlite_repo import V2Repository
from .mirror import MirrorError, MirrorWriter


# 支持的事件 topic（前缀匹配）
SUPPORTED_TOPICS: tuple[str, ...] = (
    "document.stored",
    "document.version_activated",
    "document.tombstoned",
)


def is_supported(topic: str) -> bool:
    """是否 MirrorWriter 订阅的事件。"""
    return any(topic == t or topic.startswith(t + ".") for t in SUPPORTED_TOPICS)


# ---------- handler ----------

def make_mirror_handler(
    repo: V2Repository,
    writer: MirrorWriter,
) -> "callable":
    """构造一个可被 SyncWorker 订阅的同步 handler。

    返回函数签名：``(ev: OutboxEvent) -> None``。
    失败抛异常（由 SyncWorker 处理退避 / 死信）。
    """
    def handle(ev: OutboxEvent) -> None:
        if not is_supported(ev.topic):
            # 不订阅的事件：直接 ack（不抛异常，避免不必要退避）
            return

        # 1. 解析 payload
        payload = ev.payload or {}
        doc_id = payload.get("doc_id") or ev.aggregate_id
        version_id = payload.get("version_id")
        if not doc_id:
            raise MirrorError(
                f"outbox {ev.id} (topic={ev.topic}) 缺 doc_id/aggregate_id"
            )

        # 2. 拿 Document
        doc = repo.get_document(doc_id)
        if doc is None:
            raise MirrorError(f"doc {doc_id} 不存在（被 tombstoned/删除？跳过）")

        # 3. 拿 DocumentVersion（优先按 version_id；否则取当前活跃版）
        version = None
        if version_id:
            version = repo.get_version(version_id)
        if version is None:
            # 活跃版本（superseded_by IS NULL）
            version = repo.active_version(doc_id)

        # 4. 启动 sync_run
        run = SyncRun.new(source_id=doc.source_id, cursor_before=None)
        run = replace(
            run,
            stats={"topic": ev.topic, "doc_id": doc_id, "version_id": version_id},
        )
        repo.record_sync_run(run)

        try:
            # 5. 路由
            if ev.topic.startswith("document.tombstoned"):
                sha = writer.commit_tombstone(doc, version)
                created_n, updated_n, deleted_n = 0, 0, 1
            else:
                if version is None:
                    raise MirrorError(
                        f"doc {doc_id} 没有 active version 可镜像"
                    )
                sha = writer.commit_doc(doc, version)
                # 区分 created / updated：hash 不一致 → updated（首次 commit → created）
                # 简化：用 sha 是否等于 sync_run cursor_before 前的 SHA 判定
                # 这里直接记为 updated；首次 commit 也算 updated（语义上都是改）
                created_n, updated_n, deleted_n = 0, 1, 0

            # 6. 收尾 sync_run
            repo.complete_sync_run(
                run.id,
                cursor_after=sha,
                discovered=1,
                created_n=created_n,
                updated_n=updated_n,
                deleted_n=deleted_n,
                failed_n=0,
                stats={
                    "sha": sha,
                    "topic": ev.topic,
                    "doc_id": doc_id,
                    "version_id": version_id,
                },
            )
        except MirrorError as e:
            # 镜像错误：sync_run 标 FAILED（业务异常由 SyncWorker 退避）
            try:
                repo.fail_sync_run(run.id, str(e))
            except Exception:
                pass
            raise
        except Exception as e:
            try:
                repo.fail_sync_run(run.id, str(e))
            except Exception:
                pass
            raise

    return handle


__all__ = [
    "SUPPORTED_TOPICS",
    "is_supported",
    "make_mirror_handler",
]
