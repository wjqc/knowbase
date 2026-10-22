"""Shared credential policy for observation writes, material delivery and card rejection.

Only synthetic examples belong in tests. Detection diagnostics never return a secret.
"""

import json
import re

REDACTED = "[REDACTED:credential]"
_WORD = r"(?:pass(?:word|wd)?|secret|token|api[_-]?key|access[_-]?(?:key|token)|private[_-]?key|credential|authorization)"
_CONTROL = r"(?:limit|budget|size|count|usage|timeout|threshold|window|length|capacity|rate|cost|price)s?"
# Match complete identifiers; token_limit / max_tokens are configuration, not credentials.
_KEY = rf"(?![a-z0-9_-]*[_-]{_CONTROL}(?:[_-]|\b))(?:[a-z0-9]+[_-])*{_WORD}(?:[_-][a-z0-9]+)*"
_KEY_RE = re.compile(rf"{_KEY}\Z", re.I)
_Q = r"[\\]*[\"']"
_VALUE = rf"(?!\[REDACTED:)(?:(?:{_Q})[^\"'\r\n]+(?:{_Q})|[^\s'\"\\<>,;\}}\]]{{6,}})"
_FLAG = r"(?:--(?:api[-_]?key|access[-_]?(?:key|token)|app[-_]?secret|client[-_]?secret|secret|token|password|passwd|credential)|-s)"
_FLAG_RE = re.compile(rf"{_FLAG}\Z", re.I)
_PATTERN_SRCS = (
    rf"(?<![\w-]){_KEY}(?:{_Q})?\s*[=:]\s*{_VALUE}",
    # Both shell '--api-key value' and JSON argv ['--api-key', 'value'], at any escape depth.
    rf"(?<![\w-]){_FLAG}(?:(?:{_Q})?\s*,\s*|[=\s]+){_VALUE}",
    rf"(?:proxy-)?authorization(?:{_Q})?\s*:\s*(?:{_Q})?(?:bearer|basic|token)\s+[\w.\-+/=]{{8,}}",
    r"\bbearer\s+[A-Za-z0-9\-._~+/]{16,}={0,2}",
    r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
    r"\b(?:sk-[A-Za-z0-9_-]{16,}|figd_[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,}|xox[baprs]-[A-Za-z0-9-]{16,})",
    r"\b[a-z][a-z0-9+.\-]*://[^\s:/@]+:[^\s/]{3,}@[^\s/]+",
)
SECRET_PATTERNS = tuple(re.compile(src, re.I) for src in _PATTERN_SRCS)
SECRET_RE = re.compile("|".join(f"(?:{src})" for src in _PATTERN_SRCS), re.I)


def _placeholder(value):
    return isinstance(value, str) and re.fullmatch(r"\[REDACTED:[\w-]+\]", value) is not None


def redact_text(text):
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def transform(value, *, text_filter=redact_text, _depth=0):
    """Walk dict/argv and JSON-encoded strings, preserving unchanged text and JSON shape.

    Preview fields may be the only evidence, so retain and sanitize them rather than
    dropping every field called preview. Bound adversarial nesting and fail closed.
    """
    if _depth > 64:
        return "[REDACTED:nesting-limit]"
    if isinstance(value, dict):
        return {k: REDACTED if _KEY_RE.fullmatch(str(k)) and v not in (None, "")
                else transform(v, text_filter=text_filter, _depth=_depth + 1)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        result = []
        for i, item in enumerate(value):
            flagged = i > 0 and isinstance(value[i - 1], str) and _FLAG_RE.fullmatch(value[i - 1])
            result.append(REDACTED if flagged else transform(item, text_filter=text_filter, _depth=_depth + 1))
        return tuple(result) if isinstance(value, tuple) else result
    if not isinstance(value, str) or _placeholder(value):
        return value
    if value.lstrip().startswith(("{", "[", '"')):
        try:
            decoded = json.loads(value)
        except (ValueError, RecursionError):
            pass
        else:
            if isinstance(decoded, (dict, list, str)):
                clean = transform(decoded, text_filter=text_filter, _depth=_depth + 1)
                return value if clean == decoded else json.dumps(clean, ensure_ascii=False)
    return text_filter(value)


def contains_credentials(value):
    return transform(value) != value
