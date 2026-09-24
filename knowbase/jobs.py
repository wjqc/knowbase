"""持久任务队列：入队 → 领取 → 续租 → 提交 → 完成。

设计要点：
- 所有输入先落 spool 再入队，崩溃后可恢复；
- claim 原子 CAS，带 lease_token + fencing_token + 到期时间；
- 同一 idempotency_key 不重复创建；
- 宿主退出或租约到期自动回到可领取状态；
- 旧宿主迟到提交返回 LEASE_EXPIRED，不覆盖接管结果。
"""

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import migrations

# ---------- 常量 ----------

KIND_CAPTURE = "capture"       # 对话沉淀
KIND_EXTRACT = "extract"       # 文档导入提炼

STATE_QUEUED = "queued"
STATE_PREPARING = "preparing"
STATE_AWAITING = "awaiting_agent"
STATE_CLAIMED = "claimed"
STATE_VALIDATING = "validating"
STATE_SUCCEEDED = "succeeded"
STATE_NO_CANDIDATE = "no_candidate"
STATE_NEEDS_REVIEW = "needs_review"
STATE_RETRY_WAIT = "retry_wait"
STATE_FAILED = "failed"
STATE_BLOCKED = "blocked"

TERMINAL_STATES = {STATE_SUCCEEDED, STATE_NO_CANDIDATE, STATE_NEEDS_REVIEW, STATE_FAILED}
ACTIVE_STATES = {STATE_QUEUED, STATE_PREPARING, STATE_AWAITING, STATE_CLAIMED,
                 STATE_VALIDATING, STATE_RETRY_WAIT, STATE_BLOCKED}

# 错误码
LEASE_EXPIRED = "LEASE_EXPIRED"
BATCH_INCOMPLETE = "BATCH_INCOMPLETE"
IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
NO_WORK = "no_work"

# 默认参数
DEFAULT_LEASE_SECONDS = 300       # 5 分钟
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_MAX_BATCH_CHARS = 8000


# ---------- 连接 ----------

def connect(repo: Path):
    """获取已迁移的数据库连接。"""
    from . import index
    conn = index.connect(repo)
    migrations.apply_all(conn)
    return conn


# ---------- 入队 ----------

def _idempotency_key(kind: str, scope: str, input_hash: str) -> str:
    return f"{kind}:{scope}:{input_hash}"


def _input_hash(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def enqueue(conn, kind: str, scope: str, payload: dict, *,
            idempotency_key: str = "", max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> dict:
    """入队一个任务。同 key 已存在则返回既有 job（幂等）。

    返回 {"job_id", "state", "created"} — created=False 表示幂等命中。
    """
    if not idempotency_key:
        idempotency_key = _idempotency_key(kind, scope, _input_hash(payload))

    # 幂等检查
    existing = conn.execute(
        "SELECT job_id, state, result, error FROM jobs WHERE idempotency_key=?",
        (idempotency_key,),
    ).fetchone()
    if existing:
        return {"job_id": existing[0], "state": existing[1], "created": False,
                "result": existing[2], "error": existing[3]}

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    job_id = f"J-{uuid.uuid4().hex[:12]}"
    ih = _input_hash(payload)

    # spool 持久化
    spool_id = f"SP-{uuid.uuid4().hex[:12]}"
    spool_json = json.dumps(payload, ensure_ascii=False)
    spool_cs = hashlib.sha256(spool_json.encode("utf-8")).hexdigest()[:16]
    conn.execute(
        "INSERT INTO job_spool(spool_id,job_id,input_json,checksum,created_at) VALUES(?,?,?,?,?)",
        (spool_id, job_id, spool_json, spool_cs, now),
    )

    conn.execute(
        "INSERT INTO jobs(job_id,kind,scope,input_hash,idempotency_key,state,attempt,"
        "max_attempts,next_run_at,fencing_token,batch_cursor,batch_total,"
        "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (job_id, kind, scope, ih, idempotency_key, STATE_QUEUED, 0,
         max_attempts, now, 0, 0, 0, now, now),
    )
    conn.commit()
    return {"job_id": job_id, "state": STATE_QUEUED, "created": True}


# ---------- 状态转换 ----------

def _transition(conn, job_id: str, from_state: str, to_state: str) -> bool:
    """CAS 状态转换：仅当当前状态为 from_state 时才转为 to_state。"""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cur = conn.execute(
        "SELECT state FROM jobs WHERE job_id=?", (job_id,)
    ).fetchone()
    if not cur or cur[0] != from_state:
        return False
    conn.execute(
        "UPDATE jobs SET state=?, updated_at=? WHERE job_id=?",
        (to_state, now, job_id),
    )
    conn.commit()
    return True


# ---------- 预处理 ----------

def set_preparing(conn, job_id: str) -> bool:
    """queued → preparing（开始解析/分段）。"""
    return _transition(conn, job_id, STATE_QUEUED, STATE_PREPARING)


def set_awaiting(conn, job_id: str, batch_total: int) -> bool:
    """preparing → awaiting_agent（材料就绪，等待宿主领取）。"""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cur = conn.execute("SELECT state FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    if not cur or cur[0] != STATE_PREPARING:
        return False
    conn.execute(
        "UPDATE jobs SET state=?, batch_total=?, next_run_at=?, updated_at=? WHERE job_id=?",
        (STATE_AWAITING, batch_total, now, now, job_id),
    )
    conn.commit()
    return True


# ---------- 领取 ----------

def claim(conn, job_id: str, host_session_id: str, *,
          lease_seconds: int = DEFAULT_LEASE_SECONDS) -> dict:
    """原子领取：CAS 设置 lease，返回批次材料和令牌。

    返回 {"batch_id", "materials", "lease_token", "expires_at", "fencing_token", "cursor"}
    或 {"error": NO_WORK / LEASE_EXPIRED}。
    """
    now = datetime.now(timezone.utc)
    now_str = now.isoformat(timespec="seconds")
    expires = (now + timedelta(seconds=lease_seconds)).isoformat(timespec="seconds")

    row = conn.execute(
        "SELECT state, lease_owner, lease_until, fencing_token, batch_cursor, "
        "batch_total, attempt FROM jobs WHERE job_id=?",
        (job_id,),
    ).fetchone()
    if not row:
        return {"error": "not_found"}

    state, lease_owner, lease_until, fencing, cursor, total, attempt = row

    if state == STATE_AWAITING or (state == STATE_CLAIMED and lease_until and lease_until < now_str):
        # 可领取（首次或租约过期）
        new_fencing = fencing + 1
        new_attempt = attempt + 1 if state == STATE_AWAITING else attempt
        lease_token = uuid.uuid4().hex
        batch_id = f"B-{uuid.uuid4().hex[:8]}"

        conn.execute(
            "UPDATE jobs SET state=?, lease_owner=?, lease_until=?, fencing_token=?,"
            " attempt=?, updated_at=? WHERE job_id=?",
            (STATE_CLAIMED, f"{host_session_id}:{lease_token}", expires,
             new_fencing, new_attempt, now_str, job_id),
        )
        conn.commit()

        # 加载 spool 材料
        spool = conn.execute(
            "SELECT input_json FROM job_spool WHERE job_id=? ORDER BY created_at DESC LIMIT 1",
            (job_id,),
        ).fetchone()
        materials = json.loads(spool[0]) if spool else {}

        return {
            "batch_id": batch_id,
            "materials": materials,
            "lease_token": lease_token,
            "expires_at": expires,
            "fencing_token": new_fencing,
            "cursor": cursor,
            "batch_total": total,
        }

    if state in TERMINAL_STATES:
        return {"error": "already_done", "state": state}

    if state == STATE_CLAIMED:
        return {"error": LEASE_EXPIRED, "held_by": lease_owner}

    return {"error": NO_WORK, "state": state}


# ---------- 续租 ----------

def renew(conn, job_id: str, lease_token: str, *,
          lease_seconds: int = DEFAULT_LEASE_SECONDS) -> dict:
    """续租：验证 lease_token 后延长到期时间。"""
    now = datetime.now(timezone.utc)
    now_str = now.isoformat(timespec="seconds")
    expires = (now + timedelta(seconds=lease_seconds)).isoformat(timespec="seconds")

    row = conn.execute(
        "SELECT state, lease_owner, lease_until FROM jobs WHERE job_id=?",
        (job_id,),
    ).fetchone()
    if not row:
        return {"error": "not_found"}

    state, owner, until = row
    expected_owner = f"{lease_token}"
    # lease_owner 格式为 "session:token"，只需验证 token 部分
    if state != STATE_CLAIMED or not owner or not owner.endswith(f":{lease_token}"):
        return {"error": LEASE_EXPIRED}

    conn.execute(
        "UPDATE jobs SET lease_until=?, updated_at=? WHERE job_id=?",
        (expires, now_str, job_id),
    )
    conn.commit()
    return {"expires_at": expires, "lease_token": lease_token}


# ---------- 提交 ----------

def submit(conn, job_id: str, batch_id: str, lease_token: str, request_id: str,
           candidates: list[dict], segment_results: list[dict] | None = None) -> dict:
    """宿主提交提炼结果。

    candidates: [{"candidate_key", "body_json", "evidence_json", "scope", "decision"}]
    segment_results: [{"segment_id", "decision", "reason"}]
      - decision: "distilled" | "skipped" | "no_value"
      - reason: 跳过或无价值的原因（可选）

    返回 {"accepted": N, "needs_review": N, "next_cursor", "state"}。
    """
    now = datetime.now(timezone.utc)
    now_str = now.isoformat(timespec="seconds")

    row = conn.execute(
        "SELECT state, lease_owner, fencing_token, batch_cursor, batch_total "
        "FROM jobs WHERE job_id=?",
        (job_id,),
    ).fetchone()
    if not row:
        return {"error": "not_found"}

    state, owner, fencing, cursor, total = row

    # 验证租约
    if state != STATE_CLAIMED or not owner or not owner.endswith(f":{lease_token}"):
        return {"error": LEASE_EXPIRED}

    # 幂等：同 request_id 已提交
    existing = conn.execute(
        "SELECT candidate_id FROM candidates WHERE request_id=?", (request_id,)
    ).fetchone()
    if existing:
        return {"error": "duplicate_request", "candidate_id": existing[0]}

    accepted = 0
    needs_review = 0
    candidate_map = {}  # segment_id -> candidate_id

    for c in candidates:
        cid = f"C-{uuid.uuid4().hex[:12]}"
        ckey = c.get("candidate_key", cid)
        body = json.dumps(c.get("body", {}), ensure_ascii=False)
        evidence = json.dumps(c.get("evidence", []), ensure_ascii=False)
        decision = c.get("decision", "pending")
        scope = c.get("scope", "")
        segment_id = c.get("segment_id", "")

        if decision == "needs_review":
            needs_review += 1
        else:
            accepted += 1

        conn.execute(
            "INSERT INTO candidates(candidate_id,job_id,batch_id,candidate_key,scope,"
            "body_json,evidence_json,decision,version,request_id,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, job_id, batch_id, ckey, scope, body, evidence, decision, 1,
             request_id, now_str, now_str),
        )

        # 记录 segment -> candidate 映射
        if segment_id:
            candidate_map[segment_id] = cid

    # M3-3: 持久化 segment_results
    segment_results = segment_results or []
    processed_segments = 0
    for sr in segment_results:
        seg_id = sr.get("segment_id", "")
        if not seg_id:
            continue
        decision = sr.get("decision", "pending")
        reason = sr.get("reason", "")
        cid = candidate_map.get(seg_id, "")

        conn.execute(
            "INSERT OR REPLACE INTO segment_results(segment_id,job_id,batch_id,decision,"
            "reason,candidate_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (seg_id, job_id, batch_id, decision, reason, cid, now_str, now_str),
        )
        # 只有 decision 不是 pending 的 segment 才算已处理
        if decision != "pending":
            processed_segments += 1

    # 更新游标：如果有 segment_results，按已处理的 segment 数量推进；否则按 candidates 数量推进
    if segment_results:
        new_cursor = cursor + processed_segments
    else:
        new_cursor = cursor + len(candidates)
    new_state = STATE_VALIDATING

    # 判断是否所有片段都已处理
    if total > 0 and new_cursor >= total:
        # 全部完成
        if needs_review > 0 and accepted == 0:
            new_state = STATE_NEEDS_REVIEW
        else:
            new_state = STATE_SUCCEEDED
    else:
        # 还有后续批次 → 回到 awaiting
        new_state = STATE_AWAITING

    conn.execute(
        "UPDATE jobs SET state=?, batch_cursor=?, lease_owner=NULL, lease_until=NULL,"
        " updated_at=? WHERE job_id=?",
        (new_state, new_cursor, now_str, job_id),
    )
    conn.commit()

    return {
        "accepted": accepted,
        "needs_review": needs_review,
        "next_cursor": new_cursor,
        "state": new_state,
    }


# ---------- 提交无候选 ----------

def submit_no_candidate(conn, job_id: str, lease_token: str, request_id: str,
                        reason: str = "") -> dict:
    """宿主判断无需提炼，提交空结果。"""
    now_str = datetime.now(timezone.utc).isoformat(timespec="seconds")

    row = conn.execute(
        "SELECT state, lease_owner FROM jobs WHERE job_id=?", (job_id,)
    ).fetchone()
    if not row:
        return {"error": "not_found"}

    state, owner = row
    if state != STATE_CLAIMED or not owner or not owner.endswith(f":{lease_token}"):
        return {"error": LEASE_EXPIRED}

    conn.execute(
        "UPDATE jobs SET state=?, lease_owner=NULL, lease_until=?, result=?, updated_at=? "
        "WHERE job_id=?",
        (STATE_NO_CANDIDATE, now_str, reason, now_str, job_id),
    )
    conn.commit()
    return {"state": STATE_NO_CANDIDATE}


# ---------- 失败 ----------

def fail(conn, job_id: str, error: str, *, retryable: bool = True) -> dict:
    """标记任务失败。可重试的进入 retry_wait，否则 failed。"""
    now = datetime.now(timezone.utc)
    now_str = now.isoformat(timespec="seconds")

    row = conn.execute(
        "SELECT state, attempt, max_attempts FROM jobs WHERE job_id=?", (job_id,)
    ).fetchone()
    if not row:
        return {"error": "not_found"}

    state, attempt, max_attempts = row
    if state in TERMINAL_STATES:
        return {"error": "already_done", "state": state}

    if retryable and attempt < max_attempts:
        # 指数退避
        delay = min(300, 5 * (2 ** attempt))
        next_run = (now + timedelta(seconds=delay)).isoformat(timespec="seconds")
        conn.execute(
            "UPDATE jobs SET state=?, error=?, next_run_at=?, lease_owner=NULL,"
            " lease_until=NULL, updated_at=? WHERE job_id=?",
            (STATE_RETRY_WAIT, error, next_run, now_str, job_id),
        )
    else:
        conn.execute(
            "UPDATE jobs SET state=?, error=?, lease_owner=NULL, lease_until=NULL,"
            " updated_at=? WHERE job_id=?",
            (STATE_FAILED, error, now_str, job_id),
        )
    conn.commit()
    return {"job_id": job_id, "state": conn.execute(
        "SELECT state FROM jobs WHERE job_id=?", (job_id,)
    ).fetchone()[0]}


# ---------- 重试 ----------

def retry(conn, job_id: str) -> dict:
    """手动重试：failed/retry_wait → queued，不新增逻辑保存。"""
    now_str = datetime.now(timezone.utc).isoformat(timespec="seconds")
    row = conn.execute(
        "SELECT state FROM jobs WHERE job_id=?", (job_id,)
    ).fetchone()
    if not row:
        return {"error": "not_found"}
    if row[0] not in (STATE_FAILED, STATE_RETRY_WAIT):
        return {"error": f"cannot_retry_{row[0]}"}
    conn.execute(
        "UPDATE jobs SET state=?, error=NULL, next_run_at=?, updated_at=? WHERE job_id=?",
        (STATE_QUEUED, now_str, now_str, job_id),
    )
    conn.commit()
    return {"job_id": job_id, "state": STATE_QUEUED}


# ---------- 状态查询 ----------

def status(conn, job_id: str) -> dict | None:
    """查询任务状态。"""
    row = conn.execute(
        "SELECT job_id, kind, scope, state, attempt, max_attempts, batch_cursor, "
        "batch_total, error, created_at, updated_at FROM jobs WHERE job_id=?",
        (job_id,),
    ).fetchone()
    if not row:
        return None
    return {
        "job_id": row[0], "kind": row[1], "scope": row[2], "state": row[3],
        "attempt": row[4], "max_attempts": row[5], "cursor": row[6],
        "total": row[7], "error": row[8], "created_at": row[9], "updated_at": row[10],
        "retryable": row[3] in (STATE_FAILED, STATE_RETRY_WAIT),
    }


def list_jobs(conn, scope: str = "", kind: str = "", state: str = "",
              limit: int = 50) -> list[dict]:
    """列出任务。"""
    conds, params = [], []
    if scope:
        conds.append("scope=?")
        params.append(scope)
    if kind:
        conds.append("kind=?")
        params.append(kind)
    if state:
        conds.append("state=?")
        params.append(state)
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    params.append(limit)
    rows = conn.execute(
        f"SELECT job_id, kind, scope, state, attempt, batch_cursor, batch_total, "
        f"error, created_at FROM jobs{where} ORDER BY created_at DESC LIMIT ?",
        params,
    ).fetchall()
    return [
        {"job_id": r[0], "kind": r[1], "scope": r[2], "state": r[3],
         "attempt": r[4], "cursor": r[5], "total": r[6], "error": r[7],
         "created_at": r[8]}
        for r in rows
    ]


# ---------- 租约过期回收 ----------

def expire_leases(conn) -> int:
    """回收过期租约：claimed 且 lease_until 已过 → awaiting_agent。返回回收数。"""
    now_str = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cur = conn.execute(
        "SELECT job_id FROM jobs WHERE state=? AND lease_until < ?",
        (STATE_CLAIMED, now_str),
    ).fetchall()
    if not cur:
        return 0
    ids = [r[0] for r in cur]
    placeholders = ",".join("?" * len(ids))
    conn.execute(
        f"UPDATE jobs SET state=?, lease_owner=NULL, lease_until=NULL, "
        f"updated_at=? WHERE job_id IN ({placeholders})",
        [STATE_AWAITING, now_str] + ids,
    )
    conn.commit()
    return len(ids)
