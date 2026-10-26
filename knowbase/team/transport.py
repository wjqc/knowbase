"""Bounded normal Git pushes in isolated bare data repositories.

Build a commit on the latest remote tree; on non-fast-forward fetch and reapply
only owned paths. No checkout/rebase/reset or mutation of the knowledge repo.
"""
import json
import os
import re
import tempfile
from pathlib import Path

from ..gitops import _run_git
from .contracts import MAX_BYTES, canonical, digest, validate_member, validate_summary


class TransportError(RuntimeError):
    pass


class GitData:
    # Corporate gateways (AIQGATE-style pre-receive rules) may require a real
    # committer domain; callers resolve one and None keeps the synthetic default.
    DEFAULT_IDENTITY = ("knowbase statistics", "statistics@knowbase.local")

    def __init__(self, root, remote, prefixes, *, identity=None):
        self.root = Path(root).resolve()
        # File remotes require an explicit, narrowly scoped file:// prefix. Defaults
        # only allow the internal team server; no implicit local/public exception.
        if not remote or not any(remote.startswith(p) for p in prefixes if p):
            raise TransportError("team remote is outside the configured allowlist")
        if "\n" in remote or "\r" in remote or remote.startswith("-"):
            raise TransportError("invalid remote")
        self.remote = remote
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.repo = self.root / "data.git"
        marker = self.root / "knowbase-team-data"
        expected = digest([remote])
        if marker.exists() and marker.read_text() != expected:
            raise TransportError("data workspace belongs to another remote")
        if self.repo.exists() and not marker.exists():
            raise TransportError("refusing an unmanaged Git directory")
        if not self.repo.exists():
            self._raw(["git", "init", "--bare", str(self.repo)])
            marker.write_text(expected)
        self.identity = tuple(identity) if identity else None

    def _raw(self, args, *, missing=False):
        try:
            code, out, _ = _run_git(args, 20)
        except Exception:
            # Git diagnostics can contain credential URLs/paths. Do not persist them.
            raise TransportError("Git network operation timed out or failed") from None
        if code == 2 and missing:
            return None
        if code:
            raise TransportError("Git operation failed; pending statistics retained")
        return out.strip()

    def run(self, *args):
        name, email = self.identity or self.DEFAULT_IDENTITY
        return self._raw(["git", "--git-dir", str(self.repo), "-c", "core.hooksPath=/dev/null",
                          "-c", f"user.name={name}", "-c", f"user.email={email}", *args])

    def fetch(self, branch):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]{0,120}", branch) or ".." in branch:
            raise TransportError("invalid statistics branch")
        out = self._raw(["git", "--git-dir", str(self.repo), "ls-remote", "--exit-code", "--heads", self.remote, f"refs/heads/{branch}"], missing=True)
        if out is None:
            return None
        if not out.strip():
            raise TransportError("Git remote unavailable; pending statistics retained")
        self.run("fetch", "--no-tags", "--depth=1", self.remote, f"+refs/heads/{branch}:refs/remotes/team/{branch}")
        return self.run("rev-parse", f"refs/remotes/team/{branch}")

    def files(self, revision):
        if revision is None:
            return []
        names = self.run("ls-tree", "-rz", "--name-only", revision).split("\0")
        names = [n for n in names if n]
        if len(names) > 50000:
            raise TransportError("statistics tree exceeds file limit; retention migration required")
        return names

    def read(self, revision, path):
        if not revision:
            return None
        # ls-tree is exact only after comparing its path, avoiding pathspec expansion.
        line = self.run("ls-tree", revision, "--", path)
        if not line:
            return None
        mode_type, actual = line.split("\t", 1)
        if actual != path or not mode_type.startswith("100644 blob "):
            raise TransportError("unsupported statistics Git entry")
        oid = mode_type.split()[2]
        size = int(self.run("cat-file", "-s", oid))
        if size > MAX_BYTES:
            raise TransportError("Git summary file exceeds size limit")
        # _run_git preserves trailing text; do not strip README bytes.
        code, out, _ = _run_git(["git", "--git-dir", str(self.repo), "cat-file", "blob", oid], 20)
        if code:
            raise TransportError("cannot read Git summary")
        return out

    def commit(self, parent, writes):
        self.run("read-tree", parent if parent else "--empty")
        for path, body in sorted(writes.items()):
            if path.startswith("/") or ".." in Path(path).parts:
                raise TransportError("invalid owned statistics path")
            with tempfile.NamedTemporaryFile(dir=self.root, delete=False) as f:
                f.write(body.encode()); name = f.name
            try:
                blob = self.run("hash-object", "-w", "--", name)
            finally:
                os.unlink(name)
            self.run("update-index", "--add", "--cacheinfo", "100644", blob, path)
        tree = self.run("write-tree")
        return self.run("commit-tree", tree, *(["-p", parent] if parent else []), "-m", "Update team statistics")

    def push(self, branch, commit):
        self.run("push", self.remote, f"{commit}:refs/heads/{branch}")


def send(git, conn, team_id, *, branch="knowbase-stats", retries=3):
    """ACK only content independently read back from the remote ref."""
    from . import state
    pending = {r["path"]: json.loads(r["body"]) for r in conn.execute("SELECT * FROM outgoing WHERE hash<>ack")}
    enrollment = state.get(conn, "enrollment")
    for path, v in pending.items():
        if path.startswith("members/"):
            validate_member(v)
            if path != f"members/{v['member_id']}.json" or (enrollment and v["member_id"] != enrollment["member_id"] and not enrollment.get("publisher")):
                raise TransportError("member write is outside this sender's ownership")
        else:
            from .contracts import summary_path
            if path != summary_path(v) or (enrollment and (v["installation_id"] != enrollment["installation_id"] or v["member_id"] != enrollment["member_id"])):
                raise TransportError("summary write is outside this sender's ownership")
    for _ in range(retries):
        revision = git.fetch(branch)
        writes = {}
        for path, v in pending.items():
            validate_member(v) if path.startswith("members/") else validate_summary(v)
            raw = git.read(revision, path)
            if raw is not None:
                other = json.loads(raw)
                accepted = conn.execute("SELECT body FROM accepted WHERE path=?", (path,)).fetchone()
                if accepted and json.loads(accepted[0])["sequence"] > other["sequence"]:
                    raise TransportError("remote ref regressed behind a confirmed sequence")
                state.accept(conn, path, other, team_id)
                if digest(other) == digest(v):
                    continue
                if other["sequence"] >= v["sequence"]:
                    if path.startswith("members/") and v["sequence"] == 1 and v["status"] == "active" and other["status"] == "pending":
                        v = {**v, "joined_at": other["joined_at"], "sequence": other["sequence"] + 1}
                        state.queue(conn, path, v)
                        pending[path] = v
                        writes[path] = canonical(v).decode()
                        continue
                    # A second installation may enroll an already active member;
                    # its initial identical registration need not rewrite that member.
                    if path.startswith("members/") and v["sequence"] == 1 and other["label"] == v["label"] and other["status"] == v["status"] == "active":
                        state.queue(conn, path, other)
                        pending[path] = other
                        continue
                    raise TransportError("remote sequence conflicts with this device; pending retained")
            writes[path] = canonical(v).decode()
        if writes:
            commit = git.commit(revision, writes)
            try:
                git.push(branch, commit)
            except TransportError:
                continue
        confirmed = git.fetch(branch)
        all_received = True
        for path, v in pending.items():
            remote = git.read(confirmed, path)
            if remote is None or digest(json.loads(remote)) != digest(v):
                all_received = False
                continue
            conn.execute("UPDATE outgoing SET ack=? WHERE path=? AND hash=?", (digest(v), path, digest(v)))
        if all_received:
            return confirmed
    raise TransportError("Git retry limit reached; pending statistics retained")


def receive(git, conn, team_id, *, branch="knowbase-stats"):
    from . import state
    revision = git.fetch(branch)
    rejected = 0
    total = 0
    for path in git.files(revision):
        if not (re.fullmatch(r"members/[A-Za-z0-9_.-]+\.json", path) or
                re.fullmatch(r"devices/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/\d{4}-\d{2}-\d{2}\.json", path)):
            rejected += 1
            continue
        try:
            raw = git.read(revision, path)
            total += len(raw.encode())
            if total > 128 * 1024 * 1024:
                raise TransportError("statistics import exceeds size limit")
            value = json.loads(raw)
            state.accept(conn, path, value, team_id)
        except (ValueError, KeyError, TypeError, TransportError):
            rejected += 1
    state.put(conn, "remote_revision", revision)
    state.put(conn, "rejected_files", rejected)
    return revision
