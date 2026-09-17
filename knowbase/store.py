"""存储层：markdown + frontmatter 是唯一真相。

负责：frontmatter 解析/渲染、lint 校验、id 分配、查重、全库遍历。
"""

import re
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path

import yaml

TYPES = ("pitfall", "standard", "decision", "workflow", "preference", "reference", "bizrule")
TYPE_DIR = {
    "pitfall": "pitfalls",
    "standard": "standards",
    "decision": "decisions",
    "workflow": "workflows",
    "preference": "preferences",
    "reference": "references",
    "bizrule": "bizrules",
}
PREFIX = {"pitfall": "P", "standard": "S", "decision": "D", "workflow": "W", "preference": "PR",
          "reference": "R", "bizrule": "B"}
ALL_DIRS = ("standards", "pitfalls", "decisions", "workflows", "preferences", "references", "bizrules", "staging")
DIR_TYPE = {v: k for k, v in TYPE_DIR.items()}

# 各类型必填小节（body 的 ## 标题需包含这些词）
REQUIRED_SECTIONS = {
    "pitfall": ["现象", "原因", "正确做法"],
    "standard": ["规则", "理由"],
    "decision": ["背景", "决策"],
    "workflow": ["步骤"],
    "preference": ["规则"],
    "bizrule": ["规则"],
}
RELATION_TYPES = ("related", "supersedes", "contradicts", "derived_from")
SOURCE_RE = re.compile(r"^agent:[\w.\-]+:[\w.\-]+$|^human:.+$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SECRET_RE = re.compile(
    r"(?i)(password|passwd|secret|api[_-]?key|token)\s*[=:]\s*['\"]?[^\s'\"]{6,}"
)
ID_RE = re.compile(r"^[A-Z]{1,2}-\d{4}-\d{4}$")


class LintError(Exception):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


# ---------- frontmatter I/O ----------

def parse(path: Path) -> tuple[dict, str]:
    text = Path(path).read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise ValueError(f"{path} 缺少 frontmatter")
    end = text.find("\n---", 3)
    if end < 0:
        raise ValueError(f"{path} frontmatter 未闭合")
    meta = yaml.safe_load(text[3:end].strip()) or {}
    body = text[end + 4:].lstrip("\n")
    return meta, body


def render(meta: dict, body: str) -> str:
    fm = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False, default_flow_style=False)
    return f"---\n{fm}---\n\n{body.strip()}\n"


# ---------- 路径与遍历 ----------

def mem_path(repo: Path, dtype: str, mid: str, staging: bool = False) -> Path:
    d = Path(repo) / ("staging" if staging else TYPE_DIR[dtype])
    return d / f"{mid}.md"


def find_file(repo: Path, mid: str) -> Path | None:
    for d in ALL_DIRS:
        p = Path(repo) / d / f"{mid}.md"
        if p.exists():
            return p
    return None


def load(repo: Path, mid: str) -> tuple[dict | None, str | None, Path | None]:
    p = find_file(repo, mid)
    if not p:
        return None, None, None
    meta, body = parse(p)
    return meta, body, p


def iter_all(repo: Path, include_staging: bool = False):
    repo = Path(repo)
    dirs = ALL_DIRS if include_staging else ALL_DIRS[:-1]
    for d in dirs:
        dd = repo / d
        if not dd.exists():
            continue
        for f in sorted(dd.glob("*.md")):
            try:
                meta, body = parse(f)
            except Exception:
                continue
            yield meta, body, f


# ---------- id 与查重 ----------

def alloc_id(repo: Path, dtype: str, year: int | None = None) -> str:
    year = year or date.today().year
    d = Path(repo) / TYPE_DIR[dtype]
    seq = 0
    pat = re.compile(rf"^{PREFIX[dtype]}-{year}-(\d+)\.md$")
    for directory in (d, Path(repo) / "staging"):
        for f in directory.glob("*.md"):
            m = pat.match(f.name)
            if m:
                seq = max(seq, int(m.group(1)))
    return f"{PREFIX[dtype]}-{year}-{seq + 1:04d}"


def normalize_title(t: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", t or "").lower()


def find_similar(repo: Path, title: str, threshold: float = 0.82):
    """标题归一化相似查重。返回 (ratio, meta, path) 或 None。"""
    nt = normalize_title(title)
    best = None
    for meta, _body, path in iter_all(repo, include_staging=True):
        ratio = SequenceMatcher(None, nt, normalize_title(meta.get("title", ""))).ratio()
        if ratio >= threshold and (best is None or ratio > best[0]):
            best = (ratio, meta, path)
    return best


# ---------- lint ----------

def lint(meta: dict, body: str) -> list[str]:
    errs = []
    t = meta.get("type")
    if t not in TYPES:
        errs.append(f"type 非法: {t}（须为 {TYPES}）")
    if not str(meta.get("title", "")).strip():
        errs.append("title 必填")
    if not str(meta.get("scope", "")).strip():
        errs.append("scope 必填")
    if not isinstance(meta.get("tags", []), list):
        errs.append("tags 必须是列表")
    for k in ("created", "updated", "last_verified"):
        v = meta.get(k)
        if v and not DATE_RE.match(str(v)):
            errs.append(f"{k} 日期格式须为 yyyy-mm-dd: {v}")
    if meta.get("source") and not SOURCE_RE.match(str(meta["source"])):
        errs.append(f"source 格式须为 agent:<工具>:<会话> 或 human:<姓名>: {meta['source']}")
    rel = meta.get("relations") or []
    if not isinstance(rel, list):
        errs.append("relations 必须是列表")
    else:
        for r in rel:
            if not (isinstance(r, dict) and r.get("id") and r.get("type") in RELATION_TYPES):
                errs.append(f"relations 项须含 id 与合法 type {RELATION_TYPES}: {r}")
    heads = re.findall(r"^#{1,4}\s*(.+)$", body or "", re.M)
    for sec in REQUIRED_SECTIONS.get(t, []):
        if not any(sec in h for h in heads):
            errs.append(f"缺少必填小节: {sec}")
    if t == "bizrule" and not str(meta.get("provenance", "")).strip():
        errs.append("业务规则必须带出处 provenance（PRD 编号/条款/业务方确认记录），无出处的规则不入库")
    if SECRET_RE.search(body or ""):
        errs.append("body 疑似包含明文密钥(password/token 等)，请脱敏后再存")
    return errs


def lint_warnings(meta: dict, body: str) -> list[str]:
    """写作规范建议（warn 级，不阻断入库）：面向跨语言检索质量。"""
    warns = []
    title = str(meta.get("title", ""))
    tags = [str(t) for t in (meta.get("tags") or [])]
    title_ascii = any(c.isascii() and c.isalnum() for c in title)
    tags_ascii = any(any(c.isascii() and c.isalnum() for c in t) for t in tags)
    tags_cjk = any("\u4e00" <= c <= "\u9fff" for t in tags for c in t)
    if not title_ascii and not tags_ascii:
        warns.append("标题与标签均无英文/字母关键词，跨语言检索易漏检，建议补技术名词")
    if not tags_cjk:
        warns.append("标签无中文关键词，建议补一个中文通俗说法")
    return warns


def new_meta(dtype: str, title: str, scope: str, tags: list, source: str,
             relations: list | None = None, evidence: list | None = None) -> dict:
    """新建记忆的 frontmatter。confidence/status 由服务端定，调用方不可指定。"""
    today = date.today().isoformat()
    return {
        "id": "",
        "type": dtype,
        "title": title.strip(),
        "scope": scope,
        "tags": tags or [],
        "source": source,
        "evidence": evidence or [],
        "confidence": "once",
        "status": "active",
        "last_verified": "",
        "helpful_count": 0,
        "unhelpful_count": 0,
        "created": today,
        "updated": today,
        "relations": relations or [],
    }
