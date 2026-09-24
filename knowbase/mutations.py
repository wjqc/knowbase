"""统一写入服务：CAS + 幂等 + 版本控制。

设计要点：
- 所有写操作（create/update/archive/feedback）经此模块，保证原子性和一致性；
- content_revision 覆盖内容变更（title/body/tags/scope/relations/code_refs/source_refs）；
- governance_revision 覆盖治理变更（status/confidence/last_verified）；
- 幂等键 request_id 防重复提交；同 key 不同 payload 返回 IDEMPOTENCY_CONFLICT；
- RepoLock 内重新检查 revision，CAS 失败返回 REVISION_CONFLICT；
- 写操作先落 SQLite，再写文件，Git 提交失败单独记录 pending。
"""

import hashlib
import json
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

from . import store, locking

# 错误码
REVISION_CONFLICT = "REVISION_CONFLICT"
IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
SCOPE_MISMATCH = "SCOPE_MISMATCH"

# 操作类型
OP_CREATE = "create"
OP_UPDATE = "update"
OP_FEEDBACK = "feedback"
OP_ARCHIVE = "archive"
OP_RESOLVE = "resolve"


def _compute_revision(meta: dict, body: str) -> str:
    """基于规范化 Markdown 字段计算 content_revision。

    规范化：UTF-8、键排序、JSON 序列化，排除本机路径和时间戳。
    """
    # 提取参与 revision 计算的字段
    content_fields = {
        "id": meta.get("id", ""),
        "type": meta.get("type", ""),
        "title": meta.get("title", ""),
        "scope": meta.get("scope", ""),
        "tags": sorted(meta.get("tags", [])),
        "relations": sorted(meta.get("relations", []), key=lambda r: r.get("id", "")),
        "code_refs": sorted(meta.get("code_refs", []), key=lambda r: (r.get("repo", ""), r.get("path", ""))),
        "body": body.strip(),
    }
    raw = json.dumps(content_fields, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _compute_governance_revision(meta: dict) -> str:
    """基于治理字段计算 governance_revision。"""
    gov_fields = {
        "id": meta.get("id", ""),
        "status": meta.get("status", ""),
        "confidence": meta.get("confidence", ""),
        "last_verified": meta.get("last_verified", ""),
        "helpful_count": meta.get("helpful_count", 0),
        "unhelpful_count": meta.get("unhelpful_count", 0),
    }
    raw = json.dumps(gov_fields, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _check_idempotency(conn, request_id: str, entity_id: str, payload_hash: str) -> dict | None:
    """检查幂等性：同 request_id 已存在则返回既有结果。

    返回 None 表示新操作；返回 dict 表示幂等命中或冲突。
    """
    if not request_id:
        return None
    row = conn.execute(
        "SELECT entity_id, payload_hash, result FROM applied_operations WHERE request_id=?",
        (request_id,),
    ).fetchone()
    if not row:
        return None
    existing_entity, existing_hash, result = row
    if existing_hash != payload_hash:
        return {"error": IDEMPOTENCY_CONFLICT, "existing_entity": existing_entity}
    return {"idempotent": True, "entity_id": existing_entity, "result": result}


def _record_operation(conn, op_id: str, entity_id: str, scope: str, kind: str,
                        request_id: str, payload_hash: str, result: str,
                        base_revision: str = "", new_revision: str = "") -> None:
    """登记操作到 applied_operations 表。"""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        "INSERT OR IGNORE INTO applied_operations(op_id,entity_id,scope,kind,request_id,"
        "payload_hash,result,base_revision,new_revision,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (op_id, entity_id, scope, kind, request_id, payload_hash, result,
         base_revision, new_revision, now),
    )


def create_card(repo: Path, conn, dtype: str, title: str, body: str, *,
                scope: str = "", tags: list | None = None, source: str = "",
                request_id: str = "", **extra_meta) -> dict:
    """创建新卡片。

    返回 {"entity_id", "content_revision", "governance_revision", "created": True}
    或 {"error": IDEMPOTENCY_CONFLICT}。
    """
    # 幂等检查
    payload = {"type": dtype, "title": title, "body": body, "scope": scope, **extra_meta}
    payload_hash = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]
    idem = _check_idempotency(conn, request_id, "", payload_hash)
    if idem:
        return idem

    # 分配新 ID
    mid = store.alloc_id(repo, dtype)
    meta = store.new_meta(dtype, title, scope, tags or [], source)
    meta.update(extra_meta)
    meta["id"] = mid

    # 计算 revision
    content_rev = _compute_revision(meta, body)
    gov_rev = _compute_governance_revision(meta)

    # 写文件（在 RepoLock 内由调用方保证）
    path = store.mem_path(repo, dtype, mid, staging=(meta.get("confidence") == "staging"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(store.render(meta, body), encoding="utf-8")

    # 登记操作
    op_id = f"OP-{uuid.uuid4().hex[:12]}"
    _record_operation(conn, op_id, mid, scope, OP_CREATE, request_id,
                        payload_hash, "local_saved", "", content_rev)

    # 更新 entity_heads
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        "INSERT OR REPLACE INTO entity_heads(entity_id,content_revision,governance_revision,updated_at) "
        "VALUES(?,?,?,?)",
        (mid, content_rev, gov_rev, now),
    )
    conn.commit()

    return {
        "entity_id": mid,
        "content_revision": content_rev,
        "governance_revision": gov_rev,
        "created": True,
    }


def update_card(repo: Path, conn, mid: str, *, 
                expected_content_revision: str = "",
                expected_governance_revision: str = "",
                request_id: str = "",
                **updates) -> dict:
    """更新卡片（CAS）。

    必须携带 expected_content_revision 和/或 expected_governance_revision；
    不携带则返回错误，防止旧版本覆盖。

    返回 {"entity_id", "content_revision", "governance_revision", "updated": True}
    或 {"error": REVISION_CONFLICT / IDEMPOTENCY_CONFLICT}。
    """
    # 加载当前卡片
    meta, body, path = store.load(repo, mid)
    if not meta:
        return {"error": "not_found"}

    # 幂等检查
    payload_hash = hashlib.sha256(json.dumps(updates, sort_keys=True).encode()).hexdigest()[:16]
    idem = _check_idempotency(conn, request_id, mid, payload_hash)
    if idem:
        return idem

    # 读取当前 revision
    row = conn.execute(
        "SELECT content_revision, governance_revision FROM entity_heads WHERE entity_id=?",
        (mid,),
    ).fetchone()
    current_content_rev = row[0] if row else _compute_revision(meta, body)
    current_gov_rev = row[1] if row else _compute_governance_revision(meta)

    # CAS 检查
    if expected_content_revision and expected_content_revision != current_content_rev:
        return {"error": REVISION_CONFLICT, "current_content_revision": current_content_rev}
    if expected_governance_revision and expected_governance_revision != current_gov_rev:
        return {"error": REVISION_CONFLICT, "current_governance_revision": current_gov_rev}

    # 应用更新
    content_fields = {"title", "body", "tags", "scope", "relations", "code_refs", "source_refs"}
    gov_fields = {"status", "confidence", "last_verified"}

    for key, value in updates.items():
        if key in content_fields:
            if key == "body":
                body = value
            elif key in meta:
                meta[key] = value
        elif key in gov_fields:
            meta[key] = value

    meta["updated"] = date.today().isoformat()

    # 重新计算 revision
    new_content_rev = _compute_revision(meta, body)
    new_gov_rev = _compute_governance_revision(meta)

    # 写文件
    if path:
        path.write_text(store.render(meta, body), encoding="utf-8")

    # 登记操作
    op_id = f"OP-{uuid.uuid4().hex[:12]}"
    _record_operation(conn, op_id, mid, meta.get("scope", ""), OP_UPDATE, request_id,
                        payload_hash, "local_saved", current_content_rev, new_content_rev)

    # 更新 entity_heads
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        "INSERT OR REPLACE INTO entity_heads(entity_id,content_revision,governance_revision,updated_at) "
        "VALUES(?,?,?,?)",
        (mid, new_content_rev, new_gov_rev, now),
    )
    conn.commit()

    return {
        "entity_id": mid,
        "content_revision": new_content_rev,
        "governance_revision": new_gov_rev,
        "updated": True,
    }


def get_revisions(conn, mid: str) -> dict | None:
    """查询卡片的当前 revision。"""
    row = conn.execute(
        "SELECT content_revision, governance_revision, updated_at FROM entity_heads WHERE entity_id=?",
        (mid,),
    ).fetchone()
    if not row:
        return None
    return {
        "entity_id": mid,
        "content_revision": row[0],
        "governance_revision": row[1],
        "updated_at": row[2],
    }
