"""P4-D Lite Profile 同步协调器。

实现 V2 计划 §8.2 的一次同步：
    1. fetch remote branch → 拿到 remote_revision
    2. 比对 local HEAD vs remote：
       - 相同 → noop，run SUCCEEDED
       - remote 是 local 祖先 → 本地领先，fast-forward push
       - local 是 remote 祖先 → 本地落后，fast-forward pull
       - 都不是祖先 → diverged：
         a. merge-tree 干运行 → clean → rebase onto remote → push
         b. dirty → 标记 conflicted，sync_run CONFLICTED；state.last_error 保留冲突路径
    3. 失败：sync_run FAILED + state.last_failed_at / last_error 更新
    4. 写 source_sync_state（last_remote_check_at / last_pull_at / last_push_at / last_failed_at）

不接管 outbox（P4-B 范围）；本协调器只覆盖 git fetch/rebase/push 路径。
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from ..domain.models import Source, SourceSyncState, SyncRun, SyncRunStatus
from ..repositories.sqlite_repo import V2Repository
from .lite_client import LiteSyncClient, LiteSyncError


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class LiteSyncCoordinator:
    """Lite Profile 同步协调器。

    使用：
        client = LiteSyncClient(work_dir, allowed_remote_prefixes=("file://",))
        client.set_remote("origin", "file:///path/to/remote.git")
        coord = LiteSyncCoordinator(repo, client)
        run = coord.sync(source, branch="main")
        # run.status ∈ {SUCCEEDED, FAILED, CONFLICTED}
    """

    def __init__(self, repo: V2Repository, client: LiteSyncClient):
        self.repo = repo
        self.client = client

    # ---------- 主入口 ----------

    def sync(self, source: Source, *, branch: str = "main") -> SyncRun:
        """执行一次 Lite sync run；返回 SyncRun。

        副作用：
        - 写 sync_run 行（status 终态）
        - upsert source_sync_state
        """
        run = SyncRun.new(source_id=source.stable_id)
        self.repo.record_sync_run(run)

        # 状态初始化
        try:
            prior_state = self.repo.get_sync_state(source.stable_id)
        except Exception:
            prior_state = None
        local_rev = ""
        try:
            local_rev = self.client.current_revision("HEAD")
        except LiteSyncError as e:
            return self._finish_failed(run, prior_state, str(e), local_rev="")

        state = self._ensure_state(source.stable_id, local_rev)

        try:
            # 1. fetch
            fetch_result = self.client.fetch(branch)
            remote_rev = fetch_result.remote_revision
            state = replace(
                state,
                last_remote_check_at=_now_iso(),
                last_remote_revision=remote_rev,
                local_revision=local_rev,
            )

            # 2. 比对
            if remote_rev is None or remote_rev == local_rev:
                # 没有新东西或已经同步
                return self._finish_succeeded(
                    run, state,
                    cursor_after=remote_rev or local_rev,
                    discovered=0, created_n=0, updated_n=0, deleted_n=0,
                    stats={"branch": branch, "noop": True},
                )

            if self.client.is_ancestor(local_rev, remote_rev):
                # 远端领先 → fast-forward pull
                if not self.client.fast_forward("FETCH_HEAD"):
                    raise LiteSyncError("fast-forward merge 失败")
                new_local = self.client.current_revision("HEAD")
                state = replace(
                    state,
                    local_revision=new_local,
                    last_pull_at=_now_iso(),
                )
                return self._finish_succeeded(
                    run, state,
                    cursor_after=new_local,
                    discovered=1, created_n=0, updated_n=1, deleted_n=0,
                    stats={"branch": branch, "action": "fast-forward"},
                )

            if self.client.is_ancestor(remote_rev, local_rev):
                # 本地领先 → push
                self.client.push(branch)
                new_local = self.client.current_revision("HEAD")
                state = replace(state, last_push_at=_now_iso())
                return self._finish_succeeded(
                    run, state,
                    cursor_after=new_local,
                    discovered=0, created_n=0, updated_n=0, deleted_n=0,
                    stats={"branch": branch, "action": "push"},
                )

            # 3. diverged：merge-tree 干运行
            base = self.client.merge_base(local_rev, remote_rev)
            if base is None:
                raise LiteSyncError("无可计算公共祖先")
            mt = self.client.merge_tree(base, local_rev, remote_rev)
            if mt.clean:
                # rebase
                rb = self.client.rebase(remote_rev)
                if not rb.success:
                    # 干运行说 clean 但实跑冲突 → 保守冲突
                    return self._finish_conflicted(
                        run, state,
                        f"merge-tree clean 但 rebase 冲突: {list(rb.conflicting_paths)}",
                        discovered=1, failed_n=1,
                        stats={"branch": branch, "base": base},
                    )
                # push
                self.client.push(branch)
                new_local = self.client.current_revision("HEAD")
                state = replace(
                    state,
                    local_revision=new_local,
                    last_pull_at=_now_iso(),
                    last_push_at=_now_iso(),
                )
                return self._finish_succeeded(
                    run, state,
                    cursor_after=new_local,
                    discovered=1, created_n=0, updated_n=1, deleted_n=0,
                    stats={
                        "branch": branch, "action": "rebase+push",
                        "base": base, "result_tree": mt.result_tree,
                    },
                )

            # 冲突：保留双方版本（不动 local，等人工合并）
            return self._finish_conflicted(
                run, state,
                f"内容冲突: {list(mt.conflicting_paths)}",
                discovered=1, failed_n=1,
                stats={
                    "branch": branch,
                    "base": base,
                    "conflicting_paths": list(mt.conflicting_paths),
                },
            )

        except LiteSyncError as e:
            return self._finish_failed(run, state, str(e), local_rev=local_rev)
        except Exception as e:  # noqa: BLE001
            return self._finish_failed(
                run, state, f"未预期异常: {type(e).__name__}: {e}",
                local_rev=local_rev,
            )

    # ---------- 收尾 ----------

    def _finish_succeeded(
        self, run: SyncRun, state: SourceSyncState, *,
        cursor_after: str, discovered: int, created_n: int,
        updated_n: int, deleted_n: int, stats: dict,
    ) -> SyncRun:
        self.repo.complete_sync_run(
            run.id,
            cursor_after=cursor_after,
            discovered=discovered,
            created_n=created_n,
            updated_n=updated_n,
            deleted_n=deleted_n,
            failed_n=0,
            stats=stats,
        )
        state = replace(
            state,
            last_sync_run_id=run.id,
            last_error=None,
        )
        self.repo.upsert_sync_state(state)
        return self.repo.get_sync_run(run.id) or run

    def _finish_failed(
        self, run: SyncRun, state: SourceSyncState | None, error: str, *,
        local_rev: str = "",
    ) -> SyncRun:
        self.repo.fail_sync_run(run.id, error)
        prior_local = state.local_revision if state else None
        new_state = self._ensure_state(
            run.source_id, local_rev or prior_local or "",
        )
        new_state = replace(
            new_state,
            last_failed_at=_now_iso(),
            last_error=error,
            last_sync_run_id=run.id,
            local_revision=local_rev or prior_local or "",
        )
        self.repo.upsert_sync_state(new_state)
        return self.repo.get_sync_run(run.id) or run

    def _finish_conflicted(
        self, run: SyncRun, state: SourceSyncState, error: str, *,
        discovered: int, failed_n: int, stats: dict,
    ) -> SyncRun:
        self.repo.conflict_sync_run(run.id, error)
        new_state = replace(
            state,
            last_failed_at=_now_iso(),
            last_error=error,
            last_sync_run_id=run.id,
            # conflicted_docs 计数由 doc status 决定，不在 state 字段冗余；
            # 这里保守自增 1，避免 compute_sync_status 漏报
            conflicted_docs=state.conflicted_docs + 1,
        )
        self.repo.upsert_sync_state(new_state)
        # 也写 run 的 stats
        try:
            self.repo.complete_sync_run(
                run.id, cursor_after=state.local_revision or "",
                discovered=discovered, created_n=0, updated_n=0,
                deleted_n=0, failed_n=failed_n, stats=stats,
            )
            # 上面覆盖了 status；改回 conflicted：
            self.repo.conflict_sync_run(run.id, error)
        except Exception:
            pass
        return self.repo.get_sync_run(run.id) or run

    # ---------- helpers ----------

    def _ensure_state(self, source_id: str, local_rev: str) -> SourceSyncState:
        existing = self.repo.get_sync_state(source_id)
        if existing is not None:
            if existing.local_revision != local_rev:
                existing = replace(existing, local_revision=local_rev)
            return existing
        return SourceSyncState(source_id=source_id, local_revision=local_rev)


__all__ = ["LiteSyncCoordinator"]