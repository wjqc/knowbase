"""Local lexical matching and excerpt selection for source document chunks."""

from collections import Counter
import math
import re

from . import index, sources

_HAN = re.compile(r"[\u3400-\u9fff]+")
_WORD = re.compile(r"[a-zA-Z0-9_][a-zA-Z0-9_.-]*")
_QUESTION_WORDS = re.compile(
    r"请问|请帮我|我想知道|告诉我|怎么(?:样)?|怎样|如何|能不能|能否|是否|会不会|"
    r"多少|几次|什么|哪些|为什么|才能|了以后|之后|的时候|可以|需要|应该|的|了|吗|呢"
)
_QUESTION_CUES = re.compile(r"怎么|怎样|如何|何时|多少|几次|什么|哪些|为什么|会不会|能否|是否|吗[？?]?$")
_DOCUMENT_TERMS = (
    (("旧版", "旧版本", "历史版本", "历史快照", "historical"), ("历史快照", "历史读取", "历史版本", "快照版本", "查历史")),
    (("文档变", "版本变", "更新"), ("版本漂移", "源已更新")),
    (("查看", "读回", "回溯"), ("读取", "定位")),
    (("宿主", "提交协议", "提交格式"), ("提交", "候选", "字段")),
    (("升级失败", "更新失败", "恢复旧版本"), ("失败回滚", "失败恢复", "执行回滚", "旧包")),
    (("重复计算", "重复统计", "算两次"), ("重叠", "不相加", "重复计数")),
    (("可信", "verified"), ("verified", "独立任务")),
)


def aliases(query):
    return {term for triggers, variants in _DOCUMENT_TERMS if any(trigger in query for trigger in triggers)
            for term in variants}



def question_parts(query):
    return [part for run in _HAN.findall(query) for part in _QUESTION_WORDS.split(run)
            if len(part) >= 2]


def signals(query):
    parts = question_parts(query)
    terms = {term.lower(): 3 for term in _WORD.findall(query) if len(term) >= 2}
    for term in aliases(query):
        terms[term.lower()] = 5
    for part in parts:
        for size in range(2, min(len(part), 4) + 1):
            for offset in range(len(part) - size + 1):
                term = part[offset:offset + size]
                terms[term] = max(terms.get(term, 0), size - 1)
    return terms


def find_candidates(conn, query, scope):
    parts = question_parts(query)
    words = [w.lower() for w in _WORD.findall(query)]
    keys = list(dict.fromkeys(
        words + sorted(aliases(query)) + [part[i:i + 3] for part in parts for i in range(len(part) - 2)]
    ))[:64]
    keys = [key for key in keys if len(key) >= 3 and key not in {"please", "what", "how", "the"}]
    if not keys:
        return []
    # Identifiers are literal evidence anchors, including when embedded in a question.
    required = [word for word in words if len(word) >= 3 and re.search(r"[0-9_./-]", word)]
    expression = " OR ".join(index.fts_term(term) for term in keys)
    if required:
        expression = "(" + expression + ") AND " + " AND ".join(index.fts_term(term) for term in required)
    return [dict(row) for row in conn.execute(
        "WITH matches AS MATERIALIZED (SELECT chunk_id,rank FROM source_chunks_fts WHERE source_chunks_fts MATCH ?) "
        "SELECT d.id document_id,d.title,d.path,d.scope,c.chunk_id,c.start_line,c.end_line,"
        "c.text _text,d.sha256 FROM matches f JOIN source_chunks c ON c.chunk_id=f.chunk_id "
        "JOIN source_documents d ON d.id=c.document_id WHERE d.status='available' AND (?='' OR d.scope=?) "
        "ORDER BY f.rank,d.path,c.start_line LIMIT 500", (expression, scope, scope),
    )]


def match_weights(query, rows):
    terms = signals(query)
    counts = Counter()
    # Document frequency avoids treating every overlapping chunk as a separate source.
    documents = {}
    for row in rows:
        documents.setdefault(row["document_id"], set()).update(
            term for term in terms if term in (row["_text"].lower() + "\n" + row["title"].lower())
        )
    for matches in documents.values():
        counts.update(matches)
    n = len(documents)
    return {term: weight * (1 + math.log((n + 1) / (counts[term] + 1)))
            for term, weight in terms.items()}


def _paragraphs(lines):
    heading = ""
    start = 0
    while start < len(lines):
        value = lines[start].strip()
        if not value:
            start += 1
            continue
        if value.startswith("#"):
            heading = value
            start += 1
            continue
        end = start + 1
        while (end < len(lines) and end - start < 5 and lines[end].strip()
               and not lines[end].lstrip().startswith(("#", "- ", "* ", "|"))
               and not re.match(r"\d+[.)]\s", lines[end].lstrip())):
            end += 1
        yield start, end, heading
        start = end


def excerpt(text, query, weights):
    lines = sources.snapshot_lines(text)
    first, last = 0, min(1, len(lines))
    best = 0.0
    question = bool(_QUESTION_CUES.search(query))
    wants_history = bool(re.search(r"旧版|历史|historical", query, re.I))
    wants_submission_format = bool(re.search(r"提交协议|提交格式", query))
    wants_stats_dedup = "统计" in query or "重复计算" in query
    asks_count = bool(re.search(r"多少|几[个次]|how many", query, re.I))
    wants_procedure = bool(re.search(r"怎么|怎样|如何|how (?:to|do)", query, re.I))
    lowered = [line.lower() for line in lines]
    for start, end, heading in _paragraphs(lines):
        body = "".join(lowered[start:end])
        matched = sum(weight for term, weight in weights.items() if term in body)
        if not matched:
            continue
        if not question and end - start > 1:
            matching = [i for i in range(start, end) if any(term in lowered[i] for term in weights)]
            if matching:
                start, end = matching[0], matching[-1] + 1
        score = matched + 0.26 * sum(weight for term, weight in weights.items()
                                     if term in heading.lower() and term not in body)
        if (asks_count or "独立任务" in query) and re.search(r"\d+\s*(?:个|次|条|项|组)", body):
            score *= 2.3
        if "恢复" in query and re.search(r"重复|不相加|重叠|回滚", body):
            score *= 1.4
        if wants_procedure and re.search(r"使用|通过|执行|指定|先|再|需要|即可|应当|必须|返回|用 ", body):
            score *= 1.16
        if wants_history and re.search(r"查历史|显式(?:选择|指定)|历史读取|--historical", body):
            score = score * 2 + 18
        if wants_stats_dedup and re.search(r"不相加|重复计数|重叠", body):
            score = score * 2 + 5
        if wants_submission_format and re.search(r"顶层|候选内部|字段|参数", body):
            score *= 1.55
        if wants_submission_format and re.search(r"每个.*(?:内|单独)|不能放在.*顶层", body):
            score = score * 2 + 18
        if "升级" in query and re.search(r"回滚|恢复", body + heading) and re.search(r"失败|旧|中断", body):
            score *= 1.4
        if question and re.search(r"？|\?\s*$", body):
            score *= 0.8
        if score > best:
            best, first, last = score, start, end
    if last == first + 1 and first > 0 and lines[first].lstrip().startswith(("- ", "* ")):
        # An example on the next nonempty line is useful when the bullet introduces it.
        if lines[first].rstrip().endswith(("：", ":")) and last < len(lines) and lines[last].strip():
            last += 1
    if best and len(weights) > 3:
        if len(lines[first].strip()) < 140 and last < len(lines):
            next_line = last
            while next_line < min(last + 3, len(lines)) and not lines[next_line].strip():
                next_line += 1
            if next_line < len(lines) and next_line < last + 3 and not lines[next_line].lstrip().startswith("#"):
                last = next_line + 1
        if wants_history and re.search(r"显式(?:选择|指定)|历史读取", "".join(lines[first:last])):
            for i in range(last, min(len(lines), last + 7)):
                if "historical" in lines[i].lower():
                    last = i + 1
                    for following in range(last, min(len(lines), last + 5)):
                        if "历史读取" in lines[following]:
                            last = following + 1
                            break
                    break
        previous = first - 1
        while previous >= max(0, first - 3) and not lines[previous].strip():
            previous -= 1
        if previous >= max(0, first - 2) and lines[previous].lstrip().startswith("#"):
            first = previous
    return best, first, last, "".join(lines[first:last])
