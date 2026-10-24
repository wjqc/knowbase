"""revision 计算、写路径共用常量与 revision 查询（生产共用）。

本模块现只保留纯计算与只读职责：
- _compute_revision / _compute_governance_revision 是全仓共用的 revision
  计算：commit_adapter、index、doctor、hooks 等均从本模块导入。
  content_revision 覆盖内容字段（title/body/tags/scope/relations/code_refs/
  source_refs/provenance/rule_context），governance_revision 覆盖治理字段
  （status/confidence/last_verified/verified_content_revision/反馈计数）。
- REVISION_CONFLICT / IDEMPOTENCY_CONFLICT 等错误码与 OP_* 操作类型
  常量由 commit_adapter 等模块共用。
- get_revisions 按 entity_heads 查询卡片的当前 revision。

写路径（create/update）已统一到 commit_adapter（intent 持久化 + 锁内
CAS + 崩溃恢复）。早期的本模块 create_card / update_card 已删除：它们
没有生产调用方，与生产实现存在机制性差异（幂等机制、锁内查重、原子
写、加锁模型均不同），其全部测试调用方已迁移到 adapter_create /
adapter_update。新代码不要在本模块再加写实现。
"""

import hashlib
import json

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
        "source_refs": meta.get("source_refs", []),
        "provenance": meta.get("provenance", ""),
        "rule_context": meta.get("rule_context", {}),
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
        "verified_content_revision": meta.get("verified_content_revision", ""),
        "helpful_count": meta.get("helpful_count", 0),
        "unhelpful_count": meta.get("unhelpful_count", 0),
    }
    raw = json.dumps(gov_fields, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


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
