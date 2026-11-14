"""Shared lexical evidence for explicit natural-language retrieval."""

import re


_HAN = re.compile(r"[\u3400-\u9fff]+")
_CUES = re.compile(r"[?？]|怎么|怎样|如何|何时|哪儿|哪里|多少|几块|几项|什么|谁先|能不能|能否|会不会|是否|吗$")
_FILLER = re.compile(
    r"请问|请帮我|我想知道|告诉我|哪儿|哪里|哪个|哪些|哪几|什么|怎么|怎样|如何|"
    r"现在|以前|那套|那块|那天|已经|一共|分别|能不能|能否|会不会|是不是|"
    r"多少钱|多少|跟全国差|各管|接着办|才能|之后|可以|需要|应该|有没有|"
    r"^(?:按|用)|的|了|吗|呢"
)
_VERSION = re.compile(r"(?<![a-z0-9])v[0-9]+(?:\.[0-9]+)+(?![0-9]|\.[0-9])", re.I)
_GENERIC = {
    "方案", "系统", "项目", "功能", "管理", "业务", "公司", "报告", "现在", "支持",
    "湖南", "全国", "北京", "地方", "地市", "工作", "建设", "用户", "默认", "配置", "开通",
}
# Each group describes one concept; multiple spellings count as one match.
_CONCEPTS = (
    (r"跨(?:区|区域|地市|市).*抢单|跨区抢单", ("跨区抢单", "跨区域抢单", "跨地市抢单", "跨市抢单")),
    (r"共单", ("共单",)),
    (r"在途审批|在走.*审批|在办.*审批|审批.*续办", ("在途审批", "旧系统续办", "在途实例", "审批续办", "在办、会签")),
    (r"后台.*(?:拆|哪几|各管)|后端.*(?:拆|哪几|各管|职责)", ("四个后端", "后端的责任", "业务核心", "调度中心", "流程控制中心", "风控中心")),
    (r"(?:剩|余|余额|结余).*(?:垫|垫资)|项目资金结余", ("资金结余", "垫资情况", "收支情况")),
    (r"切.*(?:谁先|先切|顺序)|(?:查询|录入).*审批.*切", ("切换顺序", "先切换查询", "切换写入", "切换流程")),
    (r"第?一期|第一期", ("一期", "第一期", "首期")),
    (r"默认.*[开关]|默认启用|默认关闭", ("默认关闭", "默认开启", "默认启用", "开关默认关")),
)


class Question:
    def __init__(self, query, aliases=None, *, extra_groups=()):
        self.text = query
        self.version = next(iter(_VERSION.findall(query.lower())), "")
        self.groups = [terms for pattern, terms in _CONCEPTS if re.search(pattern, query)]
        self.required_groups = tuple(self.groups)
        self.groups.extend(tuple(terms) for phrase, terms in (aliases or {}).items() if phrase.lower() in query.lower())
        self.groups.extend(extra_groups)
        self.parts = tuple(dict.fromkeys(
            term for run in _HAN.findall(query) for part in _FILLER.split(run)
            if len(term := part.removesuffix("是")) >= 2 and term not in _GENERIC
        ))
        # Contiguous trigrams provide mentions without matching arbitrary separated characters.
        self.grams = tuple(dict.fromkeys(gram for part in self.parts for i in range(len(part) - 2)
                                        if (gram := part[i:i + 3]) not in _GENERIC))
        self.words = tuple(dict.fromkeys(re.findall(r"[a-zA-Z][a-zA-Z0-9_]+", _VERSION.sub("", query).lower())))

    def terms(self):
        terms = (*[term for group in self.groups for term in group], *self.grams, *self.words)
        return tuple(dict.fromkeys(term.lower() for term in terms if len(term) >= 3))[:72]

    def version_matches(self, text):
        return not self.version or self.version in _VERSION.findall(text.lower())

    def score(self, text, *, metadata="", corroborate_fragments=False):
        content = text.lower()
        heading = metadata.lower()
        phrases = [part for part in self.parts if len(part) >= 3 and part in content]
        grams = [gram for gram in self.grams if gram in content]
        concepts = sum(any(term.lower() in content for term in group) for group in self.groups)
        if self.required_groups and not all(any(term in content for term in group)
                                            for group in self.required_groups):
            return 0
        # Two distant common words can coincide. Require a concept or an actual
        # continuous query phrase, or corroboration from independent query parts.
        corroborated = sum(any(part[i:i + 3] in content for i in range(len(part) - 2))
                           for part in self.parts) >= 2
        long_overlap = any(part[i:i + 4] in content for part in self.parts for i in range(len(part) - 3))
        # For shared cards in project searches, a fragment from one clause can
        # be incidental even when it contains several adjacent matching grams.
        if (corroborate_fragments and sum(len(part) >= 3 for part in self.parts) > 1
                and not concepts and not corroborated and not any(len(part) >= 4 for part in phrases)):
            return 0
        if not (concepts or any(len(p) >= 4 for p in phrases) or long_overlap
                or len(grams) >= 2 and corroborated):
            return 0
        return (8 * concepts + sum(min(12, 2 * len(p)) for p in phrases)
                + len(grams) + 5 * sum(word in content for word in self.words)
                + 2 * sum(part in heading for part in phrases)
                + 4 * sum(any(term.lower() in heading for term in group) for group in self.groups))


def is_question(query):
    return len("".join(_HAN.findall(query))) >= 6 and bool(_CUES.search(query))
