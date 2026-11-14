import os
import sqlite3
from pathlib import Path

import pytest

from knowbase import sources
from knowbase.document_library import DocumentLibrary


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "inputs"
    root.mkdir()
    return DocumentLibrary(tmp_path / "library"), root


def test_incremental_and_locator(library, monkeypatch):
    lib, root = library
    file = root / "guide.md"
    file.write_text("# 业务说明\n审批需要凭证\n结算流程\n")
    assert lib.sync(root, scope="test")["indexed"] == 1
    hit = lib.search("审批", scope="test")[0]
    assert hit["start_line"] == 2
    assert lib.search("审批", scope="another") == []
    from knowbase.parsers.markdown_parser import MarkdownParser
    monkeypatch.setattr(MarkdownParser, "parse", lambda *a: pytest.fail("unchanged file parsed"))
    assert lib.sync(root, scope="test")["unchanged"] == 1
    result = lib.read(hit["document_id"], sha256=hit["sha256"], start_line=2, end_line=2)
    assert result["text"] == "审批需要凭证\n"
    assert result["locator_kind"] == "parsed_snapshot_lines"
    assert not result["stale"]


def test_same_size_mtime_and_stale_locator(library):
    lib, root = library
    file = root / "a.txt"
    file.write_text("old value\n")
    lib.sync(root, scope="x")
    doc = lib.list_documents()[0]
    stat = file.stat()
    file.write_text("new value\n")
    os.utime(file, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert lib.read(doc["id"])["stale"]
    assert lib.sync(root, scope="x")["indexed"] == 1
    assert not lib.search("old")
    assert lib.search("new")
    with pytest.raises(ValueError, match="snapshot changed"):
        lib.read(doc["id"], sha256=doc["sha256"])
    assert lib.reindex()["indexed"] == 0  # no resurrection of superseded local versions


def test_deletion_keeps_snapshot(library):
    lib, root = library
    f = root / "a.md"
    f.write_text("# 原文\narchive content")
    lib.sync(root, scope="x")
    doc = lib.list_documents()[0]
    f.unlink()
    assert lib.sync(root, scope="x")["deleted"] == 1
    assert not lib.search("archive")
    assert "archive" in lib.read(doc["id"])["text"]
    assert lib.read(doc["id"])["stale"]


def test_failed_parse_preserves_old_snapshot(library, monkeypatch):
    lib, root = library
    f = root / "a.md"
    f.write_text("# Good\nprior content")
    lib.sync(root, scope="x")
    doc = lib.list_documents()[0]
    f.write_text("# changed")
    from knowbase.parsers.markdown_parser import MarkdownParser
    def fail(*args):
        raise ValueError("broken")
    monkeypatch.setattr(MarkdownParser, "parse", fail)
    assert lib.sync(root, scope="x")["errors"]
    assert lib.list_documents()[0]["status"] == "error"
    assert "prior content" in lib.read(doc["id"])["text"]
    assert not lib.search("prior")


def test_symlink_hidden_and_exclude(library, tmp_path):
    lib, root = library
    outside = tmp_path / "secret.md"
    outside.write_text("outside marker")
    (root / "link.md").symlink_to(outside)
    (root / ".hidden.md").write_text("hidden marker")
    (root / "skip.md").write_text("excluded marker")
    (root / "real.md").write_text("real marker")
    result = lib.sync(root, scope="x", exclude=["skip.*"])
    assert result["indexed"] == 1
    assert result["errors"]
    assert len(lib.list_documents()) == 2
    assert not lib.search("outside")
    with pytest.raises(ValueError):
        lib.read("../../secret.md")


def test_credential_and_no_image_model(library, monkeypatch):
    lib, root = library
    (root / "creds.md").write_text("api_key = sk-" + "a" * 40)
    (root / "scan.png").write_bytes(b"not image")
    report = lib.sync(root, scope="x")
    assert report["indexed"] == 0
    assert report["skipped"] == 1
    assert report["errors"]


def test_reindex_legacy_sources_and_snapshot_tamper(library):
    lib, root = library
    sid, sha, _ = sources.import_source(lib.repo, "# imported\noriginal evidence", title="Test", scope="x",
                                      fmt="md", parser_version="v1", imported_by="test")
    assert lib.reindex()["indexed"] == 1
    doc = lib.list_documents()[0]
    assert doc["path"] == sid
    assert lib.search('evidence')[0]["document_id"] == doc["id"]
    (lib.repo / sources.object_relpath(sha)).write_text("tampered")
    with pytest.raises(ValueError, match="checksum"):
        lib.read(doc["id"])


def test_reindex_hides_missing_artifact_and_restores_same_document(library):
    lib, _ = library
    sid, sha, _ = sources.import_source(
        lib.repo, "recovery needle", title="Recovery", scope="x", fmt="md",
        parser_version="v1", imported_by="test")
    lib.reindex()
    original = lib.list_documents()[0]
    manifest = lib.repo / sources.MANIFESTS_DIR / f"{sid}.yaml"
    saved = manifest.read_bytes()
    manifest.unlink()
    lib.reindex()
    assert not lib.search("recovery")
    manifest.write_bytes(saved)
    assert lib.reindex()["indexed"] == 1
    assert lib.search("recovery")[0]["document_id"] == original["id"]
    (lib.repo / sources.object_relpath(sha)).unlink()
    assert lib.reindex()["errors"]
    assert not lib.search("recovery")


def test_directory_sync_repairs_damaged_snapshot(library):
    lib, root = library
    (root / "a.md").write_text("# original evidence", encoding="utf-8")
    assert lib.sync(root, scope="x")["indexed"] == 1
    hit = lib.search("evidence", scope="x")[0]
    (lib.repo / sources.object_relpath(hit["sha256"])).write_text("damaged")
    assert lib.sync(root, scope="x")["indexed"] == 1
    repaired = lib.read(hit["document_id"], sha256=hit["sha256"])
    assert "original evidence" in repaired["text"]
    assert len(lib.history(hit["document_id"])) == 1


def test_reindex_rejects_unscoped_or_credential_bearing_sources(library):
    lib, _ = library
    for scope, title, body in (
        ("", "Unscoped", "source needle"),
        ("x", "Unsafe", "api_key = sk-" + "z" * 40),
        ("x", "api_key = sk-" + "z" * 40, "safe body"),
    ):
        sources.import_source(lib.repo, body, title=title, scope=scope, fmt="md",
                              parser_version="v1", imported_by="test")
    report = lib.reindex()
    assert len(report["errors"]) == 3
    assert "sk-" not in str(report)
    assert not lib.list_documents()


def test_reindex_refuses_to_move_a_known_source_to_another_scope(library):
    import yaml

    lib, _ = library
    sid, _, _ = sources.import_source(
        lib.repo, "private needle", title="Private", scope="first", fmt="txt",
        parser_version="v1", imported_by="test")
    assert lib.reindex()["indexed"] == 1
    path = lib.repo / sources.MANIFESTS_DIR / f"{sid}.yaml"
    manifest = yaml.safe_load(path.read_text())
    path.write_text(yaml.safe_dump(dict(manifest, scope="second")))
    assert lib.reindex()["errors"]
    assert not lib.search("private", scope="second")


def test_model_free_and_separate_from_card_index(library):
    lib, root = library
    (root / "a.md").write_text("# searchable source")
    lib.sync(root, scope="x")
    conn = sqlite3.connect(lib.repo / "memory.db")
    assert conn.execute("SELECT count(*) FROM source_documents").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM mem_fts").fetchone()[0] == 0
    conn.close()


def test_changed_during_parse_rejected(library, monkeypatch):
    lib, root = library
    f = root / "a.md"; f.write_text("initial")
    from knowbase.parsers.markdown_parser import MarkdownParser
    parse = MarkdownParser.parse
    def race(self, path):
        result = parse(self, path)
        f.write_text("changed")
        return result
    monkeypatch.setattr(MarkdownParser, "parse", race)
    assert lib.sync(root, scope="x")["errors"]
    assert lib.list_documents()[0]["status"] == "error"


def test_multiple_scopes_duplicate_content(library):
    lib, root = library
    (root / "a.md").write_text("common evidence")
    lib.sync(root, scope="x"); lib.sync(root, scope="y")
    assert len(lib.search("evidence", scope="x")) == 1
    assert len(lib.search("evidence", scope="y")) == 1
    assert len(lib.list_documents()) == 2


def test_cli_parse_and_dispatch(library, monkeypatch, capsys):
    from knowbase.__main__ import _main
    from knowbase import config
    lib, root = library
    monkeypatch.setattr(config, "repo_path", lambda: lib.repo)
    assert _main(["documents", "list"]) == 2
    (lib.repo / ".git").mkdir()
    (root / "a.md").write_text("# cli evidence")
    assert _main(["documents", "sync", str(root), "--scope", "x"]) == 0
    assert _main(["documents", "search", "evidence", "--scope", "x"]) == 0
    assert "document_id" in capsys.readouterr().out
    assert _main(["documents", "sync", str(root), "--scope", ""]) == 2


def test_limits_and_invalid_ranges(library):
    lib, root = library
    (root / "a.md").write_text("a\nb\n")
    with pytest.raises(ValueError): lib.sync(root, scope="")
    with pytest.raises(ValueError): lib.sync(lib.repo, scope="x")
    lib.sync(root, scope="x")
    doc = lib.list_documents()[0]
    for start, end in [(0, 1), (2, 1), (1, 2001)]:
        with pytest.raises(ValueError): lib.read(doc["id"], start_line=start, end_line=end)
    assert not lib.search('" OR *')


def test_mcp_explicit_sources(library, monkeypatch):
    import json
    from knowbase import server
    lib, root = library
    (root / "a.md").write_text("# MCP\nevidence line")
    lib.sync(root, scope="x")
    monkeypatch.setattr(server, "_repo", lambda: (lib.repo, None))
    result = json.loads(server.source_search_impl("evidence", "x"))
    hit = result["items"][0]
    doc = json.loads(server.source_read_impl(hit["document_id"], "x", hit["sha256"]))
    assert doc["kind"] == "untrusted_source_material"
    assert "evidence line" in doc["text"]
    assert "error" in json.loads(server.source_read_impl(hit["document_id"], "y", hit["sha256"]))
    assert "error" in json.loads(server.source_search_impl("evidence", ""))


def test_excluded_existing_document_not_searchable(library):
    lib, root = library
    (root / "a.md").write_text("excluded content")
    lib.sync(root, scope="x")
    lib.sync(root, scope="x", exclude=["a.md"])
    assert not lib.search("excluded")
    assert lib.list_documents()[0]["status"] == "deleted"


def test_duplicate_text_keeps_file_metadata(library):
    lib, root = library
    (root / "first.txt").write_text("identical text")
    (root / "second.md").write_text("identical text")
    lib.sync(root, scope="x")
    docs = {d["path"]: d for d in lib.list_documents()}
    assert docs["first.txt"]["title"] == "identical text"
    assert docs["second.md"]["title"] == "second"
    assert docs["first.txt"]["format"] == "txt"
    assert docs["second.md"]["format"] == "md"
    assert docs["first.txt"]["sha256"] == docs["second.md"]["sha256"]


def test_new_parse_error_visible(library):
    lib, root = library
    (root / "broken.pdf").write_bytes(b"not a pdf")
    assert lib.sync(root, scope="x")["errors"]
    doc = lib.list_documents()[0]
    assert doc["status"] == "error"
    with pytest.raises(ValueError, match="no valid snapshot"):
        lib.read(doc["id"])


def test_tif_never_calls_ocr(library, monkeypatch):
    lib, root = library
    from knowbase.parsers.ocr_parser import OcrParser
    monkeypatch.setattr(OcrParser, "parse", lambda *a: pytest.fail("OCR must remain on host"))
    (root / "scan.tif").write_bytes(b"not image")
    assert lib.sync(root, scope="x")["skipped"] == 1


def test_unicode_separator_matches_snapshot_line_numbers(library):
    lib, root = library
    (root / "a.md").write_text("first\u2028still first\nneedle\n")
    lib.sync(root, scope="x")
    hit = lib.search("needle")[0]
    assert hit["start_line"] == 2
    assert lib.read(hit["document_id"], start_line=2, end_line=2)["text"] == "needle\n"


def test_reappeared_file_not_deleted(library, monkeypatch):
    lib, root = library
    (root / "a.md").write_text("still present")
    lib.sync(root, scope="x")
    monkeypatch.setattr(os, "walk", lambda *a, **k: iter([(str(root), [], [])]))
    result = lib.sync(root, scope="x")
    assert result["deleted"] == 0
    assert result["errors"]
    assert lib.search("present")


def test_parser_version_reindexes(library, monkeypatch):
    from knowbase import document_library
    lib, root = library
    (root / "a.md").write_text("same bytes")
    lib.sync(root, scope="x")
    monkeypatch.setattr(document_library, "PARSER_VERSION", "v-next")
    assert lib.sync(root, scope="x")["indexed"] == 1


def test_reindex_superseded_source_hidden(library):
    lib, root = library
    old_id, old_sha, _, _ = sources.import_versioned(
        lib.repo, "oldcontent", title="a", scope="x", fmt="md",
        parser_version="v1", imported_by="test", source_path="a.md")
    lib.reindex()
    sources.import_versioned(lib.repo, "newcontent", title="a", scope="x", fmt="md",
                             parser_version="v1", imported_by="test", source_path="a.md")
    lib.reindex()
    assert not lib.search("oldcontent")
    new_document = lib.search("newcontent")[0]["document_id"]
    assert lib.read(new_document, sha256=old_sha, historical=True)["source_id"] == old_id


@pytest.mark.parametrize("first", ["sync", "import"])
def test_unchanged_file_keeps_same_snapshot_when_switching_entry(memory_repo, tmp_path, first):
    from knowbase.__main__ import cmd_import

    lib = DocumentLibrary(memory_repo)
    root = tmp_path / "docs"
    root.mkdir()
    (root / "approval.md").write_bytes("# 审批\r\n\r\n保留行与换行。\r\n".encode())

    def ingest(entry):
        if entry == "sync":
            assert not lib.sync(root, scope="x")["errors"]
        else:
            assert cmd_import(str(root), "reference", "x", False, "human:import") == 0

    ingest(first)
    doc = lib.list_documents(scope="x")[0]
    old = lib.read(doc["id"])
    ingest("import" if first == "sync" else "sync")
    assert sources.count_sources(memory_repo) == 1
    assert len(lib.history(doc["id"])) == 1
    assert lib.read(doc["id"])["source_ref"] == old["source_ref"]
