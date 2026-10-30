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

# 宿主（Trae / IDE / CI）可能注入 GIT_AUTHOR_* / GIT_COMMITTER_*，这些
# 环境变量的优先级高于 user.name/user.email 配置，也高于命令行
# `git -c user.name=...`，会静默改写提交身份（企业网关按邮箱域校验时
# 直接拒绝推送）。只在子进程环境里剔除，父进程 os.environ 不动。
_IDENTITY_ENV_VARS = (
    "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
    "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL",
)


def _git_env() -> dict:
    """构造 git 子进程环境：剔除宿主注入的提交身份变量。"""
    env = os.environ.copy()
    for name in _IDENTITY_ENV_VARS:
        env.pop(name, None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    # rebase --continue 等流程不得打开编辑器（自动化下会永久挂起）
    env["GIT_EDITOR"] = "true"
    env["GIT_SEQUENCE_EDITOR"] = "true"
    return env


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
                           capture_output=True, timeout=5, creationflags=0x08000000)
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
    env = _git_env()
    kwargs: dict = {}
    if _IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | 0x08000000
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        # errors=replace：Windows 本地化 git 消息/凭据管理器输出可能非 UTF-8
        stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", env=env, **kwargs,
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


def with_base_trailers(message: str, bases: list[tuple[str, str]] | None) -> str:
    """B-(1)：写提交附带 base content_revision 元数据。

    bases 为 [(卡片 ID, 写前 content_revision)]，新建卡用 "none"。
    以 Git trailer 形式附在提交消息尾部，不进卡片正文、不改变修订号
    计算，Markdown 仍是唯一真相。
    """
    if not bases:
        return message
    seen: dict[str, str] = {}
    for cid, rev in bases:
        if cid and cid not in seen:
            seen[cid] = rev or "none"
    if not seen:
        return message
    trailers = "\n".join(f"Knowbase-Base: {cid}={rev}" for cid, rev in seen.items())
    return message.rstrip("\n") + "\n\n" + trailers + "\n"


# 知识卡所在目录（与 store.ALL_DIRS 保持一致）；只有这些目录下的 .md
# 才按卡片做同卡并发隔离，其他路径的冲突仍走旧的整单 abort。
_CARD_DIRS = frozenset(("standards", "pitfalls", "decisions", "workflows",
                        "preferences", "references", "bizrules", "staging"))
_ID_RE = None  # 延迟初始化，避免模块加载期多余导入


def _is_card_path(path: str) -> bool:
    """判断仓库相对路径是否为知识卡文件。"""
    import re
    global _ID_RE
    if _ID_RE is None:
        _ID_RE = re.compile(r"^[A-Z]{1,2}-\d{4}-\d{4}$|^[A-Z]{1,2}-[0-9a-f]{12}$")
    parts = Path(path).parts
    if len(parts) != 2 or parts[0] not in _CARD_DIRS or not path.endswith(".md"):
        return False
    return bool(_ID_RE.match(Path(path).stem))


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


def _raise_rebase_failure(repo: Path, original: Exception, conflicted: str, kind: str) -> None:
    """按旧口径把 rebase 失败分类报错（先 abort，再按原因抛错）。"""
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


def _rebase_onto(repo: Path, remote_ref: str, on_card_conflict=None) -> list[str]:
    """把本地提交重放到远端；失败必须 abort，不能留下半截 rebase。

    on_card_conflict 为空时行为与旧版完全一致：任何失败都 abort 并按
    原因分类报错。传入时：若全部未合并路径都是知识卡文件，则逐文件隔离
    ——回调负责把本地版（rebase 语义的 theirs）记入隔离区、把远端版
    （ours）检出到工作树并 git add——然后 rebase --continue；只要有
    一条非卡片路径冲突，仍走旧的整单 abort。返回本次隔离解决的卡片路径。
    """
    try:
        _run(repo, "rebase", remote_ref, timeout=15)
        return []
    except Exception as exc:
        original = exc
    resolved: list[str] = []
    for _ in range(50):
        try:
            conflicted = _run(repo, "diff", "--name-only", "--diff-filter=U", timeout=3)
        except Exception:
            conflicted = ""
        kind = _rebase_failure_kind(str(original), conflicted)
        paths = [line for line in conflicted.splitlines() if line.strip()]
        card_paths = [p for p in paths if _is_card_path(p)]
        if (on_card_conflict is None or kind != "conflict" or not paths
                or len(card_paths) != len(paths)):
            _raise_rebase_failure(repo, original, conflicted, kind)
        try:
            on_card_conflict(repo, card_paths)
        except Exception as exc:
            abort_failed = _abort_rebase(repo)
            raise GitSyncConflict(
                "冲突卡隔离失败，已取消 rebase 并保留本地提交"
                + ("；rebase abort 失败，请手动检查 .git 状态" if abort_failed else "")
            ) from exc
        resolved.extend(card_paths)
        try:
            _run(repo, "rebase", "--continue", timeout=15)
            return resolved
        except Exception as exc:
            if _in_rebase_state(repo, default=True):
                original = exc
                continue
            abort_failed = _abort_rebase(repo)
            raise GitSyncConflict(
                "rebase --continue 失败，已取消 rebase 并保留本地提交"
                + ("；rebase abort 失败，请手动检查 .git 状态" if abort_failed else "")
            ) from exc
    abort_failed = _abort_rebase(repo)
    raise GitSyncConflict(
        "rebase 冲突隔离轮次过多，已取消 rebase 并保留本地提交"
        + ("；rebase abort 失败，请手动检查 .git 状态" if abort_failed else ""))


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


# P3 §6.2：内网 Git 失败分层诊断。只返回脱敏类别，绝不把原始错误
# 文本（含可能的 URL/凭据/路径）写入同步记录或日志。
_DIAGNOSE_RULES = (
    ("dns", ("could not resolve host", "name resolution", "dns", "name or service not known")),
    ("tcp", ("connection refused", "connection timed out", "connection reset",
             "network is unreachable", "no route to host")),
    ("proxy", ("proxy", "connect tunnel", "proxy authentication")),
    ("auth", ("authentication", "permission denied", "could not read username",
              "invalid username or password", "access denied")),
    ("protocol", ("protocol error", "unexpected disconnect", "the remote end hung up",
                  "failed to connect to repository", "not a git repository")),
    ("git", ("conflict", "rebase", "index.lock", "identity unknown",
             "please tell me who you are", "unable to auto-detect email")),
)


def diagnose_git_failure(message: str) -> str:
    """把一次 Git 失败归入分层类别；无匹配时返回 unknown。"""
    text = (message or "").lower()
    for category, markers in _DIAGNOSE_RULES:
        if any(marker in text for marker in markers):
            return category
    return "unknown"


def confirm_push(repo: Path, remote: str, branch: str, target_sha: str) -> dict:
    """推送后读回远端目标 SHA：确认远端已包含目标提交才算 pending 可清。

    网络未知状态只返回 network_unknown，不重造提交、不谎称成功。
    返回 {confirmed, remote_rev, target_sha, reason}。
    """
    if not target_sha:
        return {"confirmed": False, "remote_rev": "", "target_sha": "",
                "reason": "no_target_sha"}
    try:
        _run(repo, "fetch", remote, branch, timeout=10)
        remote_rev = _run(repo, "rev-parse", f"{remote}/{branch}", timeout=3)
    except Exception as exc:
        return {"confirmed": False, "remote_rev": "", "target_sha": target_sha,
                "reason": "network_unknown",
                "diagnosis": diagnose_git_failure(str(exc))}
    confirmed = _is_ancestor(repo, target_sha, remote_rev)
    return {"confirmed": confirmed, "remote_rev": remote_rev,
            "target_sha": target_sha,
            "reason": "" if confirmed else "target_not_contained"}


def _integrate_remote(repo: Path, remote: str, branch: str, on_card_conflict=None) -> tuple[bool, bool]:
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
    _rebase_onto(repo, remote_ref, on_card_conflict=on_card_conflict)
    return True, True


def detect_divergent_cards(repo: Path, base_rev: str, local_rev: str,
                           remote_rev: str, skip_paths: tuple = ()) -> list[dict]:
    """B-(2)：合流后冲突检测——即使 git 静默 auto-merge 也要判定。

    两边相对 merge-base 都改了同一张卡、且两边 blob 不同，即判定为同卡
    并发写。两边改成相同内容不算冲突；任一侧删除不由本函数判定（那种
    情况 rebase 本就会停在 modify/delete 冲突上，走隔离回调）。
    返回 [{path, card_id, base_sha, local_sha, remote_sha}]。
    """
    if not base_rev or base_rev in (local_rev, remote_rev):
        return []
    try:
        local_changed = set(_run(repo, "diff", "--name-only", base_rev, local_rev,
                                 timeout=10).splitlines())
        remote_changed = set(_run(repo, "diff", "--name-only", base_rev, remote_rev,
                                  timeout=10).splitlines())
    except Exception:
        return []
    skip = set(skip_paths)
    found = []
    for path in sorted(local_changed & remote_changed):
        if path in skip or not _is_card_path(path):
            continue
        try:
            local_sha = _run(repo, "rev-parse", f"{local_rev}:{path}", timeout=3)
        except Exception:
            local_sha = ""
        try:
            remote_sha = _run(repo, "rev-parse", f"{remote_rev}:{path}", timeout=3)
        except Exception:
            remote_sha = ""
        if local_sha and remote_sha and local_sha != remote_sha:
            try:
                base_sha = _run(repo, "rev-parse", f"{base_rev}:{path}", timeout=3)
            except Exception:
                base_sha = ""
            found.append({"path": path, "card_id": Path(path).stem,
                          "base_sha": base_sha, "local_sha": local_sha,
                          "remote_sha": remote_sha})
    return found


def _quarantine_from_rebase(repo: Path, conn, path: str) -> dict:
    """rebase 冲突回调：本地版（theirs，第 3 阶段）进隔离区，远端版
    （ours，第 2 阶段）检出到工作树并 git add。返回冲突记录。"""
    from . import quarantine
    try:
        local_text = _run(repo, "show", f":3:{path}", timeout=3)
    except Exception as exc:
        raise RuntimeError(f"读取冲突本地版失败 {path}: {exc}") from exc
    try:
        remote_text = _run(repo, "show", f":2:{path}", timeout=3)
    except Exception as exc:
        raise RuntimeError(f"读取冲突远端版失败 {path}: {exc}") from exc
    card_id = Path(path).stem
    record = quarantine.record_conflict(repo, conn, card_id, base_rev="",
                                        local_text=local_text, remote_text=remote_text,
                                        source="rebase")
    _run(repo, "checkout", "--ours", "--", path, timeout=3)
    _run(repo, "add", "--", path, timeout=3)
    return record


def integrate_with_conflicts(repo: Path, remote: str, branch: str) -> tuple[bool, bool, list]:
    """B-(2)+(3)：合流 + 合流后冲突检测 + 按文件隔离 + 自动裁决。

    在 _integrate_remote 外包一层：合流前记下本地 HEAD、远端 HEAD 与
    merge-base；rebase 真冲突时回调隔离（远端进树、本地进隔离区，其余
    文件继续同步）；合流成功后对 auto-merge 的同卡改动做检测并隔离
    （本地/远端/合并产物三份原文都保留）；最后对全部冲突记录做证据
    强度自动裁决（打平保持 open，不选边）。
    返回 (HEAD 是否变化, 是否需要 push, 冲突记录列表)。
    调用方须持有 RepoLock（与原 _integrate_remote 的调用约定一致）。
    """
    from . import index, quarantine
    local_pre = remote_rev = base = ""
    try:
        local_pre = _run(repo, "rev-parse", "HEAD", timeout=3)
    except Exception:
        pass
    try:
        remote_rev = _run(repo, "rev-parse", f"{remote}/{branch}", timeout=3)
    except Exception:
        pass
    if local_pre and remote_rev and local_pre != remote_rev:
        try:
            base = _run(repo, "merge-base", local_pre, remote_rev, timeout=3)
        except Exception:
            base = ""
    diverged = bool(base and base != local_pre and base != remote_rev)
    records: list[dict] = []
    resolved_paths: list[str] = []

    def _on_card_conflict(r: Path, paths: list[str]) -> None:
        conn = index.connect(r)
        try:
            for p in paths:
                records.append(_quarantine_from_rebase(r, conn, p))
                resolved_paths.append(p)
        finally:
            conn.close()

    changed, push_needed = _integrate_remote(
        repo, remote, branch,
        on_card_conflict=_on_card_conflict if diverged else None)
    if not diverged:
        return changed, push_needed, records
    # 合流后检测：git 静默 auto-merge 的同卡改动（已隔离的路径跳过）。
    conn = index.connect(repo)
    try:
        for found in detect_divergent_cards(repo, base, local_pre, remote_rev,
                                            skip_paths=tuple(resolved_paths)):
            try:
                local_text = _run(repo, "show", f"{local_pre}:{found['path']}", timeout=3)
            except Exception:
                local_text = None
            try:
                remote_text = _run(repo, "show", f"{remote_rev}:{found['path']}", timeout=3)
            except Exception:
                remote_text = None
            merged_text = None
            try:
                merged_text = (Path(repo) / found["path"]).read_text(encoding="utf-8")
            except Exception:
                pass
            records.append(quarantine.record_conflict(
                repo, conn, found["card_id"], base_rev="",
                local_text=local_text, remote_text=remote_text,
                merged_text=merged_text, source="auto_merge"))
        # 自动裁决全部冲突记录；胜者版写回产生新提交时需要继续推送。
        for record in records:
            _status, _note, restored = quarantine.adjudicate(repo, conn, record)
            record["status"] = _status
            if restored:
                changed = True
                push_needed = True
    finally:
        conn.close()
    return changed, push_needed, records


def push(repo: Path, remote_url: str, allowed_prefixes: list[str]) -> str | None:
    remote = "origin"
    branch = "unknown"
    push_sha = ""  # 记录正在推送的 SHA，用于恢复核对
    try:
        cfg = app_config.load_config()
        gcfg = cfg.get("git", {})
        remote, branch, actual_url = _remote_target(repo, gcfg)
        if not allowed_prefixes:
            return f"已拒绝推送: 白名单为空，无允许的推送目标(remote {actual_url})"
        if not any(actual_url.startswith(p) for p in allowed_prefixes):
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
                integrate_with_conflicts(repo, remote, branch)
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
        # P3 §6.2：推送返回后读回远端目标 SHA，确认已包含目标提交才记 ok；
        # 网络未知状态只记 unconfirmed、不重造提交，下次同步重查。
        confirmation = confirm_push(repo, remote, branch, push_sha)
        if confirmation["confirmed"]:
            _record_sync(repo, remote, branch, "ok", push=True)
            return None
        reason = confirmation["reason"]
        diag = confirmation.get("diagnosis", reason)
        _record_sync(repo, remote, branch, "unconfirmed",
                     error=f"[{diag}] push_sha={push_sha} remote_rev={confirmation['remote_rev']}",
                     push=True)
        if reason == "network_unknown":
            return (f"push 已返回但远端读回未确认（网络未知状态，不重造提交，下次同步重查）"
                    f"：{diag} (push_sha={push_sha})")
        return (f"push 已返回但远端未包含目标提交（pending 保留，下次同步重查）"
                f"：push_sha={push_sha} remote_rev={confirmation['remote_rev']}")
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
        # .git/config 可能含中文（user.name/remote/路径）；中文 Windows 默认按
        # cp936 读会乱码甚至抛错，导致 sync 每次都降级为"远程同步检查失败"。
        git_dir = (Path(repo) / git_dir.read_text(encoding="utf-8", errors="replace").strip().removeprefix("gitdir: ")).resolve()
    common = git_dir
    if (git_dir / "commondir").is_file():
        common = (git_dir / (git_dir / "commondir").read_text(encoding="utf-8", errors="replace").strip()).resolve()
    data = {"remote": remote, "branch": branch, "declared":git_cfg.get("remote", {}),
            "allowed":git_cfg.get("allowed_remote_prefixes", []),
            "head":(git_dir/"HEAD").read_text(encoding="utf-8", errors="replace"),
            "git_config":(common/"config").read_text(encoding="utf-8", errors="replace")}
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
                changed, push_needed, _conflicts = integrate_with_conflicts(repo, remote, branch)
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
