"""项目识别：为本地路径建立有出处的 scope 绑定（P4）。

解析优先级（明确映射优先）：
1. 配置 scope_map 的显式映射（精确路径或最具体的祖先项目根；刻意不用
   config.mapped_scope 的 bare-name 别名——相似名不构成映射）；
2. 已确认绑定中路径祖先最具体的一条（嵌套仓库取最具体已确认映射）；
3. 已确认绑定的 Git 身份（toplevel 相同，或 remote 相同）匹配——不同
   checkout 映射同一项目就靠这一条；
4. 以上皆无 → scope_unresolved，不猜测。

绝不做的事：不以目录名/项目名相似合并；不读其他项目的知识来猜归属；
后台只补充未确认的候选元数据，不产生已确认绑定。
"""

import hashlib
import os
import re
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import config


from .processes import no_window

def normalize_path(path) -> Path:
    """规范化路径：展开 ~、解析符号链接（不存在的路径不报错）。"""
    return Path(path).expanduser().resolve()


def _existing_ancestor(path: Path) -> Path:
    cur = path
    while not cur.exists():
        if cur.parent == cur:
            return cur
        cur = cur.parent
    return cur


def git_identity(path) -> dict | None:
    """返回该路径所在 Git 仓库的身份 {toplevel, remote}；不在仓库内返回 None。

    只读 git 查询，有界超时；任何失败都视为"无 Git 身份"，不抛异常。
    remote 取 origin；没有 origin 时 remote 为 None（仍可用 toplevel 匹配）。
    """
    try:
        base = _existing_ancestor(normalize_path(path))
        top = subprocess.run(
            ["git", "-C", str(base), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10, **no_window())
        if top.returncode != 0:
            return None
        toplevel = top.stdout.strip()
        if not toplevel:
            return None
        rem = subprocess.run(
            ["git", "-C", str(base), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=10, **no_window())
        remote = rem.stdout.strip() if rem.returncode == 0 else None
        return {"toplevel": toplevel, "remote": remote or None}
    except Exception:
        return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _tables_present(conn) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='project_bindings'").fetchone()
        return row is not None
    except sqlite3.Error:
        return False


def _confirmed_bindings(conn) -> list[dict]:
    """全部已确认绑定；表不存在时返回空（不把缺迁移当错误）。"""
    if not _tables_present(conn):
        return []
    try:
        rows = conn.execute(
            "SELECT project_path,scope,git_toplevel,git_remote,source,created_at "
            "FROM project_bindings WHERE confirmed=1").fetchall()
    except sqlite3.Error:
        return []
    return [{"project_path": p, "scope": s, "git_toplevel": t, "git_remote": r,
             "source": src, "created_at": c} for p, s, t, r, src, c in rows]


def _is_within(path: Path, ancestor: Path) -> bool:
    try:
        path.relative_to(ancestor)
        return True
    except ValueError:
        return False


def _scope_map_lookup(cfg: dict, norm: Path) -> str | None:
    """显式映射查找：精确路径或最具体的祖先项目根。

    刻意不用 config.mapped_scope 的 bare-name 别名：P4 语义下"相似名"
    不构成映射——只有路径本身或其祖先被显式映射时才算映射。
    """
    mapping = cfg.get("scope_map", {}) or {}
    best: tuple[int, str] | None = None
    for key, value in mapping.items():
        if not str(key).startswith(("/", "~")):
            continue
        try:
            kpath = Path(key).expanduser().resolve()
        except (OSError, RuntimeError):
            continue
        if norm == kpath or _is_within(norm, kpath):
            if best is None or len(str(kpath)) > best[0]:
                best = (len(str(kpath)), str(value))
    return best[1] if best else None


def resolve_binding(conn, path, cfg: dict | None = None) -> dict:
    """解析一条路径的 scope 绑定。

    返回 {status, scope, source, reason, detail}：
    - status=resolved 时 scope 有效，source 为 explicit/confirmed/git_identity；
    - status=scope_unresolved 时 scope 为 None，reason 为 no_mapping/ambiguous，
      调用方必须保持 scope_unresolved，不得退化为全库或按名猜测。
    """
    cfg = cfg or config.load_config()
    norm = normalize_path(path)
    mapped = _scope_map_lookup(cfg, norm)
    if mapped:
        return {"status": "resolved", "scope": str(mapped), "source": "explicit",
                "reason": "", "path": str(norm)}
    bindings = _confirmed_bindings(conn)
    # 祖先最具体优先：路径祖先中取最长的一条；同长度不同 scope 视为冲突。
    best_len = -1
    best: dict | None = None
    ambiguous = False
    for b in bindings:
        bpath = Path(b["project_path"])
        if _is_within(norm, bpath):
            ln = len(str(bpath))
            if ln > best_len:
                best_len, best, ambiguous = ln, b, False
            elif ln == best_len and best is not None and best["scope"] != b["scope"]:
                ambiguous = True
    if ambiguous:
        return {"status": "scope_unresolved", "scope": None, "source": "",
                "reason": "ambiguous",
                "detail": "多条同等具体的已确认绑定指向不同 scope，需显式映射裁决",
                "path": str(norm)}
    if best is not None:
        return {"status": "resolved", "scope": best["scope"],
                "source": best["source"] if best["source"] == "explicit" else "confirmed",
                "reason": "", "path": str(norm)}
    ident = git_identity(path)
    if ident:
        for b in bindings:
            # 同一项目判定：toplevel 相同（同一 checkout），或 remote 相同
            # （不同 checkout 的同一仓库）。两者任一成立即为同一项目；
            # 要求两者同时成立会使"不同 checkout 同项目"永远无法命中。
            same_checkout = bool(b.get("git_toplevel")) and b["git_toplevel"] == ident["toplevel"]
            same_remote = bool(b.get("git_remote")) and bool(ident.get("remote")) \
                and b["git_remote"] == ident["remote"]
            if same_checkout or same_remote:
                return {"status": "resolved", "scope": b["scope"],
                        "source": "git_identity", "reason": "", "path": str(norm)}
    return {"status": "scope_unresolved", "scope": None, "source": "",
            "reason": "no_mapping",
            "detail": "无显式 scope_map、无已确认绑定、无匹配的 Git 身份；"
                    "请用 knowbase scope bind 显式绑定",
            "path": str(norm)}


def record_binding(conn, path, scope: str, source: str, confirmed: bool, ident: dict | None = None) -> dict:
    """写入一条绑定（按规范化路径 upsert）；后台补充时 confirmed=False。

    只记录路径与 Git 身份元数据，不读任何项目知识。调用方负责提交事务
    （本函数只执行语句不 commit，与调用方事务保持一致）。
    ident 可传入已取得的 git_identity 结果，避免调用方重复起 git 子进程。
    """
    norm = normalize_path(path)
    if ident is None:
        ident = git_identity(path) or {}
    now = _now()
    conn.execute(
        "INSERT INTO project_bindings(project_path,scope,git_toplevel,git_remote,"
        "source,confirmed,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(project_path) DO UPDATE SET scope=excluded.scope,"
        "git_toplevel=excluded.git_toplevel,git_remote=excluded.git_remote,"
        "source=excluded.source,confirmed=excluded.confirmed,updated_at=excluded.updated_at",
        (str(norm), scope, ident.get("toplevel"), ident.get("remote"),
         source, 1 if confirmed else 0, now, now))
    return {"project_path": str(norm), "scope": scope, "source": source,
            "confirmed": bool(confirmed), "git_toplevel": ident.get("toplevel"),
            "git_remote": ident.get("remote")}


def bind_explicit(conn, path, scope: str) -> dict:
    """显式绑定：人工确认的映射，优先级高于一切推断。"""
    return record_binding(conn, path, scope, "explicit", True)


def record_candidate(conn, path) -> dict:
    """后台补充项目元数据：只记未确认候选，不产生归属结论。

    候选的 scope 为空——后台不知道它属于哪个 scope，也不猜。
    """
    norm = normalize_path(path)
    ident = git_identity(path) or {}
    now = _now()
    conn.execute(
        "INSERT INTO project_bindings(project_path,scope,git_toplevel,git_remote,"
        "source,confirmed,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(project_path) DO UPDATE SET git_toplevel=excluded.git_toplevel,"
        "git_remote=excluded.git_remote,updated_at=excluded.updated_at",
        (str(norm), "", ident.get("toplevel"), ident.get("remote"),
         "background", 0, now, now))
    return {"project_path": str(norm), "scope": "", "source": "background",
            "confirmed": False, "git_toplevel": ident.get("toplevel"),
            "git_remote": ident.get("remote")}


def suggest_binding(path, cfg: dict | None = None) -> dict:
    """后台视角的候选元数据（纯计算，不写库、不产生绑定结论）。"""
    norm = normalize_path(path)
    ident = git_identity(path) or {}
    return {"project_path": str(norm), "scope": None,
            "git_toplevel": ident.get("toplevel"), "git_remote": ident.get("remote"),
            "note": "仅候选元数据；归属需显式绑定或已确认绑定"}


def list_bindings(conn) -> list[dict]:
    """全部绑定（已确认在前、未确认候选在后）。"""
    if not _tables_present(conn):
        return []
    try:
        rows = conn.execute(
            "SELECT project_path,scope,git_toplevel,git_remote,source,confirmed,"
            "created_at,updated_at FROM project_bindings "
            "ORDER BY confirmed DESC, project_path").fetchall()
    except sqlite3.Error:
        return []
    return [{"project_path": p, "scope": s, "git_toplevel": t, "git_remote": r,
             "source": src, "confirmed": bool(c), "created_at": ca, "updated_at": u}
            for p, s, t, r, src, c, ca, u in rows]


# ---------- 自动绑定（2026-11-04 裁决：未映射项目自动建立 scope，检索/采集链路不再整场跳过） ----------


def _sanitize_scope(name: str) -> str:
    """任意 repo/目录名 → 合法 scope：小写、[a-z0-9-]、压缩连续分隔、上限 64。"""
    text = re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")
    return text[:64].strip("-") or "project"


def _remote_segments(remote: str) -> list[str]:
    """remote URL → 有效段：去协议头后按 '/'、':' 切段。

    http(s) 带端口与 scp 形式（git@host:group/repo.git）均成立；host/user 段
    天然参与切段，只取末两段作 basename/parent，不受端口段污染。
    """
    body = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", str(remote).strip())
    return [p for p in re.split(r"[/:]+", body) if p]


def _name_occupied(conn, name: str, remote: str) -> bool:
    """派生名是否已被他属占用：其他 remote 的已确认绑定，或库内同名 scope 的卡。

    同 remote 已绑此名不算占用（同一项目的另一处 checkout）。
    """
    own = conn.execute(
        "SELECT 1 FROM project_bindings WHERE confirmed=1 AND scope=? AND git_remote=? LIMIT 1",
        (name, remote)).fetchone()
    if own:
        return False
    if conn.execute("SELECT 1 FROM project_bindings WHERE confirmed=1 AND scope=? LIMIT 1",
                    (name,)).fetchone():
        return True
    return conn.execute(
        "SELECT 1 FROM meta WHERE scope=? AND staging=0 LIMIT 1", (name,)).fetchone() is not None


def _derive_auto_name(conn, remote: str) -> tuple[str, str]:
    """remote → scope 名，三级防同名融合：basename > 父目录前缀 > 哈希后缀。"""
    segs = _remote_segments(remote)
    base = (segs[-1] if segs else "").removesuffix(".git")
    name = _sanitize_scope(base)
    if not _name_occupied(conn, name, remote):
        return name, "basename"
    if len(segs) >= 2:
        parent = segs[-2].removesuffix(".git")
        candidate = _sanitize_scope(f"{parent}-{base}")
        if candidate != name and not _name_occupied(conn, candidate, remote):
            return candidate, "parent_prefixed"
    suffix = hashlib.sha1(remote.encode("utf-8")).hexdigest()[:6]
    return f"{name}-{suffix}", "hash_suffixed"


def auto_bind(conn, path) -> dict:
    """未解析路径的自动绑定。

    git 仓库以 remote 派生 scope 名（跨机稳定：同仓库不同 checkout/不同机器落同一
    scope；同名不同库加父目录或哈希前缀防融合）；无 remote 的 git 仓库与非 git
    目录按规范化绝对路径绑定（本机私有，不跨机融合）。绑定写 project_bindings
    （confirmed=1），config 的 scope_map 不被触碰——显式映射优先级仍最高。

    首次绑定付出 2 次 git 子进程（约几十毫秒），此后路径祖先匹配纯 DB 读。
    任何失败返回 scope_unresolved，由调用方回退 global，不阻断检索。
    """
    norm = normalize_path(path)
    ident = git_identity(path) or {}
    remote = ident.get("remote")
    bind_path = ident.get("toplevel") or str(norm)
    source = "auto_remote" if remote else "auto_path"
    try:
        conn.execute("BEGIN IMMEDIATE")
        if remote:
            name, how = _derive_auto_name(conn, remote)
        else:
            name, how = _sanitize_scope(Path(bind_path).name), "path_basename"
        record_binding(conn, bind_path, name, source, True, ident=ident)
        conn.commit()
    except sqlite3.Error:
        if conn.in_transaction:
            conn.rollback()
        return {"status": "scope_unresolved", "scope": None, "source": "",
                "reason": "bind_failed", "path": str(norm)}
    return {"status": "resolved", "scope": name, "source": source, "reason": "",
            "path": str(norm), "detail": f"auto:{how}"}
