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
        "auto_pull": True,
        "pull_ttl_seconds": 60,
        "push_retries": 3,
        "remote": {"name": "origin", "url": "", "branch": ""},
        "allowed_remote_prefixes": ["http://10.21.20.112/", "http://10.21.20.112:18084/"],
    },
    "index": {"max_lines": 300, "vector_model": "char-ngram-v1", "dense_threshold": 0.25},
    "embedding": {"enabled": False, "provider": "openai_compatible", "api_root": None,
                  "model": None, "allowed_hosts": [], "allowed_scopes": [],
                  "api_key_env": "KNOWBASE_EMBED_API_KEY", "auth_kind": "bearer",
                  "timeout_seconds": 0.7, "deployment_id": None, "quality_report": None,
                  "query_prefix": "", "document_prefix": "", "chunk_chars": 6000,
                  "max_chunks": 8, "auto_index": True},
    "scope_map": {},
    "stats": {"enabled": True},
    "dashboard": {"host_usage_source": "claude"},
    "team": {"enabled": False, "data_dir": "~/.knowbase/team", "interval_seconds": 900,
             "dashboard_output": "~/knowbase-dashboard.html"},
    "evolution": {"enabled": True, "auto_activate_bizrules": True,
                  "min_helpful_tasks": 3, "min_sessions": 2, "retire_not_helpful_tasks": 2,
                  # Phase 1 自进化：stale 满期自动归档与 review 候选超期丢弃的期限（天）。
                  # 归档只看证据账本（stale_since 起点 + 期间无成功复验证据），不看 wall-clock 猜测。
                  "archive_after_stale_days": 90, "review_candidate_ttl_days": 30,
                  # P4 显式激活策略：团队允许自动激活的类型/范围与策略版本。
                  # 默认 None/None = 沿用已有授权范围（不改变现有行为）：
                  # decide() 的 promotable 门仍在（staging 卡须为 GOVERNED
                  # 类型且有出处；已激活卡由证据驱动）。团队收紧时显式
                  # 填 allowed_types/allowed_scopes，策略只 gate 晋升/激活，
                  # 不影响证据收集与候选存储。
                  "activation_policy": {"allowed_types": None,
                                        "allowed_scopes": None, "policy_revision": "1"}},
    "value": {"events_enabled": False, "aggregate_db": "~/.knowbase/value-aggregate.db",
              # P3 §6.3：证据交换角色，默认 off（不自动发送/聚合）；sender 聚合
              # 前须经一次性团队配置显式指定；evidence_role 关闭时采集与同步继续。
              "evidence_role": "off", "evidence_remote": "",
              "evidence_branch": "knowbase-value-events-v2",
              "evidence_root": "~/.knowbase/evidence", "evidence_team_id": "default"},
    # P3 §6.1：worker 常驻循环 tick 间隔（秒）；OS 负责启动/保活。
    "worker": {"tick_seconds": 5.0},
    "hooks": {"enabled": True, "inject_min_confidence": "verified",
              "query_hygiene": True, "inject_max_chars": 900},
    # Phase 1 检索分层配置（02 号设计§6）：gate 默认关闭，阈值待评测校准（06 号§4）。
    "retrieval": {"mode": "auto", "auto_hook_semantic": False,
                  "gate": {"enabled": False, "min_fused": None}},
    "inflight": {
        "enabled": False, "capture_enabled": True, "mode": "assisted", "protocol_version": 2,
        "checkpoint_work_ops": 6, "checkpoint_minutes": 10,
        "max_queries_per_topic": 3, "max_queries_per_event": 6,
        "max_candidates": 3, "context_token_budget": 800,
        "material_page_chars": 8000, "max_batches_per_opportunity": 2,
        "hook_notice_budget": 2, "hook_notice_chars": 900,
        "fast_path_timeout_ms": 1000, "max_pending_jobs": 200,
        "host_profile": "", "member_label": "unknown",
        "device_label": "unknown",
    },
    # P1 执行器探针：显式配置的执行器列表（空表示仅做本机发现）。
    # 每项为 command 或 intranet_text 配置；探针不输出其中任何密钥引用值。
    "automation": {"executors": [], "enabled": False, "allowed_scopes": [],
                   "allow_trusted_commands": False, "max_batches": 2,
                   "batch_seconds": 120, "job_seconds": 600,
                   "max_input_chars": 128000, "max_output_chars": 64000,
                   "daily_calls": 20, "daily_input_chars": 512000},
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class ConfigError(ValueError):
    """配置文件存在但无法使用：消息带文件路径与损坏原因，禁止静默回退。

    继承 ValueError，兼容既有按 ValueError 捕获配置错误的调用方。
    不回退默认配置：repo_path 等关键路径被悄悄换掉后，工具会在错误
    的仓库上读写，风险高于立刻报错；报错后由人修复或删除配置文件。
    """


def load_config() -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            raw = CONFIG_PATH.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ConfigError(f"配置文件无法读取: {CONFIG_PATH}: {exc}") from exc
        try:
            override = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"配置文件损坏（JSON 解析失败）: {CONFIG_PATH}: {exc}；"
                "请修复或删除该文件，未回退默认配置"
            ) from exc
        if not isinstance(override, dict):
            raise ConfigError(
                f"配置文件格式错误（顶层必须是 JSON 对象）: {CONFIG_PATH}；"
                "请修复或删除该文件，未回退默认配置"
            )
        cfg = _deep_merge(cfg, override)
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


def mapped_scope(project_dir: str, cfg: dict | None = None) -> str | None:
    """接受配置中的原路径和规范路径别名（macOS /var 与 /private/var 等）。"""
    cfg = cfg or load_config()
    path = Path(project_dir).expanduser()
    canonical = path.resolve()
    mapping = cfg.get("scope_map", {}) or {}
    direct = mapping.get(str(path)) or mapping.get(str(canonical)) or mapping.get(canonical.name)
    if direct:
        return str(direct)
    for key, value in mapping.items():
        if (str(key).startswith(("/", "~")) and Path(key).expanduser().resolve() == canonical):
            return str(value)
    return None
