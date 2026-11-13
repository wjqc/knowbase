"""Model-free source browsing projection; never participates in card retrieval.

Source text uses the existing content-addressed sources store. Absolute paths,
watch roots, and the search projection live only in the local memory.db.
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import sqlite3
import stat
import tempfile
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import config, credentials, index, sources, store
from .locking import RepoLock
from .parsers import register_builtin, registry

PARSER_VERSION = "document-library-v2"
MAX_BYTES = 16 * 1024 * 1024
MAX_TEXT_BYTES = 8 * 1024 * 1024
IGNORED_DIRS = {"node_modules", "venv", "__pycache__", "dist", "build"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".tif", ".webp"}


def _hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class DocumentLibrary:
    def __init__(self, repo: Path):
        self.repo = Path(repo).expanduser().resolve()
        self.repo.mkdir(parents=True, exist_ok=True)
        with closing(index.connect(self.repo)):
            pass

    @contextmanager
    def _connect(self):
        with closing(sqlite3.connect(index.db_path(self.repo), timeout=10)) as conn, conn:
            conn.row_factory = sqlite3.Row
            yield conn

    @staticmethod
    def _identity(root: str, path: str, scope: str) -> str:
        return _hash((root + "\0" + path + "\0" + scope).encode())[:24]

    @staticmethod
    def validate_scope(scope: str) -> str:
        if not scope.strip() or scope != scope.strip() or len(scope) > 200 or any(ord(c) < 32 for c in scope):
            raise ValueError("scope must be explicit (use global for shared documents)")
        return scope

    def _snapshot(self, sha: str) -> str:
        # Never trust object paths from synchronized manifests.
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise ValueError("invalid source hash")
        path = self.repo / sources.OBJECTS_DIR / (sha + ".md")
        if path.is_symlink() or not path.resolve().is_relative_to(self.repo):
            raise ValueError("source snapshot escapes library")
        if path.stat().st_size > MAX_TEXT_BYTES:
            raise ValueError("source snapshot exceeds size limit")
        raw = path.read_bytes()
        if _hash(raw) != sha:
            raise ValueError("source snapshot checksum mismatch")
        return raw.decode("utf-8")

    def _project(self, conn, *, doc_id, root, path, scope, manifest, text,
                 raw_hash="", parser_version="", title=None, fmt=None):
        conn.execute("DELETE FROM source_chunks_fts WHERE chunk_id IN "
                     "(SELECT chunk_id FROM source_chunks WHERE document_id=?)", (doc_id,))
        conn.execute("DELETE FROM source_chunks WHERE document_id=?", (doc_id,))
        title = str(title if title is not None else manifest.get("title", path))[:200]
        conn.execute("INSERT OR REPLACE INTO source_documents VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (doc_id, root, path, scope, title, str(fmt if fmt is not None else manifest.get("format", "text")),
                      manifest["id"], manifest["sha256"], raw_hash, parser_version,
                      "available", "", _now()))
        conn.execute("INSERT OR IGNORE INTO source_document_history VALUES(?,?)", (doc_id, manifest["id"]))
        conn.execute("INSERT OR IGNORE INTO source_snapshot_versions VALUES(?,?,?,?,?,?)",
                     (doc_id, manifest["id"], manifest["sha256"], scope, title, _now()))
        # Headings/paragraphs are preferred boundaries; bounded overlap preserves
        # phrases crossing a boundary. Coordinates always use snapshot LF lines.
        for number, (first, last, chunk) in enumerate(self._structured_chunks(text)):
            cid = f"{doc_id}:{number}"
            conn.execute("INSERT INTO source_chunks VALUES(?,?,?,?,?)", (cid, doc_id, first, last, chunk))
            conn.execute("INSERT INTO source_chunks_fts VALUES(?,?,?)", (cid, title + "\n" + path, chunk))

    @staticmethod
    def _structured_chunks(text):
        lines = [m.group() for m in re.finditer(r"[^\n]*\n|[^\n]+$", text)]
        start = 0
        while start < len(lines):
            end, size = start, 0
            while end < len(lines) and end - start < 40:
                length = len(lines[end])
                if end > start and size + length > 6000:
                    break
                size += length
                end += 1
            # Avoid chopping paragraphs or starting a new heading mid-window.
            if end < len(lines):
                boundaries = [i for i in range(start + 16, end)
                              if not lines[i - 1].strip() or re.match(r"^#{1,6}\s", lines[i])]
                if boundaries:
                    end = boundaries[-1]
            part = "".join(lines[start:end])
            # A minified/very long line still has a bounded index window.
            offset = 0
            while offset < len(part):
                chunk = part[offset:offset + 6000]
                first = start + part[:offset].count("\n") + 1
                yield first, first + chunk.rstrip("\n").count("\n"), chunk
                if offset + 6000 >= len(part):
                    break
                offset += 5600
            if end == len(lines):
                break
            start = max(start + 1, end - 5)

    def sync(self, directory: Path, *, scope: str, exclude=(), progress=None, cancelled=None) -> dict:
        """Explicit local directory sync. No git commit/push or model calls.

        Hash every supported file (including same-mtime changes), parse changed
        files from an immutable temporary copy, retain prior snapshots on error.
        Missing/ignored files leave searchable results but retain historical text.
        """
        scope = self.validate_scope(scope)
        requested_root = Path(directory).expanduser()
        if requested_root.is_symlink():
            raise ValueError("document root cannot be a symlink")
        root = requested_root.resolve()
        if not root.is_dir():
            raise ValueError("document directory does not exist")
        if root == self.repo or root.is_relative_to(self.repo) or self.repo.is_relative_to(root):
            raise ValueError("document directory and library cannot contain one another")
        register_builtin()
        root_s = str(root)
        report = {"indexed": 0, "unchanged": 0, "deleted": 0, "skipped": 0, "errors": []}
        # Reading and parsing never hold the repository mutation lock.
        with self._connect() as conn:
            previous = {r["id"]: dict(r) for r in conn.execute(
                "SELECT * FROM source_documents WHERE root=? AND scope=?", (root_s, scope))}
        seen, prepared = set(), []
        prepared_bytes = 0
        scanned = 0
        def checkpoint(phase):
            if cancelled and cancelled():
                raise KeyboardInterrupt("document sync cancelled")
            if progress:
                progress({"phase": phase, "scanned": scanned, "prepared": len(prepared),
                          "applied": report["indexed"] + report["unchanged"] + report["deleted"]})
            if cancelled and cancelled():
                raise KeyboardInterrupt("document sync cancelled")
        checkpoint("scan")
        def walk_error(error):
            raise error  # abort, never turn inaccessible subtrees into deletions
        for base, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in IGNORED_DIRS
                             and not (Path(base) / d).is_symlink())
            for name in sorted(files):
                scanned += 1
                checkpoint("scan")
                file = Path(base) / name
                rel = file.relative_to(root).as_posix()
                if name.startswith(".") or any(fnmatch.fnmatch(rel, p) for p in exclude):
                    continue
                if credentials.contains_credentials(rel):
                    report["errors"].append({"error": "possible plaintext credential in file path"})
                    continue
                parser = registry().find(file)
                if parser is None or parser.name == "ocr" or file.suffix.lower() in IMAGE_SUFFIXES:
                    report["skipped"] += 1
                    continue
                doc_id = self._identity(root_s, rel, scope)
                seen.add(doc_id)
                old = previous.get(doc_id)
                item = {"id": doc_id, "file": file, "rel": rel}
                try:
                    raw = self._read_file(file, root)
                    raw_hash = _hash(raw)
                    version = f"{PARSER_VERSION}:{parser.name}"
                    item.update(raw_hash=raw_hash, version=version)
                    if old and old["raw_hash"] == raw_hash and old["parser_version"] == version and old["status"] == "available":
                        try:
                            self._snapshot(old["sha256"])
                        except (OSError, ValueError):
                            pass
                        else:
                            item["unchanged"] = True
                    if not item.get("unchanged"):
                        with tempfile.TemporaryDirectory(prefix="knowbase-doc-") as tmp:
                            copy = Path(tmp) / file.name
                            copy.write_bytes(raw)
                            parsed = parser.parse(copy)
                        text = parsed.text.replace("\r\n", "\n").replace("\r", "\n")
                        if not text.strip():
                            raise ValueError("parser returned no text (scans require host OCR)")
                        text_bytes = len(text.encode())
                        if text_bytes > MAX_TEXT_BYTES:
                            raise ValueError("parsed text exceeds 8 MiB")
                        title = str(parsed.meta.get("title") or file.stem)[:200]
                        if credentials.contains_credentials(text) or credentials.contains_credentials(title):
                            raise ValueError("possible plaintext credential; source not imported")
                        prepared_bytes += text_bytes
                        if prepared_bytes > 64 * 1024 * 1024:
                            raise ValueError("sync batch exceeds 64 MiB parsed text; split input directories")
                        item.update(text=text, title=title)
                except Exception as exc:
                    item["error"] = credentials.redact_text(f"{type(exc).__name__}: {exc}")[:200]
                prepared.append(item)
        checkpoint("prepared")
        with RepoLock(self.repo, config.load_config().get("lock_timeout", 10)), self._connect() as conn:
            conn.execute("BEGIN")
            available = None
            for item in prepared:
                checkpoint("apply")
                doc_id, file, rel = item["id"], item["file"], item["rel"]
                try:
                    if "error" in item:
                        raise ValueError(item["error"])
                    if _hash(self._read_file(file, root)) != item["raw_hash"]:
                        raise ValueError("source changed during parsing; retry sync")
                    current = conn.execute("SELECT * FROM source_documents WHERE id=?", (doc_id,)).fetchone()
                    if item.get("unchanged"):
                        # Another sync may have replaced the projection in the meantime.
                        if current and current["raw_hash"] == item["raw_hash"] and current["parser_version"] == item["version"]:
                            conn.execute("UPDATE source_documents SET status='available',error='' WHERE id=?", (doc_id,))
                            report["unchanged"] += 1
                            continue
                        raise ValueError("index changed concurrently; retry sync")
                    if available is None:
                        available = {
                            (scope, m["sha256"]): m for m in sources.iter_manifests(self.repo)
                            if m.get("scope") == scope and isinstance(m.get("sha256"), str)
                            and sources.SRC_ID_RE.fullmatch(f'{m["id"]}.yaml')
                            and m.get("status", "available") == "available"
                        }
                    sid, sha, _ = sources.import_source(
                        self.repo, item["text"], title=item["title"], scope=scope,
                        fmt=file.suffix.lstrip("."), parser_version=item["version"],
                        imported_by="human:documents-sync", kind=sources.kind_for(file.suffix),
                        known_sources=available)
                    manifest = {"id": sid, "sha256": sha}
                    self._project(conn, doc_id=doc_id, root=root_s, path=rel, scope=scope,
                                  manifest=manifest, raw_hash=item["raw_hash"], parser_version=item["version"],
                                  text=item["text"], title=item["title"], fmt=file.suffix.lstrip("."))
                    conn.execute("UPDATE source_documents SET status='deleted',updated_at=? "
                                 "WHERE root='' AND source_id=? AND scope=? AND status!='deleted'",
                                 (_now(), sid, scope))
                    report["indexed"] += 1
                except (OSError, ValueError) as exc:
                    error = credentials.redact_text(f"{type(exc).__name__}: {exc}")[:200]
                    conn.execute("INSERT OR IGNORE INTO source_documents VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (doc_id, root_s, rel, scope, file.stem, file.suffix.lstrip("."),
                                  "", "", "", "", "error", error, _now()))
                    conn.execute("UPDATE source_documents SET status='error',error=?,updated_at=? WHERE id=?",
                                 (error, _now(), doc_id))
                    report["errors"].append({"path": rel, "error": error})
            for doc_id, row in previous.items():
                checkpoint("apply")
                if doc_id not in seen and row["status"] != "deleted":
                    rel = row["path"]
                    parts = Path(rel).parts
                    excluded = (any(p.startswith(".") or p in IGNORED_DIRS for p in parts)
                                or any(fnmatch.fnmatch(rel, pattern) for pattern in exclude)
                                or any((root.joinpath(*parts[:i])).is_symlink() for i in range(1, len(parts) + 1)))
                    current = conn.execute("SELECT * FROM source_documents WHERE id=?", (doc_id,)).fetchone()
                    if not current or current["updated_at"] != row["updated_at"] or current["sha256"] != row["sha256"]:
                        report["errors"].append({"path": rel, "error": "index changed concurrently; retry sync"})
                    elif (root / rel).exists() and not excluded:
                        report["errors"].append({"path": rel, "error": "source reappeared during scan; retry sync"})
                    else:
                        conn.execute("UPDATE source_documents SET status='deleted',updated_at=? WHERE id=?", (_now(), doc_id))
                        report["deleted"] += 1
            checkpoint("complete")
        return report

    @staticmethod
    def _read_file(path: Path, root: Path) -> bytes:
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError("symlink/outside root rejected")
        if not path.is_file():
            raise ValueError("source must be a regular file")
        # Walk with directory descriptors on POSIX to reject intermediate link
        # swaps as well as final-component symlinks. No arbitrary source reads.
        if os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW"):
            folder = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                parts = path.relative_to(root).parts
                for part in parts[:-1]:
                    new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=folder)
                    os.close(folder)
                    folder = new
                fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
            finally:
                os.close(folder)
        else:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("source must be a regular file")
            raw = stream.read(MAX_BYTES + 1)
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError("source path changed during read")
        if len(raw) > MAX_BYTES:
            raise ValueError("file exceeds 16 MiB")
        return raw

    def reindex(self) -> dict:
        """Index existing shared/imported source artifacts; no file roots needed."""
        result = {"indexed": 0, "unchanged": 0, "errors": []}
        with RepoLock(self.repo, config.load_config().get("lock_timeout", 10)), self._connect() as conn:
            pinned = {}
            for row in conn.execute("SELECT source_id,scope,sha256 FROM source_snapshot_versions"):
                pinned.setdefault(row["source_id"], set()).add((row["scope"], row["sha256"]))
            manifests = {}
            for manifest in sources.iter_manifests(self.repo):
                sid = manifest["id"]
                if (not sources.SRC_ID_RE.fullmatch(f"{sid}.yaml") or
                        manifest.get("status", "available") != "available"):
                    continue
                try:
                    scope = self.validate_scope(str(manifest.get("scope") or ""))
                except ValueError as exc:
                    result["errors"].append({"source_id": sid, "error": str(exc)})
                    continue
                if sid in pinned and (scope, str(manifest.get("sha256", ""))) not in pinned[sid]:
                    result["errors"].append({"source_id": sid, "error": "source ID changed scope or snapshot hash"})
                    continue
                manifests[self._identity("", sid, scope)] = (manifest, scope)
            standalone = {row["id"]: row for row in conn.execute(
                "SELECT * FROM source_documents WHERE root=''")}
            local_history = {tuple(row) for row in conn.execute(
                "SELECT h.source_id,d.scope FROM source_document_history h "
                "JOIN source_documents d ON d.id=h.document_id WHERE d.root<>''")}
            for doc_id, row in standalone.items():
                if row["status"] != "deleted" and (doc_id not in manifests or
                        (row["source_id"], row["scope"]) in local_history):
                    conn.execute("UPDATE source_documents SET status='deleted',updated_at=? WHERE id=?", (_now(), row["id"]))
            for doc_id, (manifest, scope) in manifests.items():
                sid = manifest["id"]
                if (sid, scope) in local_history:
                    result["unchanged"] += 1
                    continue
                old = standalone.get(doc_id)
                title = str(manifest.get("title") or sid)[:200]
                sha = str(manifest.get("sha256", ""))
                try:
                    body = self._snapshot(sha)
                    if credentials.contains_credentials(body) or credentials.contains_credentials(title):
                        raise ValueError("possible plaintext credential; snapshot not indexed")
                    if old and old["status"] == "available" and old["parser_version"] == PARSER_VERSION and old["title"] == title:
                        result["unchanged"] += 1
                        continue
                    self._project(conn, doc_id=doc_id, root="", path=sid, scope=scope,
                                  manifest=manifest, text=body, title=title,
                                  parser_version=PARSER_VERSION)
                    result["indexed"] += 1
                except (OSError, ValueError) as exc:
                    error = credentials.redact_text(str(exc))[:200]
                    if old:
                        conn.execute("UPDATE source_documents SET status='error',error=?,updated_at=? WHERE id=?",
                                     (error, _now(), doc_id))
                    result["errors"].append({"source_id": sid, "error": error})
        return result

    def list_documents(self, scope="", limit=200) -> list[dict]:
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT id,title,path,scope,status,format,updated_at,sha256,error FROM source_documents "
                "WHERE (?='' OR scope=?) ORDER BY updated_at DESC,id LIMIT ?", (scope, scope, max(1, min(int(limit), 1000))))]

    def search(self, query: str, scope="", limit=30) -> list[dict]:
        """Compatibility API: return chunk hits. Use search_page for file results."""
        return self.search_page(query, scope, limit, group_by_file=False)["items"]

    def search_page(self, query: str, scope="", limit=30, offset=0, *, group_by_file=True) -> dict:
        """Rank deterministic lexical hits, optionally grouped by document.

        Title and path matches outrank body-only matches. Pagination happens
        after grouping, so repeated chunks cannot monopolize a file-results page.
        Scope remains a filter, not a multi-user authorization boundary.
        """
        if len(query) > 1000:
            raise ValueError("query exceeds 1000 characters")
        limit, offset = max(1, min(int(limit), 100)), int(offset)
        if offset < 0:
            raise ValueError("offset must be nonnegative")
        terms = re.findall(r"[\w]+", query, re.UNICODE)[:12]
        page = {"items": [], "total": 0, "offset": offset, "limit": limit, "has_more": False}
        if not terms:
            return page
        # Do not use BM25 in a window expression: SQLite FTS auxiliaries are
        # context-sensitive. Explicit bounded boosts are portable and auditable.
        score, score_params = [], []
        for term in terms:
            score.append("(CASE WHEN instr(lower(d.title),?)>0 THEN 20 ELSE 0 END + "
                         "CASE WHEN instr(lower(d.path),?)>0 THEN 10 ELSE 0 END + "
                         "CASE WHEN instr(lower(c.text),?)>0 THEN 1 ELSE 0 END)")
            score_params += [term.lower()] * 3
        base = ("SELECT d.id document_id,d.title,d.path,d.scope,c.chunk_id,c.start_line,c.end_line,"
                "c.text _text,d.sha256,(" + "+".join(score) + ") score FROM source_documents d "
                "JOIN source_chunks c ON d.id=c.document_id ")
        params = list(score_params)
        long_terms = [t for t in terms if len(t) >= 3]
        if long_terms:
            base += "JOIN source_chunks_fts f ON f.chunk_id=c.chunk_id "
        base += "WHERE d.status='available' AND (?='' OR d.scope=?) "
        params += [scope, scope]
        if long_terms:
            base += "AND source_chunks_fts MATCH ? "
            params.append(" AND ".join('"' + t.replace('"', '""') + '"' for t in long_terms))
        for term in terms:
            if len(term) < 3:
                base += "AND (instr(lower(c.text),?)>0 OR instr(lower(d.title),?)>0 OR instr(lower(d.path),?)>0) "
                params += [term.lower()] * 3
        cte = "WITH candidates AS (" + base + "), ranked AS (SELECT *, " \
              "row_number() OVER(PARTITION BY document_id ORDER BY score DESC,start_line,chunk_id) position," \
              "count(*) OVER(PARTITION BY document_id) matched_chunks FROM candidates) "
        where = "WHERE position=1 " if group_by_file else ""
        with self._connect() as conn:
            conn.execute("BEGIN")
            page["total"] = conn.execute(cte + "SELECT count(*) FROM ranked " + where, params).fetchone()[0]
            rows = [dict(r) for r in conn.execute(
                cte + "SELECT * FROM ranked " + where + "ORDER BY score DESC,document_id,start_line,chunk_id LIMIT ? OFFSET ?",
                params + [limit, offset])]
        for row in rows:
            text = row.pop("_text")
            row.pop("position")
            offsets = [text.lower().find(t.lower()) for t in terms]
            match_offset = min((p for p in offsets if p >= 0), default=0)
            row["start_line"] += text[:match_offset].count("\n")
            row["end_line"] = min(row["end_line"], row["start_line"] + 3)
            row["snippet"] = text[max(0, match_offset - 80):match_offset + 520]
            row["locator_kind"] = "parsed_snapshot_lines"
        page["items"] = rows
        page["has_more"] = offset + len(rows) < page["total"]
        return page

    def history(self, document_id: str) -> list[dict]:
        """Only versions linked to this document, never arbitrary object hashes."""
        with self._connect() as conn:
            document = conn.execute("SELECT * FROM source_documents WHERE id=?", (document_id,)).fetchone()
            versions = conn.execute("SELECT * FROM source_snapshot_versions WHERE document_id=?", (document_id,)).fetchall()
        if not document:
            raise ValueError("unknown document")
        result = []
        for version in versions:
            if version["scope"] != document["scope"]:
                continue
            result.append({"source_id": version["source_id"], "sha256": version["sha256"],
                           "current": version["sha256"] == document["sha256"],
                           "created_at": version["recorded_at"], "title": version["title"]})
        return sorted(result, key=lambda v: (v["current"], v["created_at"], v["source_id"]), reverse=True)

    def read(self, document_id: str, sha256=None, start_line=None, end_line=None, *,
             historical=False, scope=None, include_related=True) -> dict:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM source_documents WHERE id=?", (document_id,)).fetchone()
        if not row or (scope is not None and row["scope"] != scope):
            raise ValueError("unknown document")
        document = dict(row)
        if not document["sha256"]:
            raise ValueError("document has no valid snapshot; fix source and sync again")
        old_version = bool(sha256 and sha256 != document["sha256"])
        selected = {"sha256": document["sha256"], "source_id": document["source_id"]}
        if old_version:
            if not historical:
                raise ValueError("snapshot changed; refresh search before locating lines")
            selected = next((v for v in self.history(document_id) if v["sha256"] == sha256), None)
            if not selected:
                raise ValueError("snapshot does not belong to this document")
        text = self._snapshot(selected["sha256"])
        if credentials.contains_credentials(text) or credentials.contains_credentials(document["title"]):
            raise ValueError("possible plaintext credential; snapshot not delivered")
        stale = old_version or document["status"] != "available"
        if document["root"]:
            root = Path(document["root"])
            path = root / document["path"]
            try:
                stale = stale or _hash(self._read_file(path, root)) != document["raw_hash"]
            except (OSError, ValueError):
                stale = True
        lines = [m.group() for m in re.finditer(r"[^\n]*\n|[^\n]+$", text)]
        start = 1 if start_line is None else int(start_line)
        end = min(len(lines), start + 1999) if end_line is None else int(end_line)
        if start < 1 or end < start or end - start >= 2000 or start > len(lines):
            raise ValueError("invalid line range (maximum 2000 lines)")
        end = min(end, len(lines))
        # References convey snapshot integrity, not the truth of the source's claims.
        related = []
        cards = store.iter_all(self.repo) if include_related else ()
        for meta, _body, _path in cards:
            if (meta.get("scope") != document["scope"] or meta.get("status", "active") != "active"
                    or meta.get("type") == "reference"):
                continue
            refs = meta.get("source_refs") or []
            if any(isinstance(ref, dict) and ref.get("source_id") == selected["source_id"]
                   and ref.get("sha256", selected["sha256"]) == selected["sha256"] for ref in refs):
                related.append({"id": meta.get("id"), "title": meta.get("title", ""),
                                "status": meta.get("status", ""), "scope": meta.get("scope")})
        return {"id": document["id"], "title": selected.get("title", document["title"]), "path": document["path"],
                "scope": document["scope"], "format": document["format"], "status": document["status"],
                "sha256": selected["sha256"], "source_id": selected["source_id"],
                "stale": stale, "historical": old_version, "start_line": start,
                "end_line": end, "total_lines": len(lines), "related_cards": related,
                "citation": f"knowbase-source:{selected['source_id']}@{selected['sha256']}#L{start}-L{end}",
                "source_ref": {"source_id": selected["source_id"], "sha256": selected["sha256"],
                               "locator": {"type": "line", "start_line": start, "end_line": end}},
                "text": "".join(lines[start - 1:end]), "locator_kind": "parsed_snapshot_lines"}
