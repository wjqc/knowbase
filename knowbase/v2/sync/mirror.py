"""P4-C 镜像写入器：唯一 Git 写入者，把审批后的 V2 文档镜像到独立 Git 仓库。

设计（V2 计划 §8.1「不再采用客户端直接写共享 main」+ §3.2「Git Audit Mirror（唯一写入者）」）：
- 本地 bare repo 路径 = ``<repo_root>/.knowbase/mirror.git``
- 临时 worktree 路径 = ``<repo_root>/.knowbase/mirror-work/``
- 文档按 ``{subdir}/{source_id}/{doc_id}.md`` 落盘（避免同 source 下不同 doc 撞名）
- commit message 格式：``mirror(<doc_id>): <title> [v<version_no>]``
- 同一 (doc_id, content_hash) 不重复 commit（幂等）
- push 默认关；远端推送走白名单（与 V1 gitops.push 对齐）
- tombstone 写入同路径，body 标 TOMBSTONE（不物理删除，避免远端拉回后死灰复燃）

线程/异步模型：
- 同步类（所有方法在 asyncio worker 内通过 run_in_executor 调用）
- bare repo 的 worktree 操作必须互斥：用 file lock 串行化（git CLI 不支持跨进程互斥）
"""
from __future__ import annotations

import fcntl
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..domain.models import Document, DocumentStatus, DocumentVersion
from .mirror_renderer import render_doc_markdown, render_tombstone_markdown


# ---------- 错误类型 ----------

class MirrorError(RuntimeError):
    """镜像操作失败（Git 错误、冲突等）。"""


class MirrorConflict(MirrorError):
    """内容冲突：本地与 remote 同一 doc_id 的 content_hash 不同。"""


class MirrorPushDenied(MirrorError):
    """远程 URL 不在白名单内，拒绝推送。"""


# ---------- 配置 ----------

@dataclass
class MirrorConfig:
    """MirrorWriter 行为配置。

    Attributes:
        mirror_subdir: bare repo 内文档子目录（默认 docs）。
        mirror_branch: 默认分支（默认 main；唯一写入者视角）。
        allowed_remote_prefixes: 远端 URL 白名单（防止误推公网）。
        auto_push: 默认 False（本地化底线；CI/测试按需开）。
        commit_author_name / commit_author_email: commit identity（默认 knowbase-mirror）。
        worktree_lock_timeout: worktree 锁等待秒数。
    """

    mirror_subdir: str = "docs"
    mirror_branch: str = "main"
    allowed_remote_prefixes: list[str] = field(default_factory=list)
    auto_push: bool = False
    commit_author_name: str = "knowbase-mirror"
    commit_author_email: str = "mirror@knowbase.local"
    worktree_lock_timeout: float = 10.0


# ---------- Git CLI 包装 ----------

def _git(cwd: Path, *args: str, check: bool = True) -> str:
    """调 git CLI；check=False 时不抛异常。"""
    r = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, text=True, encoding="utf-8",
    )
    out = (r.stdout or "") + (r.stderr or "")
    if check and r.returncode != 0:
        raise MirrorError(f"git {' '.join(args)} 失败: {out.strip()}")
    return out.strip()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- Writer ----------

class MirrorWriter:
    """Git Mirror 唯一写入者。

    使用：
        writer = MirrorWriter(repo_root)
        sha = writer.commit_doc(doc, version)
        writer.commit_tombstone(doc, version)
        # writer.push(remote_url)  # 默认不开；远端推送受白名单约束
    """

    def __init__(self, repo_root: Path, *, config: MirrorConfig | None = None):
        self.repo_root = Path(repo_root)
        self.config = config or MirrorConfig()
        # bare repo 与 worktree 都在 .knowbase/ 下；避免污染业务目录
        self.bare_dir = self.repo_root / ".knowbase" / "mirror.git"
        self.worktree_dir = self.repo_root / ".knowbase" / "mirror-work"
        self.lock_path = self.repo_root / ".knowbase" / "mirror.lock"
        self._init_if_needed()

    # ---------- 生命周期 ----------

    def _init_if_needed(self) -> None:
        """bare repo 不存在则 init --bare + 创建 worktree。

        关键：
        - bare repo 用 ``git --git-dir=BARE init --bare --initial-branch BRANCH`` 建；
        - bare repo 不能 ``commit --allow-empty``（"operation must be run in a work tree"）；
          也不能直接 ``worktree add -b``（HEAD 未指向任何 commit 会 "not a valid object name"）。
        - 解法：先用 ``commit-tree`` 在 bare repo 里创建一个 init commit（指向空 tree），
          再 ``update-ref refs/heads/BRANCH SHA``，最后 ``worktree add``。
        """
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock():
            if not self.bare_dir.exists():
                self.bare_dir.parent.mkdir(parents=True, exist_ok=True)
                self.bare_dir.mkdir(parents=True, exist_ok=False)
                _git(self.bare_dir, "init", "--bare",
                     "--initial-branch", self.config.mirror_branch)

                # 在 bare repo 里手工创建 init commit（空 tree）
                env = {
                    "GIT_AUTHOR_NAME": self.config.commit_author_name,
                    "GIT_AUTHOR_EMAIL": self.config.commit_author_email,
                    "GIT_COMMITTER_NAME": self.config.commit_author_name,
                    "GIT_COMMITTER_EMAIL": self.config.commit_author_email,
                }
                empty_tree = subprocess.run(
                    ["git", "-C", str(self.bare_dir), "hash-object", "-t", "tree",
                     "/dev/null"],
                    capture_output=True, text=True,
                ).stdout.strip()
                init_sha = subprocess.run(
                    ["git", "-C", str(self.bare_dir), "commit-tree",
                     empty_tree, "-m", "init: knowbase mirror"],
                    capture_output=True, text=True, env={
                        **__import__("os").environ, **env,
                    },
                ).stdout.strip()
                subprocess.run(
                    ["git", "-C", str(self.bare_dir), "update-ref",
                     f"refs/heads/{self.config.mirror_branch}", init_sha],
                    capture_output=True, text=True,
                    check=True,
                )

            # worktree 不存在则 add（首次新建分支，已有 init commit 可用）
            if not self.worktree_dir.exists():
                _git(
                    self.bare_dir, "worktree", "add",
                    str(self.worktree_dir), self.config.mirror_branch,
                )

    @contextmanager
    def _lock(self):
        """文件锁串行化 worktree 操作（多 worker 并发 / 跨进程）。"""
        lock_dir = self.lock_path.parent
        lock_dir.mkdir(parents=True, exist_ok=True)
        fh = open(self.lock_path, "a+")
        try:
            deadline = time.monotonic() + self.config.worktree_lock_timeout
            while True:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise MirrorError(
                            f"worktree 锁超时({self.config.worktree_lock_timeout}s): {self.lock_path}"
                        )
                    time.sleep(0.05)
            yield
        finally:
            try:
                fcntl.flock(fh, fcntl.LOCK_UN)
            finally:
                fh.close()

    # ---------- 路径与文件 ----------

    def _rel_path(self, doc: Document) -> Path:
        """doc 在 mirror repo 内的相对路径：``{subdir}/{source_id}/{doc_id}.md``。

        source_id 是 stable_id（16 字符 hex），doc_id 是 uuid4 hex；
        用 source_id 隔离不同来源，避免路径冲突。
        """
        sub = self.config.mirror_subdir.strip("/")
        return Path(sub) / doc.source_id / f"{doc.id}.md"

    def _worktree_path(self, doc: Document) -> Path:
        return self.worktree_dir / self._rel_path(doc)

    def _read_existing_hash(self, doc: Document) -> str | None:
        """读 worktree 内已存在文档的 content_hash（用于幂等判断）。

        返回 None 表示文件不存在；返回 str 是 frontmatter 里的 content_hash。
        """
        path = self._worktree_path(doc)
        if not path.exists():
            return None
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        # 解析 frontmatter 里的 content_hash
        if not text.startswith("---\n"):
            return None
        end = text.find("\n---\n", 4)
        if end < 0:
            return None
        fm_block = text[4:end]
        for line in fm_block.splitlines():
            if line.startswith("content_hash:"):
                val = line.split(":", 1)[1].strip()
                if val.startswith('"') and val.endswith('"'):
                    return val[1:-1]
                return val
        return None

    # ---------- 提交 ----------

    def commit_doc(self, doc: Document, version: DocumentVersion) -> str:
        """镜像一个 (doc, version)。返回 commit SHA。

        - 幂等：同一 (doc_id, content_hash) 已存在则跳过，返回原 SHA；
        - 写 worktree → add → commit → 返回 SHA；
        - 走 _lock 串行化（避免并发 worktree 状态错乱）。
        """
        with self._lock():
            existing = self._read_existing_hash(doc)
            if existing == version.content_hash:
                # 幂等：返回当前 HEAD
                return _git(self.bare_dir, "rev-parse", "HEAD")

            md = render_doc_markdown(doc, version)
            path = self._worktree_path(doc)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(md, encoding="utf-8")

            _git(self.worktree_dir, "add", str(self._rel_path(doc)))
            msg = f"mirror({doc.id}): {doc.title or doc.path} [v{version.version_no}]"
            env = {
                "GIT_AUTHOR_NAME": self.config.commit_author_name,
                "GIT_AUTHOR_EMAIL": self.config.commit_author_email,
                "GIT_COMMITTER_NAME": self.config.commit_author_name,
                "GIT_COMMITTER_EMAIL": self.config.commit_author_email,
            }
            self._git_with_env(
                self.worktree_dir,
                ["commit", "-m", msg],
                env=env,
            )
            sha = _git(self.bare_dir, "rev-parse", "HEAD")
            return sha

    def commit_tombstone(self, doc: Document, version: DocumentVersion | None = None) -> str:
        """镜像一个删除标记。返回 commit SHA。

        - 幂等：文件已存在且 frontmatter.status == tombstoned 则跳过；
        - 渲染 render_tombstone_markdown 写入 worktree；
        - 落盘后 commit。
        """
        with self._lock():
            existing_path = self._worktree_path(doc)
            if existing_path.exists():
                # 已 tombstoned？幂等跳过
                try:
                    text = existing_path.read_text(encoding="utf-8")
                    if "status: tombstoned" in text:
                        return _git(self.bare_dir, "rev-parse", "HEAD")
                except (OSError, UnicodeDecodeError):
                    pass

            md = render_tombstone_markdown(doc, version)
            existing_path.parent.mkdir(parents=True, exist_ok=True)
            existing_path.write_text(md, encoding="utf-8")

            _git(self.worktree_dir, "add", str(self._rel_path(doc)))
            msg = f"mirror({doc.id}): tombstone [{doc.title or doc.path}]"
            env = {
                "GIT_AUTHOR_NAME": self.config.commit_author_name,
                "GIT_AUTHOR_EMAIL": self.config.commit_author_email,
                "GIT_COMMITTER_NAME": self.config.commit_author_name,
                "GIT_COMMITTER_EMAIL": self.config.commit_author_email,
            }
            self._git_with_env(
                self.worktree_dir,
                ["commit", "-m", msg],
                env=env,
            )
            sha = _git(self.bare_dir, "rev-parse", "HEAD")
            return sha

    # ---------- 推送 ----------

    def push(self, remote_url: str) -> str:
        """推送到远端；远端 URL 必须命中白名单。

        返回远端 SHA（push 成功后从 mirror.git log 读 HEAD）；
        拒绝时抛 MirrorPushDenied。
        """
        if not self.config.allowed_remote_prefixes:
            raise MirrorPushDenied(
                f"未配置 allowed_remote_prefixes，禁止推送（本地化底线）"
            )
        if not any(remote_url.startswith(p) for p in self.config.allowed_remote_prefixes):
            raise MirrorPushDenied(
                f"remote {remote_url} 不在白名单 {self.config.allowed_remote_prefixes} 内"
            )
        # bare repo 直接 push 到 remote（绕过 worktree）
        with self._lock():
            # 加 remote（如未加）
            try:
                _git(self.bare_dir, "remote", "get-url", "origin")
            except MirrorError:
                _git(self.bare_dir, "remote", "add", "origin", remote_url)
            _git(self.bare_dir, "push", "origin", self.config.mirror_branch)
            return _git(self.bare_dir, "rev-parse", "HEAD")

    # ---------- 拉取（Lite fetch 用，P4-D 准备）----------

    def fetch(self, remote_url: str) -> str:
        """从远端 fetch（不 merge）；返回 remote HEAD SHA。

        - 远端 URL 必须命中白名单；
        - 不修改 worktree（合并留给 P4-D 处理）；
        - 用于 cursor 推进 + 冲突检测。
        """
        if not any(remote_url.startswith(p) for p in self.config.allowed_remote_prefixes):
            raise MirrorPushDenied(
                f"remote {remote_url} 不在白名单 {self.config.allowed_remote_prefixes} 内"
            )
        with self._lock():
            try:
                _git(self.bare_dir, "remote", "get-url", "origin")
            except MirrorError:
                _git(self.bare_dir, "remote", "add", "origin", remote_url)
            _git(self.bare_dir, "fetch", "origin", self.config.mirror_branch)
            return _git(
                self.bare_dir, "rev-parse", f"origin/{self.config.mirror_branch}",
            )

    # ---------- 内部：带环境变量 git ----------

    def _git_with_env(self, cwd: Path, args: list[str], env: dict) -> None:
        import os
        full_env = os.environ.copy()
        full_env.update(env)
        r = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True, text=True, encoding="utf-8",
            env=full_env,
        )
        if r.returncode != 0:
            raise MirrorError(f"git {' '.join(args)} 失败: {(r.stderr or r.stdout).strip()}")

    # ---------- 诊断 ----------

    def head_sha(self) -> str:
        return _git(self.bare_dir, "rev-parse", "HEAD")

    def log(self, n: int = 10) -> list[str]:
        """最近 n 个 commit message（调试用）。"""
        out = _git(self.bare_dir, "log", f"-n{n}", "--pretty=%H %s")
        return [line for line in out.splitlines() if line]


__all__ = [
    "MirrorWriter",
    "MirrorConfig",
    "MirrorError",
    "MirrorConflict",
    "MirrorPushDenied",
]
