"""P3-C：HybridSearch 集成 ACL 前置 filter。

覆盖：
  - principal 过滤跨组织 / 跨项目 / personal 非 owner / platform_admin 看 personal
  - acl_predicate 路径
  - backward compat：None 时行为不变
  - HybridResult 4 字段（principal_id / acl_enabled / acl_denied / acl_denied_ids）
  - fail-closed 渠道：channel 拿不到无权 doc
"""
from __future__ import annotations

import pytest

from knowbase.v2.acl import (
    Principal,
    VIS_ORG,
    VIS_PERSONAL,
    VIS_PROJECT,
    VIS_PUBLIC,
    pre_filter,
)
from knowbase.v2.retrieval.hybrid_search import HybridSearch


# ---- fixtures ----

def _doc(doc_id: str, *, title: str, body: str,
         org_id: str | None = None,
         project_id: str | None = None,
         owner_id: str | None = None,
         visibility: str = VIS_ORG,
         tags: list[str] | None = None) -> dict:
    return {
        "doc_id": doc_id,
        "title": title,
        "body": body,
        "path": f"/notes/{doc_id}.md",
        "kind": "note",
        "tags": tags or [],
        "org_id": org_id,
        "project_id": project_id,
        "owner_id": owner_id,
        "visibility": visibility,
    }


@pytest.fixture
def org_a_docs() -> list[dict]:
    """Org A：Alice 是 contributor，Bob 是 viewer。"""
    return [
        _doc("d-public", title="公共模板",
             body="public template body 关键词 alpha",
             visibility=VIS_PUBLIC),
        _doc("d-org-a", title="Org A 内部",
             body="org A only body 关键词 alpha",
             org_id="org-a", visibility=VIS_ORG),
        _doc("d-org-b", title="Org B 内部",
             body="org B only body 关键词 alpha",
             org_id="org-b", visibility=VIS_ORG),
        _doc("d-proj-a1", title="Proj A1 共享",
             body="proj a1 body 关键词 alpha",
             org_id="org-a", project_id="proj-a1",
             owner_id="alice", visibility=VIS_PROJECT),
        _doc("d-proj-a2", title="Proj A2 共享",
             body="proj a2 body 关键词 alpha",
             org_id="org-a", project_id="proj-a2",
             owner_id="alice", visibility=VIS_PROJECT),
        _doc("d-personal-alice", title="Alice 个人笔记",
             body="alice personal 关键词 alpha",
             owner_id="alice", visibility=VIS_PERSONAL),
        _doc("d-personal-bob", title="Bob 个人笔记",
             body="bob personal 关键词 alpha",
             owner_id="bob", visibility=VIS_PERSONAL),
    ]


def _principal(principal_id: str, **kwargs) -> Principal:
    return Principal(principal_id=principal_id, **kwargs)


# ---- backward compat ----

def test_without_principal_keeps_backward_compat(org_a_docs):
    """None 时：所有 doc 都参与召回（V2 计划 §6 向后兼容）。"""
    hs = HybridSearch(docs=org_a_docs)
    res = hs.search("alpha", top_k=10)
    ids = {h.doc_id for h in res.hits}
    # public / org-a / org-b / proj-a1 / proj-a2 / personal-alice / personal-bob 都应命中
    assert {"d-public", "d-org-a", "d-org-b",
            "d-proj-a1", "d-proj-a2",
            "d-personal-alice", "d-personal-bob"} <= ids
    assert res.principal_id is None
    assert res.acl_enabled is False
    assert res.acl_denied == 0
    assert res.acl_denied_ids == []


def test_self_docs_is_unfiltered_when_principal_none(org_a_docs):
    hs = HybridSearch(docs=org_a_docs)
    assert len(hs.docs) == len(org_a_docs)
    assert hs.acl_denied_ids == []


# ---- principal 启用：跨组织 / 跨项目 / personal 非 owner ----

def test_principal_org_member_only_sees_org_docs(org_a_docs):
    """Alice 是 org-a contributor + proj-a1/proj-a2 contributor：可见 org-a + 自己 proj + 自己 personal。"""
    p = _principal("alice",
                   org_roles={"org-a": "contributor"},
                   project_roles={"proj-a1": "contributor", "proj-a2": "contributor"})
    hs = HybridSearch(docs=org_a_docs, principal=p)
    res = hs.search("alpha", top_k=10)
    ids = {h.doc_id for h in res.hits}
    # 应可见：public, org-a, proj-a1, proj-a2, personal-alice
    assert "d-public" in ids
    assert "d-org-a" in ids
    assert "d-proj-a1" in ids
    assert "d-proj-a2" in ids
    assert "d-personal-alice" in ids
    # 不可见
    assert "d-org-b" not in ids
    assert "d-personal-bob" not in ids


def test_principal_org_member_records_denied_ids(org_a_docs):
    p = _principal("alice",
                   org_roles={"org-a": "contributor"},
                   project_roles={"proj-a1": "contributor", "proj-a2": "contributor"})
    hs = HybridSearch(docs=org_a_docs, principal=p)
    res = hs.search("alpha", top_k=10)
    assert res.acl_enabled is True
    assert res.principal_id == "alice"
    assert res.acl_denied == 2
    assert set(res.acl_denied_ids) == {"d-org-b", "d-personal-bob"}


def test_principal_without_org_role_sees_only_public_and_personal(org_a_docs):
    """无任何 org 角色：仅 public 可见；personal 必须 owner 才可见。"""
    p = _principal("eve")
    hs = HybridSearch(docs=org_a_docs, principal=p)
    res = hs.search("alpha", top_k=10)
    ids = {h.doc_id for h in res.hits}
    assert ids == {"d-public"}


def test_personal_owner_sees_own_personal(org_a_docs):
    p = _principal("alice", org_roles={"org-a": "contributor"})
    hs = HybridSearch(docs=org_a_docs, principal=p)
    res = hs.search("alpha", top_k=10)
    assert "d-personal-alice" in {h.doc_id for h in res.hits}


def test_non_owner_cannot_see_personal(org_a_docs):
    """Bob 同在 org-a，但 alice 的 personal 应不可见。"""
    p = _principal("bob", org_roles={"org-a": "viewer"})
    hs = HybridSearch(docs=org_a_docs, principal=p)
    res = hs.search("alpha", top_k=10)
    ids = {h.doc_id for h in res.hits}
    assert "d-personal-alice" not in ids
    assert "d-personal-bob" in ids  # 自己的 personal 可见


def test_platform_admin_cannot_read_personal(org_a_docs):
    """platform_admin 显式规则：personal 不可读（§7.1）。"""
    p = _principal("super", is_platform_admin=True,
                    org_roles={"org-a": "platform_admin"})
    hs = HybridSearch(docs=org_a_docs, principal=p)
    res = hs.search("alpha", top_k=10)
    ids = {h.doc_id for h in res.hits}
    assert "d-personal-alice" not in ids
    assert "d-personal-bob" not in ids
    # public / org-a / proj-* 可见
    assert {"d-public", "d-org-a", "d-proj-a1", "d-proj-a2"} <= ids


def test_platform_admin_sees_org_a(org_a_docs):
    p = _principal("super", is_platform_admin=True,
                    org_roles={"org-a": "platform_admin"})
    hs = HybridSearch(docs=org_a_docs, principal=p)
    res = hs.search("alpha", top_k=10)
    ids = {h.doc_id for h in res.hits}
    assert "d-org-a" in ids


# ---- project 级别过滤 ----

def test_project_member_only_sees_own_project_shared(org_a_docs):
    """Charlie 只在 proj-a1；不应看到 proj-a2。"""
    p = _principal("charlie",
                   org_roles={"org-a": "contributor"},
                   project_roles={"proj-a1": "contributor"})
    hs = HybridSearch(docs=org_a_docs, principal=p)
    res = hs.search("alpha", top_k=10)
    ids = {h.doc_id for h in res.hits}
    assert "d-proj-a1" in ids
    assert "d-proj-a2" not in ids


def test_org_member_without_project_role_sees_org_global_not_project_shared(org_a_docs):
    """org-a 成员但不在任何 proj：可见 d-org-a / d-public，但 proj-shared 不可见。"""
    p = _principal("dave", org_roles={"org-a": "viewer"})
    hs = HybridSearch(docs=org_a_docs, principal=p)
    res = hs.search("alpha", top_k=10)
    ids = {h.doc_id for h in res.hits}
    assert "d-org-a" in ids
    assert "d-public" in ids
    assert "d-proj-a1" not in ids  # 没有 project 角色
    assert "d-proj-a2" not in ids
    # personal 不可见（不是 owner）
    assert "d-personal-alice" not in ids


# ---- fail-closed 渠道 ----

def test_acl_filter_runs_before_channels(org_a_docs):
    """channel 完全拿不到无权 doc（§7.2）。"""
    p = _principal("alice", org_roles={"org-a": "contributor"})
    hs = HybridSearch(docs=org_a_docs, principal=p)
    # 内部 channels 的 docs 应当是 allowed，不含 d-org-b / d-personal-bob
    for ch in hs.channels:
        channel_doc_ids = {d["doc_id"] for d in ch.docs if "doc_id" in d}
        assert "d-org-b" not in channel_doc_ids, f"{ch.name} leaks org-b"
        assert "d-personal-bob" not in channel_doc_ids, f"{ch.name} leaks personal-bob"


def test_acl_filtered_doc_index(org_a_docs):
    p = _principal("alice", org_roles={"org-a": "contributor"})
    hs = HybridSearch(docs=org_a_docs, principal=p)
    assert "d-org-b" not in hs.doc_index
    assert "d-personal-bob" not in hs.doc_index
    assert "d-org-a" in hs.doc_index


# ---- acl_predicate 路径 ----

def test_acl_predicate_filters(org_a_docs):
    """acl_predicate 与 principal 二选一时，acl_predicate 优先。"""
    # 只允许 d-public
    pred = lambda d: d.get("visibility") == VIS_PUBLIC  # noqa: E731
    hs = HybridSearch(docs=org_a_docs, acl_predicate=pred)
    res = hs.search("alpha", top_k=10)
    ids = {h.doc_id for h in res.hits}
    assert ids == {"d-public"}
    assert res.acl_enabled is True
    assert res.acl_denied == 6


def test_acl_predicate_overrides_principal(org_a_docs):
    pred = lambda d: d.get("visibility") == VIS_PUBLIC  # noqa: E731
    p = _principal("alice", org_roles={"org-a": "contributor"})
    hs = HybridSearch(docs=org_a_docs, principal=p, acl_predicate=pred)
    # principal 不影响 channels（acl_predicate 优先）
    res = hs.search("alpha", top_k=10)
    ids = {h.doc_id for h in res.hits}
    assert ids == {"d-public"}


# ---- HybridResult 字段契约 ----

def test_hybrid_result_acl_fields_default():
    """无 ACL 时：principal_id=None, acl_enabled=False, acl_denied=0。"""
    hs = HybridSearch(docs=[_doc("d1", title="t", body="b alpha", visibility=VIS_PUBLIC)])
    res = hs.search("alpha")
    assert res.principal_id is None
    assert res.acl_enabled is False
    assert res.acl_denied == 0
    assert res.acl_denied_ids == []


def test_hybrid_result_acl_fields_with_principal():
    p = _principal("alice", org_roles={"org-a": "contributor"})
    docs = [
        _doc("d-public", title="t", body="b alpha", visibility=VIS_PUBLIC),
        _doc("d-org-a", title="t", body="b alpha", org_id="org-a", visibility=VIS_ORG),
        _doc("d-org-b", title="t", body="b alpha", org_id="org-b", visibility=VIS_ORG),
    ]
    hs = HybridSearch(docs=docs, principal=p)
    res = hs.search("alpha")
    assert res.principal_id == "alice"
    assert res.acl_enabled is True
    assert res.acl_denied == 1
    assert res.acl_denied_ids == ["d-org-b"]


# ---- pre_filter 接口一致性 ----

def test_pre_filter_integration_matches_internal(org_a_docs):
    """HybridSearch 内部用的 pre_filter 与外部调用应一致。"""
    p = _principal("alice", org_roles={"org-a": "contributor"})
    allowed_ext, denied_ext = pre_filter(p, org_a_docs)
    hs = HybridSearch(docs=org_a_docs, principal=p)
    allowed_ids_int = {d["doc_id"] for d in hs.docs}
    denied_ids_int = set(hs.acl_denied_ids)
    assert allowed_ids_int == {d["doc_id"] for d in allowed_ext}
    assert denied_ids_int == {d["doc_id"] for d, _ in denied_ext}


# ---- fail-closed 行为透传 ----

def test_fail_closed_propagates_to_search_result():
    """visibility 未知 / 缺字段 -> deny。"""
    p = _principal("alice", org_roles={"org-a": "contributor"})
    docs = [
        _doc("d1", title="t", body="alpha", visibility="bogus"),
        _doc("d2", title="t", body="alpha", visibility=VIS_ORG),  # 缺 org_id
    ]
    hs = HybridSearch(docs=docs, principal=p)
    res = hs.search("alpha", top_k=10)
    assert res.hits == []
    assert res.acl_denied == 2


def test_search_returns_empty_when_all_denied():
    p = _principal("eve")  # 无任何角色
    docs = [
        _doc("d1", title="t", body="alpha", org_id="org-a", visibility=VIS_ORG),
    ]
    hs = HybridSearch(docs=docs, principal=p)
    res = hs.search("alpha")
    assert res.hits == []
    assert res.acl_denied == 1
    assert res.acl_denied_ids == ["d1"]