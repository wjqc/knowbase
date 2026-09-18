"""P3-D：Document read 对象级授权（V2 计划 §7.2 末句）。

直接按 doc_id 读 doc 也走 ACL；防止拿到 doc_id 后绕过 recall 过滤。
覆盖：
  - 跨组织直读 deny
  - 跨项目直读 deny
  - personal 非 owner 直读 deny
  - personal owner 直读 allow
  - platform_admin 直读 personal deny
  - list_documents_for_principal 仅返回可见 doc
  - filter_doc_ids_by_principal 批量检查
  - check_doc_access 直接对 Document 对象判定
"""
from __future__ import annotations

from pathlib import Path

import pytest

from knowbase.v2.acl import (
    Principal,
    VIS_ORG,
    VIS_PERSONAL,
    VIS_PROJECT,
    VIS_PUBLIC,
    check_doc_access,
    filter_doc_ids_by_principal,
    get_document_for_principal,
    list_documents_for_principal,
)
from knowbase.v2.domain.models import (
    Document,
    KnowledgeKind,
    Organization,
    Principal as DomainPrincipal,
    PrincipalKind,
    Project,
    Source,
    SourceKind,
)
from knowbase.v2.repositories import V2Repository


@pytest.fixture
def repo(tmp_path: Path) -> V2Repository:
    r = V2Repository(tmp_path)
    yield r
    r.close()


def _seed(repo: V2Repository):
    """建立 org-a / org-b + 4 个用户 + 4 份 doc 的最小 ACL 场景。"""
    org_a = Organization.new("Org A", "org-a"); repo.upsert_organization(org_a)
    org_b = Organization.new("Org B", "org-b"); repo.upsert_organization(org_b)
    proj_a1 = Project.new(org_a.id, "Proj A1", "proj-a1"); repo.upsert_project(proj_a1)

    alice = DomainPrincipal(id="alice", display_name="alice", kind=PrincipalKind.USER); repo.upsert_principal(alice)
    bob = DomainPrincipal(id="bob", display_name="bob", kind=PrincipalKind.USER); repo.upsert_principal(bob)
    super_ = DomainPrincipal(id="super", display_name="super", kind=PrincipalKind.SERVICE); repo.upsert_principal(super_)
    eve = DomainPrincipal(id="eve", display_name="eve", kind=PrincipalKind.USER); repo.upsert_principal(eve)

    # alice 在 org-a 是 contributor + proj-a1 是 contributor
    from knowbase.v2.domain.models import Membership, ProjectMembership
    repo.upsert_membership(Membership.new(alice.id, org_a.id, "contributor"))
    repo.upsert_membership(Membership.new(super_.id, org_a.id, "platform_admin"))
    repo.upsert_membership(Membership.new(bob.id, org_b.id, "viewer"))
    repo.upsert_project_membership(ProjectMembership.new(alice.id, proj_a1.id, "contributor"))

    # 4 份 doc
    src = Source.from_locator(SourceKind.FILE, "/test")
    repo.upsert_source(src)
    src_id = src.stable_id
    docs = {
        "d-public": Document.new(src_id, "/p.md",
            kind=KnowledgeKind.PITFALL, title="public",
            visibility=VIS_PUBLIC),
        "d-org-a": Document.new(src_id, "/a.md",
            kind=KnowledgeKind.PITFALL, title="org-a",
            org_id=org_a.id, visibility=VIS_ORG),
        "d-org-b": Document.new(src_id, "/b.md",
            kind=KnowledgeKind.PITFALL, title="org-b",
            org_id=org_b.id, visibility=VIS_ORG),
        "d-proj-a1": Document.new(src_id, "/proj.md",
            kind=KnowledgeKind.PITFALL, title="proj-a1",
            org_id=org_a.id, project_id=proj_a1.id,
            owner_id=alice.id, visibility=VIS_PROJECT),
        "d-personal-alice": Document.new(src_id, "/alice.md",
            kind=KnowledgeKind.PITFALL, title="alice personal",
            owner_id=alice.id, visibility=VIS_PERSONAL),
        "d-personal-bob": Document.new(src_id, "/bob.md",
            kind=KnowledgeKind.PITFALL, title="bob personal",
            owner_id=bob.id, visibility=VIS_PERSONAL),
    }
    for d in docs.values():
        repo.upsert_document(d)
    return {"org_a": org_a, "org_b": org_b, "proj_a1": proj_a1,
            "alice": alice, "bob": bob, "super": super_, "eve": eve,
            "doc_ids": {k: v.id for k, v in docs.items()}}


# ---- get_document_for_principal ----

def test_get_document_public_returns_doc(repo):
    seed = _seed(repo)
    p = Principal("eve")  # 没有任何角色
    doc = get_document_for_principal(repo, p, seed["doc_ids"]["d-public"])
    assert doc is not None
    assert doc.title == "public"


def test_get_document_cross_org_denied(repo):
    """Alice 在 org-a，直读 org-b 文档应 deny。"""
    seed = _seed(repo)
    p = Principal("alice",
                  org_roles={seed["org_a"].id: "contributor"})
    doc = get_document_for_principal(repo, p, seed["doc_ids"]["d-org-b"])
    assert doc is None


def test_get_document_same_org_allowed(repo):
    seed = _seed(repo)
    p = Principal("alice",
                  org_roles={seed["org_a"].id: "contributor"})
    doc = get_document_for_principal(repo, p, seed["doc_ids"]["d-org-a"])
    assert doc is not None


def test_get_document_cross_project_denied(repo):
    """Alice 是 org-a contributor，没 proj role，直读 proj-shared 应 deny。"""
    seed = _seed(repo)
    p = Principal("alice",
                  org_roles={seed["org_a"].id: "contributor"})  # 没 proj role
    doc = get_document_for_principal(repo, p, seed["doc_ids"]["d-proj-a1"])
    assert doc is None


def test_get_document_project_member_allowed(repo):
    seed = _seed(repo)
    p = Principal("alice",
                  org_roles={seed["org_a"].id: "contributor"},
                  project_roles={seed["proj_a1"].id: "contributor"})
    doc = get_document_for_principal(repo, p, seed["doc_ids"]["d-proj-a1"])
    assert doc is not None


def test_get_document_personal_non_owner_denied(repo):
    """Bob 直读 alice 的 personal，应 deny。"""
    seed = _seed(repo)
    p = Principal("bob",
                  org_roles={seed["org_b"].id: "viewer"})
    doc = get_document_for_principal(repo, p, seed["doc_ids"]["d-personal-alice"])
    assert doc is None


def test_get_document_personal_owner_allowed(repo):
    seed = _seed(repo)
    p = Principal("alice",
                  org_roles={seed["org_a"].id: "contributor"},
                  project_roles={seed["proj_a1"].id: "contributor"})
    doc = get_document_for_principal(repo, p, seed["doc_ids"]["d-personal-alice"])
    assert doc is not None


def test_get_document_platform_admin_cannot_read_personal(repo):
    """platform_admin 显式 deny personal（§7.1 末尾）。"""
    seed = _seed(repo)
    p = Principal("super",
                  is_platform_admin=True,
                  org_roles={seed["org_a"].id: "platform_admin"})
    doc = get_document_for_principal(repo, p, seed["doc_ids"]["d-personal-alice"])
    assert doc is None


def test_get_document_platform_admin_can_read_org_global(repo):
    seed = _seed(repo)
    p = Principal("super",
                  is_platform_admin=True,
                  org_roles={seed["org_a"].id: "platform_admin"})
    doc = get_document_for_principal(repo, p, seed["doc_ids"]["d-org-a"])
    assert doc is not None


def test_get_document_platform_admin_can_read_project_shared(repo):
    seed = _seed(repo)
    p = Principal("super",
                  is_platform_admin=True,
                  org_roles={seed["org_a"].id: "platform_admin"})
    doc = get_document_for_principal(repo, p, seed["doc_ids"]["d-proj-a1"])
    assert doc is not None


def test_get_document_unknown_id_returns_none(repo):
    seed = _seed(repo)
    p = Principal("alice",
                  org_roles={seed["org_a"].id: "contributor"})
    assert get_document_for_principal(repo, p, "nonsense") is None


# ---- list_documents_for_principal ----

def test_list_documents_only_returns_visible(repo):
    """Alice 看 list：应有 public / org-a / proj-a1 / personal-alice；不含 org-b / personal-bob。"""
    seed = _seed(repo)
    p = Principal("alice",
                  org_roles={seed["org_a"].id: "contributor"},
                  project_roles={seed["proj_a1"].id: "contributor"})
    visible = list_documents_for_principal(repo, p)
    titles = {d.title for d in visible}
    assert "public" in titles
    assert "org-a" in titles
    assert "proj-a1" in titles
    assert "alice personal" in titles
    assert "org-b" not in titles
    assert "bob personal" not in titles


def test_list_documents_no_role_only_public(repo):
    seed = _seed(repo)
    p = Principal("eve")
    visible = list_documents_for_principal(repo, p)
    titles = {d.title for d in visible}
    assert titles == {"public"}


def test_list_documents_platform_admin_no_personal(repo):
    seed = _seed(repo)
    p = Principal("super",
                  is_platform_admin=True,
                  org_roles={seed["org_a"].id: "platform_admin"})
    visible = list_documents_for_principal(repo, p)
    titles = {d.title for d in visible}
    assert "alice personal" not in titles
    assert "bob personal" not in titles
    # org-a / proj-a1 / org-b 都可看（platform_admin 自动）
    assert {"public", "org-a", "org-b", "proj-a1"} <= titles


def test_list_documents_filters_by_source_id(repo):
    """source_id 透传：只列对应 source 的可见 doc。"""
    seed = _seed(repo)
    # 把 d-public 切换到另一个 source
    from knowbase.v2.domain.models import Source, SourceKind
    src2 = Source.from_locator(SourceKind.FILE, "/elsewhere.md"); repo.upsert_source(src2)
    other = Document.new(src2.stable_id, "/x.md",
        kind=KnowledgeKind.PITFALL, title="other",
        visibility=VIS_PUBLIC)
    repo.upsert_document(other)
    p = Principal("eve")
    visible = list_documents_for_principal(repo, p, source_id=src2.stable_id)
    assert {d.title for d in visible} == {"other"}


# ---- filter_doc_ids_by_principal ----

def test_filter_doc_ids_by_principal_returns_allowed(repo):
    seed = _seed(repo)
    p = Principal("alice",
                  org_roles={seed["org_a"].id: "contributor"},
                  project_roles={seed["proj_a1"].id: "contributor"})
    doc_ids = [seed["doc_ids"][k] for k in
               ("d-public", "d-org-a", "d-org-b", "d-personal-alice", "d-personal-bob")]
    allowed = filter_doc_ids_by_principal(repo, p, doc_ids)
    assert seed["doc_ids"]["d-public"] in allowed
    assert seed["doc_ids"]["d-org-a"] in allowed
    assert seed["doc_ids"]["d-personal-alice"] in allowed
    assert seed["doc_ids"]["d-org-b"] not in allowed
    assert seed["doc_ids"]["d-personal-bob"] not in allowed


def test_filter_doc_ids_skips_unknown(repo):
    seed = _seed(repo)
    p = Principal("eve")
    allowed = filter_doc_ids_by_principal(
        repo, p, ["nope", seed["doc_ids"]["d-public"]],
    )
    assert allowed == {seed["doc_ids"]["d-public"]}


def test_filter_doc_ids_empty_input(repo):
    seed = _seed(repo)
    p = Principal("alice",
                  org_roles={seed["org_a"].id: "contributor"})
    assert filter_doc_ids_by_principal(repo, p, []) == set()


# ---- check_doc_access ----

def test_check_doc_access_returns_policy(repo):
    seed = _seed(repo)
    doc = repo.get_document(seed["doc_ids"]["d-org-a"])
    p = Principal("alice",
                  org_roles={seed["org_a"].id: "contributor"})
    policy = check_doc_access(p, doc)
    assert policy.allow is True
    assert policy.visibility == VIS_ORG


def test_check_doc_access_deny(repo):
    seed = _seed(repo)
    doc = repo.get_document(seed["doc_ids"]["d-personal-alice"])
    p = Principal("eve")  # 不是 owner
    policy = check_doc_access(p, doc)
    assert policy.allow is False
    assert policy.visibility == VIS_PERSONAL
    assert policy.reason == "not owner"


# ---- fail-closed:DB 字段缺失 ----

def test_db_with_corrupted_visibility_denied(tmp_path):
    """DB 里 visibility 字段被改成无效值：直读应 deny（fail-closed）。"""
    r = V2Repository(tmp_path)
    try:
        seed = _seed(r)
        # 取已 seed 的 source_id（_seed 第一步已 upsert source）
        first_doc = r.get_document(next(iter(seed["doc_ids"].values())))
        doc = Document.new(first_doc.source_id, "/c.md",
            kind=KnowledgeKind.PITFALL, title="corrupt",
            org_id=seed["org_a"].id, visibility=VIS_ORG)
        r.upsert_document(doc)
        # 直接 SQL 注入坏值（绕过 upsert 校验）
        r._conn.execute("UPDATE document SET visibility=? WHERE id=?",
                        ("bogus", doc.id))
        p = Principal("alice",
                      org_roles={seed["org_a"].id: "contributor"})
        # 看不到（fail-closed）
        assert get_document_for_principal(r, p, doc.id) is None
    finally:
        r.close()


# ---- principal.from_db 集成 ----

def test_principal_from_db_supplies_correct_view(repo):
    """Principal.from_db 把 DB 角色加载到 acl.Principal；直读按加载后的角色判定。"""
    seed = _seed(repo)
    # 把 alice 加载成 acl.Principal
    acl_p = Principal.from_db(repo, seed["alice"].id)
    assert acl_p is not None
    # alice 有 org-a contributor + proj-a1 contributor
    assert acl_p.has_org_role(seed["org_a"].id)
    assert acl_p.has_project_role(seed["proj_a1"].id)
    # 直读个人 personal：可见
    doc = get_document_for_principal(repo, acl_p, seed["doc_ids"]["d-personal-alice"])
    assert doc is not None
    # 直读 org-b：不可见
    doc = get_document_for_principal(repo, acl_p, seed["doc_ids"]["d-org-b"])
    assert doc is None


def test_principal_from_db_returns_none_for_unknown(repo):
    assert Principal.from_db(repo, "ghost") is None