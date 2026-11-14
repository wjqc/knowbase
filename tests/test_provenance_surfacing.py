"""出处透出回归（B1）：卡片 source_refs 进 memory_read / memory_search / memory_context。

此前卡链路检索/读取响应零出处：memory_read 只渲染 code_refs，
frontmatter 里已有的 source_refs 从不透出。本文件钉住三个入口的
透出行为与 sources.describe_source_ref 的降级格式化。
"""

from pathlib import Path

from knowbase import index, sources, store


def _write_card(repo: Path, mid: str, *, source_refs=None) -> Path:
    meta = store.new_meta("pitfall", "出处回归卡", "global", [], "pytest")
    meta["id"] = mid
    if source_refs is not None:
        meta["source_refs"] = source_refs
    path = store.mem_path(repo, "pitfall", mid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(store.render(meta, "正文内容。"), encoding="utf-8")
    index.rebuild(repo)
    return path


def test_read_renders_source_refs(memory_repo):
    _write_card(memory_repo, "P-2026-3001", source_refs=[
        {"source_id": "SRC-aaa", "sha256": "a" * 64,
         "locator": {"type": "line", "start_line": 1, "end_line": 3}},
        {"source_id": "SRC-bbb"},
    ])
    from knowbase.server import read_impl
    out = read_impl("P-2026-3001")
    assert "出处: knowbase-source:SRC-aaa@" + "a" * 64 + "#L1-L3；knowbase-source:SRC-bbb" in out


def test_read_without_refs_keeps_head_clean(memory_repo):
    _write_card(memory_repo, "P-2026-3002")
    from knowbase.server import read_impl
    assert "出处:" not in read_impl("P-2026-3002")


def test_read_ref_to_unknown_snapshot_still_renders(memory_repo):
    # 引用指向不存在的快照：纯格式化照实显示，不抛错、不隐匿
    _write_card(memory_repo, "P-2026-3003", source_refs=[
        {"source_id": "SRC-gone", "sha256": "f" * 64,
         "locator": {"type": "line", "start_line": 9}},
    ])
    from knowbase.server import read_impl
    assert "knowbase-source:SRC-gone@" + "f" * 64 + "#L9" in read_impl("P-2026-3003")


def test_search_marks_ref_count_only_for_ref_cards(memory_repo):
    _write_card(memory_repo, "P-2026-3004",
                source_refs=[{"source_id": "SRC-a1"}, {"source_id": "SRC-a2"}])
    _write_card(memory_repo, "P-2026-3005")  # 无 source_refs
    from knowbase.server import search_impl
    out = search_impl("出处回归卡", scope="global", limit=5)
    assert "refs=2" in out
    assert "refs=0" not in out
    for line in out.splitlines():
        if line.startswith("- [P-2026-3005]"):
            assert "refs=" not in line
        if line.startswith("- [P-2026-3004]"):
            assert "refs=2" in line


def test_context_candidate_carries_compact_refs(memory_repo):
    _write_card(memory_repo, "P-2026-3006", source_refs=[
        {"source_id": "SRC-c1", "sha256": "b" * 64,
         "locator": {"type": "line", "start_line": 2, "end_line": 5}},
    ])
    from knowbase.context import _candidate
    cand = _candidate(memory_repo, "P-2026-3006", "global", automatic=False, timeout=5)
    assert cand["source_refs"] == ["knowbase-source:SRC-c1@" + "b" * 64 + "#L2-L5"]


def test_describe_source_ref_degrades_without_sha_or_locator():
    assert sources.describe_source_ref({"source_id": "SRC-x"}) == "knowbase-source:SRC-x"
    assert sources.describe_source_ref(
        {"source_id": "SRC-x", "sha256": "c" * 64}) == "knowbase-source:SRC-x@" + "c" * 64
    ref = {"source_id": "SRC-x", "locator": {"type": "heading_path", "value": "部署/回滚"}}
    assert sources.describe_source_ref(ref) == "knowbase-source:SRC-x#部署/回滚"
    assert sources.describe_source_ref("not-a-dict") == ""
    assert sources.describe_source_ref({"sha256": "d" * 64}) == ""


def test_document_library_citation_uses_shared_formatter():
    assert sources.format_citation("SRC-x", "e" * 64, 8, 9) == (
        "knowbase-source:SRC-x@" + "e" * 64 + "#L8-L9")
