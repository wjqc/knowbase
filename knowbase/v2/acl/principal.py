"""P3-B：Principal 运行时身份快照（V2 计划 §7.1）。

不在 dataclass 里 frozen 是因为它由业务层构造、字段可变（来自仓库 + 临时上下文）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..repositories.sqlite_repo import V2Repository


@dataclass
class Principal:
    """调用方身份（user / service / agent）。

    org_roles: org_id -> role（来自 membership 表）
    project_roles: project_id -> role（来自 project_membership 表）
    is_platform_admin: 显式授予的 platform_admin 标志；默认 False
    """
    principal_id: str
    display_name: str = ""
    kind: str = "user"                                  # user / service / agent
    org_roles: dict[str, str] = field(default_factory=dict)
    project_roles: dict[str, str] = field(default_factory=dict)
    is_platform_admin: bool = False

    def has_org_role(self, org_id: str, *roles: str) -> bool:
        """是否在 org_id 内拥有任一指定角色。空 roles 表示『该 org 内任意角色』。"""
        if org_id not in self.org_roles:
            return False
        if not roles:
            return True
        return self.org_roles[org_id] in roles

    def has_project_role(self, project_id: str, *roles: str) -> bool:
        if project_id not in self.project_roles:
            return False
        if not roles:
            return True
        return self.project_roles[project_id] in roles

    @staticmethod
    def local_user() -> "Principal":
        """本机单用户身份（Phase 6 之前的默认；不带任何组织/项目 ACL）。

        注意：local_user 不自动拥有 org/project 角色。
        因此在开启 ACL 的场景下，locally authored docs 必须显式
        分配到某个 org + 包含 principal_id 才能被看到。
        """
        return Principal(
            principal_id="local-user",
            display_name="local user",
            kind="user",
        )

    @staticmethod
    def from_db(repo: "V2Repository", principal_id: str) -> "Principal | None":
        """从 V2 仓库加载 Principal（含所有 membership）。"""
        p = repo.get_principal(principal_id)
        if p is None:
            return None
        org_roles: dict[str, str] = {}
        is_platform_admin = False
        for m in repo.list_org_memberships(principal_id):
            org_roles[m.org_id] = m.role
            if m.role == "platform_admin":
                is_platform_admin = True
        project_roles: dict[str, str] = {}
        for pm in repo.list_project_memberships(principal_id):
            project_roles[pm.project_id] = pm.role
        return Principal(
            principal_id=p.id,
            display_name=p.display_name,
            kind=p.kind.value,
            org_roles=org_roles,
            project_roles=project_roles,
            is_platform_admin=is_platform_admin,
        )