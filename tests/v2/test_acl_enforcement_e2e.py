"""P3-E：ACL 越权 E2E 测试（V2 计划 §7.2 五类拦截 + 泄露率 = 0）。

类别：
  1. 跨组织越权
  2. 跨项目越权
  3. 直接 doc_id 越权
  4. 缓存污染：channel 索引或 staging 列表混入无权 doc，二次访问仍 deny
  5. staging 状态绕过：即使 status=STAGING，ACL 仍按 visibility 拒绝

跨入口验证：HybridSearch / get_document_for_principal / list_documents_for_principal
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
    get_document_for_principal,
    list_documents_for_principal,
)
from knowbase.v2.domain.models import (
    Document,
    DocumentStatus,
    KnowledgeKind,
    Membership,
    Organization,
    Principal as DomainPrincipal,
    PrincipalKind,
    Project,
    ProjectMembership,
    Source,
    SourceKind,
)
from knowbase.v2.repositories import V2Repository
from knowbase.v2.retrieval.hybrid_search import HybridSearch


# ---- fixtures ----

@pytest.fixture
def repo(tmp_path: Path) -> V2Repository:
    r = V2Repository(tmp_path)
    yield r
    r.close()


def _build_world(repo: V2Repository) -> dict:
    """org-a + org-b + proj-a1 + 4 用户 + 6 doc（含 staging）。"""
    org_a = Organization(id="org-a", name="Org A", slug="org-a"); repo.upsert_organization(org_a)
    org_b = Organization(id="org-b", name="Org B", slug="org-b"); repo.upsert_organization(org_b)
    proj_a1 = Project(id="proj-a1", org_id=org_a.id, name="Proj A1", slug="proj-a1")
    repo.upsert_project(proj_a1)

    alice = DomainPrincipal(id="alice", display_name="alice", kind=PrincipalKind.USER); repo.upsert_principal(alice)
    bob = DomainPrincipal(id="bob", display_name="bob", kind=PrincipalKind.USER); repo.upsert_principal(bob)
    super_ = DomainPrincipal(id="super", display_name="super", kind=PrincipalKind.SERVICE); repo.upsert_principal(super_)
    eve = DomainPrincipal(id="eve", display_name="eve", kind=PrincipalKind.USER); repo.upsert_principal(eve)

    repo.upsert_membership(Membership.new(alice.id, org_a.id, "contributor"))
    repo.upsert_membership(Membership.new(super_.id, org_a.id, "platform_admin"))
    repo.upsert_membership(Membership.new(bob.id, org_b.id, "viewer"))
    repo.upsert_project_membership(ProjectMembership.new(alice.id, proj_a1.id, "contributor"))

    src = Source.from_locator(SourceKind.FILE, "/e2e")
    repo.upsert_source(src)
    src_id = src.stable_id

    docs_by_key = {
        "d-public": Document.new(src_id, "/p.md", kind=KnowledgeKind.PITFALL,
            title="public", visibility=VIS_PUBLIC),
        "d-org-a": Document.new(src_id, "/a.md", kind=KnowledgeKind.PITFALL,
            title="org-a", org_id=org_a.id, visibility=VIS_ORG),
        "d-org-b": Document.new(src_id, "/b.md", kind=KnowledgeKind.PITFALL,
            title="org-b", org_id=org_b.id, visibility=VIS_ORG),
        "d-proj-a1": Document.new(src_id, "/proj.md", kind=KnowledgeKind.PITFALL,
            title="proj-a1", org_id=org_a.id, project_id=proj_a1.id,
            owner_id=alice.id, visibility=VIS_PROJECT),
        "d-personal-alice": Document.new(src_id, "/alice.md", kind=KnowledgeKind.PITFALL,
            title="alice personal", owner_id=alice.id, visibility=VIS_PERSONAL),
        "d-personal-bob": Document.new(src_id, "/bob.md", kind=KnowledgeKind.PITFALL,
            title="bob personal", owner_id=bob.id, visibility=VIS_PERSONAL),
        # staging 状态的 cross-org 文档（试图以未发布为借口绕过 ACL）
        "d-staging-org-b": Document.new(src_id, "/staging.md", kind=KnowledgeKind.PITFALL,
            title="staging org-b", org_id=org_b.id, visibility=VIS_ORG),
    }
    for d in docs_by_key.values():
        repo.upsert_document(d)
    # 把 d-staging-org-b 显式置 STORED（模拟 staging）
    repo.update_document_status(docs_by_key["d-staging-org-b"].id, DocumentStatus.STORED)
    return {"org_a": org_a, "org_b": org_b, "proj_a1": proj_a1,
            "alice": alice, "bob": bob, "super": super_, "eve": eve,
            "doc_ids": {k: v.id for k, v in docs_by_key.items()}}


def _hs_seed_dicts(repo: V2Repository, doc_ids: dict[str, str]) -> list[dict]:
    """把 DB doc 翻译成 HybridSearch 用的 dict（visibility 4 字段透传）。"""
    out = []
    for short, did in doc_ids.items():
        d = repo.get_document(did)
        out.append({
            "doc_id": d.id,
            "title": d.title,
            "body": f"body of {d.title} 关键词 alpha",
            "path": d.path,
            "kind": d.kind.value if d.kind else "",
            "tags": [],
            "org_id": d.org_id,
            "project_id": d.project_id,
            "owner_id": d.owner_id,
            "visibility": d.visibility,
        })
    return out


# ---- 类别 1：跨组织越权（HybridSearch + 直读 + list）----

def test_category1_cross_org_hybrid_search_blocks(repo):
    seed = _build_world(repo)
    p = Principal("alice", org_roles={"org-a": "contributor"},
                   project_roles={"proj-a1": "contributor"})
    docs = _hs_seed_dicts(repo, seed["doc_ids"])
    hs = HybridSearch(docs=docs, principal=p)
    res = hs.search("alpha", top_k=20)
    ids = {h.doc_id for h in res.hits}
    assert seed["doc_ids"]["d-org-b"] not in ids
    assert seed["doc_ids"]["d-staging-org-b"] not in ids


def test_category1_cross_org_direct_read_blocks(repo):
    seed = _build_world(repo)
    p = Principal("alice", org_roles={"org-a": "contributor"},
                   project_roles={"proj-a1": "contributor"})
    assert get_document_for_principal(repo, p, seed["doc_ids"]["d-org-b"]) is None


def test_category1_cross_org_list_blocks(repo):
    seed = _build_world(repo)
    p = Principal("alice", org_roles={"org-a": "contributor"},
                   project_roles={"proj-a1": "contributor"})
    visible = list_documents_for_principal(repo, p)
    ids = {d.id for d in visible}
    assert seed["doc_ids"]["d-org-b"] not in ids
    assert seed["doc_ids"]["d-staging-org-b"] not in ids


# ---- 类别 2：跨项目越权 ----

def test_category2_cross_project_hybrid_search_blocks(repo):
    """alice 没 proj-a1 之外的 project role，但给她 project role 后，d-personal-bob 仍 deny。"""
    seed = _build_world(repo)
    p = Principal("alice", org_roles={"org-a": "contributor"},
                   project_roles={"proj-a1": "contributor"})
    docs = _hs_seed_dicts(repo, seed["doc_ids"])
    hs = HybridSearch(docs=docs, principal=p)
    res = hs.search("alpha", top_k=20)
    ids = {h.doc_id for h in res.hits}
    # proj-a1 可见
    assert seed["doc_ids"]["d-proj-a1"] in ids
    # bob personal 不可见
    assert seed["doc_ids"]["d-personal-bob"] not in ids


def test_category2_no_project_role_blocks_project_shared(repo):
    seed = _build_world(repo)
    # eve 没有 project 角色，且不是 alice
    p = Principal("eve")
    docs = _hs_seed_dicts(repo, seed["doc_ids"])
    hs = HybridSearch(docs=docs, principal=p)
    res = hs.search("alpha", top_k=20)
    ids = {h.doc_id for h in res.hits}
    assert seed["doc_ids"]["d-proj-a1"] not in ids


# ---- 类别 3：直接 doc_id 越权 ----

def test_category3_unknown_principal_cannot_read_anything_except_public(repo):
    """eve 没有任何角色：所有 visibility 直读都 deny，只 public allow。"""
    seed = _build_world(repo)
    p = Principal("eve")
    for k in ("d-org-a", "d-org-b", "d-proj-a1", "d-personal-alice", "d-personal-bob",
              "d-staging-org-b"):
        assert get_document_for_principal(repo, p, seed["doc_ids"][k]) is None
    assert get_document_for_principal(repo, p, seed["doc_ids"]["d-public"]) is not None


def test_category3_random_uuid_returns_none(repo):
    seed = _build_world(repo)
    p = Principal("alice", org_roles={"org-a": "contributor"})
    assert get_document_for_principal(repo, p, "deadbeef" * 4) is None


# ---- 类别 4：缓存污染 ----

def test_category4_hybrid_search_re_filter_each_construction(repo):
    """HybridSearch 每次构造都重新评估 ACL，不存在"缓存污染"。
    第二次以不同 principal 构造，channel 完全用新 principal 过滤。
    """
    seed = _build_world(repo)
    docs = _hs_seed_dicts(repo, seed["doc_ids"])

    # 第一次：alice 看 org-a + proj-a1 + personal-alice
    p_alice = Principal("alice", org_roles={"org-a": "contributor"},
                        project_roles={"proj-a1": "contributor"})
    hs1 = HybridSearch(docs=docs, principal=p_alice)
    res1 = hs1.search("alpha", top_k=20)
    ids1 = {h.doc_id for h in res1.hits}
    assert seed["doc_ids"]["d-org-a"] in ids1
    assert seed["doc_ids"]["d-org-b"] not in ids1

    # 第二次：bob 看 org-b + personal-bob；alice 的内容不能泄露
    p_bob = Principal("bob", org_roles={"org-b": "viewer"})
    hs2 = HybridSearch(docs=docs, principal=p_bob)
    res2 = hs2.search("alpha", top_k=20)
    ids2 = {h.doc_id for h in res2.hits}
    assert seed["doc_ids"]["d-org-b"] in ids2
    assert seed["doc_ids"]["d-org-a"] not in ids2
    assert seed["doc_ids"]["d-personal-alice"] not in ids2


def test_category4_filter_doc_ids_re_eval_each_call(repo):
    """filter_doc_ids_by_principal 不缓存；不同 principal 调应得不同结果。"""
    from knowbase.v2.acl import filter_doc_ids_by_principal
    seed = _build_world(repo)
    ids = [seed["doc_ids"][k] for k in
           ("d-org-a", "d-org-b", "d-personal-alice", "d-personal-bob")]
    # alice 视角
    p_alice = Principal("alice", org_roles={"org-a": "contributor"})
    allowed_alice = filter_doc_ids_by_principal(repo, p_alice, ids)
    assert seed["doc_ids"]["d-org-a"] in allowed_alice
    assert seed["doc_ids"]["d-personal-alice"] in allowed_alice
    # bob 视角
    p_bob = Principal("bob", org_roles={"org-b": "viewer"})
    allowed_bob = filter_doc_ids_by_principal(repo, p_bob, ids)
    assert seed["doc_ids"]["d-org-b"] in allowed_bob
    assert seed["doc_ids"]["d-personal-bob"] in allowed_bob
    # alice 不能看到 org-b
    assert seed["doc_ids"]["d-org-b"] not in allowed_alice
    # bob 不能看到 org-a
    assert seed["doc_ids"]["d-org-a"] not in allowed_bob


# ---- 类别 5：staging 状态绕过 ----

def test_category5_staging_status_does_not_bypass_acl(repo):
    """status=STORED（视为 staging）的跨 org doc，alice 不能读。"""
    seed = _build_world(repo)
    p = Principal("alice", org_roles={"org-a": "contributor"})
    assert get_document_for_principal(repo, p, seed["doc_ids"]["d-staging-org-b"]) is None


def test_category5_staging_status_hybrid_search_blocks(repo):
    seed = _build_world(repo)
    p = Principal("alice", org_roles={"org-a": "contributor"})
    docs = _hs_seed_dicts(repo, seed["doc_ids"])
    hs = HybridSearch(docs=docs, principal=p)
    res = hs.search("alpha", top_k=20)
    ids = {h.doc_id for h in res.hits}
    assert seed["doc_ids"]["d-staging-org-b"] not in ids


# ---- 零泄露率断言 ----

def test_zero_leakage_for_eve_against_all_docs(repo):
    """eve 在所有 visibility 下：直读 + 召回 + list 都应零泄露（仅 public）。"""
    seed = _build_world(repo)
    p = Principal("eve")
    docs = _hs_seed_dicts(repo, seed["doc_ids"])
    hs = HybridSearch(docs=docs, principal=p)
    res = hs.search("alpha", top_k=20)
    leaked = {h.doc_id for h in res.hits}
    # 只允许 d-public 可见
    assert leaked <= {seed["doc_ids"]["d-public"]}
    # list 也只 public
    visible = list_documents_for_principal(repo, p)
    assert {d.id for d in visible} <= {seed["doc_ids"]["d-public"]}
    # 直读也只 public
    for k in ("d-org-a", "d-org-b", "d-proj-a1", "d-personal-alice",
              "d-personal-bob", "d-staging-org-b"):
        assert get_document_for_principal(repo, p, seed["doc_ids"][k]) is None


def test_zero_leakage_platform_admin_personal(repo):
    """platform_admin 对所有 personal doc：直读 + 召回 + list 全部 deny。"""
    seed = _build_world(repo)
    p = Principal("super", is_platform_admin=True,
                  org_roles={"org-a": "platform_admin"})
    docs = _hs_seed_dicts(repo, seed["doc_ids"])
    hs = HybridSearch(docs=docs, principal=p)
    res = hs.search("alpha", top_k=20)
    ids = {h.doc_id for h in res.hits}
    assert seed["doc_ids"]["d-personal-alice"] not in ids
    assert seed["doc_ids"]["d-personal-bob"] not in ids
    # list
    visible = list_documents_for_principal(repo, p)
    titles = {d.title for d in visible}
    assert "alice personal" not in titles
    assert "bob personal" not in titles
    # 直读
    assert get_document_for_principal(repo, p, seed["doc_ids"]["d-personal-alice"]) is None
    assert get_document_for_principal(repo, p, seed["doc_ids"]["d-personal-bob"]) is None


# ---- fail-closed：缺字段 / 非法值 ----

def test_fail_closed_visibility_null_denied(repo):
    seed = _build_world(repo)
    # 把 d-public 的 visibility 改成 NULL（模拟数据损坏）
    repo._conn.execute("UPDATE document SET visibility=NULL WHERE id=?",
                       (seed["doc_ids"]["d-public"],))
    p = Principal("alice", org_roles={"org-a": "contributor"})
    assert get_document_for_principal(repo, p, seed["doc_ids"]["d-public"]) is None


def test_fail_closed_unknown_visibility_denied(repo):
    seed = _build_world(repo)
    repo._conn.execute("UPDATE document SET visibility=? WHERE id=?",
                       ("forged-visibility", seed["doc_ids"]["d-public"]))
    p = Principal("alice", org_roles={"org-a": "contributor"})
    assert get_document_for_principal(repo, p, seed["doc_ids"]["d-public"]) is None


def test_fail_closed_org_doc_missing_org_id_denied(repo):
    """organization-global 但缺 org_id：fail-closed deny。"""
    seed = _build_world(repo)
    repo._conn.execute("UPDATE document SET org_id=NULL WHERE id=?",
                       (seed["doc_ids"]["d-org-a"],))
    p = Principal("alice", org_roles={"org-a": "contributor"})
    assert get_document_for_principal(repo, p, seed["doc_ids"]["d-org-a"]) is None


def test_fail_closed_project_doc_missing_project_id_denied(repo):
    """project-shared 但缺 project_id：fail-closed deny。"""
    seed = _build_world(repo)
    repo._conn.execute("UPDATE document SET project_id=NULL WHERE id=?",
                       (seed["doc_ids"]["d-proj-a1"],))
    p = Principal("alice", org_roles={"org-a": "contributor"},
                  project_roles={"proj-a1": "contributor"})
    assert get_document_for_principal(repo, p, seed["doc_ids"]["d-proj-a1"]) is None


def test_fail_closed_personal_missing_owner_denied(repo):
    """personal 但缺 owner_id：fail-closed deny（连 owner 自己也看不到）。"""
    seed = _build_world(repo)
    repo._conn.execute("UPDATE document SET owner_id=NULL WHERE id=?",
                       (seed["doc_ids"]["d-personal-alice"],))
    p = Principal("alice", org_roles={"org-a": "contributor"})
    assert get_document_for_principal(repo, p, seed["doc_ids"]["d-personal-alice"]) is None


# ---- 跨入口泄露率统一断言 ----

@pytest.mark.parametrize("visibility,other_role_key,principal", [
    (VIS_ORG, "d-org-b", Principal("alice", org_roles={"org-a": "contributor"})),
    (VIS_PROJECT, "d-proj-a1",
        Principal("alice", org_roles={"org-a": "contributor"})),  # 无 proj role
    (VIS_PERSONAL, "d-personal-alice", Principal("eve")),  # 不是 owner
])
def test_no_leakage_across_all_entries_for_each_visibility(repo, visibility, other_role_key, principal):
    """同一 visibility 的无权 doc：3 个入口（HybridSearch / get / list）全部 deny。"""
    seed = _build_world(repo)
    target = seed["doc_ids"][other_role_key]
    docs = _hs_seed_dicts(repo, seed["doc_ids"])
    hs = HybridSearch(docs=docs, principal=principal)
    res = hs.search("alpha", top_k=20)
    leaked_hs = target in {h.doc_id for h in res.hits}
    leaked_get = get_document_for_principal(repo, principal, target) is not None
    leaked_list = target in {d.id for d in list_documents_for_principal(repo, principal)}
    assert not leaked_hs, f"HybridSearch leaked {visibility} {target}"
    assert not leaked_get, f"get_document leaked {visibility} {target}"
    assert not leaked_list, f"list_documents leaked {visibility} {target}"