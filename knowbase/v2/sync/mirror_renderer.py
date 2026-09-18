"""P4-C 镜像渲染器：把 V2 文档/版本反射成 Markdown + frontmatter。

设计：
- 纯函数（无 IO），便于单测；
- 与 V1 store.render 对齐（同一份 Markdown 兼容性优先）；
- frontmatter 包含 id/source/path/kind/visibility/org/project/owner/version_no/content_hash，
  body 保留原始正文（Markdown 用 raw，PDF/DOCX 在 P4-C 阶段仍以 plain text 占位）。
"""
from __future__ import annotations

from typing import Any

from ..domain.models import Document, DocumentStatus, DocumentVersion, KnowledgeKind


# Markdown frontmatter 字段顺序：与 V1 store.render 保持视觉一致
_FRONTMATTER_FIELDS = (
    "id", "type", "source", "path", "title",
    "visibility", "org_id", "project_id", "owner_id",
    "version_no", "content_hash", "status", "tags",
)


def _yaml_value(v: Any) -> str:
    """最小 YAML 标量序列化：None/str/int/float/bool/list[str]."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        # YAML 标量：含特殊字符时加双引号
        if any(c in v for c in (":", "#", "\n", '"', "'")) or v.strip() != v:
            escaped = v.replace("\\", "\\\\").replace('"', '\\"')
            return f'"{escaped}"'
        return v
    if isinstance(v, list):
        return "[" + ", ".join(_yaml_value(x) for x in v) + "]"
    # dict 退化为 JSON 字符串
    import json
    return _yaml_value(json.dumps(v, ensure_ascii=False))


def render_frontmatter(d: dict) -> str:
    """渲染 YAML frontmatter 块（不含 --- 包围）。"""
    lines = []
    for k in _FRONTMATTER_FIELDS:
        if k in d:
            lines.append(f"{k}: {_yaml_value(d[k])}")
    # 额外字段追加（不重复上面的键）
    extra = {k: v for k, v in d.items() if k not in _FRONTMATTER_FIELDS}
    for k, v in extra.items():
        lines.append(f"{k}: {_yaml_value(v)}")
    return "\n".join(lines)


def render_doc_markdown(doc: Document, version: DocumentVersion) -> str:
    """把 (Document, DocumentVersion) 渲染成完整 Markdown（frontmatter + body）。

    - tombstone 文档走 render_tombstone_markdown；
    - kind 缺失时退化为普通文档；
    - body 末尾保留单换行，避免 git diff 噪声。
    """
    if doc.status == DocumentStatus.TOMBSTONED:
        return render_tombstone_markdown(doc, version)
    fm = {
        "id": doc.id,
        "type": (doc.kind.value if isinstance(doc.kind, KnowledgeKind) else "reference"),
        "source": doc.source_id,
        "path": doc.path,
        "title": doc.title or doc.path,
        "visibility": doc.visibility,
        "org_id": doc.org_id,
        "project_id": doc.project_id,
        "owner_id": doc.owner_id,
        "version_no": version.version_no,
        "content_hash": version.content_hash,
        "status": doc.status.value,
        "tags": list((doc.meta or {}).get("tags") or []),
    }
    fm_text = render_frontmatter(fm)
    body = (version.body or "").rstrip("\n")
    return f"---\n{fm_text}\n---\n\n{body}\n"


def render_tombstone_markdown(doc: Document, version: DocumentVersion | None = None) -> str:
    """渲染删除标记：保留 frontmatter 但 body 写 TOMBSTONE 说明。

    根据 V2 计划 §8.2「删除写 tombstone 并传播，不以『本地文件消失』直接物理删除」：
    - 保留文档 ID、content_hash 历史；
    - status: tombstoned；
    - body 写人可读的删除原因（如有）。
    """
    fm = {
        "id": doc.id,
        "type": (doc.kind.value if isinstance(doc.kind, KnowledgeKind) else "reference"),
        "source": doc.source_id,
        "path": doc.path,
        "title": doc.title or doc.path,
        "visibility": doc.visibility,
        "org_id": doc.org_id,
        "project_id": doc.project_id,
        "owner_id": doc.owner_id,
        "status": DocumentStatus.TOMBSTONED.value,
        "tags": list((doc.meta or {}).get("tags") or []),
    }
    fm_text = render_frontmatter(fm)
    reason = ""
    if version is not None:
        reason = (version.meta or {}).get("tombstone_reason", "")
    body = f"# TOMBSTONE\n\n本文档已删除。"
    if reason:
        body += f"\n\n原因：{reason}\n"
    else:
        body += "\n"
    if version is not None:
        body += f"\n---\n\n(content_hash: `{version.content_hash}`)\n"
    return f"---\n{fm_text}\n---\n\n{body}"


# 公共导出
__all__ = [
    "render_frontmatter",
    "render_doc_markdown",
    "render_tombstone_markdown",
]
