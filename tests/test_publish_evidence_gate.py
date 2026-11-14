"""主张级发布门回归（B3，open-source-reuse §5 验收）。

发布前必须能从服务端快照重算每条 source_refs 引用：
- 篡改引用（text_hash 与快照重算不符）不得进入自动发布
- 越界位置（locator 超出快照行数）不得进入自动发布
- 跨材料引用（manifest scope 与卡不符）不得进入自动发布
- 服务端快照字节被篡改时，重算哈希失配，发布拒绝
- 无 source_refs 的卡不受此门约束（§5：存量卡按现有策略复核）
"""

import hashlib
from pathlib import Path

from knowbase import index, sources, store
from knowbase.knowledge_lifecycle.admission import evaluate_admission
from knowbase.mutations import _compute_revision

TEXT = "第一行 真相\n第二行 事实\n第三行 补充\n"


def _import_source(repo: Path, scope: str, text: str = TEXT) -> tuple[str, str]:
    sid, sha, _created = sources.import_source(
        repo, text, title="门禁出处", scope=scope, fmt="md",
        parser_version="gate-test", imported_by="human:gate-test")
    return sid, sha


def _write_card(repo: Path, mid: str, scope: str, source_refs) -> str:
    meta = store.new_meta("pitfall", "发布门回归卡", scope, [], "pytest")
    meta["id"] = mid
    meta["source_refs"] = source_refs
    body = "正文内容。"
    path = store.mem_path(repo, "pitfall", mid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(store.render(meta, body), encoding="utf-8")
    return _compute_revision(meta, body)


def _gate_check(result: dict) -> dict | None:
    return next((c for c in result["checks"]
                 if c["check"] == "source_evidence_recomputed"), None)


def test_clean_reference_passes_recompute(memory_repo):
    repo = memory_repo
    sid, sha = _import_source(repo, "proj")
    quoted = "".join(TEXT.splitlines(keepends=True)[:2])
    good_hash = hashlib.sha256(quoted.encode()).hexdigest()
    rev = _write_card(repo, "P-2026-5001", "proj", [
        {"source_id": sid, "sha256": sha, "text_hash": good_hash, "quote": quoted,
         "locator": {"type": "line", "start_line": 1, "end_line": 2}}])
    conn = index.connect(repo)
    result = evaluate_admission(conn, repo, "P-2026-5001", rev)
    check = _gate_check(result)
    assert check and check["result"] == "pass"
    assert "recomputed 1" in check["detail"]


def test_tampered_text_hash_rejected(memory_repo):
    repo = memory_repo
    sid, sha = _import_source(repo, "proj")
    forged = hashlib.sha256(TEXT.encode()).hexdigest()
    rev = _write_card(repo, "P-2026-5002", "proj", [
        {"source_id": sid, "sha256": sha, "text_hash": forged,
         "locator": {"type": "line", "start_line": 1, "end_line": 2}}])
    conn = index.connect(repo)
    result = evaluate_admission(conn, repo, "P-2026-5002", rev)
    check = _gate_check(result)
    assert check and check["result"] == "fail"
    assert "text_hash 不匹配" in check["detail"]


def test_out_of_bounds_locator_rejected(memory_repo):
    repo = memory_repo
    sid, sha = _import_source(repo, "proj")
    rev = _write_card(repo, "P-2026-5003", "proj", [
        {"source_id": sid, "sha256": sha,
         "locator": {"type": "line", "start_line": 2, "end_line": 99}}])
    conn = index.connect(repo)
    result = evaluate_admission(conn, repo, "P-2026-5003", rev)
    check = _gate_check(result)
    assert check and check["result"] == "fail"
    assert "越界" in check["detail"]


def test_cross_material_reference_rejected(memory_repo):
    repo = memory_repo
    sid, _sha = _import_source(repo, "other")  # manifest scope 与卡不符
    rev = _write_card(repo, "P-2026-5004", "proj", [
        {"source_id": sid, "locator": {"type": "line", "start_line": 1}}])
    conn = index.connect(repo)
    result = evaluate_admission(conn, repo, "P-2026-5004", rev)
    check = _gate_check(result)
    assert check and check["result"] == "fail"
    assert "项目不匹配" in check["detail"]


def test_server_snapshot_tampering_rejected(memory_repo):
    """快照对象在写卡后被改动：发布门重算哈希失配，拒绝发布。"""
    repo = memory_repo
    sid, sha = _import_source(repo, "proj")
    rev = _write_card(repo, "P-2026-5005", "proj", [
        {"source_id": sid, "sha256": sha,
         "locator": {"type": "line", "start_line": 1, "end_line": 1}}])
    (repo / sources.object_relpath(sha)).write_text("被替换的内容\n", encoding="utf-8")
    conn = index.connect(repo)
    result = evaluate_admission(conn, repo, "P-2026-5005", rev)
    check = _gate_check(result)
    assert check and check["result"] == "fail"
    assert "哈希不匹配" in check["detail"]


def test_cards_without_source_refs_unaffected(memory_repo):
    repo = memory_repo
    rev = _write_card(repo, "P-2026-5006", "proj", None)
    conn = index.connect(repo)
    result = evaluate_admission(conn, repo, "P-2026-5006", rev)
    assert _gate_check(result) is None
