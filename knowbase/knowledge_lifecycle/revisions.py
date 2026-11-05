"""不可变内容版本快照、legacy 导入与影子对账（V2-04）。

权威与存放（设计 §9.1）：
- 已发布内容权威在知识 Git：``lifecycle/revisions/<id>/<revision>.md``（不可
  变快照，写入后内容不可 UPDATE）+ ``lifecycle/publications/``（发布
  manifest，V2-07）。SQLite ``knowledge_revisions`` 等表是本地物化。
- legacy 导入只做快照与对账，不写 published_heads（影子；正式 head 切换
  属于 V2-07 发布内核），不重算旧 revision——历史 feedback 按旧
  content-v1 身份继续可关联。

幂等：同 (memory_id, revision_algo, content_revision) 重跑不产生新行、
不重写快照；快照文件用 O_EXCL 写入，已存在则校验 snapshot_hash 一致。
"""
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .. import store
from . import contracts

REVISIONS_DIR = "lifecycle/revisions"
PUBLICATIONS_DIR = "lifecycle/publications"
IMPORTS_DIR = "lifecycle/imports"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def revision_path(repo: Path, memory_id: str, content_revision: str) -> Path:
    if not contracts.ID_RE.match(memory_id or ""):
        raise ValueError(f"非法卡片 ID: {memory_id!r}")
    if not re_ok(content_revision):
        raise ValueError(f"非法 revision: {content_revision!r}")
    return Path(repo) / REVISIONS_DIR / memory_id / f"{content_revision}.md"


def re_ok(revision: str) -> bool:
    import re
    return bool(re.fullmatch(r"[0-9a-f]{12,64}", revision or ""))


def snapshot_exists(conn, memory_id: str, revision_algo: str, content_revision: str) -> dict | None:
    row = conn.execute(
        "SELECT memory_id,revision_algo,content_revision,snapshot_hash,snapshot_path,"
        "parent_revisions_json,change_id,created_at FROM knowledge_revisions "
        "WHERE memory_id=? AND revision_algo=? AND content_revision=?",
        (memory_id, revision_algo, content_revision)).fetchone()
    if not row:
        return None
    return {"memory_id": row[0], "revision_algo": row[1], "content_revision": row[2],
            "snapshot_hash": row[3], "snapshot_path": row[4],
            "parent_revisions": json.loads(row[5]), "change_id": row[6],
            "created_at": row[7]}


def record_revision(repo: Path, conn, meta: dict, body: str, *,
                    change_id: str = "", parent_revisions: list | None = None,
                    raw_bytes: bytes | None = None) -> dict:
    """写入一个不可变内容版本快照并登记。幂等：同身份已存在时校验一致返回。

    raw_bytes 提供时以原字节为快照内容（审计原件，CRLF/BOM 不抹掉）；
    默认以 store.render 的规范 LF 渲染为准。snapshot_hash 是快照文件字节
    全文 SHA256；content_revision 语义 hash（v1 或 v2）照卡片标记派发。
    """
    repo = Path(repo)
    identity = contracts.revision_identity(meta, body)
    memory_id = meta.get("id", "")
    algo, revision = identity["revision_algo"], identity["content_revision"]
    existing = snapshot_exists(conn, memory_id, algo, revision)
    parents = sorted(parent_revisions or [])
    if existing:
        path = repo / existing["snapshot_path"]
        if not path.is_file():
            return {"error": "SNAPSHOT_FILE_MISSING", "identity": existing}
        on_disk = hashlib.sha256(path.read_bytes()).hexdigest()
        if on_disk != existing["snapshot_hash"]:
            return {"error": "SNAPSHOT_HASH_CONFLICT", "identity": existing}
        return {"identity": existing, "idempotent": True}

    path = revision_path(repo, memory_id, revision)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = raw_bytes if raw_bytes is not None else store.render(meta, body).encode("utf-8")
    snap_hash = contracts.snapshot_hash(data)
    temp = path.with_name(path.name + ".tmp-" + os.urandom(6).hex())
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    rel = str(path.relative_to(repo))
    conn.execute(
        "INSERT INTO knowledge_revisions(memory_id,revision_algo,content_revision,"
        "snapshot_hash,snapshot_path,parent_revisions_json,change_id,applicability_json,"
        "claims_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (memory_id, algo, revision, snap_hash, rel, json.dumps(parents, ensure_ascii=False),
         change_id, json.dumps(meta.get("applicability") or {}, ensure_ascii=False),
         json.dumps(meta.get("claims") or [], ensure_ascii=False), _now()))
    return {"identity": {"memory_id": memory_id, "revision_algo": algo,
                         "content_revision": revision, "snapshot_hash": snap_hash,
                         "snapshot_path": rel, "parent_revisions": parents,
                         "change_id": change_id, "created_at": _now()},
            "idempotent": False}


def load_revision(repo: Path, conn, memory_id: str, revision_algo: str,
                  content_revision: str) -> dict:
    """独立读回一个快照：返回原字节、语义 meta/body 与登记信息。"""
    existing = snapshot_exists(conn, memory_id, revision_algo, content_revision)
    if not existing:
        return {"error": "NOT_FOUND"}
    path = Path(repo) / existing["snapshot_path"]
    if not path.is_file():
        return {"error": "SNAPSHOT_FILE_MISSING", "identity": existing}
    data = path.read_bytes()
    meta, body = store.parse_text(data.decode("utf-8-sig"), str(path))
    return {"identity": existing, "raw": data, "meta": meta, "body": body,
            "hash_ok": contracts.snapshot_hash(data) == existing["snapshot_hash"]}


def _import_manifest_path(repo: Path, digest_hex: str) -> Path:
    return Path(repo) / IMPORTS_DIR / f"legacy-{digest_hex[:24]}.json"


def legacy_import(repo: Path, conn, *, dry_run: bool = False) -> dict:
    """把现存全部卡片（含 staging）的当前内容版本登记为不可变快照。

    - 每卡按 content-v1 身份（历史卡不带 v2 标记）登记，快照保留磁盘原字节
      （CRLF/BOM 不改写）；语义等价不产生两版。
    - 畸形卡（parse 失败）、缺 id、重复 id 单列，不阻断其他卡。
    - 影子对账：entity_heads 中登记的 content_revision 与重算 v1 hash 不一致
      的卡单列 drift（不修改 entity_heads；真实修复在 V2-17 受控迁移）。
    - 幂等：重复执行快照与登记零增量；同 digest 的 import manifest 只写一次。
    """
    repo = Path(repo)
    report = {"dry_run": dry_run, "snapshots": 0, "existing": 0, "errors": [],
              "drift": [], "needs_review": [], "manifest": ""}
    parse_errors: list = []
    cards = list(store.iter_all(repo, include_staging=True, errors=parse_errors))
    for path, exc in parse_errors:
        report["needs_review"].append({"path": str(path), "reason": f"parse:{type(exc).__name__}"})
    seen_ids: set[str] = set()
    rows = []
    for meta, body, path in cards:
        mid = str(meta.get("id") or "")
        if not contracts.ID_RE.match(mid):
            report["needs_review"].append(
                {"path": str(path), "reason": f"invalid_id:{mid[:24]}"})
            continue
        if mid in seen_ids:
            report["needs_review"].append(
                {"path": str(path), "reason": f"duplicate_id:{mid}"})
            continue
        seen_ids.add(mid)
        identity = contracts.revision_identity(meta, body)
        head = conn.execute(
            "SELECT content_revision FROM entity_heads WHERE entity_id=?", (mid,)).fetchone()
        if head and head[0] != identity["content_revision"]:
            report["drift"].append({"memory_id": mid, "head_revision": head[0],
                                    "recomputed": identity["content_revision"]})
        rows.append({"memory_id": mid, "revision_algo": identity["revision_algo"],
                     "content_revision": identity["content_revision"],
                     "path": str(path.relative_to(repo)) if path.is_relative_to(repo) else str(path)})
    if dry_run:
        report["candidates"] = len(rows)
        return report
    for meta, body, path in cards:
        mid = str(meta.get("id") or "")
        if not contracts.ID_RE.match(mid) or mid not in seen_ids:
            continue
        raw = path.read_bytes()
        result = record_revision(repo, conn, meta, body, change_id="legacy-import",
                                 raw_bytes=raw)
        if result.get("error"):
            report["errors"].append({"memory_id": mid, "error": result["error"]})
        elif result["idempotent"]:
            report["existing"] += 1
        else:
            report["snapshots"] += 1
    manifest = {
        "schema": "knowbase.lifecycle.legacy-import.v1",
        "generated_at": _now(),
        "repo": str(repo),
        "counts": {"snapshots": report["snapshots"], "existing": report["existing"],
                   "needs_review": len(report["needs_review"]), "drift": len(report["drift"])},
        "cards": rows,
        "needs_review": report["needs_review"],
        "drift": report["drift"],
        "note": "shadow import only; published_heads untouched; entity_heads unchanged",
    }
    # 显式提交：快照文件写入不依赖事务，但登记行必须落库——漏提交时连接
    # 关闭即回滚，磁盘文件与 DB 账本脱节（真实库首跑曾因此回滚）。
    conn.commit()
    digest_hex = hashlib.sha256(
        contracts.canonical_json(
            {"cards": sorted(rows, key=lambda r: r["memory_id"]),
             "needs_review": report["needs_review"], "drift": report["drift"]}
        ).encode("utf-8")).hexdigest()
    mpath = _import_manifest_path(repo, digest_hex)
    if not mpath.exists():
        mpath.parent.mkdir(parents=True, exist_ok=True)
        store.write_shared_text(mpath, json.dumps(manifest, ensure_ascii=False, indent=1))
    report["manifest"] = str(mpath)
    report["candidates"] = len(rows)
    return report


def shadow_heads_compare(repo: Path, conn) -> dict:
    """影子对账：published_heads（V2 服务视图）与 entity_heads（现行索引视图）
    差异。legacy 导入后 published_heads 为空属预期；V2-07 发布后此函数用于
    验证投影与镜像一致。"""
    repo = Path(repo)
    pub = {r[0]: r[1] for r in conn.execute(
        "SELECT memory_id,content_revision FROM published_heads")}
    heads = {r[0]: r[1] for r in conn.execute(
        "SELECT entity_id,content_revision FROM entity_heads")}
    return {"published_only": sorted(set(pub) - set(heads)),
            "heads_only": sorted(set(heads) - set(pub)),
            "both_mismatch": [{"memory_id": mid, "published": pub[mid], "head": heads[mid]}
                              for mid in sorted(set(pub) & set(heads)) if pub[mid] != heads[mid]]}
