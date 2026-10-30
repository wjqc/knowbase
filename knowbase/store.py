"""存储层：markdown + frontmatter 是唯一真相。

负责：frontmatter 解析/渲染、lint 校验、id 分配、查重、全库遍历。
"""

import re
import uuid
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path

import yaml

from .credentials import SECRET_PATTERNS, SECRET_RE, contains_credentials

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

# 各类型必填小节（body 的 ## 标题需包含这些词）——仅适用于未打 card-v2 标记的存量卡
REQUIRED_SECTIONS = {
    "pitfall": ["现象", "原因", "正确做法"],
    "standard": ["规则", "理由"],
    "decision": ["背景", "决策"],
    "workflow": ["步骤"],
    "preference": ["规则"],
    "bizrule": ["规则"],
}

# 统一知识卡 schema（2026-09-21 起）：八项小节强制 + 内容质量校验。
# 存量卡不带 schema 标记，沿用各自旧规则；迁移时补标记即升级。
CARD_V2_SCHEMA = "card-v2"
CARD_SECTIONS = ("结论", "解决的问题", "适用条件", "不适用条件",
                 "可执行动作", "关键证据", "验证情况", "未知与待确认")

# 八项结构内容质量：出现标题不算通过（vague 条件 / 纯引用证据 / 空洞验证都要拦）
_VAGUE_CONDITION_RE = re.compile(r"视情况而定|根据实际情况|具体情况具体分析|按需处理")
_REF_ONLY_RE = re.compile(r"^(?:参见|参考|详见|见[:：\s])")
_TEST_PASS_ONLY_RE = re.compile(r"^(?:测试通过|已测试|测试全部通过|自测通过|无)$")
RELATION_TYPES = ("related", "supersedes", "contradicts", "derived_from")
SOURCE_RE = re.compile(r"^agent:[\w.\-]+:[\w.\-]+$|^human:.+$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ID_RE = re.compile(r"^[A-Z]{1,2}-\d{4}-\d{4}$|^[A-Z]{1,2}-[0-9a-f]{12}$")

def find_secrets(text) -> list[str]:
    """返回安全的命中提示，拒绝时也不把原凭据回显给宿主。"""
    return ["[REDACTED:credential]"] if contains_credentials(text) else []


# 经验卡是共享资产，不能依赖作者机器上的文件。代码定位允许使用仓库相对路径；
# 文档引用仅允许指向 knowbase 仓库内实际存在的相对路径。
_MACHINE_PATH_RE = re.compile(
    r"(?<![\w.-])(?:~[/\\]|/(?:Users|home)/[^\s`'\"<>]+|[A-Za-z]:\\+Users\\+[^\s`'\"<>]+)"
)
_DOC_EXT = r"(?:md|markdown|txt|pdf|docx?|xlsx?|pptx?|html?|rtf)"
_BACKTICK_DOC_RE = re.compile(rf"`([^`\n]+\.{_DOC_EXT}(?:#[^`\n]+)?)`", re.I)
_LINK_DOC_RE = re.compile(rf"\]\(([^)\n]+\.{_DOC_EXT}(?:#[^)\n]+)?)\)", re.I)
_PLAIN_DOC_RE = re.compile(
    rf"(?<![\w:/.-])((?:\.\.?/)?(?:[\w\u4e00-\u9fff.-]+[/\\])+"
    rf"[\w\u4e00-\u9fff.-]+\.{_DOC_EXT})(?=$|[\s)`'\"，。；：,;:])",
    re.I,
)
_CODE_REF_RE = re.compile(r"^code:[A-Za-z0-9_.-]+/(?!/)(?!.*(?:^|/)\.\.(?:/|$)).+")
_CODE_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_CODE_LINES_RE = re.compile(r"^\d+(?:-\d+)?$")


def _all_text(meta: dict, body: str) -> str:
    """收集会写进共享 Markdown 的字符串字段，供引用边界校验。"""
    values = [body or ""]

    def visit(value):
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, dict):
            for item in value.values():
                visit(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item)

    visit(meta)
    return "\n".join(values)


def reference_errors(meta: dict, body: str, repo: Path | None = None) -> list[str]:
    """校验共享经验卡的文件引用边界。"""
    text = _all_text(meta, body)
    errs = []
    machine_paths = sorted(set(m.group(0).rstrip(".,;:，。；：") for m in _MACHINE_PATH_RE.finditer(text)))
    if machine_paths:
        sample = "、".join(machine_paths[:3])
        errs.append(
            f"禁止保存本机路径（{sample}）。文档内容须写入卡片或放在 knowbase 仓库内；"
            "代码证据请写“项目/仓库标识 + 仓库相对路径”"
        )
    doc_refs = []
    for pattern in (_BACKTICK_DOC_RE, _LINK_DOC_RE, _PLAIN_DOC_RE):
        doc_refs.extend(m.group(1) for m in pattern.finditer(text))
    for value in doc_refs:
        raw = value.replace("\\", "/").strip()
        if _CODE_REF_RE.match(raw):
            continue
        raw = raw.split("#", 1)[0]
        if raw.startswith("../") or "/../" in raw:
            errs.append(f"文档引用越出 knowbase 仓库: {raw}")
            continue
        if repo is not None:
            candidate = (Path(repo) / raw.removeprefix("./")).resolve()
            root = Path(repo).resolve()
            if root not in candidate.parents and candidate != root:
                errs.append(f"文档引用越出 knowbase 仓库: {raw}")
            elif not candidate.is_file():
                errs.append(
                    f"文档引用不在 knowbase 仓库内: {raw}。请把关键内容写入卡片正文，"
                    "或先将文档纳入 knowbase 后再用仓库相对路径引用"
                )
    return list(dict.fromkeys(errs))


def code_refs_errors(meta: dict) -> list[str]:
    """Validate structured repository-relative code locations in frontmatter."""
    refs = meta.get("code_refs")
    if refs is None:
        return []
    if not isinstance(refs, list):
        return ["code_refs 必须是列表"]
    errs = []
    for pos, ref in enumerate(refs, 1):
        prefix = f"code_refs[{pos}]"
        if not isinstance(ref, dict):
            errs.append(f"{prefix} 必须是对象")
            continue
        repo = ref.get("repo")
        path = ref.get("path")
        if not isinstance(repo, str) or not repo.strip():
            errs.append(f"{prefix}.repo 必填")
        elif not _CODE_REPO_RE.fullmatch(repo):
            errs.append(f"{prefix}.repo 只允许 [A-Za-z0-9_.-]+: {repo}")
        if not isinstance(path, str) or not path.strip():
            errs.append(f"{prefix}.path 必填")
        else:
            if path.startswith("/"):
                errs.append(f"{prefix}.path 必须是仓库相对路径: {path}")
            if "\\" in path:
                errs.append(f"{prefix}.path 必须统一使用正斜杠: {path}")
            if ".." in path.split("/"):
                errs.append(f"{prefix}.path 禁止 .. 路径穿越: {path}")
        lines = ref.get("lines")
        if lines is not None and not _CODE_LINES_RE.fullmatch(str(lines)):
            errs.append(f"{prefix}.lines 格式须为行号或范围（如 120 或 120-180）: {lines}")
    return errs

# 任务执行日志特征（warn 级）：结果数字/阶段收尾属于过程产物，应放 progress/docs 而非记忆库
LOG_SIGNALS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"\d+\s*/\s*\d+\s*(通过|passed)", re.I), "通过率数字"),
    (re.compile(r"\bHR@\d|\bMRR\b\s*[=＝]|P\d{1,2}\s*=\s*\d+(\.\d+)?\s*(ms|毫秒)", re.I), "评测指标"),
    (re.compile(r"(提升到|涨到|达到|命中率?)[^。\n]{0,8}\d+(\.\d+)?\s*%"), "百分比结果"),
    (re.compile(r"收尾实测|Phase\s*\S{1,6}\s*收尾"), "阶段收尾记录"),
)


class LintError(Exception):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


# ---------- frontmatter I/O ----------

def parse(path: Path) -> tuple[dict, str]:
    # utf-8-sig：兼容 Windows 记事本等保存的 BOM 文件，避免整卡被"缺少 frontmatter"跳过
    return parse_text(Path(path).read_text(encoding="utf-8-sig"),str(path))


def parse_text(text: str, source: str = "memory") -> tuple[dict, str]:
    path=source
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


def write_shared_text(path: Path, text: str) -> None:
    """写共享 Git 仓库文件：固定 UTF-8 + LF。

    Windows 文本模式默认把 \\n 写成 \\r\\n，同一内容在两平台落盘字节不同：
    Git 会判成 blob 伪分歧并引发 rebase 冲突；sources 的 `{sha}.md` 文件名
    按内存 LF 文本计算，磁盘 CRLF 会导致文件名与实际内容对不上。"""
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


# ---------- 路径与遍历 ----------

def valid_id(mid: str) -> bool:
    """卡片 ID 必须是分配器产出的格式，拒绝任何含路径分隔符的输入。"""
    return bool(mid) and bool(ID_RE.match(mid))


def mem_path(repo: Path, dtype: str, mid: str, staging: bool = False) -> Path:
    if not valid_id(mid):
        raise ValueError(f"非法卡片 ID: {mid!r}")
    d = Path(repo) / ("staging" if staging else TYPE_DIR[dtype])
    return d / f"{mid}.md"


def _contained(repo: Path, path: Path) -> Path | None:
    """确认 path 落在 repo 内（含符号链接解析后仍在仓库内），否则返回 None。"""
    try:
        root = Path(repo).resolve()
        resolved = path.resolve()
    except OSError:
        return None
    if resolved != root and root not in resolved.parents:
        return None
    return path


def find_file(repo: Path, mid: str) -> Path | None:
    if not valid_id(mid):
        return None
    for d in ALL_DIRS:
        p = _contained(repo, Path(repo) / d / f"{mid}.md")
        if p is not None and p.exists():
            return p
    return None


def load(repo: Path, mid: str) -> tuple[dict | None, str | None, Path | None]:
    p = find_file(repo, mid)
    if not p:
        return None, None, None
    meta, body = parse(p)
    return meta, body, p


def iter_all(repo: Path, include_staging: bool = False, errors: list | None = None):
    """遍历全部可解析卡片，产出 (meta, body, path)；解析失败的文件默认跳过。

    传入 errors 列表时，每张被跳过的坏卡追加一条 (路径, 异常)，供 doctor
    等诊断方显性报告；不传时行为与从前完全一致（坏卡静默跳过，各调用方
    语义不变）——但坏卡若无人收集 errors，就对全系统不可见。
    """
    repo = Path(repo)
    dirs = ALL_DIRS if include_staging else ALL_DIRS[:-1]
    for d in dirs:
        dd = repo / d
        if not dd.exists():
            continue
        for f in sorted(dd.glob("*.md")):
            try:
                meta, body = parse(f)
            except Exception as exc:
                if errors is not None:
                    errors.append((f, exc))
                continue
            yield meta, body, f


# ---------- id 与查重 ----------

def alloc_id(repo: Path, dtype: str, year: int | None = None) -> str:
    """分配新卡片 ID。

    新格式：`P-<uuid12>`（如 `P-a1b2c3d4e5f6`），跨机唯一；
    旧格式 `P-2026-0001` 保留兼容，不再新分配。
    """
    short = uuid.uuid4().hex[:12]
    return f"{PREFIX[dtype]}-{short}"


def normalize_title(t: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", t or "").lower()


def find_similar(repo: Path, title: str, threshold: float = 0.82, scope: str | None = None):
    """标题归一化相似查重。返回 (ratio, meta, path) 或 None。

    预筛选策略（减少完整 ratio() 调用次数）：
    1. 先对所有候选计算 quick_ratio()（仅比较字符频率，极快）；
    2. 过滤掉 quick_ratio < threshold * 0.8 的候选；
    3. 按 quick_ratio 降序排序，仅对前 max(50, N/2) 个候选计算完整 ratio()。

    quick_ratio() 始终 >= ratio()，因此不会漏掉任何 ratio >= threshold 的候选。
    """
    nt = normalize_title(title)

    # 收集候选（延迟创建 SequenceMatcher 以避免一次性大内存占用）
    matchers: list[tuple[SequenceMatcher, dict, Path]] = []
    for meta, _body, path in iter_all(repo, include_staging=True):
        if scope is not None and meta.get("scope") != scope:
            continue
        sm = SequenceMatcher(None, nt, normalize_title(meta.get("title", "")))
        matchers.append((sm, meta, path))

    if not matchers:
        return None

    # quick_ratio 预筛选
    threshold_quick = threshold * 0.8
    scored: list[tuple[float, SequenceMatcher, dict, Path]] = []
    for sm, meta, path in matchers:
        qr = sm.quick_ratio()
        if qr >= threshold_quick:
            scored.append((qr, sm, meta, path))

    if not scored:
        return None

    # 按 quick_ratio 降序，仅对排名靠前的候选计算完整 ratio
    scored.sort(key=lambda x: x[0], reverse=True)
    top_k = max(50, len(scored) // 2)

    best = None
    for _qr, sm, meta, path in scored[:top_k]:
        ratio = sm.ratio()
        if ratio >= threshold and (best is None or ratio > best[0]):
            best = (ratio, meta, path)

    return best


# ---------- 统一知识卡（card-v2）八项结构校验 ----------

def _sections(body: str) -> dict[str, str]:
    """## 小节名 → 小节正文（到下一个任意级别标题前，strip 后返回）。"""
    out: dict[str, list[str]] = {}
    cur = None
    for line in (body or "").splitlines():
        m = re.match(r"^#{1,4}\s*(.+)$", line)
        if m:
            cur = m.group(1).strip()
            out.setdefault(cur, [])
        elif cur is not None:
            out[cur].append(line)
    return {k: "\n".join(v).strip() for k, v in out.items()}


def _card_section_text(secs: dict[str, str], name: str) -> str | None:
    """按小节标题精确匹配取正文；小节不存在返回 None。

    不能用包含匹配：「不适用条件」包含「适用条件」，子串匹配会让缺失
    「适用条件」的卡片借道「不适用条件」的正文通过 card-v2 校验，
    八项小节门禁形同虚设。标题合法形态以卡片模板与测试为准（纯标题名）。
    """
    return secs.get(name)


def card_v2_errors(body: str) -> list[str]:
    """八项小节强制 + 内容质量。仅出现标题不算通过。"""
    errs = []
    secs = _sections(body)
    texts = {}
    for sec in CARD_SECTIONS:
        text = _card_section_text(secs, sec)
        if text is None:
            errs.append(f"缺少必填小节: {sec}")
            continue
        if not text:
            errs.append(f"小节「{sec}」仅有标题无内容")
        texts[sec] = text
    cond = texts.get("适用条件", "")
    if cond and _VAGUE_CONDITION_RE.search(cond):
        errs.append("适用条件含“视情况而定/根据实际情况”类表述，不可判断；须写可判定的条件（环境/版本/触发现象）")
    evidence = texts.get("关键证据", "")
    if evidence:
        lines = [re.sub(r"^[-*\d.\s]+", "", l).strip() for l in evidence.splitlines() if l.strip()]
        if lines and all(_REF_ONLY_RE.match(l) for l in lines):
            errs.append("关键证据不能只写“参见/参考某文档”；须摘录事实、命令输出或代码定位（code:<项目>/<相对路径>）")
    verified = texts.get("验证情况", "")
    if verified and _TEST_PASS_ONLY_RE.match(verified):
        errs.append("“测试通过”不构成验证记录；须含时间、环境、方法与结果")
    return errs


# ---------- lint ----------

def source_refs_errors(meta: dict, repo: Path | None = None) -> list[str]:
    refs = meta.get("source_refs") or []
    if not isinstance(refs, list):
        return ["source_refs 必须为列表"]
    errors = []
    for ref in refs:
        if not isinstance(ref, dict) or not ref.get("source_id"):
            errors.append("source_refs 每项必须包含 source_id")
            continue
        if repo is None:
            continue
        from . import sources
        import hashlib
        manifest = sources.load_manifest(repo, ref["source_id"])
        if not manifest or manifest.get("scope") not in (meta.get("scope"), "global"):
            errors.append("source_refs 来源不存在或项目不匹配")
            continue
        root = (Path(repo) / sources.OBJECTS_DIR).resolve()
        path = (Path(repo) / str(manifest.get("object", ""))).resolve()
        if root not in path.parents or not path.is_file():
            errors.append("source_refs 来源对象不存在或路径越界")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            errors.append("source_refs 来源对象无法读取")
            continue
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if manifest.get("sha256") != sha or ref.get("sha256", sha) != sha:
            errors.append("source_refs 来源对象哈希不匹配")
            continue
        fragment = text
        if ref.get("segment_id"):
            from .extraction.segmenter import segment_text
            segments = {s.segment_id: s for s in segment_text(text, source_format=manifest.get("format", "markdown"))}
            segment = segments.get(ref["segment_id"])
            if not segment:
                errors.append("source_refs segment_id 不存在")
                continue
            fragment = segment.text
        expected_hash = ref.get("text_hash")
        if expected_hash:
            actual = hashlib.sha256(fragment.encode("utf-8")).hexdigest()
            if expected_hash not in (actual, actual[:16]):
                errors.append("source_refs text_hash 不匹配")
    return errors


def lint(meta: dict, body: str, repo: Path | None = None) -> list[str]:
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
    if meta.get("schema") == CARD_V2_SCHEMA:
        errs.extend(card_v2_errors(body))
    else:
        for sec in REQUIRED_SECTIONS.get(t, []):
            if not any(sec in h for h in heads):
                errs.append(f"缺少必填小节: {sec}")
    if t == "bizrule" and not str(meta.get("provenance", "")).strip():
        errs.append("业务规则必须带出处 provenance（PRD 编号/条款/业务方确认记录），无出处的规则不入库")
    rule = meta.get("rule_context")
    if rule is not None:
        if not isinstance(rule, dict):
            errs.append("rule_context 必须为对象")
        else:
            for key in ("business_object", "trigger", "required_or_forbidden", "exceptions", "applicable_version"):
                if not str(rule.get(key, "")).strip():
                    errs.append(f"rule_context.{key} 必填；未知项需明确标注")
            if rule.get("business_confirmation") not in ("explicit_source", "code_inferred", "unconfirmed"):
                errs.append("rule_context.business_confirmation 非法")
            if rule.get("implementation_status") not in ("not_implemented", "implemented_unverified", "verified", "unknown"):
                errs.append("rule_context.implementation_status 非法")
    leaks = find_secrets(_all_text(meta, body))
    if leaks:
        sample = "、".join(leaks[:2])
        errs.append(f"疑似包含明文密钥(password/token/Authorization 等): {sample}，请脱敏后再存")
    errs.extend(code_refs_errors(meta))
    errs.extend(source_refs_errors(meta, repo))
    errs.extend(reference_errors(meta, body, repo))
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
    if re.search(r"(?<![\w.-])code:[A-Za-z0-9_.-]+/", body or "") and not meta.get("code_refs"):
        warns.append("正文含 code: 代码定位，建议同步写入 frontmatter code_refs 字段")
    for pat, label in LOG_SIGNALS:
        hit = pat.search(title) or pat.search(body or "")
        if hit:
            warns.append(
                f"疑似任务执行记录（{label}）：结果数字/阶段收尾属于过程产物，"
                "建议放 progress.md 或 docs；只提炼\"下次还会踩/还会用\"的结论再入库"
            )
            break
    return warns


def new_meta(dtype: str, title: str, scope: str, tags: list, source: str,
             relations: list | None = None, evidence: list | None = None,
             code_refs: list | None = None) -> dict:
    """新建记忆的 frontmatter。confidence/status 由服务端定，调用方不可指定。"""
    today = date.today().isoformat()
    return {
        "id": "",
        "schema": CARD_V2_SCHEMA,
        "type": dtype,
        "title": title.strip(),
        "scope": scope,
        "tags": tags or [],
        "source": source,
        "evidence": evidence or [],
        "code_refs": code_refs or [],
        "confidence": "once",
        "status": "active",
        "last_verified": "",
        "helpful_count": 0,
        "unhelpful_count": 0,
        "created": today,
        "updated": today,
        "relations": relations or [],
    }
