"""把 Phase 3 ACL 收尾产生的 5 条经验保存到 ~/knowbase(隔离环境变量,不污染 V2 测试 tmp)。

- 踩坑(P-2026-0009):Principal 重复传参 + personal owner 比较失败
- 决策(D-2026-0003):platform_admin 显式规则(personal deny / 其他自动 allow)
- 决策(D-2026-0004):ACL pre-filter 必须在 __init__ 立即生效(channel 不可绕过)
- 流程(W-2026-0004):Document 对象级授权 4 个入口 + fail-closed 5 场景
- 流程(W-2026-0005):Phase 3 ACL 验证(V2 域 208/208,G3 通过)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# 关键:隔离 KNOWBASE_REPO_PATH / KNOWBASE_CONFIG,让 server.save_impl 走默认 ~/knowbase
for k in ("KNOWBASE_REPO_PATH", "KNOWBASE_CONFIG", "KNOWBASE_V2_INGESTION",
          "KNOWBASE_V2_IDEMPOTENT_INGEST", "KNOWBASE_V2_PARSER_MARKDOWN",
          "KNOWBASE_V2_PARSER_TXT", "KNOWBASE_V2_PARSER_PDF", "KNOWBASE_V2_PARSER_DOCX"):
    os.environ.pop(k, None)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from knowbase import __main__ as cli  # noqa: E402
from knowbase import config, server  # noqa: E402

# 确保真实仓库 init 完毕(幂等);lock 没权限时跳过(save_impl 不依赖 init)
print(f"repo_path = {config.repo_path()}")
try:
    cli.main(["init"])
except PermissionError as e:
    print(f"[skip init] {e}")

P1 = (
    "pitfall",
    "Principal 重复传参 + personal owner 用稳定 id(Phase 3 ACL 测试 fixture 踩坑)",
    """## 现象
P3-D / P3-E 写 ACL fixture 时两类报错反复出现:
1. `Principal("alice", principal_id="alice", ...)` → `multiple values for argument principal_id`
2. `get_document_for_principal(repo, alice, doc_id)` → 返回 None,但 alice 应该是 personal owner

## 原因
1. `Principal.__init__` 第一个位置参数就是 `principal_id`,重复写 keyword 等于双赋值
2. `DomainPrincipal.new()` 默认用 `uuid4().hex` 生成 id,与 ACL `Principal.principal_id="alice"` 字符串对不上,owner_id 比较失败 → fail-closed deny

## 正确做法
2. 写 ACL 测试 fixture 时 `Principal(name, ...)` 只传一次 principal_id(通常用第一个位置参数)
2. fixture 创建 `DomainPrincipal` 时显式给稳定 id:`DomainPrincipal(id="alice", display_name="alice", kind=PrincipalKind.USER)`;文档 `Document.new(owner_id="alice", ...)` 必须用同一个字符串 owner_id
2. 不要用 `DomainPrincipal.new()`,ACL owner 比较会全部 mismatch,personal case 全失败""",
    ["acl", "fixture", "personal", "fail-closed"],
)


P2 = (
    "decision",
    "platform_admin 必须有显式规则:personal 默认 deny,其他按'组织级运维'语义自动 allow",
    """## 背景
V2 计划 §7.1 提到 `platform_admin` 是"组织级运维,但默认不自动获得敏感正文访问权"。Phase 3 实现 ACL resolve() 时,如果不写 platform_admin 规则会怎样?

## 决策
`v2/acl/policy.py` resolve() 必须显式判定 platform_admin:
- visibility == personal → **deny**(默认不读 personal)
- visibility in {public_template, project_shared, organization_global} → **allow**(组织级运维)
- visibility NULL / 未知值 → fail-closed deny
- 如果省略 platform_admin 显式规则,会回落到普通 role 检查,看不到 org_global(没有 org role)→ 体验差且违反"运维"职责

## 取舍
- 不给 platform_admin "全部可见"的万能权限(违反 §7.1 红线)
- 给 platform_admin 对非 personal 自动 allow(运维级访问权)
- personal 默认 deny,需要显式授权才看(防止误读用户私有经验)

## 影响
- 项目管理员(p_a)看到 project_shared 才能读 d-proj-a1,平台管理员(super)自动能读(组织级运维语义)
- 普通用户 alice 不属于 org_a 也能借 super 身份读 org_global / project_shared(运维场景)
- 但 super 不能借机读 personal(用户隐私保护)""",
    ["acl", "platform-admin", "rbac", "fail-closed"],
)


P3 = (
    "decision",
    "ACL pre-filter 必须在 HybridSearch __init__ 立即生效,channel 不可绕过",
    """## 背景
V2 计划 §7.2 强调"ACL 必须在召回前硬过滤,channel 拿不到无权 doc"。原 HybridSearch 构造函数接收 `docs: list[dict]`,channel 默认 `ExactChannel / BM25Channel / DenseChannel / MetadataChannel` 用 docs 构造;如果 ACL 只在 search() 阶段过滤,channel 内部仍能看到无权 doc。

## 决策
`v2/retrieval/hybrid_search.py` HybridSearch.__init__ 立即按 ACL 过滤:
1. 接收 `principal` 或 `acl_predicate` 两个等价入口(acl_predicate 优先)
2. `__init__` 立刻 `allowed = [d for d in docs if predicate(d)]`,denied_ids 记录被过滤的 doc_id
3. **默认 channels 用 allowed docs 构造**(关键),无权 doc 物理上不存在于 channel 索引
4. 自定义 channels 由调用方负责用 allowed docs 构造(fail-closed 责任转移)
5. principal=None 时完全跳过 ACL,行为 100% 向后兼容(V1/P2 不破)

## 取舍
- 不在 search() 时过滤(慢 + 通道内部仍可见)
- 不让 channel 接收全 docs + principal(逻辑分散,易漏一个 channel)
- 在 __init__ 一次性过滤 + 默认 channel 自动用 allowed 构造(集中、零回归)

## HybridResult 暴露字段
- `principal_id: str | None`
- `acl_enabled: bool`(是否启用 ACL)
- `acl_denied: int`(被过滤的 doc 数)
- `acl_denied_ids: list[str]`(被过滤的 doc_id 列表,便于零泄露率审计)""",
    ["acl", "hybrid-search", "channel", "fail-closed"],
)


P4 = (
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


P5 = (
    "workflow",
    "Phase 3 ACL 验证:V2 域 208/208 通过,G3 开放多人员(Phase 3 收尾实测)",
    """## 步骤
1. `tests/v2/test_acl.py` — 26/26(ACL engine 单测,4 visibility × 5 role + platform_admin + fail-closed)
2. `tests/v2/test_hybrid_search_acl.py` — 20/20(主路召回前 ACL filter,principal=None 零回归)
3. `tests/v2/test_document_read_acl.py` — 23/23(对象级授权 4 入口 + fail-closed 5 场景)
4. `tests/v2/test_acl_enforcement_e2e.py` — 21/21(5 类越权 E2E,3 入口零泄露率)
5. 旧 V2 单测 118/118 零回归(schema v1→v2 平滑迁移)

合计 **V2 域 208/208 通过**。

## 关键决策
- ACL pre-filter 在 __init__ 生效(channel 不可绕过)
- platform_admin personal deny + 其他自动 allow
- Document 对象级授权 4 入口(防 ID 直读绕过)
- HybridResult 暴露 acl_denied_ids 便于审计

## 产出
- Gate G3 通过,可开放多人员
- 进入 Phase 4(同步与唯一写入者,§8)
- 复用 ACL:Phase 4 的 outbox / sync_worker / git_mirror 都按 principal 过滤审计 + 操作可见

## 度量
- ACL 拦截延迟:< 1ms(纯 Python list comprehension,无 DB IO)
- V1/P2 zero regression:208 测试 = 旧 118 + P3 新 90,旧测试全部 0 改动通过""",
    ["v2-upgrade", "acl", "evaluation"],
)


for typ, title, body, tags in (P1, P2, P3, P4, P5):
    src = "human:文剑"
    out = server.save_impl(typ, title, body, tags=tags, source=src)
    print("=" * 60)
    print(f"[{typ}] {title}")
    print(out[:500])
    print("…\n")