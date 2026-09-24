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

OBJECTS_DIR = Path("sources/objects")
MANIFESTS_DIR = Path("sources/manifests")
# 兼容旧格式 SRC-2026-0001 和新格式 SRC-<uuid12>
SRC_ID_RE = re.compile(r"^SRC-(\d{4})-(\d{4})\.yaml$|^SRC-([0-9a-f]{12})\.yaml$")

# 后缀 → manifest kind（粗分类：文档 / 代码 / 日志）
_KIND_BY_EXT = {".py": "code", ".js": "code", ".ts": "code", ".java": "code", ".log": "log"}


def kind_for(suffix: str) -> str:
    return _KIND_BY_EXT.get((suffix or "").lower(), "document")


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
                  source_modified_at: str = "") -> tuple[str, str, bool]:
    """写入一个 source artifact，返回 (src_id, sha256, created)。

    同内容（sha256）已存在时不重复写，返回既有 id 且 created=False；
    同路径源文件变化 = 新 sha = 新快照（内容寻址语义，无覆盖问题）。
    """
    repo = Path(repo)
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    prior = find_by_sha(repo, sha, scope=scope)
    if prior:
        return str(prior["id"]), sha, False
    ensure_dirs(repo)
    sid = alloc_id(repo)
    (repo / OBJECTS_DIR / f"{sha}.md").write_text(text, encoding="utf-8")
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
    (repo / MANIFESTS_DIR / f"{sid}.yaml").write_text(
        yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return sid, sha, True


def load_manifest(repo: Path, source_id: str) -> dict | None:
    """加载指定 source_id 的 manifest。"""
    repo = Path(repo)
    manifest_path = repo / MANIFESTS_DIR / f"{source_id}.yaml"
    if not manifest_path.exists():
        return None
    try:
        data = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else None
    except Exception:
        return None


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
    (repo / OBJECTS_DIR / f"{sha}.md").write_text(text, encoding="utf-8")
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

    (repo / MANIFESTS_DIR / f"{sid}.yaml").write_text(
        yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False), encoding="utf-8")

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
        manifest_path.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    except Exception:
        pass


def find_knowledge_cards_referencing(repo: Path, source_id: str) -> list[str]:
    """M4-7: 查找引用指定 source_id 的知识卡 ID 列表。

    用于版本更新时生成复核任务。
    """
    from . import store
    referencing = []
    for mid, meta, _ in store.iter_all(repo, include_staging=True):
        source_refs = meta.get("source_refs") or []
        for ref in source_refs:
            if isinstance(ref, dict) and ref.get("source_id") == source_id:
                referencing.append(mid)
                break
    return referencing
