"""Explicit local credential scrub. Reports contain counts, never matching content.

Backups deliberately retain the original database for recovery and must stay private.
No schema migration, card edit or Git history rewrite is performed here.
"""

import hashlib
import json
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import config, jobs, materials
from .credentials import transform
from .locking import RepoLock

_JSON_COLUMNS = {
    "context_events": ("observation_json", "result_json"),
    "context_sessions": ("topics_json",),
    "job_batches": ("similar_json",),
    "candidates": ("payload_json",),
    "job_submissions": ("payload_json", "result_json"),
    "jobs": ("result", "error"),
}


def _changes(conn):
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    updates, counts, affected = [], {}, set()
    for table, columns in _JSON_COLUMNS.items():
        if table not in tables:
            continue
        present = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
        for column in columns:
            if column not in present:
                continue
            for rowid, raw in conn.execute(f'SELECT rowid,"{column}" FROM "{table}"'):
                if not isinstance(raw, str):
                    continue
                clean = transform(raw)
                if clean != raw:
                    updates.append((table, column, rowid, clean))
                    label = f"{table}.{column}"
                    counts[label] = counts.get(label, 0) + 1
    if "job_spool" in tables:
        for rowid, job_id, raw, checksum in conn.execute("SELECT rowid,job_id,input_json,checksum FROM job_spool"):
            if checksum and hashlib.sha256(raw.encode()).hexdigest()[:16] != checksum:
                raise ValueError("MATERIAL_CHECKSUM_MISMATCH")
            payload = json.loads(raw)
            clean = materials.redact_payload(payload)
            if clean != payload:
                updates.append(("job_spool", "input_json", rowid, json.dumps(clean, ensure_ascii=False)))
                counts["job_spool.input_json"] = counts.get("job_spool.input_json", 0) + 1
                affected.add(job_id)
    return updates, counts, affected


def scrub(repo, *, apply=False, backup_dir=None):
    """Dry-run by default; apply under repository + SQLite write locks, after backup."""
    repo = Path(repo).expanduser().resolve()
    db = repo / "memory.db"
    if not db.is_file():
        return {"error": "DATABASE_MISSING"}
    if apply and not backup_dir:
        return {"error": "BACKUP_DIRECTORY_REQUIRED"}
    if apply:
        backup_dir = Path(backup_dir).expanduser().resolve()
        if backup_dir == repo or repo in backup_dir.parents:
            return {"error": "BACKUP_MUST_BE_OUTSIDE_REPOSITORY"}
    with RepoLock(repo):
        conn = sqlite3.connect(db.as_uri() + ("?mode=rw" if apply else "?mode=ro"), uri=True, timeout=10)
        try:
            if apply:
                conn.execute("PRAGMA secure_delete=ON")
                conn.execute("BEGIN IMMEDIATE")
            else:
                conn.execute("BEGIN")
            updates, counts, affected = _changes(conn)
            result = {"mode": "apply" if apply else "dry_run", "changed_fields": counts,
                      "affected_jobs": len(affected), "total_changed_fields": len(updates)}
            if not apply:
                conn.rollback()
                return result
            # A partially applying submission needs recovery, not silent evidence mutation.
            for job_id in affected:
                pending = conn.execute("SELECT 1 FROM job_submissions WHERE job_id=? AND state!='done' LIMIT 1",
                                       (job_id,)).fetchone()
                if pending:
                    raise ValueError("ACTIVE_MATERIALIZATION_REQUIRES_RECOVERY")
            backup_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
            os.chmod(backup_dir, 0o700)
            backup_path = backup_dir / "memory.db"
            backup_path.touch(mode=0o600)
            # The reserved write lock blocks writers; a separate reader can back up safely.
            with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as source:
                with sqlite3.connect(backup_path) as target:
                    source.backup(target)
            if config.CONFIG_PATH.is_file():
                shutil.copy2(config.CONFIG_PATH, backup_dir / "config.json")
                os.chmod(backup_dir / "config.json", 0o600)
            for table, column, rowid, clean in updates:
                conn.execute(f'UPDATE "{table}" SET "{column}"=? WHERE rowid=?', (clean, rowid))
                if table == "job_spool":
                    conn.execute("UPDATE job_spool SET checksum=? WHERE rowid=?",
                                 (hashlib.sha256(clean.encode()).hexdigest()[:16], rowid))
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            for job_id in affected:
                raw = conn.execute("SELECT input_json FROM job_spool WHERE job_id=? ORDER BY rowid DESC LIMIT 1", (job_id,)).fetchone()[0]
                payload = json.loads(raw)
                for segment in payload.get("segments", []):
                    conn.execute("UPDATE job_segments SET source_version=?,text_hash=?,original_length=?,"
                                 "start_offset=?,end_offset=? WHERE job_id=? AND segment_id=?",
                                 (payload.get("input_version", ""), segment["text_hash"], segment.get("original_length", len(segment["text"])),
                                  segment.get("start_offset", 0), segment.get("end_offset", len(segment["text"])), job_id, segment["segment_id"]))
                conn.execute("UPDATE job_batches SET state='redacted' WHERE job_id=?", (job_id,))
                conn.execute("UPDATE jobs SET input_hash=?,fencing_token=fencing_token+1,lease_owner=NULL,lease_until=NULL,"
                             "state=CASE WHEN state IN ('succeeded','no_candidate','needs_review','failed') THEN state ELSE 'awaiting_agent' END,"
                             "updated_at=? WHERE job_id=?", (jobs._input_hash(payload), now, job_id))
            remaining = _changes(conn)[1]
            if remaining or conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("SCRUB_VERIFICATION_FAILED")
            conn.commit()
            # Remove historical free-page content too; active readers may defer WAL cleanup.
            compacted = True
            try:
                conn.execute("VACUUM")
                checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            except sqlite3.Error:
                compacted = False
                checkpoint = (1,)
            result.update(backup_path=str(backup_path), remaining_changed_fields=remaining,
                          integrity="ok", compacted=compacted, wal_checkpoint_busy=checkpoint[0])
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
