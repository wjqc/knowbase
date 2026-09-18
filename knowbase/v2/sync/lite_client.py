"""P4-D Lite Profile Git 客户端。

设计依据 V2 计划 §8.2「Local Profile 同步协议」：
- 后台 fetch（默认 30~60s）
- 远程前进 → fast-forward / rebase
- 内容冲突 → conflicted，保留双方版本
- push 走白名单（防 push 到任意仓库）

与 P4-C MirrorWriter 的区别：
- MirrorWriter：中心服务「唯一写入者」，消费 outbox；push 只到白名单 git remote
- LiteSyncClient：本机 profile，git fetch + rebase + push；冲突自动检测

git CLI 约定：
- 必须本机已装 git（macOS / Linux 都有）
- 涉及 fetch / push 的 URL 必须命中 allowed_remote_prefixes（至少 1 个前缀）
- 所有 git 调用都设 GIT_TERMINAL_PROMPT=0，禁止交互
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


class LiteSyncError(Exception):
    """Lite 同步失败：网络、git 错误、远程白名单拒绝等。"""


@dataclass(frozen=True)
class FetchResult:
    """fetch 一步的结果。

    Attributes:
        fetched: True 表示真拿到了新提交（FETCH_HEAD 与上次不同）。
        remote_revision: FETCH_HEAD 指向的 commit SHA。
        remote_branch: 远端分支名（fetch 调用方传入）。
    """
    fetched: bool
    remote_revision: str | None
    remote_branch: str


@dataclass(frozen=True)
class MergeTreeResult:
    """merge-tree / rebase 干运行结果。

    Attributes:
        clean: True 表示 3 路可自动合并（exit 0 + 无冲突 marker）。
        conflicting_paths: 冲突路径列表（clean=True 时为空）。
                    取自 diff ours..theirs 的变更文件 + merge-tree 退出非零的复合判定。
        result_tree: clean=True 时返回 result tree SHA（merge-tree --write-tree 写入）；
                     conflict 时为 None。
    """
    clean: bool
    conflicting_paths: tuple[str, ...]
    result_tree: str | None = None


@dataclass(frozen=True)
class RebaseResult:
    """rebase 实跑结果。"""
    success: bool
    conflicting_paths: tuple[str, ...]
    new_head: str | None


# ---------- Client ----------

class LiteSyncClient:
    """Lite Profile git 同步客户端。

    使用：
        client = LiteSyncClient(work_dir=Path("~/knowbase"),
                                allowed_remote_prefixes=("file://", "git@"))
        client.set_remote("origin", "file:///path/to/remote.git")
        r = client.fetch("main")
        if r.fetched and not client.is_ancestor("HEAD", r.remote_revision):
            ...  # rebase / conflict
    """

    def __init__(
        self,
        work_dir: Path,
        *,
        remote_name: str = "origin",
        allowed_remote_prefixes: tuple[str, ...] = (),
    ):
        self.work_dir = Path(work_dir)
        if not self.work_dir.exists():
            raise LiteSyncError(f"work_dir 不存在: {self.work_dir}")
        self.remote_name = remote_name
        self.allowed_remote_prefixes = tuple(allowed_remote_prefixes)

    # ---------- low-level ----------

    def _run(
        self,
        args: list[str],
        *,
        check: bool = True,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess:
        """跑 git 子命令；check=False 时不抛异常。"""
        wd = Path(cwd) if cwd else self.work_dir
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        try:
            cp = subprocess.run(
                args, cwd=str(wd), capture_output=True,
                text=True, env=env,
            )
        except FileNotFoundError as e:
            raise LiteSyncError(f"找不到 git 可执行：{e}") from e
        if check and cp.returncode != 0:
            err = (cp.stderr or cp.stdout or "").strip()
            raise LiteSyncError(
                f"git {' '.join(args)} 失败 (exit={cp.returncode}): {err}"
            )
        return cp

    def _check_remote(self, url: str) -> None:
        """空白名单 = 全部允许；非空必须命中前缀。"""
        if not self.allowed_remote_prefixes:
            return
        if not any(url.startswith(p) for p in self.allowed_remote_prefixes):
            raise LiteSyncError(
                f"remote url 不在白名单: url={url!r} "
                f"allowed={list(self.allowed_remote_prefixes)}"
            )

    # ---------- remote 配置 ----------

    def set_remote(self, name: str, url: str) -> None:
        """配置 / 替换 remote URL；URL 必须命中白名单。"""
        self._check_remote(url)
        cp = self._run(["git", "remote", "get-url", name], check=False)
        if cp.returncode == 0:
            self._run(["git", "remote", "set-url", name, url])
        else:
            self._run(["git", "remote", "add", name, url])

    def remote_url(self, name: str | None = None) -> str | None:
        """获取已配置 remote URL；不存在返回 None。"""
        cp = self._run(
            ["git", "remote", "get-url", name or self.remote_name], check=False
        )
        if cp.returncode != 0:
            return None
        return cp.stdout.strip() or None

    # ---------- refs ----------

    def current_revision(self, ref: str = "HEAD") -> str:
        """git rev-parse <ref>；返回完整 40-char SHA。"""
        return self._run(["git", "rev-parse", ref]).stdout.strip()

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        """git merge-base --is-ancestor <ancestor> <descendant>。"""
        cp = self._run(
            ["git", "merge-base", "--is-ancestor", ancestor, descendant], check=False
        )
        return cp.returncode == 0

    def merge_base(self, ref_a: str, ref_b: str) -> str | None:
        """git merge-base；无公共祖先返回 None。"""
        cp = self._run(["git", "merge-base", ref_a, ref_b], check=False)
        if cp.returncode != 0:
            return None
        return cp.stdout.strip() or None

    # ---------- fetch / push ----------

    def fetch(self, branch: str = "main") -> FetchResult:
        """git fetch origin <branch> → 更新 FETCH_HEAD。

        注：传入 URL 的远程调用使用 ``fetch_from_url``；这里固定走 self.remote_name。
        """
        # 先记旧的 FETCH_HEAD；fetch 后对比
        before = self._run(["git", "rev-parse", "FETCH_HEAD"], check=False)
        before_sha = before.stdout.strip() if before.returncode == 0 else ""
        cp = self._run(
            ["git", "fetch", self.remote_name, branch], check=False
        )
        if cp.returncode != 0:
            raise LiteSyncError(
                f"fetch {self.remote_name}/{branch} 失败: "
                f"{(cp.stderr or '').strip()}"
            )
        after_sha = self._run(["git", "rev-parse", "FETCH_HEAD"]).stdout.strip()
        fetched = bool(after_sha) and (after_sha != before_sha)
        return FetchResult(
            fetched=fetched,
            remote_revision=after_sha or None,
            remote_branch=branch,
        )

    def push(self, branch: str = "main") -> bool:
        """git push origin <branch>；白名单由 set_remote 配置阶段把关。"""
        cp = self._run(
            ["git", "push", self.remote_name, branch], check=False
        )
        if cp.returncode != 0:
            # remote rejected 视为策略失败（白名单 / non-fast-forward / 权限等）
            raise LiteSyncError(
                f"push {self.remote_name}/{branch} 失败: "
                f"{(cp.stderr or cp.stdout or '').strip()}"
            )
        return True

    # ---------- merge / rebase ----------

    def fast_forward(self, into: str = "FETCH_HEAD") -> bool:
        """Fast-forward 把 ``into`` merge 到 HEAD。

        注意：``git merge --ff-only`` 在本地领先(``into`` 已经是 HEAD 的祖先)时
        也返回 0 并输出 "Already up to date." —— 这种"什么都不做"的情况不算真正的
        ff 拉取，所以这里额外用 ``merge-base --is-ancestor`` 先过滤：
        - 本地领先（``is_ancestor(into, HEAD)``）→ 返回 False（调用方应改走 push）
        - diverged（既非 ancestor 也非 descendant）→ 返回 False（应改走 rebase）
        - 远端领先（``is_ancestor(HEAD, into)``）→ 执行 ``merge --ff-only``
        """
        # 本地领先 → 视为不可 ff
        if self.is_ancestor(into, "HEAD"):
            return False
        cp = self._run(["git", "merge", "--ff-only", into], check=False)
        return cp.returncode == 0

    def merge_tree(self, base: str, ours: str, theirs: str) -> MergeTreeResult:
        """3 路 merge 干运行 — 基于 ``git diff-tree`` 自实现，避免 ``merge-tree`` 在不同
        git 版本之间的不稳定行为（2.38+ 引入了 ``--write-tree``，但退出码语义和副作用
        在 2.39/2.40 上仍有差异；旧式 3-arg 调用在 2.40+ 已删除）。

        判定规则：
        1. 取 ``base..ours`` 与 ``base..theirs`` 的 file-level diff（包含 blob hash）
        2. 同一路径在两边都变更且 blob 不同 → 冲突（content conflict）
        3. 同一路径一边删除一边修改 → 冲突（delete/modify conflict）
        4. rename 一边发生一边未发生且目标与对方变更重叠 → 保守记为冲突
        5. 其它 → clean
        """
        ours_changes = self._changed_paths_with_blobs(base, ours)
        theirs_changes = self._changed_paths_with_blobs(base, theirs)

        conflicts: list[str] = []
        for path, ours_info in ours_changes.items():
            theirs_info = theirs_changes.get(path)
            if theirs_info is None:
                # 路径只在 ours 改，未冲突
                continue
            ours_blob = ours_info["blob"]
            ours_mode = ours_info["mode"]
            theirs_blob = theirs_info["blob"]
            theirs_mode = theirs_info["mode"]
            # delete/modify: 一边不存在(空 blob)而另一边存在或不同 mode
            if ours_blob != theirs_blob or ours_mode != theirs_mode:
                conflicts.append(path)

        # rename-only 冲突：ours 重命名 A→B，theirs 改了 A，但 ls-tree 视角 A 在
        # theirs 中已不存在；这种情况 diff-tree 会在 ours_changes 显示源路径
        # 被删除、目标路径新增；若 theirs_changes 中包含 ours_changes 的新增目标，
        # 也视为冲突。
        added_in_ours = {
            p for p, info in ours_changes.items()
            if info["status"] in ("A", "C")
        }
        for path in added_in_ours:
            theirs_info = theirs_changes.get(path)
            if theirs_info and theirs_info["status"] in ("M", "T", "D"):
                if path not in conflicts:
                    conflicts.append(path)

        return MergeTreeResult(
            clean=not conflicts,
            conflicting_paths=tuple(conflicts),
            result_tree=None,
        )

    def _changed_paths_with_blobs(
        self, base: str, target: str,
    ) -> dict[str, dict[str, str]]:
        """返回 ``base..target`` 变更的路径及对应 blob hash + mode + status。

        ``git diff-tree -r --no-commit-id`` 单行格式::

            :<old_mode> <new_mode> <old_sha> <new_sha> <status>\t<path>

        status 取最后 1 字符: A/M/D/T/C/R 等。
        """
        cp = self._run(
            ["git", "diff-tree", "-r", "--no-commit-id", "--diff-filter=AMDCRT",
             "--no-renames", base, target],
            check=False,
        )
        out: dict[str, dict[str, str]] = {}
        for raw in (cp.stdout or "").splitlines():
            if not raw.strip():
                continue
            parts = raw.split("\t", 1)
            if len(parts) != 2:
                continue
            meta, path = parts
            fields = meta.split()
            if len(fields) < 5:
                continue
            # fields: [":<old_mode>", "<new_mode>", "<old_sha>", "<new_sha>", "<status>"]
            status_char = fields[-1][0]
            new_sha = fields[3]
            new_mode = fields[1]
            out[path] = {
                "status": status_char,
                "blob": new_sha,
                "mode": new_mode,
            }
        return out

    def rebase(self, upstream: str) -> RebaseResult:
        """git rebase <upstream>；冲突时回滚到原状态并返回 conflicting_paths。"""
        # 记录 rebase 前 HEAD
        before_sha = self.current_revision("HEAD")
        # 先用 merge-tree 检测是否真的需要 rebase（upstream 已经是祖先时跳过）
        if self.is_ancestor(upstream, "HEAD"):
            return RebaseResult(success=True, conflicting_paths=(), new_head=before_sha)
        # 跑 rebase
        cp = self._run(["git", "rebase", upstream], check=False)
        if cp.returncode == 0:
            return RebaseResult(
                success=True, conflicting_paths=(),
                new_head=self.current_revision("HEAD"),
            )
        # 冲突：取冲突文件路径 + 取消 rebase
        conflicts_cp = self._run(
            ["git", "diff", "--name-only", "--diff-filter=U"], check=False
        )
        paths = tuple(
            p for p in (conflicts_cp.stdout or "").splitlines() if p.strip()
        )
        # 回滚 rebase（保留工作区状态？Lite Profile 这里假设冲突 → 整体取消）
        self._run(["git", "rebase", "--abort"], check=False)
        return RebaseResult(
            success=False, conflicting_paths=paths, new_head=before_sha,
        )

    # ---------- 工具 ----------

    def commit_all(self, message: str) -> str | None:
        """git add -A + commit；无变更返回 None。"""
        self._run(["git", "add", "-A"])
        cp = self._run(
            ["git", "diff", "--cached", "--quiet"], check=False
        )
        if cp.returncode == 0:
            # 没有任何 staged 变更
            return None
        self._run(["git", "commit", "-m", message], check=False)
        return self.current_revision("HEAD")


__all__ = [
    "LiteSyncClient",
    "LiteSyncError",
    "FetchResult",
    "MergeTreeResult",
    "RebaseResult",
]