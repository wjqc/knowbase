"""检查点构建：从归一化事件生成可提炼的 checkpoint。

幂等键 = host_profile + session_id + checkpoint_hash + scope + extractor_version。
会话继续追加生成新 checkpoint；已有一次 save 不阻止后续检查。
"""

import hashlib
import json
from dataclasses import dataclass

from .normalizer import NormalizedEvent
from ..extraction.sanitizer import sanitize


@dataclass
class Checkpoint:
    """一个可提炼的检查点。"""
    session_id: str
    checkpoint_index: int
    events: list[dict]       # 归一化后的事件摘要
    scope: str
    checkpoint_hash: str
    idempotency_key: str
    char_count: int


def checkpoint_hash(events: list[dict]) -> str:
    """计算事件的确定性哈希。"""
    canonical = json.dumps(events, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def build_checkpoint(events: list[NormalizedEvent], *,
                     session_id: str, scope: str,
                     checkpoint_index: int = 0,
                     host_profile: str = "",
                     extractor_version: str = "v1",
                     max_chars: int = 16000) -> Checkpoint:
    """从事件列表构建一个检查点。

    - 脱敏处理
    - 按 max_chars 截断
    - 生成幂等键
    """
    event_dicts = []
    total_chars = 0
    for ev in events:
        sanitized_text = sanitize(ev.text)
        entry = {
            "role": ev.role,
            "text": sanitized_text,
            "tool_name": ev.tool_name,
            "timestamp": ev.timestamp,
        }
        entry_len = len(json.dumps(entry, ensure_ascii=False))
        if total_chars + entry_len > max_chars:
            break
        event_dicts.append(entry)
        total_chars += entry_len

    ch_hash = checkpoint_hash(event_dicts)
    idem_key = f"capture:{host_profile}:{session_id}:{ch_hash}:{scope}:{extractor_version}"

    return Checkpoint(
        session_id=session_id,
        checkpoint_index=checkpoint_index,
        events=event_dicts,
        scope=scope,
        checkpoint_hash=ch_hash,
        idempotency_key=idem_key,
        char_count=total_chars,
    )


def build_checkpoints(events: list[NormalizedEvent], *,
                      session_id: str, scope: str,
                      host_profile: str = "",
                      extractor_version: str = "v1",
                      max_chars: int = 16000) -> list[Checkpoint]:
    """从长会话事件列表构建多个顺序 checkpoint。

    按 max_chars 预算将事件切分为多个 checkpoint，每个 checkpoint 有递增的
    checkpoint_index。幂等键包含 checkpoint_index，保证各批次独立幂等。

    返回 checkpoint 列表，至少包含一个（即使事件为空）。
    """
    if not events:
        # 空会话返回单个空 checkpoint
        ch_hash = checkpoint_hash([])
        idem_key = f"capture:{host_profile}:{session_id}:{ch_hash}:{scope}:{extractor_version}:0"
        return [Checkpoint(
            session_id=session_id,
            checkpoint_index=0,
            events=[],
            scope=scope,
            checkpoint_hash=ch_hash,
            idempotency_key=idem_key,
            char_count=0,
        )]

    checkpoints = []
    current_events = []
    current_chars = 0
    checkpoint_index = 0

    for ev in events:
        sanitized_text = sanitize(ev.text)
        entry = {
            "role": ev.role,
            "text": sanitized_text,
            "tool_name": ev.tool_name,
            "timestamp": ev.timestamp,
        }
        entry_len = len(json.dumps(entry, ensure_ascii=False))

        # 如果当前 checkpoint 已满，先保存再开始新的
        if current_chars + entry_len > max_chars and current_events:
            ch_hash = checkpoint_hash(current_events)
            idem_key = f"capture:{host_profile}:{session_id}:{ch_hash}:{scope}:{extractor_version}:{checkpoint_index}"
            checkpoints.append(Checkpoint(
                session_id=session_id,
                checkpoint_index=checkpoint_index,
                events=current_events,
                scope=scope,
                checkpoint_hash=ch_hash,
                idempotency_key=idem_key,
                char_count=current_chars,
            ))
            checkpoint_index += 1
            current_events = []
            current_chars = 0

        # 如果单条事件超过 max_chars，单独作为一个 checkpoint
        if entry_len > max_chars:
            ch_hash = checkpoint_hash([entry])
            idem_key = f"capture:{host_profile}:{session_id}:{ch_hash}:{scope}:{extractor_version}:{checkpoint_index}"
            checkpoints.append(Checkpoint(
                session_id=session_id,
                checkpoint_index=checkpoint_index,
                events=[entry],
                scope=scope,
                checkpoint_hash=ch_hash,
                idempotency_key=idem_key,
                char_count=entry_len,
            ))
            checkpoint_index += 1
            continue

        current_events.append(entry)
        current_chars += entry_len

    # 保存最后一个 checkpoint
    if current_events:
        ch_hash = checkpoint_hash(current_events)
        idem_key = f"capture:{host_profile}:{session_id}:{ch_hash}:{scope}:{extractor_version}:{checkpoint_index}"
        checkpoints.append(Checkpoint(
            session_id=session_id,
            checkpoint_index=checkpoint_index,
            events=current_events,
            scope=scope,
            checkpoint_hash=ch_hash,
            idempotency_key=idem_key,
            char_count=current_chars,
        ))

    return checkpoints
