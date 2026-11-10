"""统一发布内核（V2-07）：单卡与多卡（MERGE/SPLIT）同一事务语义。

发布点序列（设计 §6.3）：
1. 锁外准备：候选输出渲染为不可变快照字节、构造 manifest。
2. RepoLock 内：
   a. 重读全部 base heads/policy/validation digest，不符 → REVISION_CONFLICT；
   b. publication_batches 落 prepared（含 before/after hash）；
   c. 写全部不可变 revision 快照（逐项 hash 校验）；
   d. manifest 原子 rename 为 durable commit point（pending → 正式名）；
   e. SQLite 短事务：published_heads 全批一次切换 + 兼容镜像（类型目录
      Markdown）+ 索引 + 处置（alias/retired/redirect）；
3. 独立读回：manifest、published_heads、镜像逐项 hash 核对。

恢复语义：
- prepared 无 manifest：半途作废，recover 丢弃（prepared 版本从未服务）；
- manifest 在而 SQLite 未切完：roll-forward，按 manifest 重建 heads/镜像；
- 回滚 = 新 publication 指回旧 revision；不删历史、不复活已撤销 verified
  （回滚发布的新 head confidence 一律置 once，治理由证据重算）。
"""
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .. import config, gitops, index, store
from ..locking import RepoLock
from . import changes as _changes
from . import contracts, revisions

PUBLISHED = "published"
MATERIALIZED = "materialized"
PREPARED = "prepared"
REPAIR = "repair_required"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write(path: Path, text: str) -> None:
    from ..commit_adapter import _atomic_write as _aw
    _aw(path, text)


def _manifest_path(repo: Path, publication_id: str) -> Path:
    return Path(repo) / revisions.PUBLICATIONS_DIR / f"{publication_id}.json"


def _validation_digest(conn, change_id: str) -> str | None:
    rows = conn.execute(
        "SELECT report_id,result FROM admission_reports WHERE change_id=? "
        "ORDER BY created_at", (change_id,)).fetchall()
    passed = [r[0] for r in rows if r[1] == "pass"]
    if not passed:
        return None
    import hashlib
    return hashlib.sha256("|".join(sorted(passed)).encode("utf-8")).hexdigest()[:32]


def publish(repo, conn, change_id: str, *, expected_policy=contracts.CHANGE_POLICY_REVISION,
            operator: str = "", require_validation: bool = True) -> dict:
    """把一个 ready 候选发布为可见变更集。同 change 只有一个 publication。"""
    repo = Path(repo)
    change = _changes.get_change(conn, change_id)
    if change.get("error"):
        return change
    if change["relation"] == "NO_CHANGE":
        return {"error": "NO_CHANGE_NOT_PUBLISHABLE",
                "detail": "处置已记录于候选，无内容输出"}
    if change["state"] == "published":
        row = conn.execute("SELECT publication_id FROM publication_batches "
                           "WHERE change_id=?", (change_id,)).fetchone()
        return {"error": "ALREADY_PUBLISHED", "publication_id": row[0] if row else None}
    if change["state"] != "ready":
        return {"error": "NOT_READY", "state": change["state"]}
    if change["relation"] == "NO_CHANGE":
        return {"error": "NO_CHANGE_NOT_PUBLISHABLE",
                "detail": "处置已记录于候选，无内容输出"}
    validation_digest = _validation_digest(conn, change_id)
    if require_validation and not validation_digest:
        return {"error": "VALIDATION_FAILED", "detail": "no passing validation/admission report"}
    outputs = change["outputs"]
    if not outputs:
        return {"error": "NO_OUTPUTS"}
    publication_id = "PUB-" + uuid.uuid4().hex[:20]
    before_after = {}
    cfg = config.load_config()
    with RepoLock(repo, cfg.get("lock_timeout", 10)):
        # a. 重读 base heads。
        for head in change["base_heads"]:
            row = conn.execute(
                "SELECT content_revision FROM entity_heads WHERE entity_id=?",
                (head["memory_id"],)).fetchone()
            current = row[0] if row else None
            if current != head["content_revision"]:
                _changes.set_state(conn, change_id, "needs_rebase",
                                   reason="base head moved before publish")
                return {"error": "REVISION_CONFLICT", "base_head": head["memory_id"],
                        "expected": head["content_revision"], "current": current}
        # a2. NEW 提案的源是 staging 文件：立案后源内容若被改过，冻结输出已过期。
        #     与 a 的 base-head 重读同一性质（发布前重验输入未动），否则正式镜像
        #     会写回旧内容、随后的 staging 清理会丢掉新编辑。仅 NEW 需要此检查：
        #     其他关系的输入由 base heads 守卫，staging 文件不是其来源。
        if change["relation"] == "NEW":
            for out in outputs:
                staging_path = Path(repo) / "staging" / f"{out['memory_id']}.md"
                if not staging_path.exists():
                    continue
                cur_meta, cur_body = store.parse(staging_path)
                cur_hash = contracts.snapshot_hash(
                    contracts.canonical_json({"meta": cur_meta, "body": cur_body})
                    .encode("utf-8"))
                if cur_hash != out["snapshot_hash"]:
                    _changes.set_state(conn, change_id, "needs_rebase",
                                       reason="staging source changed after proposal")
                    return {"error": "SOURCE_MOVED", "memory_id": out["memory_id"],
                            "detail": "staging source changed after proposal; re-propose"}
        if change["policy_revision"] != expected_policy:
            return {"error": "POLICY_CONFLICT",
                    "candidate": change["policy_revision"], "expected": expected_policy}
        # b. prepared 批次登记。
        conn.execute(
            "INSERT INTO publication_batches VALUES(?,?,?,?,?,?,?,?,?)",
            (publication_id, change_id, json.dumps(change["base_heads"], ensure_ascii=False),
             "", str(_manifest_path(repo, publication_id).relative_to(repo)),
             PREPARED, json.dumps(before_after, ensure_ascii=False), "", _now()))
        conn.commit()
        # c. 不可变快照：先保住被替换的旧 head（旧版本可读回是硬不变式，
        #    未做过 legacy 导入的库在此补拍），再写全部输出快照。
        for head in change["base_heads"]:
            old_meta, old_body, old_path = store.load(repo, head["memory_id"])
            if not old_meta:
                continue
            revisions.record_revision(
                repo, conn, old_meta, old_body, change_id=f"base:{change_id}",
                parent_revisions=[], raw_bytes=old_path.read_bytes())
        written = []
        for out in outputs:
            result = revisions.record_revision(
                repo, conn, out["meta"], out["body"], change_id=change_id,
                parent_revisions=[h["content_revision"] for h in change["base_heads"]
                                  if h["memory_id"] == out["memory_id"]],
                raw_bytes=store.render(out["meta"], out["body"]).encode("utf-8"))
            if result.get("error"):
                conn.execute("UPDATE publication_batches SET state=? WHERE publication_id=?",
                             (REPAIR, publication_id))
                conn.commit()
                return {"error": result["error"], "stage": "snapshot",
                        "output": out["memory_id"]}
            written.append(result["identity"])
        manifest = {
            "schema": "knowbase.lifecycle.publication.v1",
            "publication_id": publication_id, "change_id": change_id,
            "relation": change["relation"], "risk": change["risk"], "scope": change["scope"],
            "policy_revision": change["policy_revision"], "operator": operator,
            "validation_digest": validation_digest,
            "base_heads": change["base_heads"],
            "outputs": [{"memory_id": o["memory_id"],
                         "content_revision": next(w["content_revision"] for w in written
                                                  if w["memory_id"] == o["memory_id"]),
                         "revision_algo": next(w["revision_algo"] for w in written
                                               if w["memory_id"] == o["memory_id"]),
                         "snapshot_hash": o["snapshot_hash"],
                         "disposition": o["disposition"]} for o in outputs],
            "source_dispositions": change["source_dispositions"],
            "claim_map": change["claim_map"],
            "created_at": _now(),
        }
        import hashlib as _h
        manifest_hash = _h.sha256(contracts.canonical_json(manifest).encode("utf-8")).hexdigest()
        manifest["manifest_hash"] = manifest_hash
        # d. manifest 原子 rename = durable commit point。
        mpath = _manifest_path(repo, publication_id)
        mpath.parent.mkdir(parents=True, exist_ok=True)
        pending = mpath.with_name(mpath.name + ".pending")
        store.write_shared_text(pending, json.dumps(manifest, ensure_ascii=False, indent=1))
        pending.replace(mpath)
        conn.execute("UPDATE publication_batches SET state=?,manifest_hash=?,"
                     "before_after_hashes_json=? WHERE publication_id=?",
                     (PUBLISHED, manifest_hash,
                      json.dumps({o["memory_id"]: {
                          "before": next((h["content_revision"] for h in change["base_heads"]
                                          if h["memory_id"] == o["memory_id"]), ""),
                          "after": next(w["content_revision"] for w in written
                                        if w["memory_id"] == o["memory_id"])}
                          for o in outputs}, ensure_ascii=False), publication_id))
        conn.commit()
        # e. SQLite 一次切换 heads + 兼容镜像 + 处置 + 索引。
        _materialize(repo, conn, manifest, outputs)
        _changes.set_state(conn, change_id, "published",
                           reason=f"publication {publication_id}")
    # 3. 独立读回。
    rb = readback(repo, conn, publication_id)
    result = {"publication_id": publication_id, "change_id": change_id,
              "local_published": True, "manifest_hash": manifest_hash,
              "readback": rb, "sync_state": "git_pending"}
    if cfg.get("git", {}).get("auto_commit", True):
        paths = [mpath] + [repo / i["snapshot_path"] for i in written]
        for out in outputs:
            target = store.mem_path(repo, out["meta"].get("type", "pitfall"),
                                    out["meta"]["id"],
                                    staging=False)
            if target.exists():
                paths.append(target)
        try:
            gitops.commit_paths(repo, f"lifecycle publish {publication_id} ({change['relation']})",
                                paths)
            result["sync_state"] = "git_committed"
        except Exception as exc:  # Git 失败不撤销已确认本地发布
            result["sync_state"] = "git_pending"
            result["git_warning"] = type(exc).__name__
    return result


def _materialize(repo: Path, conn, manifest: dict, outputs: list[dict]) -> None:
    """SQLite 短事务 + 镜像写入：published_heads 全批一次切换。"""
    dispositions = {d["memory_id"]: d for d in manifest["source_dispositions"]}
    by_mid = {o["memory_id"]: o for o in outputs}
    try:
        conn.execute("BEGIN IMMEDIATE")
        for out in manifest["outputs"]:
            mid = out["memory_id"]
            conn.execute(
                "INSERT INTO published_heads VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(memory_id) DO UPDATE SET content_revision=excluded.content_revision,"
                "revision_algo=excluded.revision_algo,publication_id=excluded.publication_id,"
                "visibility=excluded.visibility,redirect_ids_json=excluded.redirect_ids_json",
                (mid, out["content_revision"], out["revision_algo"],
                 manifest["publication_id"], out["disposition"],
                 json.dumps((dispositions.get(mid) or {}).get("redirect_ids") or []),
                 manifest["manifest_hash"]))
        conn.execute("UPDATE publication_batches SET state=? WHERE publication_id=?",
                     (MATERIALIZED, manifest["publication_id"]))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    # 兼容镜像：正式目录 Markdown 是最新内容镜像（可 diff、旧 reader 可读）。
    for out in outputs:
        meta = dict(out["meta"])
        mid = meta["id"]
        disposition = out.get("disposition", "active")
        if disposition == "active":
            meta["confidence"] = "once"  # 新版本不继承 verified（设计 §6.2）
            meta.pop("verified_content_revision", None)
            meta.pop("last_verified", None)
            meta["helpful_count"] = 0
            meta["unhelpful_count"] = 0
            target = store.mem_path(repo, meta.get("type", "pitfall"), mid, staging=False)
            target.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(target, store.render(meta, out["body"]))
            # NEW 提案的源是 staging 文件：正式镜像落盘后删除 staging 源，
            # 否则 iter_all(include_staging=True) 会两次产出同一张卡、
            # pending_admissions 会重复列出已发布卡。a2 已在发布前校验源未动，
            # 此处删的是与冻结输出一致的源文件，不丢编辑。
            staging_path = Path(repo) / "staging" / f"{mid}.md"
            if staging_path.exists() and staging_path.resolve() != target.resolve():
                staging_path.unlink()
            index.upsert(conn, *store.load(repo, mid), commit=False)
        else:  # alias / retired / redirect：旧卡处置镜像
            old_meta, old_body, old_path = store.load(repo, mid)
            if old_meta:
                old_meta["status"] = "archived" if disposition != "redirect" else "active"
                old_meta["redirect_ids"] = (dispositions.get(mid) or {}).get(
                    "redirect_ids") or []
                old_meta["archive_reason"] = f"{manifest['relation']}:{manifest['publication_id']}"
                _atomic_write(old_path, store.render(old_meta, old_body))
                index.upsert(conn, *store.load(repo, mid), commit=False)
    conn.commit()


def readback(repo: Path, conn, publication_id: str) -> dict:
    """独立读回：manifest、heads、快照逐项核对。"""
    repo = Path(repo)
    row = conn.execute(
        "SELECT change_id,manifest_hash,state FROM publication_batches "
        "WHERE publication_id=?", (publication_id,)).fetchone()
    if not row:
        return {"error": "NOT_FOUND"}
    mpath = _manifest_path(repo, publication_id)
    if not mpath.is_file():
        return {"error": "MANIFEST_MISSING", "state": row[2]}
    manifest = json.loads(mpath.read_text(encoding="utf-8"))
    problems = []
    if manifest.get("manifest_hash") != row[1]:
        problems.append("manifest_hash_mismatch")
    heads_ok = True
    for out in manifest["outputs"]:
        head = conn.execute(
            "SELECT content_revision FROM published_heads WHERE memory_id=?",
            (out["memory_id"],)).fetchone()
        if not head or head[0] != out["content_revision"]:
            heads_ok = False
            problems.append(f"head_not_switched:{out['memory_id']}")
        snap = conn.execute(
            "SELECT snapshot_hash,snapshot_path FROM knowledge_revisions WHERE "
            "memory_id=? AND content_revision=?",
            (out["memory_id"], out["content_revision"])).fetchone()
        if not snap:
            problems.append(f"snapshot_missing:{out['memory_id']}")
        else:
            data = (repo / snap[1]).read_bytes()
            if contracts.snapshot_hash(data) != snap[0]:
                problems.append(f"snapshot_hash_mismatch:{out['memory_id']}")
    return {"publication_id": publication_id, "state": row[2],
            "heads_switched": heads_ok, "problems": problems,
            "ok": not problems and row[2] == MATERIALIZED}


def recover(repo, conn) -> dict:
    """启动恢复：prepared 作废（从未服务）；published 未物化 → roll-forward。

    published manifest 是 durable commit point：manifest 在即重建 heads/镜像，
  不回滚已向用户确认的版本；镜像/Git 后续失败继续修复，不偷偷回退。"""
    repo = Path(repo)
    result = {"discarded_prepared": [], "rolled_forward": [], "already_ok": 0}
    for pub_id, change_id, state in conn.execute(
            "SELECT publication_id,change_id,state FROM publication_batches").fetchall():
        mpath = _manifest_path(repo, pub_id)
        if state == PREPARED:
            if mpath.exists():
                mpath.unlink(missing_ok=True)
            conn.execute("UPDATE publication_batches SET state=? WHERE publication_id=?",
                         ("rejected", pub_id))
            result["discarded_prepared"].append(pub_id)
        elif state == PUBLISHED and mpath.exists():
            manifest = json.loads(mpath.read_text(encoding="utf-8"))
            change = _changes.get_change(conn, change_id)
            outputs = change.get("outputs", [])
            _materialize(repo, conn, manifest, outputs)
            result["rolled_forward"].append(pub_id)
        elif state == MATERIALIZED:
            result["already_ok"] += 1
    conn.commit()
    return result


def rollback(repo, conn, publication_id: str, *, reason: str,
             operator: str = "") -> dict:
    """回滚 = 新 publication 指回 before revision；保留引发回滚的反证记录。

    不删除坏版本快照与 manifest；回滚后的 head confidence=once（旧证据不
    转嫁，verified 由当前证据重算）。"""
    repo = Path(repo)
    row = conn.execute(
        "SELECT change_id,before_after_hashes_json FROM publication_batches "
        "WHERE publication_id=? AND state=?", (publication_id, MATERIALIZED)).fetchone()
    if not row:
        return {"error": "NOT_PUBLISHED"}
    change_id, before_after = row[0], json.loads(row[1])
    change = _changes.get_change(conn, change_id)
    if change.get("error"):
        return change
    outputs = []
    for out in change["outputs"]:
        mid = out["memory_id"]
        before_rev = (before_after.get(mid) or {}).get("before")
        if not before_rev:
            continue  # 新增卡回滚 = retired 处置，不恢复正文
        snap = conn.execute(
            "SELECT snapshot_path FROM knowledge_revisions WHERE memory_id=? AND "
            "content_revision=?", (mid, before_rev)).fetchone()
        if not snap:
            return {"error": "BEFORE_SNAPSHOT_MISSING", "memory_id": mid}
        meta, body = store.parse(repo / snap[0])
        meta["confidence"] = "once"
        meta.pop("verified_content_revision", None)
        meta["rollback_reason"] = reason[:200]
        outputs.append({"memory_id": mid, "disposition": "active",
                        "meta": meta, "body": body})
    if not outputs:
        return {"error": "NOTHING_TO_ROLLBACK",
                "detail": "publication only created new cards; archive them instead"}
    # 回滚也走同一发布内核：以当前 heads 为 base 的新 change。
    base_heads = []
    for out in outputs:
        head = conn.execute(
            "SELECT content_revision FROM published_heads WHERE memory_id=?",
            (out["memory_id"],)).fetchone()
        base_heads.append({"memory_id": out["memory_id"],
                           "revision_algo": "content-v1",
                           "content_revision": head[0] if head else ""})
    proposal = _changes.propose_change(
        conn, repo, relation="REPLACE", risk="R2", scope=change["scope"],
        base_heads=base_heads, outputs=outputs,
        source_dispositions=[{"memory_id": o["memory_id"], "action": "revise",
                              "redirect_ids": []} for o in outputs],
        claim_map=[{"old_claim_id": "C-whole", "new_claim_ids": ["C-whole"],
                    "mapping": "preserved",
                    "justification_ref": f"rollback:{publication_id}"}],
        proposer=f"rollback:{operator or 'system'}")
    if proposal.get("error"):
        return proposal
    _changes.set_state(conn, proposal["change_id"], "ready",
                       reason=f"rollback of {publication_id}: {reason}")
    conn.execute(
        "INSERT OR REPLACE INTO admission_reports VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("ADM-rollback-" + publication_id[-12:], outputs[0]["memory_id"],
         base_heads[0]["content_revision"], proposal["change_id"], "", "R2", "pass",
         json.dumps([{"check": "rollback", "result": "pass", "detail": reason[:200]}]),
         "{}", reason[:200], "lifecycle-rollback", "", _now()))
    conn.commit()
    return publish(repo, conn, proposal["change_id"], operator=operator or "rollback")
