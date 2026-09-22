"""git 操作：审计提交 + 受白名单约束的推送。

推送失败永不阻塞保存（离线可用是本地化工具的底线）；
远端白名单校验把"永不推公网"从纪律变成代码。
"""

import os
import signal
import subprocess
from datetime import datetime
from pathlib import Path

from . import config as app_config
from .locking import RepoLock

_LAST_FETCH_MONO: dict[str, float] = {}

_IS_WINDOWS = os.name == "nt"


class GitSyncConflict(RuntimeError):
    """远端提交无法自动重放时的真实内容冲突。"""


def _kill_tree(proc: subprocess.Popen) -> None:
    """强杀 git 及其全部子孙进程。

    Windows 上 proc.kill() 只终止 git.exe 本身；孙进程（git-remote-http、
    凭据管理器、gc）继承 stdout/stderr 管道句柄继续存活，communicate()
    等 EOF 会永久阻塞——这是 memory_save 在 Windows 上无限加载的根因。
    """
    try:
        if _IS_WINDOWS:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=5)
        else:
            # start_new_session=True 使 pgid == proc.pid，整组击杀覆盖全部子孙
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    finally:
        if proc.poll() is None:
            proc.kill()


def _run_git(cmd: list[str], timeout: float) -> tuple[int, str, str]:
    """带超时执行 git，超时强杀整棵进程树后回收，绝不在管道 EOF 上永久阻塞。

    stdin 接 DEVNULL 杜绝子进程等终端输入；POSIX 放进独立会话以便 killpg
    整组击杀，Windows 新建进程组后用 taskkill /T 按进程树击杀。
    """
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    kwargs: dict = {}
    if _IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="utf-8", env=env, **kwargs,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:  # 树已死，正常应立即返回；再兜一层超时防句柄泄漏导致的二次挂起
            out, err = proc.communicate(timeout=5)
        except Exception:
            out, err = "", ""
        raise RuntimeError(
            f"git 超时({timeout}s)，已强制终止进程树: {' '.join(cmd[3:])}"
            f"；输出: {((err or out) or '').strip()[:200]}")
    return proc.returncode, out or "", err or ""


def _run(repo: Path, *args, timeout: float = 15.0) -> str:
    code, out, err = _run_git(["git", "-C", str(repo), *args], timeout)
    if code != 0:
        raise RuntimeError((err or out).strip())
    return out.strip()


def is_repo(repo: Path) -> bool:
    return (Path(repo) / ".git").exists()


def commit_all(repo: Path, message: str) -> str | None:
    """add -A + commit。返回告警信息（None=成功），绝不抛出。"""
    try:
        _run(repo, "add", "-A")
        code, out, err = _run_git(["git", "-C", str(repo), "commit", "-m", message], 15)
        output = out + err
        if code != 0 and "nothing to commit" not in output:
            return f"git commit 失败: {output.strip()}"
        return None
    except Exception as e:  # git 缺失、无身份配置、超时等：保存已落盘，只告警
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
        code, out, err = _run_git(
            ["git", "-C", str(repo), "commit", "-m", message, "--", *dict.fromkeys(relative)], 15)
        output = out + err
        if code != 0 and "nothing to commit" not in output:
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


def _rebase_onto(repo: Path, remote_ref: str) -> None:
    """把本地提交重放到远端；失败必须 abort，不能留下半截 rebase。"""
    try:
        _run(repo, "rebase", remote_ref, timeout=15)
    except Exception as exc:
        try:
            conflicted = _run(repo, "diff", "--name-only", "--diff-filter=U", timeout=3)
        except Exception:
            conflicted = ""
        try:
            _run(repo, "rebase", "--abort", timeout=5)
        except Exception:
            pass
        files = "、".join(line for line in conflicted.splitlines() if line.strip())
        detail = f"：{files}" if files else ""
        raise GitSyncConflict(f"检测到真实内容冲突{detail}；已取消 rebase 并保留本地提交") from exc


def _is_ancestor(repo: Path, older: str, newer: str) -> bool:
    try:
        _run(repo, "merge-base", "--is-ancestor", older, newer, timeout=3)
        return True
    except Exception:
        return False


def _has_user_changes(porcelain: str) -> bool:
    """RepoLock 自己创建的 `.lock` 不算用户工作区修改。"""
    for line in porcelain.splitlines():
        path = line[3:].strip() if len(line) >= 4 else line.strip()
        if path not in {".lock", "./.lock"}:
            return True
    return False


def _is_push_race(exc: Exception) -> bool:
    """只有远端抢先推进才值得 fetch/rebase 重试；认证/网络错误立即返回。"""
    message = str(exc).lower()
    return any(marker in message for marker in (
        "non-fast-forward", "fetch first", "failed to push some refs", "[rejected]",
    ))


def _integrate_remote(repo: Path, remote: str, branch: str) -> tuple[bool, bool]:
    """收敛到远端最新提交，返回 (本地 HEAD 是否变化, 是否需要 push)。"""
    remote_ref = f"{remote}/{branch}"
    local = _run(repo, "rev-parse", "HEAD", timeout=3)
    remote_rev = _run(repo, "rev-parse", remote_ref, timeout=3)
    if local == remote_rev:
        return False, False
    if _is_ancestor(repo, local, remote_rev):
        _run(repo, "merge", "--ff-only", remote_ref, timeout=10)
        return True, False
    if _is_ancestor(repo, remote_rev, local):
        return False, True
    _rebase_onto(repo, remote_ref)
    return True, True


def push(repo: Path, remote_url: str, allowed_prefixes: list[str]) -> str | None:
    remote = "origin"
    branch = "unknown"
    try:
        cfg = app_config.load_config()
        gcfg = cfg.get("git", {})
        remote, branch, actual_url = _remote_target(repo, gcfg)
        if allowed_prefixes and not any(actual_url.startswith(p) for p in allowed_prefixes):
            return f"已拒绝推送: 实际 remote {actual_url} 不在白名单 {allowed_prefixes} 内"
        if remote_url and remote_url != actual_url:
            return f"已拒绝推送: 配置 remote.url 与实际 {remote} URL 不一致"
        retries = max(1, int(gcfg.get("push_retries", 3)))
        last_error = None
        for attempt in range(retries):
            # 网络 IO 不持有 RepoLock；只在可能改 HEAD 的 integrate 阶段阻塞本机写入。
            _run(repo, "fetch", remote, branch, timeout=20)
            with RepoLock(repo, float(cfg.get("lock_timeout", 10.0))):
                dirty = _run(repo, "status", "--porcelain", timeout=3)
                if _has_user_changes(dirty):
                    raise RuntimeError("工作区存在未提交修改，未自动 rebase/push")
                _integrate_remote(repo, remote, branch)
            try:
                _run(repo, "push", "-u", remote, f"HEAD:{branch}", timeout=20)
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                if not _is_push_race(exc) or attempt + 1 == retries:
                    raise
        if last_error is not None:
            raise last_error
        _record_sync(repo, remote, branch, "ok", push=True)
        return None
    except GitSyncConflict as exc:
        _record_sync(repo, remote, branch, "conflict", error=str(exc), fetch=True)
        return f"同步冲突(本地提交已保留): {exc}"
    except Exception as e:
        try:
            _record_sync(repo, remote, branch, "error", error=str(e), push=True)
        except Exception:
            pass
        return f"push 失败(已忽略，本地已提交): {e}"


def schedule_push(repo: Path, git_cfg: dict) -> str | None:
    """兼容旧调用名：写锁释放后立即执行可收敛的 push。"""
    declared_url = str((git_cfg.get("remote", {}) or {}).get("url") or "")
    return push(repo, declared_url, git_cfg.get("allowed_remote_prefixes", []))


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
    """TTL 到期时安全 fetch/ff/rebase；返回 (是否更新, 告警)。"""
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
            if _has_user_changes(dirty):
                local = _run(repo, "rev-parse", "HEAD", timeout=3)
                remote_rev = _run(repo, "rev-parse", f"{remote_name}/{branch}", timeout=3)
                if local == remote_rev:
                    _record_sync(repo, remote_name, branch, "ok", fetch=True)
                    return False, None
                warning = "远端有更新，但本地工作区非干净状态；保留 last-known-good，未自动合并"
                _record_sync(repo, remote_name, branch, "blocked", error=warning, fetch=True)
                return False, warning
            changed, push_needed = _integrate_remote(repo, remote_name, branch)
            if not changed and not push_needed:
                _record_sync(repo, remote_name, branch, "ok", fetch=True)
                return False, None
            if changed:
                from . import index
                index.rebuild(repo)
                updated = True
            if push_needed:
                _record_sync(repo, remote_name, branch, "pending", fetch=True)
            else:
                _record_sync(repo, remote_name, branch, "ok", fetch=True)
    except GitSyncConflict as exc:
        warning = str(exc)
        _record_sync(repo, remote_name, branch or "unknown", "conflict", error=warning, fetch=True)
        return False, warning
    except Exception as exc:
        warning = f"远程同步检查失败，继续使用 last-known-good: {exc}"
        _record_sync(repo, remote_name, branch or "unknown", "error", error=warning, fetch=True)
        return False, warning
    if push_needed:
        warning = push(repo, str((gcfg.get("remote", {}) or {}).get("url") or ""),
                       gcfg.get("allowed_remote_prefixes", []))
        return updated, warning
    return updated, None
