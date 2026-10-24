"""提交适配器：统一写入协议 + 崩溃恢复。

设计要点（L1 轻量路线）：
- 所有写操作（create/update/feedback）经此适配器，保证原子性和崩溃恢复能力；
- 写前持久 intent（pending），写后标记 completed，崩溃恢复时扫描未入账 intent；
- 短 RepoLock 内检查 revision（CAS）、原子替换文件、更新 entity_heads；
- 锁外执行 Git 提交和推送，失败不抹除已持久化的操作；
- intent 不成为共享知识权威，不提供跨机冲突隔离（轻量路线显式限制）。
"""

import hashlib
import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import store, locking
from .locking import RepoLock
from .mutations import (
    REVISION_CONFLICT, IDEMPOTENCY_CONFLICT,
    _compute_revision, _compute_governance_revision,
    OP_CREATE, OP_UPDATE, OP_FEEDBACK,
)


def _payload_hash(data: dict) -> str:
    """计算 payload 哈希，用于崩溃恢复时识别已落盘结果。"""
    raw = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _persist_intent(conn, kind: str, entity_id: str, scope: str,
                    request_id: str, payload_hash: str) -> str:
    """持久化写入意图（pending 状态），返回 intent_id。"""
    intent_id = f"INT-{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO write_intents(intent_id,request_id,entity_id,scope,kind,"
        "payload_hash,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (intent_id, request_id, entity_id, scope, kind, payload_hash, "pending", now),
    )
    conn.commit()
    return intent_id


def _complete_intent(conn, intent_id: str, result: str, entity_id: str = "") -> None:
    """标记 intent 为 completed，记录回执。"""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if entity_id:
        conn.execute(
            "UPDATE write_intents SET status='completed', result=?, entity_id=?, completed_at=? "
            "WHERE intent_id=?",
            (result, entity_id, now, intent_id),
        )
    else:
        conn.execute(
            "UPDATE write_intents SET status='completed', result=?, completed_at=? "
            "WHERE intent_id=?",
            (result, now, intent_id),
        )
    conn.commit()


def _fail_intent(conn, intent_id: str, error: str) -> None:
    """标记 intent 为 failed。"""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        "UPDATE write_intents SET status='failed', result=?, completed_at=? "
        "WHERE intent_id=?",
        (error, now, intent_id),
    )
    conn.commit()


def _discard_intent(conn, intent_id: str) -> None:
    """删除从未执行的 intent（CAS 冲突、目标不存在、查重拦截等）。

    这类失败没有产生任何写入，不留 failed 记录：否则同 request_id 的
    后续重试会被 _check_intent_completed 以 INTENT_FAILED 永久短路，
    与服务端"获取最新 revision 后重试"的提示自相矛盾。
    """
    conn.execute("DELETE FROM write_intents WHERE intent_id=?", (intent_id,))
    conn.commit()


def _check_intent_completed(conn, request_id: str, entity_id: str, payload_hash: str) -> dict | None:
    """检查 intent 是否已完成（崩溃恢复场景）。

    返回 None 表示未完成；返回 dict 表示已完成或已失败。
    """
    if not request_id:
        return None
    row = conn.execute(
        "SELECT status, result, entity_id FROM write_intents "
        "WHERE request_id=? AND payload_hash=?",
        (request_id, payload_hash),
    ).fetchone()
    if not row:
        return None
    status, result, existing_entity = row
    if status == "completed":
        return {"idempotent": True, "entity_id": existing_entity or entity_id, "result": result}
    if status == "failed":
        return {"error": "INTENT_FAILED", "detail": result}
    return None  # pending，继续执行


def _atomic_write(path: Path, content: str) -> None:
    """原子写文件：临时文件 + rename。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(fd)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        raise


def adapter_create(repo: Path, conn, dtype: str, title: str, body: str, *,
                   scope: str = "", tags: list | None = None, source: str = "",
                   request_id: str = "", lock_timeout: float = 10.0,
                   **extra_meta) -> dict:
    """创建卡片（带 intent 持久化和崩溃恢复）。

    流程：
    1. 持久化 intent（pending）
    2. 检查是否已完成（崩溃恢复）
    3. 短 RepoLock 内：查重、分配 ID、原子写文件、更新 entity_heads
    4. 标记 intent 为 completed

    返回 {"entity_id", "content_revision", "governance_revision", "created": True}
    或 {"idempotent": True, "entity_id", "result"}（崩溃恢复命中）
    或 {"error": IDEMPOTENCY_CONFLICT}（同 request_id 不同 payload）。
    """
    # 计算 payload 哈希
    payload = {"type": dtype, "title": title, "body": body, "scope": scope,
               "tags": tags or [], "source": source, **extra_meta}
    phash = _payload_hash(payload)

    # 检查是否已完成（崩溃恢复）
    existing = _check_intent_completed(conn, request_id, "", phash)
    if existing:
        return existing

    # 检查同 request_id 不同 payload
    if request_id:
        row = conn.execute(
            "SELECT entity_id, payload_hash FROM write_intents WHERE request_id=?",
            (request_id,),
        ).fetchone()
        if row and row[1] != phash:
            return {"error": IDEMPOTENCY_CONFLICT, "existing_entity": row[0]}

    # 持久化 intent
    intent_id = _persist_intent(conn, OP_CREATE, "", scope, request_id, phash)

    # 短 RepoLock 内执行
    try:
        with RepoLock(repo, lock_timeout):
            # 等锁期间另一进程可能已完成相同写入，先重放再查相似标题。
            completed = _check_intent_completed(conn, request_id, "", phash)
            if completed:
                return completed
            # 锁内查重（未执行任何写入，失败不留痕，见 _discard_intent）
            sim = store.find_similar(repo, title, scope=scope)
            if sim:
                _, smeta, spath = sim
                _discard_intent(conn, intent_id)
                return {
                    "error": "DUPLICATE",
                    "existing_id": smeta["id"],
                    "existing_title": smeta.get("title", ""),
                    "existing_path": str(spath),
                }

            # 分配 ID，构建 meta
            mid = store.alloc_id(repo, dtype)
            meta = store.new_meta(dtype, title, scope, tags or [], source)
            # 合并 extra_meta，确保列表字段不为 None
            for k, v in extra_meta.items():
                if v is not None:
                    meta[k] = v
            meta["id"] = mid

            # 计算 revision
            content_rev = _compute_revision(meta, body)
            gov_rev = _compute_governance_revision(meta)

            # 原子写文件
            staging = meta.get("confidence") == "staging" or dtype in ("standard", "preference", "bizrule")
            path = store.mem_path(repo, dtype, mid, staging=staging)
            _atomic_write(path, store.render(meta, body))

            # 更新 entity_heads
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            conn.execute(
                "INSERT OR REPLACE INTO entity_heads(entity_id,content_revision,governance_revision,updated_at) "
                "VALUES(?,?,?,?)",
                (mid, content_rev, gov_rev, now),
            )

            # 标记 intent 完成（同时更新 entity_id）
            # 来源账本：创建事件与卡落库同事务（未启用 value 事件时为空操作）。
            from .value.events import record as _record_value
            _record_value(conn, "knowledge.created",
                          {"memory_id": mid, "content_revision": content_rev, "scope": scope,
                           "dtype": dtype, "staging": staging, "title": title},
                          source_key=f"knowledge:{mid}:created",
                          operation_id=request_id or f"card_create:{mid}")
            _complete_intent(conn, intent_id, f"created:{mid}", entity_id=mid)

        return {
            "entity_id": mid,
            "content_revision": content_rev,
            "governance_revision": gov_rev,
            "created": True,
            "path": str(path),
        }
    except Exception as e:
        _fail_intent(conn, intent_id, str(e))
        raise


def adapter_update(repo: Path, conn, mid: str, *,
                   expected_content_revision: str = "",
                   expected_governance_revision: str = "",
                   request_id: str = "",
                   lock_timeout: float = 10.0,
                   **updates) -> dict:
    """更新卡片（带 CAS 和 intent 持久化）。

    携带 expected_content_revision / expected_governance_revision 时才做
    CAS 校验（与锁内重读的当前 revision 不符返回 REVISION_CONFLICT）；
    不携带则不校验。

    返回 {"entity_id", "content_revision", "governance_revision", "updated": True}
    或 {"error": REVISION_CONFLICT / IDEMPOTENCY_CONFLICT}。
    """
    # 计算 payload 哈希
    phash = _payload_hash({"entity_id": mid, **updates})

    # 检查是否已完成（崩溃恢复）
    existing = _check_intent_completed(conn, request_id, mid, phash)
    if existing:
        return existing

    # 检查同 request_id 不同 payload
    if request_id:
        row = conn.execute(
            "SELECT entity_id, payload_hash FROM write_intents WHERE request_id=?",
            (request_id,),
        ).fetchone()
        if row and row[1] != phash and row[0] == mid:
            return {"error": IDEMPOTENCY_CONFLICT, "existing_entity": row[0]}

    # 持久化 intent
    intent_id = _persist_intent(conn, OP_UPDATE, mid, "", request_id, phash)

    try:
        with RepoLock(repo, lock_timeout):
            # 加载当前卡片
            meta, body, path = store.load(repo, mid)
            if not meta:
                _discard_intent(conn, intent_id)
                return {"error": "not_found"}

            # 读取当前 revision
            row = conn.execute(
                "SELECT content_revision, governance_revision FROM entity_heads WHERE entity_id=?",
                (mid,),
            ).fetchone()
            current_content_rev = _compute_revision(meta, body)
            current_gov_rev = _compute_governance_revision(meta)

            # CAS 检查（冲突意味着本次更新根本没有执行，失败不留痕，
            # 客户端可按提示带最新 revision 用同一 request_id 重试）
            if expected_content_revision and expected_content_revision != current_content_rev:
                _discard_intent(conn, intent_id)
                return {"error": REVISION_CONFLICT, "current_content_revision": current_content_rev}
            if expected_governance_revision and expected_governance_revision != current_gov_rev:
                _discard_intent(conn, intent_id)
                return {"error": REVISION_CONFLICT, "current_governance_revision": current_gov_rev}

            # 应用更新
            from datetime import date
            content_fields = {"title", "body", "tags", "scope", "relations", "code_refs", "source_refs",
                              "provenance", "rule_context", "evidence"}
            gov_fields = {"status", "confidence", "last_verified"}

            for key, value in updates.items():
                if key in content_fields:
                    if key == "body":
                        body = value
                    else:
                        meta[key] = value
                elif key in gov_fields:
                    meta[key] = value

            meta["updated"] = date.today().isoformat()

            # 重新计算 revision
            new_content_rev = _compute_revision(meta, body)
            if new_content_rev != current_content_rev:
                meta["confidence"] = "once"
                meta["last_verified"] = ""
                meta["verified_content_revision"] = ""
                meta["helpful_count"] = 0
                meta["unhelpful_count"] = 0
            new_gov_rev = _compute_governance_revision(meta)

            # 原子写文件
            if path:
                _atomic_write(path, store.render(meta, body))

            # 更新 entity_heads
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            conn.execute(
                "INSERT OR REPLACE INTO entity_heads(entity_id,content_revision,governance_revision,updated_at) "
                "VALUES(?,?,?,?)",
                (mid, new_content_rev, new_gov_rev, now),
            )

            # 来源账本：内容版本变化才记修订事件（稳定键按 mid+新版本幂等）。
            if new_content_rev != current_content_rev:
                from .value.events import record as _record_value
                _record_value(conn, "knowledge.revised",
                              {"memory_id": mid, "content_revision": new_content_rev,
                               "scope": meta.get("scope", ""),
                               "staging": bool(path) and path.parent.name == "staging"},
                              source_key=f"knowledge:{mid}:{new_content_rev}:revised",
                              operation_id=request_id or f"card_update:{mid}")

            # 标记 intent 完成
            _complete_intent(conn, intent_id, f"updated:{mid}")

        return {
            "entity_id": mid,
            "content_revision": new_content_rev,
            "governance_revision": new_gov_rev,
            "updated": True,
        }
    except Exception as e:
        _fail_intent(conn, intent_id, str(e))
        raise


def adapter_feedback(repo: Path, conn, mid: str, outcome: str, by: str, *,
                     expected_governance_revision: str = "",
                     request_id: str = "", lock_timeout: float = 10.0,
                     expected_content_revision: str = "", use_id: str = "",
                     task_outcome_id: str = "", task_result: str = "",
                     verification_source: str = "agent_report") -> dict:
    """复用版本/任务结果反馈回执，支持精确重放与文件落盘恢复。"""
    from . import versioned_feedback
    return versioned_feedback.apply(repo,conn,mid,outcome,by,
        expected_governance_revision=expected_governance_revision,request_id=request_id,
        lock_timeout=lock_timeout,expected_content_revision=expected_content_revision,
        use_id=use_id,task_outcome_id=task_outcome_id,task_result=task_result,
        verification_source=verification_source)


def recover_intents(repo: Path, conn) -> dict:
    """崩溃恢复：扫描未入账的 pending intent，核验写入是否已落盘。

    update/feedback 类 intent 只有取得落盘证据（文件 revision 与
    entity_heads 已提交 revision 不一致，或同 request_id 已由重试完成）
    才标记 recovered；无证据的保留 pending，等待客户端重试真正执行，
    不得凭"文件存在"假报成功。create 类 intent 无法定位目标文件，
    仍标记 failed 交人工核查。

    返回 {"recovered": int, "failed": int, "details": [...]}.
    """
    rows = conn.execute(
        "SELECT intent_id, request_id, entity_id, kind, payload_hash "
        "FROM write_intents WHERE status='pending'"
    ).fetchall()

    recovered = 0
    failed = 0
    details = []

    for intent_id, request_id, entity_id, kind, payload_hash in rows:
        # 检查文件是否已落盘
        if kind == OP_CREATE:
            # 创建操作：检查 entity_id 对应的文件是否存在
            # 由于创建时 entity_id 可能为空，需要通过其他方式检查
            # 这里简化处理：标记为 failed，由人工核查
            _fail_intent(conn, intent_id, "crash_recovery:pending_create")
            failed += 1
            details.append({"intent_id": intent_id, "kind": kind, "status": "failed",
                           "reason": "pending create requires manual review"})
        elif kind in (OP_UPDATE, OP_FEEDBACK):
            # 更新/反馈操作：必须核验更新是否真的落盘，不能只看文件存在。
            # 落盘判据：文件当前 revision（内容/治理哈希）与 entity_heads
            # 记录的已提交 revision 不一致——说明新内容已原子写盘、但
            # intent 与 entity_heads 的事务未提交即崩溃。两者一致时没有
            # 任何证据表明本次更新落盘，不得标记完成（否则客户端同
            # request_id 重试会被误判为幂等成功，更新被静默吞掉）。
            path = store.find_file(repo, entity_id)
            if not path:
                _fail_intent(conn, intent_id, "crash_recovery:file_not_found")
                failed += 1
                details.append({"intent_id": intent_id, "kind": kind,
                               "entity_id": entity_id, "status": "failed",
                               "reason": "file not found"})
                continue
            meta, body, _ = store.load(repo, entity_id)
            if not meta:
                _fail_intent(conn, intent_id, "crash_recovery:load_failed")
                failed += 1
                details.append({"intent_id": intent_id, "kind": kind,
                               "entity_id": entity_id, "status": "failed",
                               "reason": "file exists but load failed"})
                continue
            file_content_rev = _compute_revision(meta, body)
            file_gov_rev = _compute_governance_revision(meta)
            head = conn.execute(
                "SELECT content_revision, governance_revision "
                "FROM entity_heads WHERE entity_id=?",
                (entity_id,),
            ).fetchone()
            if head and (head[0] != file_content_rev or head[1] != file_gov_rev):
                # 更新已落盘但事务未入账：以文件（唯一真相）修复
                # entity_heads，再完成 intent。
                now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                conn.execute(
                    "INSERT OR REPLACE INTO entity_heads(entity_id,content_revision,"
                    "governance_revision,updated_at) VALUES(?,?,?,?)",
                    (entity_id, file_content_rev, file_gov_rev, now),
                )
                conn.commit()
                _complete_intent(conn, intent_id, f"crash_recovery:recovered:{entity_id}")
                recovered += 1
                details.append({"intent_id": intent_id, "kind": kind,
                               "entity_id": entity_id, "status": "recovered"})
                continue
            # 同一 request_id + payload 已由重试完成时，本条只是崩溃残留
            # 的重复 intent，按已完成收账。
            sibling = None
            if request_id:
                sibling = conn.execute(
                    "SELECT intent_id FROM write_intents WHERE request_id=? "
                    "AND payload_hash=? AND status='completed' AND intent_id != ?",
                    (request_id, payload_hash, intent_id),
                ).fetchone()
            if sibling:
                _complete_intent(conn, intent_id, f"crash_recovery:recovered:{entity_id}")
                recovered += 1
                details.append({"intent_id": intent_id, "kind": kind,
                               "entity_id": entity_id, "status": "recovered",
                               "reason": "same request already completed by retry"})
                continue
            # 无落盘证据：保留 pending，等待客户端以同一 request_id 重试
            # 真正执行（pending 不短路重试路径）。既不计 recovered 也不计
            # failed——结论尚未确定。
            details.append({"intent_id": intent_id, "kind": kind,
                           "entity_id": entity_id, "status": "pending",
                           "reason": "no evidence update landed; awaiting client retry"})

    return {"recovered": recovered, "failed": failed, "details": details}
