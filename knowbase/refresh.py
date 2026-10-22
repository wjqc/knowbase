"""事件触发的短生命周期同步调度；复用 Git 内核及 sync_lease，不调用模型。"""

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import config, gitops

_CHILDREN = {}


def _target(repo, cfg):
    declared = cfg["git"].get("remote", {}) or {}
    remote = str(declared.get("name") or "origin")
    branch = str(declared.get("branch") or "")
    if not branch:
        location = Path(repo) / ".git"
        if location.is_file():
            line = location.read_text().strip()
            if not line.startswith("gitdir: "):
                return remote, ""
            location = (Path(repo) / line[8:]).resolve()
        head = (location / "HEAD").read_text().strip()
        branch = head[len("ref: refs/heads/"):] if head.startswith("ref: refs/heads/") else ""
    return remote, branch


def snapshot(repo, cfg, remote, branch):
    now = datetime.now(timezone.utc)
    result = {"basis": "local_index", "as_of": now.isoformat(timespec="seconds"),
              "state": "stale", "stale": True, "last_success_at": None, "retry_at": None}
    conn = None
    try:
        conn = gitops._lease_connection(repo)
        row = conn.execute("SELECT lease_owner,lease_until,last_success,next_check_at,target_hash FROM sync_lease "
                           "WHERE repo=? AND remote=? AND branch=?", (str(Path(repo).resolve()), remote, branch)).fetchone()
        if row:
            result.update(last_success_at=row[2], retry_at=row[3])
            if row[4] != gitops.sync_target_hash(repo,cfg["git"],remote,branch):
                result.update(state="target_changed",last_success_at=None)
                return result
            if row[2]:
                checked = datetime.fromisoformat(row[2])
                if checked.tzinfo is not None:
                    age = (now-checked).total_seconds()
                    result["stale"] = age < 0 or age > max(10, cfg["git"].get("pull_ttl_seconds",60))
            result["state"] = "fresh" if not result["stale"] else "stale"
            if row[0] and row[1] and row[1] > now.isoformat(timespec="seconds"):
                result["state"] = "refresh_pending"
        status = conn.execute("SELECT status,error FROM sync_state WHERE remote=?", (f"{remote}/{branch}",)).fetchone()
        if status and status[0] not in ("ok", "pending"):
            result["warning"] = "最近远端刷新未成功，当前使用本地已知版本"
        result["sync_status"] = status[0] if status else "unknown"
    except Exception:
        result.update(state="unknown", warning="同步新鲜度不可读取")
    finally:
        if conn:
            conn.close()
    return result


def _launch(repo, remote, branch, owner):
    env = os.environ.copy()
    env.update(KNOWBASE_CONFIG=str(config.CONFIG_PATH), KNOWBASE_REPO_PATH=str(Path(repo).resolve()))
    kwargs = {"start_new_session": True} if os.name != "nt" else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    process = subprocess.Popen([sys.executable,"-I","-m","knowbase","sync-once","--remote",remote,
                                "--branch",branch,"--lease-token",owner], env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
    _CHILDREN[owner] = process


def request(repo, cfg=None, *, launcher=None):
    cfg = cfg or config.load_config()
    for key, child in list(_CHILDREN.items()):
        if child.poll() is not None:
            del _CHILDREN[key]
    if not cfg["git"].get("auto_pull", True) or not gitops.is_repo(repo):
        return {"state":"local_only","basis":"local_index","stale":True,"last_success_at":None}
    try:
        remote, branch = _target(repo, cfg)
    except (OSError, ValueError):
        remote, branch = "origin", ""
    if not branch:
        return {"state":"unknown","basis":"local_index","stale":True,"warning":"无法确定同步分支"}
    try:
        fingerprint = gitops.sync_target_hash(repo,cfg["git"],remote,branch)
    except OSError:
        return {"state":"unknown","basis":"local_index","stale":True,"warning":"同步配置不可读取"}
    owner, warning = gitops.acquire_sync_lease(repo, remote, branch,target_hash=fingerprint)
    if owner:
        try:
            (launcher or _launch)(repo, remote, branch, owner)
        except Exception:
            gitops._release_sync_lease(repo, remote, branch, False, owner=owner,
                                      cooldown=max(10,cfg["git"].get("pull_ttl_seconds",60)))
            result = snapshot(repo,cfg,remote,branch)
            result.update(state="schedule_failed",warning="无法启动同步工作，当前使用本地已知版本")
            return result
    result = snapshot(repo, cfg, remote, branch)
    if warning and result["state"] == "unknown":
        result["warning"] = warning
    return result


def run_once(repo, remote, branch, owner):
    if not gitops._owns_sync_lease(repo,remote,branch,owner):
        return {"state":"lease_expired","updated":False}
    cfg = config.load_config()
    if not cfg["git"].get("auto_pull",True):
        gitops._release_sync_lease(repo,remote,branch,False,owner=owner)
        return {"state":"cancelled","updated":False}
    # 阶段检索的后台刷新只接收同事变更；提交后的推送仍由写入链路负责。
    cfg["git"]["auto_push"] = False
    changed, warning = gitops.sync_before_read(repo,cfg,lease_owner=owner,
                                              target_remote=remote,target_branch=branch)
    return {"state":"failed" if warning else "completed","updated":changed,"warning":warning}
