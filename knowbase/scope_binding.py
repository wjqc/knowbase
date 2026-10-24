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

import os
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import config


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
            capture_output=True, text=True, timeout=10)
        if top.returncode != 0:
            return None
        toplevel = top.stdout.strip()
        if not toplevel:
            return None
        rem = subprocess.run(
            ["git", "-C", str(base), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=10)
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


def record_binding(conn, path, scope: str, source: str, confirmed: bool) -> dict:
    """写入一条绑定（按规范化路径 upsert）；后台补充时 confirmed=False。

    只记录路径与 Git 身份元数据，不读任何项目知识。调用方负责提交事务
    （本函数只执行语句不 commit，与调用方事务保持一致）。
    """
    norm = normalize_path(path)
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
