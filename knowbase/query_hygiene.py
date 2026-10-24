"""Hook 自动检索的查询卫生：意图门控 + 保守蒸馏（无模型路径）。

Phase 1（docs/retrieval-upgrade/02 §5）：长文/粘贴文档直接跳过检索与注入，
普通短句原样使用；需要蒸馏时只做确定性规则——提取首个疑问/祈使句与技术标识
（错误码/类名/反引号标识符），标识符原文保留不做同义替换。宁可跳过，不注入噪音。
"""

import re

MAX_QUERY_CHARS = 512    # 超过视为粘贴文档，整体跳过
MAX_DISTILL_CHARS = 120  # 蒸馏后查询总长上限
SENT_MAX_CHARS = 64      # 单句上限
MAX_IDENTIFIERS = 5

URL_RE = re.compile(r"https?://\S+|www\.\S+")
HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+\S", re.M)
CODEISH_RE = re.compile(r"^\s{4,}\S|^\s*```|^\s*\|", re.M)
SENT_SPLIT_RE = re.compile(r"[。！？!?\n；;]")
QUESTION_RE = re.compile(
    r"怎么|如何|为什么|咋|能不能|是不是|有没有|帮我|看看|报错|失败|异常|无法|不支持|超时")
IDENT_RE = re.compile(r"`([^`\n]{2,48})`")
ERRCODE_RE = re.compile(
    r"\b(HTTP\s?\d{3}|[A-Z]{2,10}[-_]\d+|[A-Z][A-Z0-9_]{3,}|E\d{2,}|[A-Za-z]+Error|[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+)\b")
BULLET_RE = re.compile(r"^(?:[-*•>\d.、）)>\s]+)")


def _is_doc_paste(p: str) -> str | None:
    if len(URL_RE.findall(p)) > 3:
        return "doc_paste:urls"
    if len(HEADING_RE.findall(p)) > 5:
        return "doc_paste:headings"
    lines = [ln for ln in p.splitlines() if ln.strip()]
    if lines:
        codeish = sum(1 for ln in lines if CODEISH_RE.match(ln))
        if codeish / len(lines) > 0.3:
            return "doc_paste:code_ratio"
    return None


def _identifiers(p: str) -> list[str]:
    found = []
    for m in IDENT_RE.finditer(p):
        found.append(m.group(1).strip())
    for m in ERRCODE_RE.finditer(p):
        found.append(m.group(1))
    out = []
    for t in found:
        if t and t not in out:
            out.append(t)
    return out[:MAX_IDENTIFIERS]


def _first_sentence(p: str) -> str:
    # 疑问/求助句优先，但纯标记短句（如"怎么解决"）信息量不足，须 ≥6 字；
    # 否则回退首个有信息量的句子（≤64 字截断保主干）。
    for raw in SENT_SPLIT_RE.split(p):
        s = BULLET_RE.sub("", raw.strip()).strip()
        if len(s) >= 6 and (QUESTION_RE.search(s) or s.endswith("?") or s.endswith("？")):
            return s[:SENT_MAX_CHARS]
    for raw in SENT_SPLIT_RE.split(p):
        s = BULLET_RE.sub("", raw.strip()).strip()
        if len(s) >= 6:
            return s[:SENT_MAX_CHARS]
    return ""


def prepare(prompt: str) -> dict:
    """返回 {"action": "search", "query", "query_class"} 或 {"action": "skip", "reason"}。"""
    p = (prompt or "").strip()
    # 下限取 2：单字符是噪声，但 2 字中文词（死锁）与 3 字技术标识（vpn/HAR）是合法精确查询。
    if len(p) < 2:
        return {"action": "skip", "reason": "too_short"}
    if len(p) > MAX_QUERY_CHARS:
        return {"action": "skip", "reason": "too_long"}
    if re.match(r"^(?:谢谢|好的[，。！! ]*$|收到[，。！! ]*$|(?:请|帮我)?(?:翻译|润色|排版|格式化|总结(?:以下|这段|本文)))", p):
        return {"action": "skip", "reason": "self_contained"}
    paste = _is_doc_paste(p)
    if paste:
        return {"action": "skip", "reason": paste}
    if len(p) <= SENT_MAX_CHARS:
        return {"action": "search", "query": p, "query_class": "raw"}
    sentence = _first_sentence(p)
    if sentence and len(set(sentence)) < 4:
        sentence = ""  # 单字符重复等无信息量文本，视为无信号
    idents = _identifiers(p)
    if not sentence and not idents:
        return {"action": "skip", "reason": "no_signal"}
    parts, seen = [], set()
    for t in idents + [sentence]:
        t = (t or "").strip()
        if t and t not in seen:
            seen.add(t)
            parts.append(t)
    packed = []
    for part in parts:
        if len(" ".join(packed + [part])) <= MAX_DISTILL_CHARS:
            packed.append(part)
    query = " ".join(packed).strip()
    if len(query) < 6:
        return {"action": "skip", "reason": "no_signal"}
    return {"action": "search", "query": query, "query_class": "distilled"}
