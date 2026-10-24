"""检索→递送→采用身份链（无人值守方案 §4.1，P0）。

delivery 是"经过最终候选过滤后真正递送的卡"：一次 context.execute
事件里，search 命中只是候选，只有进入最终 candidates 的才写 delivery。
采用回执（consumption.record）再凭 context_event_id + 卡 + 版本解析
到 delivery，写 use_retrieval_links；解析不到的旧观察保留未关联，
绝不按时间、标题或单个 context_event_id 猜测匹配。

本模块函数只在调用方事务内执行 INSERT/SELECT，不自行 commit；
调用方（context.execute、consumption.record）负责事务边界。
"""

from datetime import datetime, timezone

from . import materials

LINK_BASIS_CONTEXT_CANDIDATE = "context_candidate"


def delivery_id_for(search_id: str, memory_id: str, content_revision: str) -> str:
    """同一 search 内同一卡同一版本的递送身份稳定：重复写入幂等。"""
    return "DEL-" + materials.digest([search_id, memory_id, content_revision])[:24]


def record_delivery(conn, *, search_id: str, host_profile: str, session_id: str,
                    context_event_id: str, task_id: str, task_revision: str,
                    scope: str, memory_id: str, content_revision: str, rank: int,
                    source_delivery_id: str | None = None) -> str:
    """写一条递送记录。缓存重递送须传 source_delivery_id 指向原递送，
    且调用方须为重递送分配新的 search_id（同一 search 内同一卡幂等）。"""
    delivery_id = delivery_id_for(search_id, memory_id, content_revision)
    conn.execute(
        "INSERT OR IGNORE INTO retrieval_deliveries(delivery_id,search_id,host_profile,"
        "session_id,context_event_id,task_id,task_revision,scope,memory_id,content_revision,"
        "rank,source_delivery_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (delivery_id, search_id, host_profile, session_id, context_event_id, task_id,
         task_revision, scope, memory_id, content_revision, rank, source_delivery_id,
         datetime.now(timezone.utc).isoformat(timespec="seconds")))
    return delivery_id


def find_delivery(conn, context_event_id: str, memory_id: str,
                  content_revision: str) -> dict | None:
    """按递送事件 + 卡 + 版本解析唯一递送；找不到返回 None（不猜测）。"""
    row = conn.execute(
        "SELECT delivery_id,search_id,host_profile,session_id,context_event_id,task_id,"
        "task_revision,scope,memory_id,content_revision,rank,source_delivery_id,created_at "
        "FROM retrieval_deliveries WHERE context_event_id=? AND memory_id=? "
        "AND content_revision=? ORDER BY rank LIMIT 1",
        (context_event_id, memory_id, content_revision)).fetchone()
    if not row:
        return None
    keys = ("delivery_id", "search_id", "host_profile", "session_id", "context_event_id",
            "task_id", "task_revision", "scope", "memory_id", "content_revision", "rank",
            "source_delivery_id", "created_at")
    return dict(zip(keys, row))


def record_use_link(conn, *, use_id: str, delivery_id: str, search_id: str,
                    link_basis: str = LINK_BASIS_CONTEXT_CANDIDATE) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO use_retrieval_links(use_id,delivery_id,search_id,link_basis,"
        "created_at) VALUES(?,?,?,?,?)",
        (use_id, delivery_id, search_id, link_basis,
         datetime.now(timezone.utc).isoformat(timespec="seconds")))


def deliveries_for_search(conn, search_id: str) -> list[dict]:
    """某次 search 的全部递送记录（按 rank）；表不存在的旧库返回空列表。"""
    try:
        rows = conn.execute(
            "SELECT delivery_id,search_id,host_profile,session_id,context_event_id,task_id,"
            "task_revision,scope,memory_id,content_revision,rank,source_delivery_id,created_at "
            "FROM retrieval_deliveries WHERE search_id=? ORDER BY rank",
            (search_id,)).fetchall()
    except Exception:
        return []
    keys = ("delivery_id", "search_id", "host_profile", "session_id", "context_event_id",
            "task_id", "task_revision", "scope", "memory_id", "content_revision", "rank",
            "source_delivery_id", "created_at")
    return [dict(zip(keys, row)) for row in rows]


def linked_uses_for_search(conn, search_id: str) -> list[dict]:
    """经递送关联到某次 search 的采用回执：只认 link，不认 event_id 巧合。

    旧派生按 knowledge_uses.event_id = search_id 匹配，但 use 的 event_id
    是 memory_use 自身的新事件，二者永远不同——那是 G2 的根因。
    """
    try:
        rows = conn.execute(
            "SELECT u.use_id,u.memory_id,u.content_revision,u.state,u.task_id "
            "FROM use_retrieval_links l JOIN knowledge_uses u ON u.use_id=l.use_id "
            "JOIN retrieval_deliveries d ON d.delivery_id=l.delivery_id "
            "WHERE l.search_id=? AND u.current_version=1 "
            "AND u.content_revision=d.content_revision",
            (search_id,)).fetchall()
    except Exception:
        return []
    keys = ("use_id", "memory_id", "content_revision", "state", "task_id")
    return [dict(zip(keys, row)) for row in rows]
