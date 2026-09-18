"""P3-B：ACL Policy 解析器（V2 计划 §7.1 + §7.3）。

resolve(principal, doc) -> Policy
- 4 类 visibility（global 拆解）：public-template / organization-global / project-shared / personal
- platform_admin 默认 deny personal（V2 计划 §7.1 末尾："不自动获得敏感正文访问权"）
- 未知 visibility → deny（白名单 fail-closed）

doc 字段约定：
- visibility (str)：4 类之一；缺省 organization-global
- org_id (str|None)：organization-global 必填
- project_id (str|None)：project-shared 必填
- owner_id (str|None)：personal 必填
- project_ids (list[str],可选)：project-shared 显式共享列表（多项目共享场景）

未提供足够字段的 visibility 一律 deny（fail-closed）。
"""
from __future__ import annotations

from dataclasses import dataclass

from .principal import Principal

VIS_PUBLIC = "public-template"
VIS_ORG = "organization-global"
VIS_PROJECT = "project-shared"
VIS_PERSONAL = "personal"

_VALID_VISIBILITY = frozenset({VIS_PUBLIC, VIS_ORG, VIS_PROJECT, VIS_PERSONAL})


@dataclass(frozen=True)
class Policy:
    """单文档的访问决策。"""
    allow: bool
    reason: str
    visibility: str

    def as_dict(self) -> dict:
        return {"allow": self.allow, "reason": self.reason, "visibility": self.visibility}


def resolve(principal: Principal, doc: dict) -> Policy:
    """给定 principal + doc，返回访问决策。"""
    visibility = doc.get("visibility", VIS_ORG) or VIS_ORG
    if visibility not in _VALID_VISIBILITY:
        return Policy(False, f"unknown visibility: {visibility}", visibility)

    # 平台管理员显式规则：默认不读 personal（§7.1 末尾）；
    # 对其他 visibility（public-template / organization-global / project-shared）
    # 平台管理员拥有运维级访问权（"组织级运维"语义）。
    if principal.is_platform_admin and visibility == VIS_PERSONAL:
        return Policy(False, "platform_admin denied personal", visibility)

    if visibility == VIS_PUBLIC:
        return Policy(True, "public-template", visibility)

    if visibility == VIS_ORG:
        doc_org = doc.get("org_id")
        if not doc_org:
            return Policy(False, "org doc missing org_id", visibility)
        if principal.is_platform_admin:
            return Policy(True, "platform_admin ops", visibility)
        if principal.has_org_role(doc_org):
            return Policy(True, "org member", visibility)
        return Policy(False, "not in org", visibility)

    if visibility == VIS_PROJECT:
        # project-shared 至少需要 doc_project_id；可选 meta.project_ids 多项目共享
        doc_proj = doc.get("project_id")
        candidates = [doc_proj] if doc_proj else []
        candidates.extend(doc.get("project_ids", []) or [])
        if not candidates:
            return Policy(False, "project doc missing project_id", visibility)
        if principal.is_platform_admin:
            return Policy(True, "platform_admin ops", visibility)
        if any(principal.has_project_role(pid) for pid in candidates):
            return Policy(True, "project member", visibility)
        return Policy(False, "not in shared projects", visibility)

    if visibility == VIS_PERSONAL:
        owner = doc.get("owner_id")
        if not owner:
            return Policy(False, "personal doc missing owner_id", visibility)
        if owner == principal.principal_id:
            return Policy(True, "owner", visibility)
        return Policy(False, "not owner", visibility)

    # 兜底（fail-closed）
    return Policy(False, f"deny {visibility}", visibility)