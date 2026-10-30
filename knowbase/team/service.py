"""Enrollment, background cycle and local operational status."""
import json
import os
import platform
import sqlite3
import subprocess
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import config, gitops, index, telemetry
from ..fsutil import replace_with_retry
from ..locking import RepoLock
from ..value import identity
from . import collect, publish, report, state
from .contracts import canonical, digest, identifier, safe_label, stamp, validate_member
from .transport import GitData, TransportError, receive, send


def _machine_identifier():
    """Stable OS identity; never derive a sender from DHCP names/random MACs."""
    system = platform.system()
    try:
        if system == "Darwin":
            result = subprocess.run(["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                                    capture_output=True, text=True, timeout=5, check=True)
            match = re.search(r'"IOPlatformUUID"\s*=\s*"([A-Fa-f0-9-]+)"', result.stdout)
            value = match.group(1) if match else ""
        elif system == "Windows":
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography",
                                0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
                value = winreg.QueryValueEx(key, "MachineGuid")[0]
        else:
            value = Path("/etc/machine-id").read_text(encoding="utf-8").strip()
        if not isinstance(value, str) or not re.fullmatch(r"[A-Fa-f0-9-]{16,64}", value):
            raise ValueError("stable local machine identity unavailable")
        return value.lower()
    except (OSError, subprocess.SubprocessError):
        raise ValueError("stable local machine identity unavailable") from None


def device_fingerprint():
    # Only the digest stays local; the raw OS identity is never persisted/uploaded.
    return digest(["machine-v2", platform.system(), _machine_identifier()])


def commit_identity(cfg):
    """Resolve the committer identity for statistics and README commits.

    Corporate Git gateways (AIQGATE pre-receive rules) reject synthetic e-mail
    domains, so explicit team config wins, then this machine's own Git identity;
    the synthetic default stays only as a last resort for air-gapped setups.
    """
    team_cfg = cfg.get("team") or {}
    resolved = {"user.name": str(team_cfg.get("commit_name") or "").strip(),
                "user.email": str(team_cfg.get("commit_email") or "").strip()}
    for key, value in resolved.items():
        if value:
            continue
        repo=config.repo_path(cfg)
        args=['git','-C',str(repo),'config','--get',key] if gitops.is_repo(repo) else ['git','config','--get',key]
        code, out, _ = gitops._run_git(args, 5)
        resolved[key] = out.strip() if code == 0 else ""
    from .transport import GitData
    default_name, default_email = GitData.DEFAULT_IDENTITY
    return (resolved["user.name"] or default_name,
            resolved["user.email"] or default_email)


def rebind_device():
    """Explicit operator recovery; preserve sender, epoch, queued data and ACKs."""
    cfg = config.load_config(); root = root_path(cfg); repo = config.repo_path(cfg)
    with RepoLock(root), RepoLock(repo):
        conn = state.connect(root)
        try:
            e = state.get(conn, "enrollment")
            if not e:
                raise ValueError("team enrollment required")
            with sqlite3.connect(index.db_path(repo).resolve().as_uri() + "?mode=ro", uri=True) as db:
                if identity._get(db, "installation_id") != e["installation_id"]:
                    raise ValueError("local database identity mismatch; inspect before device rebind")
            fingerprint = device_fingerprint()
            if e.get("device_fingerprint") == fingerprint:
                return {"device_binding": "unchanged"}
            backup = root / "backups" / ("device-rebind-" + uuid.uuid4().hex)
            backup.mkdir(parents=True, mode=0o700)
            target = sqlite3.connect(backup / "state.db")
            try:
                conn.backup(target)
            finally:
                target.close()
            (backup / "state.db").chmod(0o600)
            state.put(conn, "device_rebind", {"at": stamp(), "previous": e.get("device_fingerprint"),
                                             "reason": "explicit_local_device_confirmation"})
            e["device_fingerprint"] = fingerprint
            state.put(conn, "enrollment", e); conn.commit()
            return {"device_binding": "rebound", "identity_preserved": True}
        finally:
            conn.close()


def root_path(cfg=None):
    cfg = cfg or config.load_config()
    root = Path(cfg["team"]["data_dir"]).expanduser().resolve()
    repo = config.repo_path(cfg).resolve()
    if root == repo or root.is_relative_to(repo) or repo.is_relative_to(root):
        raise ValueError("team data directory must be outside the knowledge checkout")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def _save_config(cfg):
    path = config.CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    # 固定临时名（.team-tmp）会被并发写互相覆盖；Windows 上目标被占用还需退避重试
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".team-tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(canonical(cfg))
        os.chmod(temp, 0o600)
        replace_with_retry(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def enroll(member_id, label, *, publisher=False, include_history=False, source_timezone="Asia/Shanghai"):
    identifier(member_id); safe_label(label); ZoneInfo(source_timezone)
    cfg = config.load_config(); repo = config.repo_path(cfg); root = root_path(cfg)
    git_cfg = cfg["git"]
    remote_name = git_cfg["remote"]["name"] or "origin"
    remote = git_cfg["remote"]["url"] or gitops._run(repo, "remote", "get-url", remote_name)
    branch = git_cfg["remote"]["branch"] or gitops._run(repo, "branch", "--show-current")
    team_id = "TEAM-" + digest([remote])[:24]
    with RepoLock(root):
        conn = state.connect(root)
        try:
            old = state.get(conn, "enrollment")
            if old:
                if old["member_id"] != member_id or old["team_id"] != team_id:
                    raise ValueError("computer already enrolled; cannot silently replace team/member identity")
                return status(conn)
            # Fail allowlist/path validation before touching identity.
            GitData(root / "stats", remote, git_cfg["allowed_remote_prefixes"])
            now = stamp()
            backup = root / "backups" / (datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8])
            backup.mkdir(parents=True, mode=0o700)
            if config.CONFIG_PATH.exists():
                (backup / "config.json").write_bytes(config.CONFIG_PATH.read_bytes())
                (backup / "config.json").chmod(0o600)
            with RepoLock(repo):
                source = sqlite3.connect(index.db_path(repo))
                dest = sqlite3.connect(backup / "memory.db")
                try:
                    source.backup(dest)
                finally:
                    dest.close(); source.close()
                db = index.connect(repo)
                try:
                    previous_id = identity.install(db)
                    install_id = "INST-" + uuid.uuid4().hex[:24]
                    identity._put(db, "installation_id", install_id)
                    identity.set_member(db, label)
                    db.commit()
                finally:
                    db.close()
            telemetry._INSTALLATION_CACHE.clear()
            enrollment = {"team_id": team_id, "member_id": member_id, "installation_id": install_id,
                          "label": label, "remote": remote, "default_branch": branch,
                          "source_timezone": source_timezone, "enrolled_at": now,
                          "include_history": include_history, "historical_ids": [previous_id],
                          "publisher": publisher, "device_fingerprint": device_fingerprint()}
            state.put(conn, "enrollment", enrollment)
            state.put(conn, "epoch", "E-" + uuid.uuid4().hex)
            member = {"schema": "knowbase.team-member.v1", "team_id": team_id, "member_id": member_id,
                      "label": label, "status": "active", "joined_at": now, "retired_at": None,
                      "sequence": 1, "confirmation": "self_confirmed"}
            state.queue(conn, f"members/{member_id}.json", validate_member(member))
            conn.commit()
            cfg["team"]["enabled"] = True
            _save_config(cfg)
            return status(conn)
        finally:
            conn.close()


def status(conn):
    e = state.get(conn, "enrollment", {})
    return {"enrolled": bool(e), "member_id": e.get("member_id"), "installation_id": e.get("installation_id"),
            "publisher": e.get("publisher", False),
            "pending_files": conn.execute("SELECT COUNT(*) FROM outgoing WHERE hash<>ack").fetchone()[0],
            "accepted_files": conn.execute("SELECT COUNT(*) FROM accepted").fetchone()[0],
            "last_collection": state.get(conn, "last_collection"), "last_sync": state.get(conn, "last_sync"),
            "last_readme": state.get(conn, "last_readme")}


def run(*, network=True):
    cfg = config.load_config(); root = root_path(cfg); repo = config.repo_path(cfg)
    if not cfg["team"]["enabled"]:
        raise ValueError("team statistics disabled; enroll first")
    with RepoLock(root, timeout=1):
        conn = state.connect(root)
        errors = []
        try:
            e = state.get(conn, "enrollment")
            if not e:
                raise ValueError("team enrollment required")
            if e.get("device_fingerprint") != device_fingerprint():
                raise ValueError("team data belongs to another computer; do not copy team state between devices")
            # Foreground hooks never execute this path. Collection and queue commit
            # finish before network, so failure cannot discard pending summaries.
            usage = []
            if cfg["dashboard"].get("host_usage_source") == "claude":
                from ..value.host_usage.claude import extract_records
                usage = list(extract_records())
            try:
                collect.collect(repo, conn, usage_records=usage)
                conn.commit()
            except (ValueError, sqlite3.Error):
                conn.rollback()
                errors.append("本库身份或日志异常，未推进采集；检查来源后运行 team recover")
                state.put(conn, "collection_error", {"at": stamp(), "error": errors[-1]}); conn.commit()
            if network:
                git = GitData(root / "stats", e["remote"], cfg["git"]["allowed_remote_prefixes"],
                              identity=commit_identity(cfg))
                with RepoLock(root / "stats", timeout=1):
                    try:
                        send(git, conn, e["team_id"])
                    except (TransportError, ValueError):
                        errors.append("统计上传失败，待发送数据保留")
                    try:
                        receive(git, conn, e["team_id"])
                    except (TransportError, ValueError):
                        errors.append("统计拉取失败，使用最后已接收快照")
                state.put(conn, "last_sync", {"at": stamp(), "ok": not errors, "errors": errors})
                conn.commit()
            inv = state.get(conn, "inventory")
            if network:
                try:
                    readme_git = GitData(root / "readme", e["remote"], cfg["git"]["allowed_remote_prefixes"],
                                         identity=commit_identity(cfg))
                    with RepoLock(root / "readme", timeout=1):
                        rev = readme_git.fetch(e["default_branch"])
                        if not rev:
                            raise TransportError("shared branch missing")
                        inv = publish.inventory(readme_git, rev)
                        state.put(conn, "inventory", inv)
                        if e["publisher"]:
                            # Keep roster/README at a bounded heartbeat cadence; don't
                            # create a knowledge commit every idle 15-minute tick.
                            snap = report.build(conn, inventory=inv)
                            key = digest({k: v for k, v in snap.items() if k not in {"generated_at", "inventory_revision"}})
                            previous = state.get(conn, "last_readme", {})
                            if previous.get("key") != key or not previous.get("ok"):
                                published = publish.publish(readme_git, e["default_branch"], snap)
                                state.put(conn, "last_readme", {"at": stamp(), "ok": True, "revision": published, "key": key})
                except TransportError as exc:
                    errors.append(str(exc))
                    state.put(conn, "last_readme", {"at": stamp(), "ok": False, "error": str(exc)})
            snapshot = report.build(conn, inventory=inv)
            last_sync = state.get(conn, "last_sync", {})
            snapshot["sync_errors"] = errors or (last_sync.get("errors", []) if not last_sync.get("ok") else [])
            snapshot["pending_files"] = status(conn)["pending_files"]
            snapshot["last_sync"] = last_sync
            conn.commit()
            fd, temp = tempfile.mkstemp(dir=root, prefix="snapshot.", suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(canonical(snapshot))
                replace_with_retry(temp, root / "snapshot.json")
            finally:
                Path(temp).unlink(missing_ok=True)
            from .. import dashboard
            dashboard.generate(repo, Path(cfg["team"]["dashboard_output"]).expanduser(), team_snapshot=snapshot)
            return {**status(conn), "coverage": snapshot["coverage"], "errors": errors}
        finally:
            conn.close()


def member(member_id, label, status_name):
    identifier(member_id); safe_label(label)
    cfg = config.load_config(); root = root_path(cfg)
    with RepoLock(root):
        conn = state.connect(root)
        try:
            e = state.get(conn, "enrollment")
            if not e or (member_id != e["member_id"] and not e["publisher"]):
                raise ValueError("only the local member or designated publisher can change registry")
            if status_name not in {"pending", "retired", "active"}:
                raise ValueError("invalid member status")
            if status_name == "active" and member_id != e["member_id"]:
                raise ValueError("active enrollment requires that member's self-confirmation")
            path = f"members/{member_id}.json"
            candidates = [json.loads(row[0]) for table in ("outgoing", "accepted")
                          for row in conn.execute(f"SELECT body FROM {table} WHERE path=?", (path,))]
            old = max(candidates, key=lambda v: v["sequence"]) if candidates else None
            if old and old["status"] == "retired" and status_name != "retired":
                raise ValueError("retired membership cannot be reactivated without a membership-history migration")
            v = {"schema": "knowbase.team-member.v1", "team_id": e["team_id"], "member_id": member_id,
                 "label": label, "status": status_name, "joined_at": old["joined_at"] if old else stamp(),
                 "retired_at": stamp() if status_name == "retired" else None,
                 "sequence": old["sequence"] + 1 if old else 1,
                 "confirmation": "self_confirmed" if status_name == "active" else "publisher_confirmed" if status_name == "retired" else "pending"}
            state.queue(conn, path, validate_member(v)); conn.commit()
            return {"member_id": member_id, "status": status_name, "pending": True}
        finally:
            conn.close()


def recover():
    """Explicit recovery never silently merges restored and original evidence."""
    cfg = config.load_config(); root = root_path(cfg); repo = config.repo_path(cfg)
    with RepoLock(root), RepoLock(repo):
        conn = state.connect(root)
        try:
            e = state.get(conn, "enrollment")
            if not e:
                raise ValueError("team enrollment required")
            backup = root / "backups" / ("recovery-" + uuid.uuid4().hex)
            backup.mkdir(parents=True, mode=0o700)
            db = sqlite3.connect(index.db_path(repo)); target = sqlite3.connect(backup / "memory.db")
            try:
                db.backup(target)
                identity._put(db, "installation_id", e["installation_id"]); db.commit()
            finally:
                target.close(); db.close()
            telemetry._INSTALLATION_CACHE.clear()
            state.put(conn, "epoch", "E-" + uuid.uuid4().hex)
            days = {r[0] for r in conn.execute("SELECT day FROM evidence")}
            state.put(conn, "day_epochs", {day: state.get(conn, "epoch") for day in days})
            state.put(conn, "recovery_days", sorted(days | set(state.get(conn, "recovery_days", []))))
            conn.commit()
            return {"recovery": "new epoch; overlapping days remain recovery_required", "days": len(days)}
        finally:
            conn.close()
