"""git 操作：审计提交 + 受白名单约束的推送。

推送失败永不阻塞保存（离线可用是本地化工具的底线）；
远端白名单校验把"永不推公网"从纪律变成代码。

M0.5: 跨进程 TTL 使用 sync_lease 表替代进程内 _LAST_FETCH_MONO；
sync_attempts 记录每次同步尝试，供观测和诊断。
"""

import os
import signal
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config as app_config
from .locking import RepoLock

# 进程内缓存保留作为快速路径优化，但跨进程协调以数据库 sync_lease 为准
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


# git 身份未配置时 rebase 的典型报错（新机器首跑最常见）：Committer/Author
# identity unknown、Please tell me who you are、unable to auto-detect email。
_IDENTITY_MARKERS = (
    "please tell me who you are", "identity unknown", "unable to auto-detect email",
    "empty ident name", "user.name", "user.email",
)


def _rebase_failure_kind(message: str, conflicted: str) -> str:
    """rebase 失败分类：conflict=真实内容冲突 / identity=git 身份未配置 / other=其他。

    未合并条目（diff-filter=U）是真实冲突的硬证据；git 明确报告 CONFLICT
    次之。身份等环境问题没有任何冲突证据，不得归入 conflict。
    """
    if conflicted.strip():
        return "conflict"
    text = (message or "").lower()
    if any(marker in text for marker in _IDENTITY_MARKERS):
        return "identity"
    if "conflict" in text:
        return "conflict"
    return "other"


def _in_rebase_state(repo: Path, default: bool) -> bool:
    """检查是否仍在 rebase 状态（.git/rebase-merge 或 .git/rebase-apply 存在）。"""
    try:
        git_dir = Path(_run(repo, "rev-parse", "--git-dir", timeout=2))
        if not git_dir.is_absolute():
            git_dir = Path(repo) / git_dir
        return (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists()
    except Exception:
        return default


def _abort_rebase(repo: Path) -> bool:
    """尝试 abort 并核实已退出 rebase 状态；返回 True 表示 abort 未能完成。

    abort 命令本身失败（如 rebase 根本没启动、无 rebase 可 abort）不等于
    abort 失败，以事后状态检查为准；状态无法核实时按 abort 命令结果取保守值。
    """
    try:
        _run(repo, "rebase", "--abort", timeout=5)
    except Exception:
        return _in_rebase_state(repo, default=True)
    return _in_rebase_state(repo, default=False)


def _rebase_onto(repo: Path, remote_ref: str) -> None:
    """把本地提交重放到远端；失败必须 abort，不能留下半截 rebase。

    失败按原因分类报错：只有真实内容冲突才抛 GitSyncConflict；git 身份
    未配置等环境问题误报“真实内容冲突”会把排查引向错误方向，故以
    RuntimeError 写明真实原因。无论哪类失败都保留本地提交。
    """
    try:
        _run(repo, "rebase", remote_ref, timeout=15)
        return
    except Exception as exc:
        original = exc
    try:
        conflicted = _run(repo, "diff", "--name-only", "--diff-filter=U", timeout=3)
    except Exception:
        conflicted = ""
    kind = _rebase_failure_kind(str(original), conflicted)
    abort_failed = _abort_rebase(repo)
    abort_note = "；rebase abort 失败，请手动检查 .git 状态" if abort_failed else ""
    if kind == "conflict":
        files = "、".join(line for line in conflicted.splitlines() if line.strip())
        detail = f"：{files}" if files else ""
        if abort_failed:
            raise GitSyncConflict(
                f"检测到真实内容冲突{detail}；rebase abort 失败，请手动检查 .git 状态"
            ) from original
        raise GitSyncConflict(f"检测到真实内容冲突{detail}；已取消 rebase 并保留本地提交") from original
    if kind == "identity":
        raise RuntimeError(
            "git 身份未配置（user.name/user.email 缺失），rebase 无法重放本地提交；"
            f"已取消 rebase 并保留本地提交，请先用 git config 配置身份后重试{abort_note}"
        ) from original
    raise RuntimeError(
        f"rebase 失败（非内容冲突）：{str(original)[:200]}；已取消 rebase 并保留本地提交{abort_note}"
    ) from original


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


def _is_auth_error(exc: Exception) -> bool:
    """认证错误应立即停止，不重试。"""
    message = str(exc).lower()
    return any(marker in message for marker in (
        "authentication", "permission denied", "could not read username",
        "invalid username or password", "access denied",
    ))


def _is_network_error(exc: Exception) -> bool:
    """网络错误应指数退避重试。"""
    message = str(exc).lower()
    return any(marker in message for marker in (
        "could not resolve host", "connection refused", "connection timed out",
        "network is unreachable", "ssl", "tls", "timeout",
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
    push_sha = ""  # 记录正在推送的 SHA，用于恢复核对
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
            # 记录即将推送的 SHA
            push_sha = _run(repo, "rev-parse", "HEAD", timeout=3)
            try:
                _run(repo, "push", "-u", remote, f"HEAD:{branch}", timeout=20)
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                # 认证错误立即停止，不重试
                if _is_auth_error(exc):
                    raise
                # 网络错误使用指数退避
                if _is_network_error(exc) and attempt + 1 < retries:
                    import time
                    time.sleep(min(30, 2 ** attempt))
                    continue
                # 竞态错误（non-fast-forward）走 fetch/rebase 重试
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
        error_type = "unknown"
        if _is_auth_error(e):
            error_type = "auth"
        elif _is_network_error(e):
            error_type = "network"
        elif _is_push_race(e):
            error_type = "race"
        try:
            _record_sync(repo, remote, branch, "error", 
                         error=f"[{error_type}] {e}" + (f" (push_sha={push_sha})" if push_sha else ""),
                         push=True)
        except Exception:
            pass
        # 网络未知结果明确报告
        if error_type == "network":
            return f"push 网络超时(本地已提交，恢复后将核对远端 SHA={push_sha}): {e}"
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


def record_sync_attempt(repo: Path, remote: str, branch: str, target_sha: str,
                        result: str, duration_ms: int, error: str = "",
                        confirmed_revision: str = "", pending_push_count: int = 0,
                        notes: str = "") -> str:
    """记录一次同步尝试到 sync_attempts 表，返回 attempt_id。

    M0.5: 供观测和诊断使用，doctor 将记录缺失/过期标为 unknown。
    """
    try:
        from . import index
        conn = index.connect(repo)
        attempt_id = f"SA-{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO sync_attempts(attempt_id,ts,remote,branch,target_sha,result,"
            "duration_ms,confirmed_revision,pending_push_count,error,notes) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (attempt_id, now, remote, branch, target_sha, result, duration_ms,
             confirmed_revision, pending_push_count, error, notes),
        )
        conn.commit()
        conn.close()
        return attempt_id
    except Exception:
        return ""


def _lease_connection(repo: Path):
    import sqlite3
    from .index import db_path
    conn = sqlite3.connect(db_path(repo), timeout=0.05)
    conn.execute("PRAGMA busy_timeout=50")
    return conn


def sync_target_hash(repo: Path, git_cfg: dict, remote: str, branch: str) -> str:
    """绑定本机可观察的远端配置/分支版本，不在日志中暴露 URL 凭据。"""
    import hashlib
    import json
    git_dir = Path(repo) / ".git"
    if git_dir.is_file():
        git_dir = (Path(repo) / git_dir.read_text().strip().removeprefix("gitdir: ")).resolve()
    common = git_dir
    if (git_dir / "commondir").is_file():
        common = (git_dir / (git_dir / "commondir").read_text().strip()).resolve()
    data = {"remote": remote, "branch": branch, "declared":git_cfg.get("remote", {}),
            "allowed":git_cfg.get("allowed_remote_prefixes", []),
            "head":(git_dir/"HEAD").read_text(), "git_config":(common/"config").read_text()}
    return hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest()


def acquire_sync_lease(repo: Path, remote: str, branch: str, *,
                       duration: float = 120.0, respect_cooldown: bool = True,
                       target_hash: str = "") -> tuple[str | None, str]:
    """原子领取同一同步目标。数据库异常时不执行无协调同步。"""
    conn = None
    try:
        conn = _lease_connection(repo)
        conn.execute("BEGIN IMMEDIATE")
        now = datetime.now(timezone.utc)
        stamp = now.isoformat(timespec="seconds")
        row = conn.execute("SELECT lease_owner,lease_until,next_check_at,target_hash FROM sync_lease "
                           "WHERE repo=? AND remote=? AND branch=?", (str(Path(repo).resolve()), remote, branch)).fetchone()
        if row and row[0] and row[1] and row[1] > stamp:
            conn.rollback()
            return None, "同步租约已被占用"
        if respect_cooldown and row and row[2] and row[2] > stamp and row[3] == target_hash:
            conn.rollback()
            return None, "同步检查仍在冷却期"
        owner = "sync-" + uuid.uuid4().hex
        expires = (now + timedelta(seconds=max(30, duration))).isoformat(timespec="seconds")
        conn.execute("INSERT INTO sync_lease(repo,remote,branch,lease_owner,lease_until,last_attempt,target_hash) "
                     "VALUES(?,?,?,?,?,?,?) ON CONFLICT(repo,remote,branch) DO UPDATE SET "
                     "lease_owner=excluded.lease_owner,lease_until=excluded.lease_until,last_attempt=excluded.last_attempt,"
                     "target_hash=excluded.target_hash,last_success=CASE WHEN sync_lease.target_hash=excluded.target_hash "
                     "THEN sync_lease.last_success ELSE NULL END",
                     (str(Path(repo).resolve()), remote, branch, owner, expires, stamp, target_hash))
        conn.commit()
        return owner, ""
    except Exception as exc:
        if conn:
            conn.rollback()
        return None, "同步租约不可用：" + type(exc).__name__
    finally:
        if conn:
            conn.close()


def _owns_sync_lease(repo: Path, remote: str, branch: str, owner: str) -> bool:
    conn = None
    try:
        conn = _lease_connection(repo)
        row = conn.execute("SELECT lease_owner,lease_until FROM sync_lease WHERE repo=? AND remote=? AND branch=?",
                           (str(Path(repo).resolve()), remote, branch)).fetchone()
        return bool(row and row[0] == owner and row[1] and row[1] > datetime.now(timezone.utc).isoformat(timespec="seconds"))
    except Exception:
        return False
    finally:
        if conn:
            conn.close()


def _check_sync_lease(repo: Path, remote: str, branch: str, ttl_seconds: float) -> tuple[bool, str]:
    owner, warning = acquire_sync_lease(repo, remote, branch, duration=max(120, ttl_seconds))
    return bool(owner), warning


def _release_sync_lease(repo: Path, remote: str, branch: str, success: bool, *,
                        owner: str | None = None, cooldown: float = 0):
    conn = None
    try:
        conn = _lease_connection(repo)
        now = datetime.now(timezone.utc)
        next_at = (now + timedelta(seconds=max(0, cooldown))).isoformat(timespec="seconds")
        where = "repo=? AND remote=? AND branch=?" + (" AND lease_owner=?" if owner else "")
        params = [str(Path(repo).resolve()), remote, branch] + ([owner] if owner else [])
        conn.execute("UPDATE sync_lease SET lease_owner='',lease_until='',next_check_at=?,"
                     "last_success=CASE WHEN ? THEN ? ELSE last_success END WHERE " + where,
                     [next_at, int(success), now.isoformat(timespec="seconds"), *params])
        conn.commit()
    except Exception:
        if conn:
            conn.rollback()  # 无法持久释放时保留旧租约，过期后才能接管。
    finally:
        if conn:
            conn.close()


def sync_before_read(repo: Path, cfg: dict, *, lease_owner: str | None = None,
                     target_remote: str | None = None, target_branch: str | None = None) -> tuple[bool, str | None]:
    """同步内核：网络不持仓库锁，正式索引重建仍受同一锁保护。"""
    import time
    gcfg = cfg.get("git", {})
    if not gcfg.get("auto_pull", True) or not is_repo(repo):
        if lease_owner and target_remote and target_branch:
            _release_sync_lease(repo, target_remote, target_branch, False, owner=lease_owner)
        return False, None
    remote, branch = target_remote or "origin", target_branch or "unknown"
    ttl = max(10.0, float(gcfg.get("pull_ttl_seconds", 60)))
    owner = lease_owner
    lease_target = (remote, branch) if lease_owner else None
    started = time.monotonic()
    updated, success, push_needed = False, False, False
    target_sha, warning = "", None
    try:
        actual_remote, actual_branch, actual_url = _remote_target(repo, gcfg)
        if lease_owner and (actual_remote != remote or actual_branch != branch):
            raise RuntimeError("同步目标已变化，未执行旧任务")
        remote, branch = actual_remote, actual_branch
        prefixes = gcfg.get("allowed_remote_prefixes", [])
        declared_url = str((gcfg.get("remote", {}) or {}).get("url") or "")
        if prefixes and not any(actual_url.startswith(prefix) for prefix in prefixes):
            raise RuntimeError("实际 remote 不在白名单")
        if declared_url and declared_url != actual_url:
            raise RuntimeError("配置 remote.url 与实际远端不一致")
        fingerprint = sync_target_hash(repo,gcfg,remote,branch)
        if not owner:
            owner, lease_warning = acquire_sync_lease(repo, remote, branch,target_hash=fingerprint)
            if not owner:
                return False, lease_warning if "不可用" in lease_warning else None
            lease_target = (remote, branch)
        if not _owns_sync_lease(repo, remote, branch, owner):
            return False, "同步租约失效，未执行"
        from contextlib import closing
        with closing(_lease_connection(repo)) as lease_conn:
            frozen = lease_conn.execute("SELECT target_hash FROM sync_lease WHERE repo=? AND remote=? AND branch=?",
                                       (str(Path(repo).resolve()),remote,branch)).fetchone()[0]
        if frozen and frozen != fingerprint:
            raise RuntimeError("同步配置版本已变化，未执行旧任务")
        target_sha = _run(repo, "rev-parse", "HEAD", timeout=3)
        # Fetch 只更新远端跟踪引用；慢网络不得阻塞本机卡片写入/读取。
        _run(repo, "fetch", remote, branch, timeout=10)
        with RepoLock(repo, float(cfg.get("lock_timeout", 10.0))):
            if not _owns_sync_lease(repo, remote, branch, owner):
                raise RuntimeError("同步租约失效，未合入远端")
            if lease_owner:
                current_cfg = app_config.load_config().get("git", {})
                if (not current_cfg.get("auto_pull", True) or
                        sync_target_hash(repo,current_cfg,remote,branch) != fingerprint):
                    raise RuntimeError("同步配置已变化，未合入旧结果")
            if _remote_target(repo, gcfg) != (remote, branch, actual_url):
                raise RuntimeError("同步目标已变化，未合入旧结果")
            if sync_target_hash(repo,gcfg,remote,branch) != fingerprint:
                raise RuntimeError("同步配置版本已变化，未合入旧结果")
            if _run(repo,"symbolic-ref","--quiet","--short","HEAD",timeout=3) != branch:
                raise RuntimeError("当前工作分支与同步目标不同，未改写工作树")
            dirty = _run(repo, "status", "--porcelain", timeout=3)
            local = _run(repo, "rev-parse", "HEAD", timeout=3)
            remote_rev = _run(repo, "rev-parse", f"{remote}/{branch}", timeout=3)
            if _has_user_changes(dirty) and local != remote_rev:
                raise RuntimeError("工作区有未提交内容，保留 last-known-good，未自动合并")
            if not _has_user_changes(dirty):
                changed, push_needed = _integrate_remote(repo, remote, branch)
                if changed:
                    from . import index
                    index.rebuild(repo)
                    updated = True
            success = True
        if push_needed and gcfg.get("auto_push", False):
            warning = push(repo, declared_url, prefixes)
            success = warning is None
        status = "error" if warning else "pending" if push_needed and not gcfg.get("auto_push", False) else "ok"
        if _owns_sync_lease(repo, remote, branch, owner):
            _record_sync(repo, remote, branch, status, error=warning or "", fetch=True)
        record_sync_attempt(repo, remote, branch, target_sha, "updated" if updated else "ok",
            int((time.monotonic()-started)*1000), confirmed_revision=_run(repo,"rev-parse","HEAD",timeout=3),
            pending_push_count=int(push_needed and not gcfg.get("auto_push",False)))
        return updated, warning
    except Exception as exc:
        warning = "远程同步检查失败，继续使用 last-known-good: " + str(exc)
        # 迟到工作不能覆盖新持有者的当前同步状态。
        if not owner or (lease_target == (remote,branch) and _owns_sync_lease(repo, remote, branch, owner)):
            _record_sync(repo, remote, branch, "conflict" if isinstance(exc, GitSyncConflict) else "error",
                         error=warning, fetch=True)
        record_sync_attempt(repo, remote, branch, target_sha, "error",
                            int((time.monotonic()-started)*1000), error=warning)
        return False, warning
    finally:
        if owner and lease_target:
            _release_sync_lease(repo, *lease_target,
                                success and _owns_sync_lease(repo,*lease_target,owner),
                                owner=owner, cooldown=ttl)
