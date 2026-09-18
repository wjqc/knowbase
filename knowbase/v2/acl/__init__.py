"""P3-B ACL 子包。

公共 API：
- Principal / Principal.local_user / Principal.from_db
- Policy / resolve
- pre_filter / build_doc_predicate

设计：
- 不依赖 retrieval 子包；retrieval 单向依赖 ACL
- 失败关闭（fail-closed）：unknown visibility / missing field 一律 deny
"""
from __future__ import annotations

from .filter import build_doc_predicate, pre_filter
from .guard import (
    check_doc_access,
    filter_doc_ids_by_principal,
    get_document_for_principal,
    list_documents_for_principal,
)
from .policy import (
    VIS_ORG,
    VIS_PERSONAL,
    VIS_PROJECT,
    VIS_PUBLIC,
    Policy,
    resolve,
)
from .principal import Principal

__all__ = [
    "Principal",
    "Policy",
    "VIS_ORG",
    "VIS_PERSONAL",
    "VIS_PROJECT",
    "VIS_PUBLIC",
    "build_doc_predicate",
    "check_doc_access",
    "filter_doc_ids_by_principal",
    "get_document_for_principal",
    "list_documents_for_principal",
    "pre_filter",
    "resolve",
]