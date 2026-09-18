"""git 操作：审计提交 + 受白名单约束的推送。

推送失败永不阻塞保存（离线可用是本地化工具的底线）；
远端白名单校验把"永不推公网"从纪律变成代码。
"""

import os
import subprocess
from datetime import datetime
from pathlib import Path

_LAST_FETCH_MONO: dict[str, float] = {}


def _run(repo: Path, *args, timeout: float = 15.0) -> str:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                       encoding="utf-8", timeout=timeout, env=env)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip())
    return r.stdout.strip()


def is_repo(repo: Path) -> bool:
    return (Path(repo) / ".git").exists()


def commit_all(repo: Path, message: str) -> str | None:
    """add -A + commit。返回告警信息（None=成功），绝不抛出。"""
    try:
        _run(repo, "add", "-A")
        r = subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", message],
            capture_output=True, text=True, encoding="utf-8", timeout=15,
        )
        out = (r.stdout or "") + (r.stderr or "")
        if r.returncode != 0 and "nothing to commit" not in out:
            return f"git commit 失败: {out.strip()}"
        return None
    except Exception as e:  # git 缺失、无身份配置等：保存已落盘，只告警
        return f"git 操作告警(不影响保存): {e}"


def push(repo: Path, remote_url: str, allowed_prefixes: list[str]) -> str | None:
    if not remote_url:
        return None
    if not any(remote_url.startswith(p) for p in allowed_prefixes):
        return f"已拒绝推送: remote {remote_url} 不在白名单 {allowed_prefixes} 内"
    try:
        _run(repo, "push", "-u", "origin", "HEAD", timeout=20)
        _record_sync(repo, "origin", "ok", push=True)
        return None
    except Exception as e:
        _record_sync(repo, "origin", "error", error=str(e), push=True)
        return f"push 失败(已忽略，本地已提交): {e}"


def _record_sync(repo: Path, remote: str, status: str, error: str = "",
                 fetch: bool = False, push: bool = False):
    try:
        from . import index
        conn = index.connect(repo)
        local = _run(repo, "rev-parse", "HEAD", timeout=3)
        try:
            remote_rev = _run(repo, "rev-parse", "origin/main", timeout=3)
        except Exception:
            remote_rev = ""
        now = datetime.now().isoformat(timespec="seconds")
        index.record_sync_state(conn, remote, local_revision=local,
                                remote_revision=remote_rev,
                                last_fetch_at=now if fetch else None,
                                last_push_at=now if push else None,
                                status=status, error=error)
        conn.close()
    except Exception:
        pass


def sync_before_read(repo: Path, cfg: dict) -> tuple[bool, str | None]:
    """TTL 到期时安全 fetch/ff-only；返回 (是否更新, 告警)。"""
    import time
    gcfg = cfg.get("git", {})
    if not gcfg.get("auto_pull", True) or not is_repo(repo):
        return False, None
    key = str(Path(repo).resolve())
    ttl = max(10.0, float(gcfg.get("pull_ttl_seconds", 60)))
    now_mono = time.monotonic()
    if now_mono - _LAST_FETCH_MONO.get(key, 0.0) < ttl:
        return False, None
    _LAST_FETCH_MONO[key] = now_mono
    try:
        remote_url = _run(repo, "remote", "get-url", "origin", timeout=3)
        prefixes = gcfg.get("allowed_remote_prefixes", [])
        if prefixes and not any(remote_url.startswith(p) for p in prefixes):
            raise RuntimeError(f"remote {remote_url} 不在白名单")
        dirty = _run(repo, "status", "--porcelain", timeout=3)
        _run(repo, "fetch", "origin", "main", timeout=10)
        local = _run(repo, "rev-parse", "HEAD", timeout=3)
        remote = _run(repo, "rev-parse", "origin/main", timeout=3)
        if local == remote:
            _record_sync(repo, "origin", "ok", fetch=True)
            return False, None
        if dirty:
            warning = "远端有更新，但本地工作区非干净状态；保留 last-known-good，未自动合并"
            _record_sync(repo, "origin", "blocked", error=warning, fetch=True)
            return False, warning
        try:
            _run(repo, "merge-base", "--is-ancestor", local, remote, timeout=3)
        except Exception:
            warning = "本地与远端已分叉；保留 last-known-good，需要人工处理"
            _record_sync(repo, "origin", "conflict", error=warning, fetch=True)
            return False, warning
        _run(repo, "merge", "--ff-only", "origin/main", timeout=10)
        from . import index
        index.rebuild(repo)
        _record_sync(repo, "origin", "ok", fetch=True)
        return True, None
    except Exception as exc:
        warning = f"远程同步检查失败，继续使用 last-known-good: {exc}"
        _record_sync(repo, "origin", "error", error=warning, fetch=True)
        return False, warning
