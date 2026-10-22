"""脱敏：去除凭据、个人信息和无关工具噪声。

对话文本在发给宿主 Agent 提炼前必须脱敏；
MCP 脱敏不能追溯撤销宿主此前收到的内容（§5.3）。
"""

import re

from ..credentials import contains_credentials, redact_text, transform

# 与入库检测共用 credentials 策略，避免写侧、交付和拒绝规则漂移。

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


def _sanitize_text(text: str) -> str:
    result = redact_text(text)
    for pattern in _PII_PATTERNS:
        result = pattern.sub("[REDACTED:pii]", result)
    result = _MACHINE_PATH_RE.sub("[REDACTED:path]", result)
    return result


def sanitize_value(value):
    """结构、argv 与 JSON 字符串使用同一脱敏策略。"""
    return transform(value, text_filter=_sanitize_text)


def sanitize(text: str) -> str:
    """去除凭据和个人信息，递归处理多层 JSON 字符串。"""
    return sanitize_value(text)


def has_credentials(text: str) -> bool:
    """检测文本是否包含凭据（用于拦截入库）。"""
    return contains_credentials(text)
