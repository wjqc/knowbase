"""git 操作：审计提交 + 受白名单约束的推送。

推送失败永不阻塞保存（离线可用是本地化工具的底线）；
远端白名单校验把"永不推公网"从纪律变成代码。
"""

import os
import subprocess
from datetime import datetime
from pathlib import Path

from . import config as app_config
from .locking import RepoLock

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


def commit_paths(repo: Path, message: str, paths: list[Path]) -> str | None:
    """只提交本事务拥有的文件，避免把人工/并行修改卷入机器提交。"""
    try:
        root = Path(repo).resolve()
        relative = []
        for path in paths:
            resolved = Path(path).resolve()
            relative.append(str(resolved.relative_to(root)))
        if not relative:
            return None
        _run(repo, "add", "--", *dict.fromkeys(relative))
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        result = subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", message, "--", *dict.fromkeys(relative)],
            capture_output=True, text=True, encoding="utf-8", timeout=15, env=env,
        )
        output = (result.stdout or "") + (result.stderr or "")
        if result.returncode != 0 and "nothing to commit" not in output:
            return f"git commit 失败: {output.strip()}"
        return None
    except Exception as exc:
        return f"git 操作告警(不影响保存): {exc}"


def _remote_target(repo: Path, git_cfg: dict) -> tuple[str, str, str]:
    declared = git_cfg.get("remote", {}) or {}
    remote = str(declared.get("name") or "origin")
    actual_url = _run(repo, "remote", "get-url", remote, timeout=3)
    branch = str(declared.get("branch") or "").strip()
    if not branch:
        branch = _run(repo, "symbolic-ref", "--quiet", "--short", "HEAD", timeout=3)
    if not branch or branch == "HEAD":
        raise RuntimeError("当前处于 detached HEAD，且未配置 git.remote.branch")
    return remote, branch, actual_url


def push(repo: Path, remote_url: str, allowed_prefixes: list[str]) -> str | None:
    try:
        cfg = app_config.load_config()
        remote, branch, actual_url = _remote_target(repo, cfg.get("git", {}))
        if allowed_prefixes and not any(actual_url.startswith(p) for p in allowed_prefixes):
            return f"已拒绝推送: 实际 remote {actual_url} 不在白名单 {allowed_prefixes} 内"
        if remote_url and remote_url != actual_url:
            return f"已拒绝推送: 配置 remote.url 与实际 {remote} URL 不一致"
        _run(repo, "push", "-u", remote, f"HEAD:{branch}", timeout=20)
        _record_sync(repo, remote, branch, "ok", push=True)
        return None
    except Exception as e:
        try:
            cfg = app_config.load_config()
            remote, branch, _ = _remote_target(repo, cfg.get("git", {}))
            _record_sync(repo, remote, branch, "error", error=str(e), push=True)
        except Exception:
            pass
        return f"push 失败(已忽略，本地已提交): {e}"


def schedule_push(repo: Path, git_cfg: dict) -> str | None:
    """写路径只记录待推送状态，绝不执行网络 IO。"""
    try:
        remote, branch, actual_url = _remote_target(repo, git_cfg)
        prefixes = git_cfg.get("allowed_remote_prefixes", [])
        if prefixes and not any(actual_url.startswith(p) for p in prefixes):
            return f"已拒绝排队推送: 实际 remote {actual_url} 不在白名单 {prefixes} 内"
        declared_url = str((git_cfg.get("remote", {}) or {}).get("url") or "")
        if declared_url and declared_url != actual_url:
            return f"已拒绝排队推送: 配置 remote.url 与实际 {remote} URL 不一致"
        _record_sync(repo, remote, branch, "pending")
        return None
    except Exception as exc:
        return f"排队推送失败(本地提交已完成): {exc}"


def _record_sync(repo: Path, remote: str, branch: str, status: str, error: str = "",
                 fetch: bool = False, push: bool = False):
    try:
        from . import index
        conn = index.connect(repo)
        local = _run(repo, "rev-parse", "HEAD", timeout=3)
        try:
            remote_rev = _run(repo, "rev-parse", f"{remote}/{branch}", timeout=3)
        except Exception:
            remote_rev = ""
        now = datetime.now().isoformat(timespec="seconds")
        index.record_sync_state(conn, f"{remote}/{branch}", local_revision=local,
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
    remote_name = str((gcfg.get("remote", {}) or {}).get("name") or "origin")
    branch = str((gcfg.get("remote", {}) or {}).get("branch") or "")
    push_needed = False
    updated = False
    try:
        with RepoLock(repo, float(cfg.get("lock_timeout", 10.0))):
            remote_name, branch, actual_url = _remote_target(repo, gcfg)
            prefixes = gcfg.get("allowed_remote_prefixes", [])
            if prefixes and not any(actual_url.startswith(p) for p in prefixes):
                raise RuntimeError(f"实际 remote {actual_url} 不在白名单")
            dirty = _run(repo, "status", "--porcelain", timeout=3)
            _run(repo, "fetch", remote_name, branch, timeout=10)
            local = _run(repo, "rev-parse", "HEAD", timeout=3)
            remote_rev = _run(repo, "rev-parse", f"{remote_name}/{branch}", timeout=3)
            if local == remote_rev:
                _record_sync(repo, remote_name, branch, "ok", fetch=True)
                return False, None
            if dirty:
                warning = "远端有更新，但本地工作区非干净状态；保留 last-known-good，未自动合并"
                _record_sync(repo, remote_name, branch, "blocked", error=warning, fetch=True)
                return False, warning
            try:
                _run(repo, "merge-base", "--is-ancestor", local, remote_rev, timeout=3)
                remote_ahead = True
            except Exception:
                remote_ahead = False
            try:
                _run(repo, "merge-base", "--is-ancestor", remote_rev, local, timeout=3)
                local_ahead = True
            except Exception:
                local_ahead = False
            if remote_ahead:
                _run(repo, "merge", "--ff-only", f"{remote_name}/{branch}", timeout=10)
                from . import index
                index.rebuild(repo)
                _record_sync(repo, remote_name, branch, "ok", fetch=True)
                updated = True
            elif local_ahead:
                _record_sync(repo, remote_name, branch, "pending", fetch=True)
                push_needed = True
            else:
                warning = "本地与远端已分叉；保留 last-known-good，需要人工处理"
                _record_sync(repo, remote_name, branch, "conflict", error=warning, fetch=True)
                return False, warning
    except Exception as exc:
        warning = f"远程同步检查失败，继续使用 last-known-good: {exc}"
        _record_sync(repo, remote_name, branch or "unknown", "error", error=warning, fetch=True)
        return False, warning
    if push_needed:
        warning = push(repo, str((gcfg.get("remote", {}) or {}).get("url") or ""),
                       gcfg.get("allowed_remote_prefixes", []))
        return False, warning
    return updated, None
