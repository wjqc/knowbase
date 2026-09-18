from pathlib import Path

from knowbase import index, store


def _meta(mid="P-2026-0001"):
    m = store.new_meta("pitfall", "Redis 缓存雪崩", "demo", ["Redis", "缓存"], "human:test")
    m["id"] = mid
    return m


def test_single_db_contains_vector_source_and_sync_state(tmp_path: Path):
    conn = index.connect(tmp_path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"meta", "mem_fts", "embedding_index", "source_state", "sync_state"} <= tables
    assert not (tmp_path / ".knowbase" / "v2.db").exists()
    conn.close()


def test_upsert_writes_embedding_in_same_db(tmp_path: Path):
    conn = index.connect(tmp_path)
    index.upsert(conn, _meta(), "## 现象\n缓存雪崩", tmp_path / "P-2026-0001.md")
    row = conn.execute(
        "SELECT model_version,content_hash,vector FROM embedding_index WHERE memory_id=?",
        ("P-2026-0001",),
    ).fetchone()
    assert row and row[0] == "char-ngram-v1" and len(row[1]) == 64 and row[2].startswith("[")
    conn.close()


def test_source_and_sync_state_are_upserted(tmp_path: Path):
    conn = index.connect(tmp_path)
    index.record_source_state(conn, "/docs/a.pdf", "abc", status="indexed")
    index.record_sync_state(conn, "origin", local_revision="l", remote_revision="r",
                            last_fetch_at="2026-09-18T00:00:00", status="blocked", error="dirty")
    assert conn.execute("SELECT status FROM source_state").fetchone()[0] == "indexed"
    assert conn.execute("SELECT status,error FROM sync_state").fetchone() == ("blocked", "dirty")
    conn.close()
