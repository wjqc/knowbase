"""同卡并发冲突隔离区：双版本保留 + 证据强度自动裁决。

B 轻量路线第 (3) 件的实现：

- 冲突记录：card_conflicts 表（迁移 v23）+ .git/knowbase-conflicts/ 下的
  版本原文（local/remote，auto-merge 时另存 merged）。隔离文件放在 .git
  内，永不进 Git、不被推送、不污染 Markdown 唯一真相。
- 自动裁决：按版本绑定证据强度比较本地版与远端版——helpful 任务数为主，
  frontmatter 的 last_verified 为次；双方均有负面证据或强度打平 → 保持
  open：双版本同时隔离、doctor 可见，等后续执行反馈再裁决；绝不自动
  选边、绝不静默丢版本。
- 非打平：胜者版原文写回工作树（content_revision 由文件内容算出，原文
  写回不改变修订号，版本绑定证据继续有效），败者版仍保留在隔离区。
- 团队汇总可见：记录冲突时顺带发一条 conflict.quarantined 价值事件
  （未启用 value 事件时为空操作），随价值导出进入团队快照。

调用约定：adjudicate / restore 假定调用方已持有 RepoLock（同步合流路径
下成立）；readjudicate_card 自己取锁，供反馈路径调用。
"""

import re
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import index, store
from .mutations import _compute_governance_revision, _compute_revision

QUARANTINE_DIRNAME = "knowbase-conflicts"
_ID_RE = re.compile(r"^[A-Z]{1,2}-\d{4}-\d{4}$|^[A-Z]{1,2}-[0-9a-f]{12}$")


from .processes import no_window

def _git(repo: Path, *args: str, timeout: float = 10.0) -> str:
    """隔离区自用的最小 git 读取（不依赖 gitops，避免模块循环导入）。"""
    env = {"GIT_TERMINAL_PROMPT": "0"}
    import os
    full_env = os.environ.copy()
    full_env.update(env)
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], stdin=subprocess.DEVNULL,
        capture_output=True, text=True, encoding="utf-8", timeout=timeout,
        env=full_env, **no_window())
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip())
    return proc.stdout.strip()


def git_dir_of(repo: Path) -> Path:
    """解析 .git 目录（兼容 worktree 的 .git 文件）。"""
    out = _git(repo, "rev-parse", "--git-dir", timeout=3)
    p = Path(out)
    if not p.is_absolute():
        p = Path(repo) / p
    return p.resolve()


def _parse_text_or_empty(text: str) -> tuple[dict, str]:
    try:
        return store.parse_text(text)
    except Exception:
        return {}, text


def _revision_of(text: str) -> tuple[str, str]:
    meta, body = _parse_text_or_empty(text)
    if not meta:
        return "", ""
    return _compute_revision(meta, body), _compute_governance_revision(meta)


def _last_verified_of(text: str) -> str:
    meta, _ = _parse_text_or_empty(text)
    return meta.get("last_verified", "") or ""


def record_conflict(repo: Path, conn, card_id: str, base_rev: str,
                    local_text: str | None, remote_text: str | None,
                    merged_text: str | None = None, source: str = "") -> dict:
    """记录一次同卡并发冲突，写隔离文件 + DB 行，返回记录 dict。

    local/remote/merged 为版本原文（None 表示该侧缺失，如删除侧）。
    """
    conflict_id = f"CF-{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    qdir = git_dir_of(repo) / QUARANTINE_DIRNAME
    qdir.mkdir(parents=True, exist_ok=True)
    files: dict[str, str] = {}
    for key, text in (("local", local_text), ("remote", remote_text),
                      ("merged", merged_text)):
        if text is None:
            files[key] = ""
            continue
        fpath = qdir / f"{conflict_id}-{key}.md"
        fpath.write_text(text, encoding="utf-8")
        files[key] = str(fpath)
    local_rev, _ = _revision_of(local_text) if local_text is not None else ("", "")
    remote_rev, _ = _revision_of(remote_text) if remote_text is not None else ("", "")
    conn.execute(
        "INSERT INTO card_conflicts(conflict_id,card_id,base_content_revision,"
        "local_content_revision,remote_content_revision,status,source,local_file,"
        "remote_file,merged_file,adjudication_note,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (conflict_id, card_id, base_rev or "", local_rev, remote_rev, "open",
         source, files["local"], files["remote"], files["merged"], "", now, now))
    conn.commit()
    try:
        from .value.events import record as _record_value
        _record_value(conn, "conflict.quarantined",
                      {"conflict_id": conflict_id, "card_id": card_id,
                       "local_content_revision": local_rev,
                       "remote_content_revision": remote_rev, "source": source},
                      source_key=f"card_conflict:{conflict_id}")
    except Exception:
        pass  # 价值事件是观测通道，失败不得影响隔离记录本身
    return {"conflict_id": conflict_id, "card_id": card_id, "status": "open",
            "base_content_revision": base_rev or "",
            "local_content_revision": local_rev,
            "remote_content_revision": remote_rev, "source": source,
            "local_file": files["local"], "remote_file": files["remote"],
            "merged_file": files["merged"]}


def get_record(conn, conflict_id: str) -> dict | None:
    row = conn.execute(
        "SELECT conflict_id,card_id,base_content_revision,local_content_revision,"
        "remote_content_revision,status,source,local_file,remote_file,merged_file,"
        "adjudication_note FROM card_conflicts WHERE conflict_id=?",
        (conflict_id,)).fetchone()
    if not row:
        return None
    keys = ("conflict_id", "card_id", "base_content_revision", "local_content_revision",
            "remote_content_revision", "status", "source", "local_file", "remote_file",
            "merged_file", "adjudication_note")
    return dict(zip(keys, row))


def list_open(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT conflict_id,card_id,status,source,created_at,adjudication_note "
        "FROM card_conflicts WHERE status='open' ORDER BY created_at").fetchall()
    return [{"conflict_id": r[0], "card_id": r[1], "status": r[2], "source": r[3],
             "created_at": r[4], "adjudication_note": r[5]} for r in rows]


def _read_file(path: str) -> str | None:
    if not path:
        return None
    try:
        return Path(path).read_text(encoding="utf-8")
    except Exception:
        return None


def _strength(conn, card_id: str, revision: str, last_verified: str) -> tuple:
    """证据强度：((helpful 任务数, last_verified), 负面证据任务数)。修订号为空时强度为零。

    注意：空修订分支必须返回与正常分支相同的 ((int, str), int) 结构；
    旧代码返回 (0, "") 会使调用方解包为 strength=0、neg=""，随后
    `neg > 0` 抛 TypeError——无修订号的冲突版本复验必崩。
    """
    if not revision:
        return (0, ""), 0
    from .evolution import evidence_for_revision
    try:
        ev = evidence_for_revision(conn, card_id, revision)
    except Exception:
        ev = {"helpful_tasks": 0, "negative_evidence_tasks": 0}
    return (ev["helpful_tasks"], last_verified or ""), ev["negative_evidence_tasks"]


def _restore_winner(repo: Path, conn, record: dict, winner_text: str,
                    base_rev: str) -> str | None:
    """把胜者版原文写回工作树并提交（调用方须持有 RepoLock）。

    原文写回不改变 content_revision；索引与 entity_heads 按胜者版重算；
    提交附带 base trailer（写前工作树版本），与 B-(1) 口径一致。
    失败返回告警字符串，成功返回 None。
    """
    from . import gitops
    path = store.find_file(repo, record["card_id"])
    if not path:
        return "胜者版无对应工作树文件，无法写回"
    meta, body = _parse_text_or_empty(winner_text)
    if not meta:
        return "胜者版无法解析，无法写回"
    try:
        from .commit_adapter import _atomic_write
        _atomic_write(path, winner_text)
        content_rev = _compute_revision(meta, body)
        gov_rev = _compute_governance_revision(meta)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn.execute(
            "INSERT OR REPLACE INTO entity_heads(entity_id,content_revision,"
            "governance_revision,updated_at) VALUES(?,?,?,?)",
            (record["card_id"], content_rev, gov_rev, now))
        conn.commit()
        index.upsert(conn, meta, body, path, path.parent.name == "staging")
        try:
            cfg = None
            try:
                from . import config as _config
                cfg = _config.load_config()
            except Exception:
                cfg = None
            if cfg is not None:
                index.build_index_md(repo, cfg)
        except Exception:
            pass  # INDEX.md 只是本地速览，下次写入时重建
        msg = (f"conflict-resolve({record['card_id']}): adjudicated winner "
               f"({record['conflict_id']})")
        msg = gitops.with_base_trailers(msg, [(record["card_id"], base_rev)])
        warn = gitops.commit_paths(repo, msg, [path])
        return warn
    except Exception as exc:
        return f"胜者版写回失败: {exc}"


def adjudicate(repo: Path, conn, record: dict) -> tuple[str, str, bool]:
    """对一条冲突记录做证据强度自动裁决（调用方须持有 RepoLock）。

    返回 (status, note, restored)：restored 表示胜者版写回并产生了新提交。
    打平或双方均有负面证据 → status 保持 open，不选边。
    """
    local_text = _read_file(record.get("local_file", ""))
    remote_text = _read_file(record.get("remote_file", ""))
    local_strength, local_neg = _strength(
        conn, record["card_id"], record.get("local_content_revision", ""),
        _last_verified_of(local_text) if local_text is not None else "")
    remote_strength, remote_neg = _strength(
        conn, record["card_id"], record.get("remote_content_revision", ""),
        _last_verified_of(remote_text) if remote_text is not None else "")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if (local_neg > 0 and remote_neg > 0) or local_strength == remote_strength:
        reason = "双方均有负面证据" if (local_neg > 0 and remote_neg > 0) else "执行证据打平"
        note = (f"conflict adjudication: {reason}，双版本同时隔离、不自动选边；"
                "等后续执行反馈再裁决")
        conn.execute("UPDATE card_conflicts SET status='open',adjudication_note=?,"
                     "updated_at=? WHERE conflict_id=?",
                     (note, now, record["conflict_id"]))
        conn.commit()
        return "open", note, False
    if local_strength > remote_strength:
        winner, winner_text, status = "local", local_text, "local_wins"
    else:
        winner, winner_text, status = "remote", remote_text, "remote_wins"
    note = (f"conflict adjudication: {winner} 版证据更强 "
            f"（helpful 任务 {local_strength[0]} vs {remote_strength[0]}），自动裁决")
    restored = False
    if winner_text is not None:
        current = store.find_file(repo, record["card_id"])
        current_text = current.read_text(encoding="utf-8") if current else None
        if current_text != winner_text:
            cur_meta, cur_body = _parse_text_or_empty(current_text) if current_text else ({}, "")
            base_rev = _compute_revision(cur_meta, cur_body) if cur_meta else ""
            warn = _restore_winner(repo, conn, record, winner_text, base_rev)
            if warn:
                note += f"；写回告警：{warn}"
            else:
                restored = True
    conn.execute("UPDATE card_conflicts SET status=?,adjudication_note=?,updated_at=? "
                 "WHERE conflict_id=?", (status, note, now, record["conflict_id"]))
    conn.commit()
    return status, note, restored


def readjudicate_card(repo: Path, conn, card_id: str) -> list[tuple[str, str, str]]:
    """对一张卡的全部 open 冲突重新裁决（后续反馈自然淘汰的触发点）。

    自己取 RepoLock（恢复写回需要）；无 open 冲突时不取锁、不写库。
    返回 [(conflict_id, status, note)]。
    """
    from .locking import RepoLock
    rows = conn.execute(
        "SELECT conflict_id FROM card_conflicts WHERE card_id=? AND status='open'",
        (card_id,)).fetchall()
    if not rows:
        return []
    outcomes = []
    with RepoLock(repo, 10.0):
        for (cid,) in rows:
            record = get_record(conn, cid)
            if not record:
                continue
            status, note, _restored = adjudicate(repo, conn, record)
            outcomes.append((cid, status, note))
    return outcomes
