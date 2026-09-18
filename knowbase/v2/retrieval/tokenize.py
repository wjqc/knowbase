"""中英统一分词（P2）。

策略：
- 英文/数字：按 \\w+ 切
- 中文：在 unicode 词边界之间补充 bigram（保留单字 + 二元组合）
- 长度过滤：去 1 字英文（避免 'a', 'I' 噪音）；中文 1-2 字都保留
- 全小写

无依赖、可复现；与 V1 store 的 FTS5 tokenizer 行为有差异，但评测对照只看相对效果。
"""
from __future__ import annotations

import re
import unicodedata

_RE_WS = re.compile(r"\s+", re.UNICODE)
_RE_CJK = re.compile(r"[\u4e00-\u9fff]")
_RE_WORD = re.compile(r"[A-Za-z0-9_]+")
_RE_PUNCT = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)


def normalize(text: str) -> str:
    if not text:
        return ""
    # 全角 → 半角
    text = unicodedata.normalize("NFKC", text)
    text = _RE_PUNCT.sub(" ", text)
    return text.strip()


def tokenize(text: str) -> list[str]:
    """分词：英文 tokens + 中文 bigrams（含单字）。"""
    text = normalize(text)
    if not text:
        return []

    tokens: list[str] = []

    # 1) 英文/数字 token
    for m in _RE_WORD.finditer(text):
        t = m.group(0).lower()
        if len(t) >= 2:
            tokens.append(t)

    # 2) 中文段:逐字 + 相邻二元
    cjk_spans: list[str] = []
    cur: list[str] = []
    for ch in text:
        if _RE_CJK.match(ch):
            cur.append(ch)
        else:
            if cur:
                cjk_spans.append("".join(cur))
                cur = []
    if cur:
        cjk_spans.append("".join(cur))

    for span in cjk_spans:
        if len(span) == 1:
            tokens.append(span)
        else:
            tokens.extend(span)  # 单字
            tokens.extend(span[i] + span[i + 1] for i in range(len(span) - 1))  # bigram

    # 3) 去重保留顺序（不强制；BM25 / dense 自行决定）
    return tokens


def tokenize_set(text: str) -> set[str]:
    return set(tokenize(text))
