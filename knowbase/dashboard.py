"""离线统计卡片快照；生成只读业务库，不输出知识/检索/任务明细。"""
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from . import config, index
from .dashboard_view import TEMPLATE


def generate(repo: Path, output: Path, *, team_snapshot: dict | None = None) -> Path:
    from .value import overview
    conn = sqlite3.connect(f"file:{index.db_path(repo)}?mode=ro", uri=True)
    try:
        local_usage = None
        if config.load_config().get("dashboard", {}).get("host_usage_source") == "claude":
            from .value.host_usage.claude import extract_records
            local_usage = list(extract_records())
        local = overview.build(conn, local_usage=local_usage)
    finally:
        conn.close()
    if team_snapshot is None and config.load_config().get("team", {}).get("enabled"):
        from .team.service import root_path
        snapshot_path = root_path() / "snapshot.json"
        if snapshot_path.exists():
            team_snapshot = json.loads(snapshot_path.read_text())
    team = (team_snapshot if team_snapshot and team_snapshot.get("scope") == "团队" and "coverage" in team_snapshot
            else overview.team(team_snapshot) if team_snapshot else None)
    data = {"generated": datetime.now().isoformat(timespec="seconds"),
            "overview": {"local": local, "team": team}}
    raw = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_name(output.name + ".tmp")
    temp.write_text(TEMPLATE.replace("__DATA__", raw), encoding="utf-8")
    temp.replace(output)
    return output
