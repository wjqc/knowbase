"""Durable per-computer outbox, acknowledgements and accepted remote snapshots."""
import json
import sqlite3
from pathlib import Path

from .contracts import canonical, digest, validate_member, validate_summary


def connect(root):
    root = Path(root).expanduser()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    conn = sqlite3.connect(root / "state.db", timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
      CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS outgoing(path TEXT PRIMARY KEY, body TEXT NOT NULL, hash TEXT NOT NULL, ack TEXT NOT NULL DEFAULT '');
      CREATE TABLE IF NOT EXISTS accepted(path TEXT PRIMARY KEY, body TEXT NOT NULL, hash TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS evidence(day TEXT PRIMARY KEY, ids TEXT NOT NULL);
    """)
    return conn


def get(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def put(conn, key, value):
    conn.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, canonical(value).decode()))


def queue(conn, path, value):
    conn.execute("INSERT INTO outgoing(path,body,hash) VALUES(?,?,?) ON CONFLICT(path) DO UPDATE SET body=excluded.body,hash=excluded.hash",
                 (path, canonical(value).decode(), digest(value)))


def accept(conn, path, value, team_id):
    if path.startswith("members/"):
        validate_member(value)
        expected = f"members/{value['member_id']}.json"
    else:
        from .contracts import summary_path
        validate_summary(value)
        expected = summary_path(value)
    if path != expected or value["team_id"] != team_id:
        raise ValueError("statistics path or team identity mismatch")
    prior = conn.execute("SELECT body,hash FROM accepted WHERE path=?", (path,)).fetchone()
    if prior:
        old = json.loads(prior[0])
        if value["sequence"] < old["sequence"]:
            return False
        if value["sequence"] == old["sequence"]:
            if digest(value) != prior[1]:
                raise ValueError("same statistics sequence has different content")
            return False
        if "installation_id" in value and value["member_id"] != old["member_id"]:
            raise ValueError("device ownership changed inside a statistics bucket")
        if "metrics" in value:
            from .contracts import COUNTS, utc
            for key in COUNTS & set(old["metrics"]) & set(value["metrics"]):
                before, after = old["metrics"][key], value["metrics"][key]
                if before is not None and (after is None or after < before):
                    raise ValueError("cumulative statistics decreased without recovery epoch")
            if old["data_watermark"] and (not value["data_watermark"] or utc(value["data_watermark"]) < utc(old["data_watermark"])):
                raise ValueError("statistics watermark regressed")
    conn.execute("INSERT INTO accepted VALUES(?,?,?) ON CONFLICT(path) DO UPDATE SET body=excluded.body,hash=excluded.hash",
                 (path, canonical(value).decode(), digest(value)))
    return True
