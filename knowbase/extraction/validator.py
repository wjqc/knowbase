"""候选校验：结构、引用、scope 检查。

MCP 只做确定性校验；语义正确性依赖宿主判断及人工抽检。
"""

import re
from dataclasses import dataclass, field

from .. import store

# 候选必填字段
REQUIRED_FIELDS = ("conclusion", "problem", "scope")
# 八项结构对应键（宿主提交用英文键名，MCP 映射到中文小节）
CANDIDATE_SECTION_MAP = {
    "conclusion": "结论",
    "problem": "解决的问题",
    "applicable_when": "适用条件",
    "not_applicable_when": "不适用条件",
    "actionable_steps": "可执行动作",
    "key_evidence": "关键证据",
    "verification": "验证情况",
    "unknown_items": "未知与待确认",
}


@dataclass
class CandidateErrors:
    """校验错误集合。"""
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def validate_candidate(candidate: dict, *, scope: str = "",
                       repo_path=None) -> CandidateErrors:
    """校验宿主提交的候选经验。

    candidate 应包含：
    - body: dict，至少含 conclusion/problem/scope
    - evidence: list，证据引用
    - type: pitfall/decision/workflow 等
    - tags: list
    - scope: 项目范围
    - suggested_type: 建议类型
    """
    result = CandidateErrors()
    body = candidate.get("body") or {}

    # 1. 必填字段
    if not isinstance(body, dict):
        result.errors.append("body 必须是对象")
        return result

    for field_name in REQUIRED_FIELDS:
        if not body.get(field_name):
            result.errors.append(f"body.{field_name} 必填")

    # 2. scope 校验
    cand_scope = candidate.get("scope") or body.get("scope") or scope
    if not cand_scope:
        result.errors.append("scope 必填")

    # 3. type 校验
    dtype = candidate.get("type") or candidate.get("suggested_type") or "pitfall"
    if dtype not in store.TYPES:
        result.errors.append(f"type 非法: {dtype}（须为 {store.TYPES}）")

    # 4. 治理权限：模型不可指定 verified
    confidence = candidate.get("confidence")
    if confidence and confidence != "once":
        result.errors.append("候选不可指定 confidence，由服务端状态机决定")

    # 5. 证据引用校验
    evidence = candidate.get("evidence") or []
    if not isinstance(evidence, list):
        result.errors.append("evidence 必须是列表")
    elif not evidence:
        result.warnings.append("无证据引用，建议补充关键证据")

    # 6. 凭据检测
    full_text = " ".join(str(v) for v in body.values() if isinstance(v, str))
    if store.SECRET_RE.search(full_text):
        result.errors.append("body 疑似包含明文密钥，请脱敏")

    # 7. code_refs 校验
    code_refs = candidate.get("code_refs") or []
    if code_refs:
        meta_stub = {"code_refs": code_refs}
        errs = store.code_refs_errors(meta_stub)
        result.errors.extend(errs)

    # 8. 引用边界
    if repo_path is not None:
        meta_stub = {"code_refs": code_refs}
        ref_errs = store.reference_errors(meta_stub, full_text, repo_path)
        result.errors.extend(ref_errs)

    # 9. source_refs 定位校验（M4-6）
    source_refs = candidate.get("source_refs") or []
    if source_refs:
        _validate_source_refs(source_refs, result, repo_path)

    return result


def _validate_source_refs(source_refs: list, result: CandidateErrors,
                          repo_path=None) -> None:
    """M4-6: 校验 source_refs 定位信息。

    source_refs 格式：
    [
        {
            "source_id": "SRC-xxx",      # 必填：源 artifact ID
            "segment_id": "seg-0001",    # 可选：片段 ID
            "text_hash": "abc123",       # 可选：文本哈希（用于校验一致性）
            "locator": {                 # 可选：定位信息
                "type": "heading_path",  # heading_path | page | paragraph | line | symbol
                "value": "## 第二章/### 2.1",
                "start_line": 10,
                "end_line": 20
            }
        }
    ]
    """
    if not isinstance(source_refs, list):
        result.errors.append("source_refs 必须是列表")
        return

    for i, ref in enumerate(source_refs):
        if not isinstance(ref, dict):
            result.errors.append(f"source_refs[{i}] 必须是对象")
            continue

        # source_id 必填
        source_id = ref.get("source_id")
        if not source_id:
            result.errors.append(f"source_refs[{i}].source_id 必填")
            continue

        # source_id 格式校验
        if not re.match(r"^SRC-\d{4}-\d{4}$|^SRC-[0-9a-f]{12}$", source_id):
            result.warnings.append(f"source_refs[{i}].source_id 格式异常: {source_id}")

        # 如果提供了 repo_path，校验 source_id 是否存在
        if repo_path is not None:
            from .. import sources
            src_meta = sources.load_manifest(repo_path, source_id)
            if not src_meta:
                result.warnings.append(f"source_refs[{i}].source_id 不存在: {source_id}")

        # segment_id 格式校验（如果提供）
        segment_id = ref.get("segment_id")
        if segment_id and not re.match(r"^seg-\d{4}(-\d{2})?$", segment_id):
            result.warnings.append(f"source_refs[{i}].segment_id 格式异常: {segment_id}")

        # text_hash 格式校验（如果提供）
        text_hash = ref.get("text_hash")
        if text_hash and not re.match(r"^[0-9a-f]{16}$", text_hash):
            result.warnings.append(f"source_refs[{i}].text_hash 格式异常: {text_hash}")

        # locator 格式校验（如果提供）
        locator = ref.get("locator")
        if locator:
            if not isinstance(locator, dict):
                result.errors.append(f"source_refs[{i}].locator 必须是对象")
            else:
                loc_type = locator.get("type")
                valid_types = {"heading_path", "page", "paragraph", "line", "symbol"}
                if loc_type and loc_type not in valid_types:
                    result.warnings.append(
                        f"source_refs[{i}].locator.type 未知: {loc_type}（建议: {valid_types}）"
                    )


def build_card_body(candidate: dict) -> str:
    """将宿主提交的候选转为八项结构 Markdown 正文。"""
    body = candidate.get("body") or {}
    sections = []
    for eng_key, cn_name in CANDIDATE_SECTION_MAP.items():
        content = body.get(eng_key, "")
        if isinstance(content, list):
            content = "\n".join(f"- {item}" for item in content)
        sections.append(f"## {cn_name}\n\n{content}")
    return "\n\n".join(sections)
