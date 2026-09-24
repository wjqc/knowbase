"""脱敏：去除凭据、个人信息和无关工具噪声。

对话文本在发给宿主 Agent 提炼前必须脱敏；
MCP 脱敏不能追溯撤销宿主此前收到的内容（§5.3）。
"""

import re
from pathlib import PurePath

# 凭据模式
_CREDENTIAL_PATTERNS = [
    # API keys / tokens: key=value, key: value, "key": "value"
    re.compile(
        r'(?i)(?:password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token'
        r'|bearer|private[_-]?key|client[_-]?secret)\s*[=:]\s*["\']?[^\s"\'<>]{6,}["\']?'
    ),
    # AWS-style keys
    re.compile(r'(?:AKIA|ASIA)[A-Z0-9]{16}'),
    # Base64-looking tokens after key labels
    re.compile(r'(?i)(?:key|token|secret)\s*[=:]\s*["\']?[A-Za-z0-9+/=]{20,}["\']?'),
]

# 个人信息模式
_PII_PATTERNS = [
    # 身份证号（18 位）
    re.compile(r'\b\d{17}[\dXx]\b'),
    # 手机号（中国大陆）
    re.compile(r'\b1[3-9]\d{9}\b'),
    # 邮箱
    re.compile(r'\b[\w.+-]+@[\w-]+\.[\w.-]+\b'),
]

# 本机路径
_MACHINE_PATH_RE = re.compile(
    r'(?<![\w.-])(?:~[/\\]|/(?:Users|home)/[^\s`\'\"<>]+|[A-Za-z]:\\+[^\s`\'\"<>]+)'
)


def sanitize(text: str) -> str:
    """去除凭据和个人信息。替换为 [REDACTED] 占位符。"""
    result = text
    for pattern in _CREDENTIAL_PATTERNS:
        result = pattern.sub("[REDACTED:credential]", result)
    for pattern in _PII_PATTERNS:
        result = pattern.sub("[REDACTED:pii]", result)
    result = _MACHINE_PATH_RE.sub("[REDACTED:path]", result)
    return result


def has_credentials(text: str) -> bool:
    """检测文本是否包含凭据（用于拦截入库）。"""
    return any(p.search(text) for p in _CREDENTIAL_PATTERNS)
