"""配置加载：默认值 < ~/.knowbase/config.json < 环境变量。

配置文件不放仓库内（克隆新机时鸡生蛋），不放各 Agent 的 MCP 配置里
（8 个 Agent 必须读同一份配置才是同一视图）。
"""

import copy
import json
import os
from pathlib import Path

CONFIG_PATH = Path(os.environ.get("KNOWBASE_CONFIG", str(Path.home() / ".knowbase" / "config.json")))

DEFAULTS = {
    "repo_path": "~/knowbase",
    "knowledge_path": "~/knowledge",
    "lock_timeout": 10.0,
    "git": {
        "auto_commit": True,
        "auto_push": False,
        "remote": {"url": ""},
        "allowed_remote_prefixes": ["http://10.21.20.112/", "http://10.21.20.112:18084/"],
    },
    "index": {"max_lines": 300},
    "stats": {"enabled": True},
    "hooks": {"enabled": True, "inject_min_confidence": "verified"},
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    if CONFIG_PATH.exists():
        cfg = _deep_merge(cfg, json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    env_repo = os.environ.get("KNOWBASE_REPO_PATH")
    if env_repo:
        cfg["repo_path"] = env_repo
    return cfg


def repo_path(cfg: dict | None = None) -> Path:
    cfg = cfg or load_config()
    return Path(os.path.expanduser(cfg["repo_path"]))


def agent_name() -> str:
    """调用方身份：各 Agent 的 MCP 配置通过 env KNOWBASE_AGENT_NAME 声明。"""
    return os.environ.get("KNOWBASE_AGENT_NAME") or "unknown"
