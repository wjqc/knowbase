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
from datetime import date
from pathlib import Path

import yaml

OBJECTS_DIR = Path("sources/objects")
MANIFESTS_DIR = Path("sources/manifests")
SRC_ID_RE = re.compile(r"^SRC-(\d{4})-(\d{4})\.yaml$")

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
    year = year or date.today().year
    seq = 0
    d = Path(repo) / MANIFESTS_DIR
    if d.exists():
        for f in d.glob(f"SRC-{year}-*.yaml"):
            m = SRC_ID_RE.match(f.name)
            if m:
                seq = max(seq, int(m.group(2)))
    return f"SRC-{year}-{seq + 1:04d}"


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


def find_by_sha(repo: Path, sha256: str) -> dict | None:
    for mf in iter_manifests(repo):
        if mf.get("sha256") == sha256:
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
    prior = find_by_sha(repo, sha)
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
