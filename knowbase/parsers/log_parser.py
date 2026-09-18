"""日志解析器（P5-A2）。

按时间戳格式识别日志类型：
- ISO 8601（`2025-01-15T10:30:45.123Z` / `2025-01-15 10:30:45+08:00`）
- Syslog（`Jan 15 10:30:45 hostname app[123]: message`）
- Common Log Format（`15/Jan/2025:10:30:45 +0000`）
- Unix timestamp（`1736939445 ...`）

输出：
- text：每行格式 `[ts] [LEVEL] message`，便于检索
- meta：format / line_count / level_counts / first_ts / last_ts

注意：`.log` 扩展名也由 TxtParser 兜底；LogParser 在 `register_builtin`
中先注册以抢占。
"""
from __future__ import annotations

import re
from pathlib import Path

from .base import DocumentParser, ParsedDocument


# 顺序敏感：先匹配更具体的；匹配成功后用作日志格式
_TIMESTAMP_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # ISO 8601 with T: 2025-01-15T10:30:45(.123)(Z|±08:00)
    (re.compile(
        r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:?\d{2})?"
    ), "iso8601"),
    # ISO 8601 with space: 2025-01-15 10:30:45(.123) — Python logging 默认格式
    (re.compile(
        r"^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:[,.]\d+)?"
    ), "iso8601_space"),
    # Syslog (RFC 3164): "Jan 15 10:30:45"（缺年份；可后续补当前年）
    (re.compile(
        r"^[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}"
    ), "syslog"),
    # Common Log Format: "15/Jan/2025:10:30:45 +0000"
    (re.compile(
        r"^\d{1,2}/[A-Z][a-z]{2}/\d{4}:\d{2}:\d{2}:\d{2}\s+[+-]\d{4}"
    ), "common"),
    # Unix timestamp (10 位): 1736939445
    (re.compile(r"^\d{10}\b"), "unix"),
]


_LEVEL_PATTERN = re.compile(
    r"\b(TRACE|DEBUG|INFO|WARN|WARNING|ERROR|FATAL|CRITICAL|NOTICE)\b"
)


def _detect_format(line: str) -> tuple[str, str] | tuple[None, None]:
    """返回 (ts, format_name) 或 (None, None) 表示无法识别。"""
    for pat, name in _TIMESTAMP_PATTERNS:
        m = pat.match(line)
        if m:
            return m.group(0), name
    return None, None


def _extract_level(line: str) -> str:
    """提取日志级别，未识别返回空串。"""
    m = _LEVEL_PATTERN.search(line)
    if not m:
        return ""
    level = m.group(1)
    if level == "WARNING":
        return "WARN"
    if level == "NOTICE":
        return "INFO"
    return level


def _strip_level(msg: str, level: str) -> str:
    """从消息中移除 level 关键字（避免 [INFO] INFO ... 重复）。"""
    if not level:
        return msg
    # 匹配首个 level 关键字 + 后随空白
    return re.sub(rf"\b{level}\b\s*", "", msg, count=1).lstrip()


def _split_message(line: str, ts: str, level: str) -> str:
    """去掉时间戳与 level 后的剩余文本。"""
    if not ts:
        return line
    rest = line[len(ts):].lstrip(" \t")
    return _strip_level(rest, level)


class LogParser(DocumentParser):
    name = "log"

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() == ".log"

    def parse(self, path: Path) -> ParsedDocument:
        from .errors import ParseError
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            raise ParseError(parser=self.name, reason=str(e), path=str(path)) from e

        lines = raw.splitlines()
        out: list[str] = []
        level_counts: dict[str, int] = {}
        detected_format = ""
        first_ts = ""
        last_ts = ""
        recognized = 0

        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            ts, fmt = _detect_format(stripped)
            if fmt:
                recognized += 1
                if not detected_format:
                    detected_format = fmt
                if not first_ts:
                    first_ts = ts
                last_ts = ts
                level = _extract_level(stripped)
                msg = _split_message(stripped, ts, level)
                if level:
                    level_counts[level] = level_counts.get(level, 0) + 1
                    out.append(f"[{ts}] [{level}] {msg}")
                else:
                    out.append(f"[{ts}] {msg}")
            else:
                # 无法识别时间戳的行：保留原文
                out.append(line)

        # 兜底：完全无法识别任何时间戳 → 视为 plain log
        if recognized == 0:
            detected_format = "plain"
            out = [l for l in lines if l.strip()]

        text = "\n".join(out).strip()
        return ParsedDocument(
            text=text,
            meta={
                "format": "log",
                "log_format": detected_format,
                "line_count": len(lines),
                "recognized_count": recognized,
                "level_counts": level_counts,
                "first_ts": first_ts,
                "last_ts": last_ts,
                "title": path.stem,
            },
        )