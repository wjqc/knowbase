"""Source Artifact 存储层：原始材料与可复用知识彻底分层（P0，2026-09-21）。

source 只负责保真，不负责复用：
- objects/ 保存解析后的文本快照，按内容 sha256 寻址（跨路径天然去重）；
- manifests/ 保存来源、哈希、解析器、导入时间等元数据；
- 不生成知识卡、不进入默认检索（memory_search / Hook 自动注入均不召回）；
- 绝对源路径只留在本机 memory.db.source_state，不进共享 Git 内容；
- 可复用知识由 memory_save 按八项小节结构另行提炼（source → card 派生关系属 P1）。
"""

import hashlib
import re
import uuid
from datetime import date
from pathlib import Path

import yaml

from .store import write_shared_text

OBJECTS_DIR = Path("sources/objects")
MANIFESTS_DIR = Path("sources/manifests")
# 兼容旧格式 SRC-2026-0001 和新格式 SRC-<uuid12>
SRC_ID_RE = re.compile(r"^SRC-(\d{4})-(\d{4})\.yaml$|^SRC-([0-9a-f]{12})\.yaml$")

# 后缀 → manifest kind（粗分类：文档 / 代码 / 日志）
_KIND_BY_EXT = {".py": "code", ".js": "code", ".ts": "code", ".java": "code", ".log": "log"}


def kind_for(suffix: str) -> str:
    return _KIND_BY_EXT.get((suffix or "").lower(), "document")


def format_citation(source_id: str, sha256: str, start_line, end_line) -> str:
    """内容寻址引用串：SRC@sha256#L起-L止。文档库与卡片出处渲染共用。"""
    return f"knowbase-source:{source_id}@{sha256}#L{int(start_line)}-L{int(end_line)}"


def describe_source_ref(ref) -> str:
    """卡片 frontmatter source_refs → 人类可读引用串。

    纯格式化，不检查快照是否存在：引用指向已删除/未导入快照时仍照实显示，
    由调用方决定是否附加有效性标记。sha256 与 locator 缺失时逐级降级。
    """
    if not isinstance(ref, dict) or not ref.get("source_id"):
        return ""
    text = f"knowbase-source:{ref['source_id']}"
    if ref.get("sha256"):
        text += f"@{ref['sha256']}"
    locator = ref.get("locator")
    if isinstance(locator, dict):
        start = locator.get("start_line")
        if start is not None:
            end = locator.get("end_line")
            try:
                text += f"#L{int(start)}" + (f"-L{int(end)}" if end else "")
            except (TypeError, ValueError, OverflowError):
                pass
        elif locator.get("value"):
            text += f"#{locator['value']}"
    return text


def card_citations(meta: dict, limit: int) -> list[str]:
    citations = []
    for ref in meta.get("source_refs") or []:
        citation = describe_source_ref(ref)
        if citation:
            citations.append(citation)
            if len(citations) == limit:
                break
    return citations


def card_source_changes(conn, meta: dict, limit: int | None = 5) -> list[dict]:
    changes = []
    for ref in meta.get("source_refs") or []:
        if not isinstance(ref, dict) or not isinstance(ref.get("source_id"), str):
            continue
        sha = ref.get("sha256")
        if sha is not None and not isinstance(sha, str):
            continue
        for document_id, source_id, old_sha, new_sha, scope in conn.execute(
                "SELECT d.id,d.source_id,v.sha256,d.sha256,d.scope FROM source_snapshot_versions v "
                "JOIN source_documents d ON d.id=v.document_id "
                "WHERE v.source_id=? AND (? IS NULL OR v.sha256=?) AND v.scope=d.scope "
                "AND d.scope IN (?, 'global') AND d.status='available' AND d.sha256<>v.sha256 "
                "ORDER BY d.id",
                (ref["source_id"], sha, sha, meta.get("scope"))):
            change = {"document_id": document_id, "source_id": source_id, "old_source_id": ref["source_id"],
                      "scope": scope, "old_sha256": old_sha, "new_sha256": new_sha}
            if change not in changes:
                changes.append(change)
                if len(changes) == limit:
                    return changes
    return changes


def ensure_dirs(repo: Path) -> None:
    (Path(repo) / OBJECTS_DIR).mkdir(parents=True, exist_ok=True)
    (Path(repo) / MANIFESTS_DIR).mkdir(parents=True, exist_ok=True)


def object_relpath(sha256: str) -> str:
    return f"{OBJECTS_DIR.as_posix()}/{sha256}.md"


def alloc_id(repo: Path, year: int | None = None) -> str:
    """分配新 source ID。

    新格式：`SRC-<uuid12>`（如 `SRC-a1b2c3d4e5f6`），跨机唯一；
    旧格式 `SRC-2026-0001` 保留兼容，不再新分配。
    """
    short = uuid.uuid4().hex[:12]
    return f"SRC-{short}"


def iter_manifests(repo: Path):
    d = Path(repo) / MANIFESTS_DIR
    if not d.exists():
        return
    for f in sorted(d.glob("SRC-*.yaml")):
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        if isinstance(data, dict) and data.get("id"):
            yield data


def count_sources(repo: Path) -> int:
    return sum(1 for _ in iter_manifests(repo))


def find_by_sha(repo: Path, sha256: str, scope: str = "") -> dict | None:
    """按 sha256 查找 manifest。

    scope 非空时只返回同 scope 的 manifest（跨 scope 不共享，独立归属）；
    scope 为空时退化为全局查找（兼容旧调用）。
    """
    for mf in iter_manifests(repo):
        if mf.get("sha256") == sha256:
            if not scope or mf.get("scope") == scope:
                return mf
    return None


def import_source(repo: Path, text: str, *, title: str, scope: str, fmt: str,
                  parser_version: str, imported_by: str, kind: str = "document",
                  source_modified_at: str = "", known_sources: dict | None = None) -> tuple[str, str, bool]:
    """写入一个 source artifact，返回 (src_id, sha256, created)。

    同内容（sha256）已存在时不重复写，返回既有 id 且 created=False；
    同路径源文件变化 = 新 sha = 新快照（内容寻址语义，无覆盖问题）。
    """
    repo = Path(repo)
    raw = text.encode("utf-8")
    sha = hashlib.sha256(raw).hexdigest()
    object_path = repo / object_relpath(sha)
    if known_sources is not None and (object_path.is_symlink() or
            not object_path.resolve().is_relative_to(repo.resolve()) or
            not (repo / MANIFESTS_DIR).resolve().is_relative_to(repo.resolve())):
        raise ValueError("source storage path escapes the repository")
    prior = (find_by_sha(repo, sha, scope=scope) if known_sources is None
             else known_sources.get((scope, sha)))
    if prior:
        if known_sources is not None:
            # A local directory can restore the canonical bytes of a damaged snapshot.
            if (not object_path.is_file() or object_path.stat().st_size != len(raw) or
                    object_path.read_bytes() != raw):
                write_shared_text(object_path, text)
        return str(prior["id"]), sha, False
    ensure_dirs(repo)
    sid = alloc_id(repo)
    write_shared_text(object_path, text)
    manifest = {
        "id": sid,
        "kind": kind,
        "title": title,
        "scope": scope,
        "object": object_relpath(sha),
        "sha256": sha,
        "format": fmt,
        "imported_at": date.today().isoformat(),
        "imported_by": imported_by,
        "parser_version": parser_version,
        "status": "available",
    }
    if source_modified_at:
        manifest["source_modified_at"] = source_modified_at
    write_shared_text(repo / MANIFESTS_DIR / f"{sid}.yaml",
                      yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False))
    if known_sources is not None:
        known_sources[scope, sha] = manifest
    return sid, sha, True


def load_manifest(repo: Path, source_id: str) -> dict | None:
    """加载指定 source_id 的 manifest。"""
    repo = Path(repo)
    if not SRC_ID_RE.match(f"{source_id}.yaml"):
        return None
    manifest_path = repo / MANIFESTS_DIR / f"{source_id}.yaml"
    if not manifest_path.exists():
        return None
    try:
        resolved = manifest_path.resolve()
        root = repo.resolve()
    except OSError:
        return None
    if resolved != root and root not in resolved.parents:
        return None
    try:
        data = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def read_snapshot(repo: Path, sha256: str, *, max_bytes: int | None = None) -> str:
    if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ValueError("来源对象哈希不匹配 (invalid source hash)")
    path = repo / object_relpath(sha256)
    if path.is_symlink() or not path.resolve().is_relative_to(repo.resolve()):
        raise ValueError("来源对象路径越界 (source snapshot escapes library)")
    if max_bytes is not None and path.stat().st_size > max_bytes:
        raise ValueError("source snapshot exceeds size limit")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != sha256:
        raise ValueError("来源对象哈希不匹配 (source snapshot checksum mismatch)")
    return raw.decode("utf-8")


def snapshot_lines(text: str) -> list[str]:
    return re.findall(r"[^\n]*\n|[^\n]+$", text)


def resolve_reference(repo: Path, ref: dict, scope: str) -> str:
    """Resolve a card reference against the immutable text it actually cites."""
    manifest = load_manifest(repo, ref["source_id"])
    if not manifest or manifest.get("scope") not in (scope, "global"):
        raise ValueError("来源不存在或项目不匹配")
    sha = manifest.get("sha256")
    if ref.get("sha256", sha) != sha:
        raise ValueError("来源对象哈希不匹配")
    try:
        text = read_snapshot(Path(repo), sha)
    except (OSError, UnicodeError) as exc:
        raise ValueError("来源对象无法读取") from exc
    locator = ref.get("locator")
    if locator is not None and not isinstance(locator, dict):
        raise ValueError("locator 必须为对象")
    locator = locator or {}
    start, end = locator.get("start_line"), locator.get("end_line")
    fragment = text
    if locator.get("type") == "line" or start is not None or end is not None:
        end = start if end is None else end
        if type(start) is not int or type(end) is not int:
            raise ValueError("locator 行号必须为整数")
        lines = snapshot_lines(text)
        if not 1 <= start <= end <= len(lines):
            raise ValueError(f"locator 越界：L{start}-L{end} 超出快照 {len(lines)} 行")
        fragment = "".join(lines[start - 1:end])
    if segment_id := ref.get("segment_id"):
        from .extraction.segmenter import segment_text
        segment = next((item for item in segment_text(text, source_format=manifest.get("format", "markdown"))
                        if item.segment_id == segment_id), None)
        if segment is None:
            raise ValueError("segment_id 不存在")
        if start is not None:
            if not segment.locator.start_line <= start <= end <= segment.locator.end_line:
                raise ValueError("locator 与 segment_id 指向不同片段")
        if locator.get("type") != "line" or start is None:
            fragment = segment.text
    if "text_hash" in ref:
        digest = hashlib.sha256(fragment.encode("utf-8")).hexdigest()
        if ref["text_hash"] not in (digest, digest[:16]):
            raise ValueError("text_hash 不匹配")
    if "quote" in ref and (not fragment.strip() or ref["quote"] != fragment):
        raise ValueError("quote 与所引用原文不匹配")
    return fragment


def find_by_path(repo: Path, source_path: str, scope: str = "") -> dict | None:
    """按源文件路径查找最新的 manifest（M4-7: 版本追踪用）。

    返回该路径的最新版本 manifest，如果不存在返回 None。
    """
    latest = None
    latest_version = -1
    for mf in iter_manifests(repo):
        if mf.get("source_path") == source_path:
            if not scope or mf.get("scope") == scope:
                version = mf.get("source_version", 1)
                if version > latest_version:
                    latest = mf
                    latest_version = version
    return latest


def import_versioned(repo: Path, text: str, *, title: str, scope: str, fmt: str,
                     parser_version: str, imported_by: str, kind: str = "document",
                     source_modified_at: str = "", source_path: str = "") -> tuple[str, str, bool, int]:
    """M4-7: 带版本追踪的 source 导入。

    同路径内容变化 → 新版本（source_version + 1），旧版本标记为 superseded。
    同内容（sha256）已存在时不重复写，返回既有 id 且 created=False。

    返回 (src_id, sha256, created, version)。
    """
    repo = Path(repo)
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()

    # 检查同内容是否已存在
    prior = find_by_sha(repo, sha, scope=scope)
    if prior:
        return str(prior["id"]), sha, False, prior.get("source_version", 1)

    ensure_dirs(repo)

    # M4-7: 版本追踪 — 检查同路径是否有旧版本
    version = 1
    superseded_id = None
    if source_path:
        old_manifest = find_by_path(repo, source_path, scope=scope)
        if old_manifest:
            old_version = old_manifest.get("source_version", 1)
            version = old_version + 1
            superseded_id = old_manifest.get("id")

    # 创建新版本
    sid = alloc_id(repo)
    write_shared_text(repo / OBJECTS_DIR / f"{sha}.md", text)
    manifest = {
        "id": sid,
        "kind": kind,
        "title": title,
        "scope": scope,
        "object": object_relpath(sha),
        "sha256": sha,
        "format": fmt,
        "imported_at": date.today().isoformat(),
        "imported_by": imported_by,
        "parser_version": parser_version,
        "status": "available",
        "source_version": version,
    }
    if source_modified_at:
        manifest["source_modified_at"] = source_modified_at
    if source_path:
        manifest["source_path"] = source_path
    if superseded_id:
        manifest["supersedes"] = superseded_id

    write_shared_text(repo / MANIFESTS_DIR / f"{sid}.yaml",
                      yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False))

    # 标记旧版本为 superseded
    if superseded_id:
        _mark_superseded(repo, superseded_id, sid)

    return sid, sha, True, version


def _mark_superseded(repo: Path, old_id: str, new_id: str) -> None:
    """将旧版本 manifest 标记为 superseded。"""
    repo = Path(repo)
    manifest_path = repo / MANIFESTS_DIR / f"{old_id}.yaml"
    if not manifest_path.exists():
        return
    try:
        data = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            return
        data["status"] = "superseded"
        data["superseded_by"] = new_id
        data["superseded_at"] = date.today().isoformat()
        write_shared_text(manifest_path,
                          yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
    except Exception:
        pass


def find_knowledge_cards_referencing(repo: Path, source_id: str) -> list[str]:
    """M4-7: 查找引用指定 source_id 的知识卡 ID 列表。

    用于版本更新时生成复核任务。
    """
    from . import store
    referencing = []
    # iter_all 产出顺序为 (meta, body, path)，卡片 ID 取 meta["id"]
    for meta, _body, _path in store.iter_all(repo, include_staging=True):
        source_refs = meta.get("source_refs") or []
        for ref in source_refs:
            if isinstance(ref, dict) and ref.get("source_id") == source_id:
                referencing.append(meta["id"])
                break
    return referencing
