"""漂移触发复验回归（B2，ADR-0002 P2 落地）。

钉住：sync 后的确定性钩子把「卡硬引用的快照被新版本取代」登记为
source_drift_events，并在 memory_search / memory_read 打「源已更新」
标记；引用修复后回填 resolved_at。旧引用可由 SRC ID 识别，非 active 卡
不产生漂移。混合边界：同卡多 ref 部分漂移、旧引用已无卡持有、
重复同步不产生重复事件。

设计边界（刻意保守）：漂移事件只做账本与可见性标记，不进
governance.decide 的负证据链（负证据会直接映射 quarantine，违反
"漂移不自动隔离"的裁决）；降级由人工或后续 worker 复验任务处理。
"""

import sqlite3
from pathlib import Path

from knowbase import index, store
from knowbase.document_library import DocumentLibrary, unresolved_drift_memory_ids


def _write_card(repo: Path, mid: str, *, source_refs=None, status="active",
                title="漂移回归卡") -> Path:
    meta = store.new_meta("pitfall", title, "x", [], "pytest")
    meta["id"] = mid
    meta["status"] = status
    if source_refs is not None:
        meta["source_refs"] = source_refs
    path = store.mem_path(repo, "pitfall", mid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(store.render(meta, "正文。"), encoding="utf-8")
    index.rebuild(repo)
    return path


def _sync_doc(lib: DocumentLibrary, root: Path) -> dict:
    report = lib.sync(root, scope="x")
    assert not report["errors"], report
    return report


def _current_snapshot(lib: DocumentLibrary) -> tuple[str, str, str]:
    doc = next(d for d in lib.list_documents(scope="x") if d["status"] == "available")
    detail = lib.read(doc["id"])
    return detail["source_id"], doc["sha256"], doc["id"]


def _drift_rows(repo: Path) -> list[tuple]:
    conn = sqlite3.connect(index.db_path(repo))
    try:
        return conn.execute("SELECT old_sha256,new_sha256,memory_id,resolved_at "
                            "FROM source_drift_events").fetchall()
    finally:
        conn.close()


def test_drift_marked_and_surfaced_on_version_change(memory_repo, tmp_path):
    repo = memory_repo
    lib = DocumentLibrary(repo)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "doc.md").write_text("第一版 内容 anchor")
    _sync_doc(lib, docs)
    sid, sha1, _doc_id = _current_snapshot(lib)
    _write_card(repo, "P-2026-4001", source_refs=[
        {"source_id": sid, "sha256": sha1,
         "locator": {"type": "line", "start_line": 1, "end_line": 1}}])

    (docs / "doc.md").write_text("第二版 内容 changed")
    report = _sync_doc(lib, docs)
    assert report["drift_marked"] == 1 and report["drift_resolved"] == 0
    _, sha2, _ = _current_snapshot(lib)
    assert sha2 != sha1

    rows = _drift_rows(repo)
    assert len(rows) == 1
    assert rows[0][0] == sha1 and rows[0][1] == sha2
    assert rows[0][2] == "P-2026-4001" and rows[0][3] == ""

    from knowbase.server import read_impl, search_impl
    assert "源已更新" in search_impl("漂移回归卡", scope="x", limit=5)
    assert "源已更新" in read_impl("P-2026-4001")
    assert unresolved_drift_memory_ids(repo) == {"P-2026-4001"}


def test_resolved_when_card_reference_updated(memory_repo, tmp_path):
    repo = memory_repo
    lib = DocumentLibrary(repo)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "doc.md").write_text("第一版 内容 anchor")
    _sync_doc(lib, docs)
    sid, sha1, _ = _current_snapshot(lib)
    _write_card(repo, "P-2026-4011", source_refs=[
        {"source_id": sid, "sha256": sha1,
         "locator": {"type": "line", "start_line": 1, "end_line": 1}}])
    (docs / "doc.md").write_text("第二版 内容 changed")
    _sync_doc(lib, docs)
    current_sid, sha2, _ = _current_snapshot(lib)

    # 修复引用 → 再次 sync 触发解决扫描
    _write_card(repo, "P-2026-4011", source_refs=[
        {"source_id": current_sid, "sha256": sha2,
         "locator": {"type": "line", "start_line": 1, "end_line": 1}}])
    report = _sync_doc(lib, docs)
    assert report["drift_marked"] == 0 and report["drift_resolved"] == 1
    assert all(row[3] for row in _drift_rows(repo))

    from knowbase.server import search_impl
    assert "源已更新" not in search_impl("漂移回归卡", scope="x", limit=5)
    assert unresolved_drift_memory_ids(repo) == set()


def test_source_id_only_detects_drift_but_inactive_card_is_skipped(memory_repo, tmp_path):
    repo = memory_repo
    lib = DocumentLibrary(repo)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "doc.md").write_text("第一版 内容 anchor")
    _sync_doc(lib, docs)
    sid, sha1, _ = _current_snapshot(lib)
    # 旧卡只记录 SRC ID，仍指向固定快照。
    _write_card(repo, "P-2026-4021", source_refs=[{"source_id": sid}])
    _write_card(repo, "P-2026-4022", status="archived", source_refs=[
        {"source_id": sid, "sha256": sha1,
         "locator": {"type": "line", "start_line": 1, "end_line": 1}}])

    (docs / "doc.md").write_text("第二版 内容 changed")
    report = _sync_doc(lib, docs)
    assert report["drift_marked"] == 1
    assert [row[2] for row in _drift_rows(repo)] == ["P-2026-4021"]


def test_partial_drift_with_mixed_references(memory_repo, tmp_path):
    """同卡多 ref 部分漂移：只有钉住旧版的硬引用产生事件。"""
    repo = memory_repo
    lib = DocumentLibrary(repo)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "doc.md").write_text("第一版 内容 anchor")
    _sync_doc(lib, docs)
    sid, sha1, _ = _current_snapshot(lib)
    _write_card(repo, "P-2026-4031", source_refs=[
        {"source_id": sid, "sha256": sha1,
         "locator": {"type": "line", "start_line": 1, "end_line": 1}},
        {"source_id": sid},  # 同一旧快照，只登记一次
        {"source_id": "SRC-别的源", "sha256": "0" * 64},  # 未接入库的源
    ])

    (docs / "doc.md").write_text("第二版 内容 changed")
    report = _sync_doc(lib, docs)
    assert report["drift_marked"] == 1
    rows = _drift_rows(repo)
    assert len(rows) == 1 and rows[0][0] == sha1


def test_resync_is_idempotent_no_duplicate_events(memory_repo, tmp_path):
    repo = memory_repo
    lib = DocumentLibrary(repo)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "doc.md").write_text("第一版 内容 anchor")
    _sync_doc(lib, docs)
    sid, sha1, _ = _current_snapshot(lib)
    _write_card(repo, "P-2026-4041", source_refs=[
        {"source_id": sid, "sha256": sha1,
         "locator": {"type": "line", "start_line": 1, "end_line": 1}}])
    (docs / "doc.md").write_text("第二版 内容 changed")
    _sync_doc(lib, docs)
    assert len(_drift_rows(repo)) == 1
    # 重复同步（内容未变）：不新增事件
    report = _sync_doc(lib, docs)
    assert report["drift_marked"] == 0
    assert len(_drift_rows(repo)) == 1


def test_cli_import_drift_reaches_mcp_and_hook_and_keeps_history(memory_repo, tmp_path):
    import json

    from knowbase import config
    from knowbase.__main__ import cmd_import
    from knowbase.adapters.host_events import render
    from knowbase.hooks import user_prompt
    from knowbase.server import context_impl, read_impl, search_impl, source_read_impl

    repo = memory_repo
    lib = DocumentLibrary(repo)
    root = tmp_path / "docs"
    root.mkdir()
    document = root / "doc.md"
    document.write_text("第一版 文档引用", encoding="utf-8")
    assert cmd_import(str(root), "reference", "x", False, "human:import") == 0
    sid, sha, doc_id = _current_snapshot(lib)
    ref = lib.read(doc_id)["source_ref"]
    mid = "P-2026-4099"
    path = _write_card(repo, mid, source_refs=[ref])
    meta, body, _ = store.load(repo, mid)
    meta["confidence"] = "verified"
    path.write_text(store.render(meta, body), encoding="utf-8")
    index.rebuild(repo)
    query = dict(session_id="drift", scope="x", current_goal="漂移回归卡")
    before = json.loads(context_impl(event_id="before", **query))["candidates"][0]
    assert "source_revalidation" not in before

    document.write_text("第二版 文档引用", encoding="utf-8")
    assert cmd_import(str(root), "reference", "x", False, "human:import") == 0
    stage = json.loads(context_impl(event_id="after", **query))
    candidate = stage["candidates"][0]
    assert candidate["mode"] == "validate_only"
    assert candidate["source_revalidation"][0]["old_sha256"] == sha
    citation = candidate["source_refs"][0]
    assert citation in read_impl(mid)
    assert citation in search_impl("漂移回归卡", scope="x")
    assert citation in render(stage) and "源已更新" in render(stage)
    cfg = config.load_config()
    cfg["scope_map"][str(root)] = "x"
    config.CONFIG_PATH.write_text(json.dumps(cfg), encoding="utf-8")
    hook = user_prompt("漂移回归卡", project_dir=str(root))
    assert citation in hook and "源已更新" in hook
    historical = json.loads(source_read_impl(sid, "x", sha, historical=True))
    assert historical["text"] == "第一版 文档引用"
    assert historical["source_ref"] == ref
    assert "源已更新" in read_impl(mid)
