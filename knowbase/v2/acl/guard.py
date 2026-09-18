"""P3-D：ACL 对象级授权入口（V2 计划 §7.2 / §7.4）。

防止「拿 doc_id 直接读」绕过召回过滤：
  - get_document_for_principal(repo, doc_id, principal) -> Document | None
  - list_documents_for_principal(repo, principal, ...) -> list[Document]
  - filter_doc_ids_by_principal(repo, principal, doc_ids) -> set[str]

所有直读接口必须经此 guard。fail-closed：resolve() 返回 deny 时一律 None / 过滤掉。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..domain.models import Document
from .policy import Policy, resolve

if TYPE_CHECKING:
    from ..repositories.sqlite_repo import V2Repository
    from .principal import Principal


def _doc_to_dict(doc: Document) -> dict:
    """Document -> policy 解析所需的 dict 视图。"""
    return {
        "doc_id": doc.id,
        "visibility": doc.visibility,
        "org_id": doc.org_id,
        "project_id": doc.project_id,
        "owner_id": doc.owner_id,
    }


def check_doc_access(principal: "Principal", doc: Document) -> Policy:
    """单文档对象级授权检查（§7.2：Document read 再做一次对象级授权）。

    返回 Policy；调用方根据 allow 决定是否返回 doc 对象。
    """
    return resolve(principal, _doc_to_dict(doc))


def get_document_for_principal(
    repo: "V2Repository",
    principal: "Principal",
    doc_id: str,
) -> Document | None:
    """按 ID 直读 doc；走 ACL 对象级授权。"""
    doc = repo.get_document(doc_id)
    if doc is None:
        return None
    policy = check_doc_access(principal, doc)
    if not policy.allow:
        return None
    return doc


def list_documents_for_principal(
    repo: "V2Repository",
    principal: "Principal",
    *,
    source_id: str | None = None,
) -> list[Document]:
    """列 doc；逐个 ACL check（§7.2：snippet、统计、dashboard 同样受 ACL 约束）。"""
    out: list[Document] = []
    for doc in repo.list_documents(source_id=source_id):
        if check_doc_access(principal, doc).allow:
            out.append(doc)
    return out


def filter_doc_ids_by_principal(
    repo: "V2Repository",
    principal: "Principal",
    doc_ids: list[str],
) -> set[str]:
    """批量按 ID 检查；返回允许访问的 doc_id 集合。

    用法：调用方拿到一批候选 doc_id（如 staging 列表、缓存、dashboard 数据），
    想知道哪些对本 principal 可见。
    """
    allowed: set[str] = set()
    for doc_id in doc_ids:
        doc = repo.get_document(doc_id)
        if doc is None:
            continue
        if check_doc_access(principal, doc).allow:
            allowed.add(doc_id)
    return allowed


__all__ = [
    "check_doc_access",
    "filter_doc_ids_by_principal",
    "get_document_for_principal",
    "list_documents_for_principal",
]