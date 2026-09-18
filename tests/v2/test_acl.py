"""P3-B ACL 单测。

覆盖：
- 4 类 visibility（public-template / organization-global / project-shared / personal）
- 5 类角色矩阵 + 缺省行为
- platform_admin 显式规则（§7.1 末尾：不读 personal）
- fail-closed：unknown visibility / missing field 一律 deny
- pre_filter / build_doc_predicate 接口契约
"""
from __future__ import annotations

from knowbase.v2.acl import (
    Principal,
    Policy,
    VIS_ORG,
    VIS_PERSONAL,
    VIS_PROJECT,
    VIS_PUBLIC,
    build_doc_predicate,
    pre_filter,
    resolve,
)


# ---------- fixtures ----------

def _p_org_member(org_id="org-1", role="viewer") -> Principal:
    return Principal(
        principal_id="alice",
        display_name="Alice",
        org_roles={org_id: role},
        project_roles={},
    )


def _p_project_member(proj_id="proj-1", role="viewer") -> Principal:
    return Principal(
        principal_id="bob",
        display_name="Bob",
        org_roles={"org-1": "viewer"},
        project_roles={proj_id: role},
    )


def _p_platform_admin() -> Principal:
    return Principal(
        principal_id="pat",
        display_name="Pat",
        org_roles={"org-1": "platform_admin"},
        is_platform_admin=True,
    )


def _p_no_membership() -> Principal:
    return Principal(principal_id="carol", display_name="Carol")


# ---------- Visibility ----------

def test_public_template_visible_to_everyone():
    doc = {"doc_id": "d1", "visibility": VIS_PUBLIC}
    assert resolve(_p_no_membership(), doc).allow
    assert resolve(_p_platform_admin(), doc).allow


def test_org_global_requires_same_org_membership():
    doc = {"doc_id": "d1", "visibility": VIS_ORG, "org_id": "org-1"}
    # org 成员：allow
    assert resolve(_p_org_member(), doc).allow
    # 非成员：deny
    assert not resolve(_p_no_membership(), doc).allow


def test_org_global_other_org_member_denied():
    doc = {"doc_id": "d1", "visibility": VIS_ORG, "org_id": "org-1"}
    outsider = Principal(principal_id="dave", org_roles={"org-99": "viewer"})
    assert not resolve(outsider, doc).allow


def test_org_global_missing_org_id_denied():
    """fail-closed：org-global 文档缺 org_id 一律 deny"""
    doc = {"doc_id": "d1", "visibility": VIS_ORG}
    assert not resolve(_p_org_member(), doc).allow
    assert "missing" in resolve(_p_org_member(), doc).reason


def test_project_shared_allows_project_member():
    doc = {"doc_id": "d1", "visibility": VIS_PROJECT, "project_id": "proj-1"}
    assert resolve(_p_project_member(), doc).allow
    assert not resolve(_p_no_membership(), doc).allow


def test_project_shared_allows_via_project_ids_list():
    """doc.project_ids 是多项目共享场景的扩展字段"""
    doc = {"doc_id": "d1", "visibility": VIS_PROJECT,
           "project_ids": ["proj-9", "proj-1"]}
    assert resolve(_p_project_member("proj-1"), doc).allow


def test_project_shared_wrong_project_denied():
    doc = {"doc_id": "d1", "visibility": VIS_PROJECT, "project_id": "proj-other"}
    assert not resolve(_p_project_member("proj-1"), doc).allow


def test_project_shared_missing_project_id_denied():
    doc = {"doc_id": "d1", "visibility": VIS_PROJECT}
    assert not resolve(_p_project_member(), doc).allow


def test_personal_only_owner_allowed():
    doc = {"doc_id": "d1", "visibility": VIS_PERSONAL, "owner_id": "alice"}
    alice = Principal(principal_id="alice")
    bob = Principal(principal_id="bob", org_roles={"org-1": "platform_admin"})
    assert resolve(alice, doc).allow
    assert not resolve(bob, doc).allow


def test_personal_missing_owner_denied():
    doc = {"doc_id": "d1", "visibility": VIS_PERSONAL}
    assert not resolve(Principal(principal_id="alice"), doc).allow


# ---------- platform_admin 规则（§7.1 末尾）----------

def test_platform_admin_cannot_read_personal():
    doc = {"doc_id": "d1", "visibility": VIS_PERSONAL, "owner_id": "pat"}
    assert not resolve(_p_platform_admin(), doc).allow


def test_platform_admin_can_read_org_global():
    doc = {"doc_id": "d1", "visibility": VIS_ORG, "org_id": "org-1"}
    assert resolve(_p_platform_admin(), doc).allow


def test_platform_admin_can_read_public_template():
    doc = {"doc_id": "d1", "visibility": VIS_PUBLIC}
    assert resolve(_p_platform_admin(), doc).allow


def test_platform_admin_without_flag_can_read_personal_as_owner():
    """非 platform_admin 的 owner 仍可读自己 personal"""
    doc = {"doc_id": "d1", "visibility": VIS_PERSONAL, "owner_id": "alice"}
    alice = Principal(principal_id="alice")
    assert not alice.is_platform_admin
    assert resolve(alice, doc).allow


# ---------- fail-closed ----------

def test_unknown_visibility_denied():
    doc = {"doc_id": "d1", "visibility": "secret-quadrant"}
    p = resolve(_p_platform_admin(), doc)
    assert not p.allow
    assert "unknown" in p.reason


def test_visibility_none_defaults_to_org_global():
    """缺省 visibility → 视作 organization-global（保守默认）"""
    doc = {"doc_id": "d1", "org_id": "org-1"}  # 无 visibility
    assert resolve(_p_org_member(), doc).allow
    assert not resolve(_p_no_membership(), doc).allow


def test_default_visibility_in_policy():
    p = resolve(_p_org_member(), {"doc_id": "d1", "org_id": "org-1"})
    assert p.visibility == VIS_ORG


# ---------- role 矩阵 ----------

def test_role_matrix_org():
    """5 类角色都能读 org-global（前提是同 org）。"""
    doc = {"doc_id": "d1", "visibility": VIS_ORG, "org_id": "org-1"}
    for role in ("viewer", "contributor", "reviewer", "project_admin", "platform_admin"):
        p = Principal(principal_id="u", org_roles={"org-1": role},
                      is_platform_admin=(role == "platform_admin"))
        assert resolve(p, doc).allow, f"role={role} should allow"


def test_role_matrix_project():
    doc = {"doc_id": "d1", "visibility": VIS_PROJECT, "project_id": "proj-1"}
    for role in ("viewer", "contributor", "reviewer", "project_admin"):
        p = Principal(principal_id="u",
                      org_roles={"org-1": "viewer"},
                      project_roles={"proj-1": role})
        assert resolve(p, doc).allow, f"role={role} should allow"


# ---------- 接口契约 ----------

def test_policy_as_dict_round_trip():
    doc = {"doc_id": "d1", "visibility": VIS_ORG, "org_id": "org-1"}
    p = resolve(_p_org_member(), doc)
    d = p.as_dict()
    assert d == {"allow": True, "reason": "org member", "visibility": VIS_ORG}


def test_pre_filter_splits_allowed_and_denied():
    docs = [
        {"doc_id": "d1", "visibility": VIS_PUBLIC},
        {"doc_id": "d2", "visibility": VIS_ORG, "org_id": "org-1"},
        {"doc_id": "d3", "visibility": VIS_ORG, "org_id": "org-99"},
        {"doc_id": "d4", "visibility": VIS_PERSONAL, "owner_id": "eve"},  # eve 不是 alice
    ]
    alice = Principal(principal_id="alice", org_roles={"org-1": "viewer"})
    allowed, denied = pre_filter(alice, docs)
    assert {d["doc_id"] for d in allowed} == {"d1", "d2"}
    assert {d["doc_id"] for d, _ in denied} == {"d3", "d4"}
    # denied 项均带 deny 原因
    for _, pol in denied:
        assert isinstance(pol, Policy)
        assert pol.allow is False


def test_pre_filter_empty_input():
    assert pre_filter(_p_org_member(), []) == ([], [])


def test_build_doc_predicate_matches_resolve():
    docs = [
        {"doc_id": "ok", "visibility": VIS_PUBLIC},
        {"doc_id": "deny", "visibility": VIS_PERSONAL, "owner_id": "x"},
    ]
    p = _p_no_membership()
    pred = build_doc_predicate(p)
    out = [d for d in docs if pred(d)]
    assert [d["doc_id"] for d in out] == ["ok"]


def test_local_user_default_has_no_memberships():
    p = Principal.local_user()
    assert p.principal_id == "local-user"
    assert p.org_roles == {}
    assert p.project_roles == {}
    assert not p.is_platform_admin
    # local_user 没有 memberships，所以默认看不到任何需 ACL 的 doc
    doc = {"doc_id": "d1", "visibility": VIS_ORG, "org_id": "org-1"}
    assert not resolve(p, doc).allow


def test_principal_has_org_role_helper():
    p = Principal(principal_id="u", org_roles={"org-1": "viewer"})
    assert p.has_org_role("org-1")
    assert p.has_org_role("org-1", "viewer")
    assert p.has_org_role("org-1", "viewer", "platform_admin")  # 任一即可
    assert not p.has_org_role("org-99")
    assert not p.has_org_role("org-1", "platform_admin")  # 不是 viewer


def test_principal_has_project_role_helper():
    p = Principal(principal_id="u",
                  org_roles={}, project_roles={"proj-1": "contributor"})
    assert p.has_project_role("proj-1")
    assert p.has_project_role("proj-1", "contributor", "reviewer")
    assert not p.has_project_role("proj-2")