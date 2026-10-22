"""Claude Code 本机 transcripts 用量提取（请求级，H0 已实证键名）。

来源：~/.claude/projects/**/*.jsonl 的 assistant 行 message.usage。
只读取 usage/model/标识/时间字段；不读取也不保留 message.content 正文。
归属：transcript 只有会话粒度——task_mapping 固定 session 级，
不冒充任务实测（方案 §8.2）。
"""

import json
from pathlib import Path

DEFAULT_PATH = "~/.claude/projects"
SEMANTIC_VERSION = "claude-transcripts-v1"
PROVIDER = "anthropic"


def extract_records(base_path: str = DEFAULT_PATH):
    """遍历 transcripts，产出用量记录 dict；同 message.id 合并（同文件内累计字段取最大已观测值）。"""
    base = Path(base_path).expanduser()
    if not base.exists():
        return
    records: dict[str, dict] = {}
    for path in sorted(base.glob("*/*.jsonl")):
        try:
            handle = open(path, encoding="utf-8", errors="ignore")
        except OSError:
            continue
        with handle:
            for line in handle:
                if '"usage"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                message = entry.get("message")
                if not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
                    continue
                usage = message["usage"]
                request_id = str(message.get("id") or entry.get("uuid") or "")
                if not request_id:
                    continue
                dedupe_key = f"{path.name}:{request_id}"
                record = {
                    "source_file": path.name,
                    "session_id": str(entry.get("sessionId") or ""),
                    "request_id": request_id,
                    "occurred_at": str(entry.get("timestamp") or ""),
                    "model": str(message.get("model") or ""),
                    "input_tokens": usage.get("input_tokens"),
                    "output_tokens": usage.get("output_tokens"),
                    "cache_read_tokens": usage.get("cache_read_input_tokens"),
                    "cache_creation_tokens": usage.get("cache_creation_input_tokens"),
                }

                if dedupe_key not in records:
                    records[dedupe_key] = record
                else:
                    # 同请求可能先落部分用量、后落最终累计值；不把首条冻结，也不相加。
                    import math
                    for field in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens"):
                        value = record.get(field)
                        prior = records[dedupe_key].get(field)
                        if (isinstance(value, (int, float)) and not isinstance(value, bool)
                                and math.isfinite(value) and value >= 0):
                            valid_prior=(isinstance(prior,(int,float)) and not isinstance(prior,bool)
                                         and math.isfinite(prior) and prior>=0)
                            records[dedupe_key][field] = max(prior, value) if valid_prior else value
    yield from records.values()


def to_event_payload(record: dict) -> dict:
    payload = {"usage_source": "claude_transcripts", "session_id": record["session_id"],
               "provider": PROVIDER, "semantic_version": SEMANTIC_VERSION,
               "task_mapping": "session"}
    for key in ("model", "request_id", "input_tokens", "output_tokens",
                "cache_read_tokens", "cache_creation_tokens"):
        value = record.get(key)
        if value is not None and value != "":
            payload[key] = value
    return payload


def source_key(record: dict) -> str:
    return f"claude_usage:{record['source_file']}:{record['request_id']}"
