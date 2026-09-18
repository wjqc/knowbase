"""单独补存 P3 workflow 'Document 对象级授权'(之前 lint 拒了,补 '步骤' 小节)。"""
from __future__ import annotations
import os
import sys
from pathlib import Path

for k in ("KNOWBASE_REPO_PATH", "KNOWBASE_CONFIG", "KNOWBASE_V2_INGESTION",
          "KNOWBASE_V2_IDEMPOTENT_INGEST", "KNOWBASE_V2_PARSER_MARKDOWN",
          "KNOWBASE_V2_PARSER_TXT", "KNOWBASE_V2_PARSER_PDF", "KNOWBASE_V2_PARSER_DOCX"):
    os.environ.pop(k, None)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from knowbase import __main__ as cli, config, server  # noqa: E402

print(f"repo_path = {config.repo_path()}")
try:
    cli.main(["init"])
except PermissionError as e:
    print(f"[skip init] {e}")

ENTRY = (
    "workflow",
    "Document 对象级授权 4 个入口 + fail-closed 5 场景(Phase 3 ACL 收尾)",
    """## 步骤
### 1. 4 个公共 API(`v2/acl/guard.py`)
1. `check_doc_access(principal, doc)` → `Policy(allow, reason, visibility)`,单点判定
2. `get_document_for_principal(repo, principal, doc_id)` → `Document | None`,防止拿 ID 直读
3. `list_documents_for_principal(repo, principal, *, source_id=None)` → `list[Document]`,列表过滤
4. `filter_doc_ids_by_principal(repo, principal, doc_ids)` → `set[str]`,批量 ID 过滤

### 2. fail-closed 必须覆盖的 5 场景
1. `visibility=NULL`(DB 字段 NULL)
2. `visibility="unknown_value"`(枚举外的脏数据)
3. org_global doc 缺 `org_id`(迁移遗留)
4. project_shared doc 缺 `project_id`(迁移遗留)
5. personal doc 缺 `owner_id`(迁移遗留)

5 类全部必须返回 `Policy(allow=False, ...)`,**不能默认 allow**。

### 3. ACL 越权 5 类测试
1. 跨组织:org_a user 读 org_b doc → deny(3 入口:HybridSearch / get / list)
2. 跨项目:proj-a member 读 proj-b doc → deny
3. 直接 doc_id:无关 user 拿 ID 直读 → deny(防 enumeration 攻击)
4. 缓存污染:每次 HybridSearch 重新构造必须重新过滤(不能缓存 self.docs 复用)
5. staging 绕过:doc status=STORED 不能跳过 ACL 判定(visibility 才是关键)

### 4. 零泄露率断言
- 对 4 visibility × 3 入口 × 多个 principal = 12 种组合,只有 owner / 授权角色才能看见
- platform_admin 单独校验:对 personal deny,对其他 allow""",
    ["acl", "document-read", "fail-closed", "test"],
)

typ, title, body, tags = ENTRY
src = "human:文剑"
out = server.save_impl(typ, title, body, tags=tags, source=src)
print("=" * 60)
print(f"[{typ}] {title}")
print(out[:600])