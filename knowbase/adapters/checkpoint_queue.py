"""Persist host checkpoint references; only the worker reads transcript content."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import index, jobs, materials
from . import host_events

# 有界尾持久化（统一生命周期 T3）：宿主（如 ZCode）可能在 Stop 返回后删除
# 临时 transcript，仅存路径引用会让后台采集永远失败。入队时同步读一次尾部
# （≤256KB、对齐完整行，毫秒级）随 spool 持久化；原件仍在时以原件为准。
TAIL_BYTES = 256 * 1024


def _read_tail(path: Path) -> str:
    """读取 transcript 尾部 ≤TAIL_BYTES；对齐到完整行（丢弃首个残行）。"""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > TAIL_BYTES:
                f.seek(size - TAIL_BYTES)
            data = f.read()
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace")
    if size > TAIL_BYTES and "\n" in text:
        text = text.split("\n", 1)[1]
    return text


def enqueue(repo, session_id, transcript, scope, host, *, project="", legacy=False, mid_session=False):
    if not repo.exists():
        return {"error": "REPOSITORY_MISSING"}
    transcript = str(Path(transcript).expanduser().absolute()) if transcript else ""
    project = str(Path(project).expanduser().absolute()) if project else ""
    tail = _read_tail(Path(transcript)) if transcript else ""
    conn = sqlite3.connect(index.db_path(repo).resolve().as_uri() + "?mode=rw", uri=True, timeout=.15)
    try:
        result = jobs.enqueue(conn, jobs.KIND_CHECKPOINT, scope, {
            "session_id": session_id, "transcript_path": transcript,
            "transcript_tail": tail,
            "host_profile": host, "project": project, "legacy": legacy, "mid_session": mid_session,
            "requested_at": datetime.now(timezone.utc).isoformat(),
        })
        if result.get("error"):
            return result
        return {"checkpoint_id": result["job_id"], "capture_status": "deferred"}
    finally:
        conn.close()


def run_due(repo, conn, cfg, guard):
    if not cfg["hooks"].get("enabled", True) or not cfg["inflight"]["capture_enabled"]:
        return {"skipped": "capture_disabled"}
    now = datetime.now(timezone.utc)
    rows = conn.execute(
        "SELECT job_id,scope,attempt FROM jobs WHERE kind=? AND state IN ('queued','retry_wait') "
        "AND next_run_at<=? ORDER BY created_at,job_id LIMIT 4",
        (jobs.KIND_CHECKPOINT, now.isoformat(timespec="seconds"))).fetchall()
    results = []
    for job_id, scope, attempt in rows:
        if not guard():
            break
        try:
            payload = materials.payload_for_job(conn, job_id)
            result = _capture(repo, conn, scope, payload, cfg)
        except (OSError, ValueError, TypeError) as exc:
            result = {"error": "MATERIAL_PARSE_FAILED", "failure_class": type(exc).__name__}
        error = result.get("error")
        if error == "BACKLOG_FULL":
            state, delay = jobs.STATE_RETRY_WAIT, 60
        elif error == "MATERIAL_PARSE_FAILED" and attempt < 2:
            state, delay, attempt = jobs.STATE_RETRY_WAIT, 30, attempt + 1
        elif error:
            state, delay = jobs.STATE_FAILED, 0
        else:
            state, delay = jobs.STATE_SUCCEEDED, 0
        conn.execute("UPDATE jobs SET state=?,attempt=?,next_run_at=?,result=?,error=?,updated_at=? WHERE job_id=?",
                     (state, attempt, (now + timedelta(seconds=delay)).isoformat(timespec="seconds"),
                      json.dumps(result, ensure_ascii=False), error, now.isoformat(timespec="seconds"), job_id))
        conn.commit()
        results.append(result)
    return {"processed": len(results), "results": results} if results else {"skipped": "no_due_checkpoints"}


def _capture(repo, conn, scope, payload, cfg):
    transcript, session, host = (payload[k] for k in ("transcript_path", "session_id", "host_profile"))
    flag = None
    tail_tmp = None
    try:
        if payload["legacy"]:
            from ..hooks import _infer_scope, _mid_capture_flag_path, _transcript_stats
            if payload.get("mid_session"):
                flag = _mid_capture_flag_path(session)
                if flag.exists() or not cfg["hooks"].get("mid_session_capture", False):
                    return {"capture_status": "no_delta", "job_ids": []}
            if not Path(transcript).is_file():
                # 宿主已删除临时 transcript：回退到入队时的有界尾持久化。
                tail = payload.get("transcript_tail") or ""
                if not tail:
                    return {"error": "MATERIAL_PARSE_FAILED"}
                import os
                import tempfile
                fd, tail_tmp = tempfile.mkstemp(prefix="knowbase-tail-", suffix=".jsonl")
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(tail)
                transcript = tail_tmp
            stats = _transcript_stats(transcript)
            threshold = cfg["hooks"].get("mid_session_threshold", 5) if flag else 1
            if stats["work_ops"] < threshold or stats["memory_write"]:
                return {"capture_status": "no_delta", "job_ids": []}
            project = payload["project"]
            scope = _infer_scope(conn, project) if project else "global"
            if not scope:
                return {"error": "SCOPE_UNRESOLVED"}
            conn.execute("INSERT OR IGNORE INTO context_sessions(host_profile,session_id,scope,updated_at) VALUES(?,?,?,?)",
                         (host, session, scope, datetime.now(timezone.utc).isoformat(timespec="seconds")))
            conn.commit()
        elif transcript and not Path(transcript).is_file():
            # Compact/rotation may remove the file; the observation path remains valid.
            transcript = ""
        result = host_events.capture_increment(repo, session, transcript, scope, host)
        if flag and result.get("job_ids") and not result.get("error"):
            flag.write_text("1", encoding="utf-8")
        return result
    finally:
        if tail_tmp:
            try:
                Path(tail_tmp).unlink(missing_ok=True)
            except OSError:
                pass
