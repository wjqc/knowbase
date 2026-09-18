# knowbase V2 完整升级方案

> 版本：v1.0（实施方案）  
> 日期：2026-09-18  
> 适用范围：knowbase 从本地 Markdown 经验库升级为支持多人员、多项目、多格式文档的可治理知识检索平台  
> 基线：当前仓库 `~/work/knowbase` 0.1.0；当前运行库 `~/knowbase`

## 1. 结论与实施原则

本次升级不应只做“给 FTS5 再加一个向量库”。完整目标是同时解决五个问题：

1. **多格式可摄取**：Markdown、PDF、Word、Excel、PowerPoint、HTML、代码、日志和图片 OCR 能进入统一文档模型。
2. **多路可召回**：精确标识、BM25、向量语义和业务元数据四路召回，融合后重排。
3. **多人多项目可隔离**：组织、项目、成员、角色和 ACL 在召回前硬过滤，scope 不再兼任权限。
4. **更新可同步**：远程变化能自动拉取并增量重建；本地写入具备 outbox、幂等、冲突和可观测状态。
5. **知识可治理**：业务规则、标准、经验和参考资料保持不同权威级别、审批链、有效期和版本语义。

实施遵循以下原则：

- 保留现有 6 个 MCP 工具的兼容入口，已有 Agent 无须一次性切换。
- Markdown + Git 继续作为经验条目的可读审计镜像，但不再让 N 个客户端直接并发写共享主分支。
- 原始文件不可变保存；解析结果、chunk、embedding 和全文索引都可由原始文件重建。
- ACL、项目、状态、有效期必须在召回阶段生效，不能在答案生成后补过滤。
- 业务规则没有来源、版本和审批记录时，只能进入 staging，不能自动注入 Agent。
- 所有写操作使用 `operation_id` 幂等；未知结果按 operation_id 查询，不盲目重试。
- 静态检查、单元测试和离线评测不等于真实 OCR、权限、同步或生产效果证明。

## 2. 当前基线和升级判定

### 2.1 已有能力

- Markdown + frontmatter 为经验条目真相，Git 提供审计。
- SQLite FTS5 trigram 检索标题、标签和正文，短词使用 `instr/LIKE` 回退。
- scope、可信度、新鲜度和反馈参与排序。
- pitfall、standard、decision、workflow、preference、reference、bizrule 七类知识具备基础治理。
- repo 级跨进程锁、SQLite WAL、staging、人审、反馈晋升和关系语义已经存在。
- MCP `memory_search/read/save/update/feedback/stats` 已形成兼容契约。

### 2.2 已确认缺口

- 没有 embedding、向量索引、查询改写、融合召回和 reranker。
- 导入只支持 Markdown，整篇文档一条记录，不分块、不增量更新。
- scope 是相关性字段，不是组织/项目 ACL。
- Hook 的 OR 兜底遍历 active 记录并做子串计数，规模增大后延迟线性上升。
- 检索前没有 fetch/pull；写后 push 失败没有持久化重试队列。
- 多人直接推共享 Git 主分支会遇到 non-fast-forward 和同文件冲突。
- 真实库业务规则为 0 条，开发经验和项目资料覆盖也集中于 CMI。
- 当前评测明确关键词 8/8，但同义口语改写 0/4。

### 2.3 V2 成功标准

V2 只有同时满足以下 Gate 才能宣称完成：

| Gate | 验收目标 |
|---|---|
| G1 摄取 | 支持约定格式；相同内容幂等；修改、删除、重命名均能增量同步 |
| G2 检索 | 各项目 golden set 的 Recall@5、MRR 达标；同义改写明显优于 V1 且精确检索不回退 |
| G3 隔离 | ACL 越权测试 100% 拦截；检索日志可证明过滤在召回前发生 |
| G4 同步 | 远程更新在 SLA 内可见；断网恢复、重复事件、冲突和未知提交均可恢复 |
| G5 治理 | bizrule/standard 未审批不得进入自动注入；旧版本和失效规则不得默认召回 |
| G6 运维 | 可观察摄取积压、索引延迟、同步失败、零命中、低质量命中和权限拒绝 |
| G7 迁移 | V1 32 条数据、ID、关系、反馈和审计可追溯；可在回滚期切回 V1 只读检索 |

## 3. 目标架构

### 3.1 两种部署剖面

#### Profile A：Local/Lite

适合个人或少量用户、单机离线环境：

- 元数据和 FTS：SQLite FTS5
- 向量：sqlite-vec 或本地 Qdrant
- embedding/reranker：本地 ONNX 模型
- 原始文件：本地文件系统
- 同步：Git remote + 本地 sync worker
- 权限：本机身份 + 项目 ACL 快照

#### Profile B：Team/Scale（目标生产形态）

适合 N 人、N 项目：

- API/MCP Gateway：统一认证、授权、限流和审计
- Metadata DB：PostgreSQL
- 全文检索：OpenSearch/Elasticsearch；初期也可 PostgreSQL FTS
- 向量检索：pgvector 或 Qdrant
- 原始文件：MinIO/S3，按 content hash 不可变存储
- 后台任务：持久任务表 + worker；规模上来后可接 Kafka/RabbitMQ
- Git Mirror Writer：唯一 Git 写入者，把审批后的经验条目镜像为 Markdown commit
- Sync/Connector Workers：Git、文件目录、对象存储和后续第三方数据源连接器

两种 Profile 使用相同领域模型、MCP 契约、chunk 规则和评测集，避免形成两套产品。

### 3.2 组件关系

```text
Agent / CLI / Dashboard
          │
          ▼
API + MCP Gateway ── Authentication ── Authorization/ACL
          │
   ┌──────┼──────────┐
   ▼      ▼          ▼
Ingestion Retrieval  Governance
   │      │          │
   ▼      ▼          ▼
Parser   Hybrid      Review/Version/
OCR      Search      Feedback/Conflict
   │      │          │
   ├──────┼──────────┤
   ▼      ▼          ▼
Metadata DB / FTS / Vector / Object Storage
          │
          ├── Outbox + Workers
          └── Git Audit Mirror（唯一写入者）
```

### 3.3 权威边界

| 数据 | 权威源 | 可重建 |
|---|---|---|
| 原始文档字节 | Object Storage / Local source | 否，必须备份 |
| 文档元数据、ACL、版本、审批、操作状态 | Metadata DB | 否，必须备份 |
| 经验 Markdown 镜像 | Git | 可从审批后的结构化记录生成 |
| chunk 与解析结果 | Metadata DB/Object Storage | 可由原始文件重建 |
| FTS 索引 | SQLite/OpenSearch | 是 |
| embedding/向量索引 | sqlite-vec/pgvector/Qdrant | 是 |
| usage/feedback/audit | Metadata DB | 否，必须备份 |

## 4. 统一数据模型

### 4.1 核心实体

#### Organization / Project / Principal

- `organization(id, name, status)`
- `project(id, organization_id, key, name, status)`
- `principal(id, type[user|service|agent], external_subject, status)`
- `project_membership(project_id, principal_id, role)`
- `acl_entry(resource_type, resource_id, principal_or_role, permission)`

#### Source / Document / Version

- `knowledge_source`
  - `id, organization_id, project_id`
  - `connector_type`: git/filesystem/upload/minio/other
  - `source_uri, sync_mode, sync_cursor, status`
  - `parser_policy_id, acl_policy_id`
- `document`
  - `id, source_id, canonical_path, title, mime_type`
  - `knowledge_type, language, sensitivity`
  - `current_version_id, lifecycle_status`
- `document_version`
  - `id, document_id, version_no, source_revision`
  - `content_hash, object_key, size, modified_at`
  - `parse_status, index_status, created_at`
- `document_chunk`
  - `id, document_version_id, ordinal`
  - `section_path, page_no, sheet_name, slide_no, code_symbol`
  - `text, token_count, content_hash`
  - `embedding_model, embedding_version`

#### Experience / Rule Governance

- `knowledge_record`
  - 保留 V1 ID，例如 `P-2026-0001`、`B-2026-0001`
  - `type, title, scope/project_id, status, confidence`
  - `effective_from, effective_to, provenance`
  - `created_by, approved_by, current_revision`
- `knowledge_relation`
  - `related, supersedes, contradicts, derived_from`
- `review_request`
  - staging、审批人、审批意见、前后 revision
- `feedback_event`
  - principal、outcome、context、任务/会话 ID

#### Operation / Sync

- `operation`
  - `operation_id` 唯一
  - `operation_type, target_id, requested_by`
  - `status[pending|running|succeeded|failed|unknown]`
  - `request_hash, result, error, created_at, updated_at`
- `outbox_event`
  - `event_id, aggregate_type, aggregate_id, event_type, payload`
  - `status, attempts, next_attempt_at, lease_owner, lease_until`
- `sync_run`
  - `source_id, cursor_before, cursor_after, status`
  - `discovered, created, updated, deleted, failed`

### 4.2 必须建立的约束和索引

- `UNIQUE(source_id, canonical_path)`
- `UNIQUE(document_id, version_no)`
- `UNIQUE(document_id, content_hash)` 防重复版本
- `UNIQUE(operation_id)` 保证写幂等
- `UNIQUE(document_version_id, ordinal)`
- project/org/ACL 字段必须有组合索引
- chunk FTS 和向量载荷必须包含 `organization_id/project_id/status/version`
- current version 通过事务或 CAS 切换，不允许先删旧索引再建新索引
- 删除使用 tombstone；完成新索引切换后才异步回收旧对象和向量

## 5. 多格式摄取方案

### 5.1 支持矩阵

| 格式 | 解析内容 | 特殊处理 |
|---|---|---|
| Markdown/TXT | 标题、段落、代码块、链接 | 保留 heading path |
| PDF | 页面文字、目录、表格、图片 | 文本优先；扫描页进入 OCR |
| DOCX | 标题层级、段落、表格、批注 | 保留章节和表格结构 |
| XLSX/CSV | sheet、表头、行区间、公式/显示值 | 按表和逻辑行块切分，禁止整表一块 |
| PPTX | 标题、正文、备注、图表替代文本 | 按 slide 切分并保留页码 |
| HTML | 主体、标题、表格、链接 | 去导航/脚本/样式，保留来源 URL |
| 图片 | OCR 文本、版面区域 | 记录 OCR 引擎、置信度和坐标 |
| 代码 | 文件、类、函数、符号 | 优先语法树/符号级切分 |
| 日志 | 时间段、trace/run/error group | 脱敏、按事件簇切分 |

解析器采用适配器接口，而不是把格式判断写入 import 命令：

```python
class DocumentParser(Protocol):
    def supports(self, mime_type: str, path: str) -> bool: ...
    def parse(self, blob: BinaryIO, context: ParseContext) -> ParsedDocument: ...
```

### 5.2 摄取状态机

```text
DISCOVERED
  → FETCHED
  → STORED
  → PARSING
  → PARSED
  → CHUNKED
  → EMBEDDING
  → INDEXING
  → READY

任一步失败 → RETRYABLE_FAILED / PERMANENT_FAILED
源删除 → TOMBSTONED → 索引切换后 PURGED
```

每一步必须记录输入 hash、处理器版本和错误，不允许只有一个模糊的 `failed`。

### 5.3 分块策略

- 标题层级优先，不按固定字符粗暴切割。
- 普通文本目标 300～700 tokens，重叠 10%～15%。
- 表格以“表头 + 连续行区间”为块，每块重复表头。
- PDF 每块保留页码和 section；禁止跨不相关章节合并。
- PPT 每页至少一个块，备注可单独作为子块。
- 代码按类/函数/方法切分，超长函数再按语法节点拆分。
- 业务规则保持原子性：一条规则一个逻辑块，并携带 provenance、有效期和版本。
- 文档级摘要、章节摘要可作为额外检索单元，但不能替代原始 chunk。

### 5.4 增量和幂等

1. 连接器根据 remote commit、ETag、mtime+size 或服务端 cursor 发现变化。
2. 读取内容并计算 SHA-256；相同 hash 不重解析、不重 embedding。
3. 创建新 `document_version`，旧版本仍保持可查询但不作为默认 current。
4. 新版本完整解析、向量化、索引成功后，以 CAS 切换 `current_version_id`。
5. 发出 `document.version.activated` 事件，再异步清理旧索引。
6. 任一步失败保持旧版本在线，避免更新期间知识不可用。

## 6. 混合检索方案

### 6.1 查询处理

请求必须携带或由服务端解析：

- `organization_id`
- `principal_id`
- `project_ids`（显式或按当前工作目录映射）
- `query`
- 可选 `knowledge_types/document_types/tags/time_range`
- `include_global`
- `include_inactive`（仅治理角色可用）

查询处理依次执行：

1. 身份解析和项目权限求交集。
2. 识别错误码、ID、API、表名、文件名、PRD 编号等精确实体。
3. 中文/英文规范化、拼写和缩写扩展；保留原查询。
4. 可选生成 1～2 个受控查询改写；原查询必须始终参与召回。
5. 构造统一 metadata filter。

### 6.2 四路召回

| 通道 | 用途 | 建议候选数 |
|---|---|---:|
| Exact | ID、错误码、标题、路径、业务规则编号 | 20 |
| BM25/FTS | 专有名词、原文、短语 | 50 |
| Dense Vector | 同义改写、自然语言、跨表述 | 50 |
| Metadata/Relation | 同项目、同实体、supersedes/derived_from | 20 |

所有通道在查询时带相同的 ACL、project、active、current-version filter。

### 6.3 融合与重排

第一阶段使用 Reciprocal Rank Fusion：

```text
RRF(d) = Σ 1 / (k + rank_channel(d))，建议 k=60
```

随后加入治理权重，但不能让权重把完全无关内容推上来：

```text
governance_factor = confidence × freshness × feedback × source_authority
final_candidate_score = RRF × governance_factor
```

对融合后的 Top 30 使用 cross-encoder reranker，输入“query + chunk + title + section”。最终返回 Top 5～10，并做：

- 同文档相邻 chunk 合并；
- 同一规则旧版本去重；
- superseded/stale 降级或排除；
- contradicts 同时展示冲突提示，不静默任选一条；
- 引用必须回到 document/version/page/section。

### 6.4 无答案与降级

- 低于阈值时返回“未找到足够证据”，不能用低相关 chunk 填满 Top K。
- embedding/reranker 不可用时，降级为 Exact + BM25，但响应必须标注 degraded mode。
- 向量索引版本切换采用双索引：新模型 shadow build，通过评测后切 alias。
- 模型、chunker、parser 版本都写入索引元数据，支持可复现重建。

### 6.5 缓存

- 权限结果短 TTL 缓存，membership/ACL 变化主动失效。
- 查询缓存 key 必须包含 principal、project set、filter、索引 revision，禁止跨权限复用。
- embedding 查询缓存可以共享规范化 query，但不缓存最终 ACL 结果。
- 热门 query 和零命中分别统计，避免缓存掩盖质量问题。

## 7. 多人员、多项目和权限隔离

### 7.1 身份与角色

最低角色集：

- `viewer`：读取已激活知识
- `contributor`：提交经验/文档、查看自己的操作状态
- `reviewer`：审批标准和业务规则
- `project_admin`：管理项目成员、来源和项目策略
- `platform_admin`：组织级运维，但默认不自动获得敏感正文访问权

Agent 身份必须绑定到用户或服务主体，审计同时记录 `principal_id + agent_name + session_id`，不能只依赖可伪造的环境变量。

### 7.2 ACL 执行点

- Gateway 做粗粒度项目授权。
- Retrieval service 生成不可绕过的服务端 filter。
- FTS/Vector 查询都使用该 filter。
- Document read 再做一次对象级授权，防止拿 ID 直接读。
- snippet、统计、搜索历史和 dashboard 同样受 ACL 约束。
- 日志不得记录用户无权看到的正文或完整 prompt。

### 7.3 global 语义

`global` 不等于所有人可见。应拆为：

- organization-global：组织内通用
- project-shared：指定项目集合共享
- public-template：不含内部数据的模板
- personal：仅创建者

## 8. 自动同步与一致性

### 8.1 不再采用客户端直接写共享 main

Team Profile 下由中心服务成为唯一写 owner：

- Agent 调 API/MCP 写数据库，使用 operation_id 幂等。
- 事务内同时写业务数据和 outbox。
- Git Mirror Worker 消费已审批事件，生成/更新 Markdown 后提交并推送。
- 远程 Git 文档源由 Connector Worker 拉取，不与审计镜像混用工作区。

这样避免多个用户对同一 main 分支执行 pull/rebase/push，也避免 Git 成功但索引失败或反向状态不一致。

### 8.2 Local Profile 同步协议

Lite 模式仍可用 Git，但必须补齐完整流程：

#### 检索前

- 后台定时 `fetch`，默认 30～60 秒，不在每次查询同步阻塞网络。
- 查询读取 `last_remote_check_at` 和 `local_revision/remote_revision`。
- 超过 freshness SLA 时触发异步 fetch；严格模式可等待限定时间。
- 检测到远程前进后，在 repo lock 内执行 fast-forward/rebase，并增量 reindex。
- 同步失败时继续提供 last-known-good，并在响应中暴露 `stale_index=true`。

#### 写入后

- 本地事务生成 operation_id，写文件、索引和 outbox。
- commit 成功后 worker 推送；失败按指数退避重试。
- non-fast-forward：fetch → rebase；无内容冲突则继续 push。
- 内容冲突进入 `conflicted`，保留双方版本，禁止自动覆盖。
- 用户可通过 `sync_status` 查看未推送、失败、冲突和最后成功时间。

#### 删除与重命名

- 删除写 tombstone 并传播，不以“本地文件消失”直接物理删除。
- 重命名依靠稳定 document ID，不生成两份知识。
- 远端强推/历史重写默认拒绝自动应用，进入人工处理。

### 8.3 同步恢复语义

- 每次同步有 `sync_run_id`、cursor_before、cursor_after。
- worker 使用 lease + heartbeat + fencing token，避免多个实例重复接管。
- 事件至少一次投递，消费者必须幂等。
- `unknown` 结果通过 operation_id 查询；不能把未知结果当失败再次提交。
- poison document 进入隔离队列，不阻塞同批其他文档。

## 9. 治理和高价值知识入库

### 9.1 知识分层

| 层级 | 内容 | 默认检索行为 |
|---|---|---|
| Authoritative | 已审批业务规则、标准、正式架构/API | 高权重，可自动注入 |
| Validated | 多次验证的经验/流程 | 正常召回，可自动注入 |
| Observed | 单次经验、导入参考资料 | 主动检索可见，自动注入受限 |
| Staging | 待审规则、标准、批量导入草案 | 普通用户不可召回 |
| Stale/Archived | 已失效/归档 | 默认排除，治理检索可见 |

### 9.2 业务规则

业务规则必须有：

- provenance：PRD、条款、会议纪要或业务方确认记录；
- domain/entity/action/condition/outcome；
- effective_from/effective_to；
- applicable_projects/regions/products；
- owner/reviewer；
- supersedes/contradicts 关系；
- 结构化规则正文与人类可读说明。

代码反推只能生成“规则候选”，不能自动成为 authoritative bizrule。

### 9.3 开发经验

保留现有 pitfall/workflow/decision 类型，但新增：

- environment/version/applicability；
- symptoms/root_cause/resolution/verification；
- evidence links；
- `last_verified_at` 和验证环境；
- 可执行检查命令与预期结果分开存储；
- 敏感信息检测和脱敏报告。

## 10. MCP/API 兼容与新增接口

### 10.1 保留接口

- `memory_search`
- `memory_read`
- `memory_save`
- `memory_update`
- `memory_feedback`
- `memory_stats`

旧参数继续有效；服务端从认证上下文取得 principal/org，不允许客户端伪造。

### 10.2 建议新增 MCP 工具

| 工具 | 用途 |
|---|---|
| `source_register` | 注册 Git、目录、对象存储等来源 |
| `source_sync` | 触发一次同步，返回 operation_id |
| `sync_status` | 查询来源、操作、冲突和索引 freshness |
| `document_ingest` | 上传或登记单个文档 |
| `document_status` | 查询解析、OCR、chunk、embedding 状态 |
| `review_list` | reviewer 查看 staging |
| `review_decide` | 审批、驳回或要求修改 |
| `search_explain` | 管理员查看各召回通道、过滤和融合解释 |

### 10.3 `memory_search` V2 返回

每条结果至少增加：

- `document_id/version_id/chunk_id`
- `project_id/knowledge_type`
- `source_uri/page/section`
- `retrieval_channels`
- `score_components`（仅 debug 权限）
- `index_revision/stale_index/degraded_mode`
- `effective_status/provenance`

分数不是正确率，API 文档中必须明确。

## 11. 文件级实施清单

建议在保持现有模块可工作的前提下逐步拆分：

```text
knowbase/
  domain/
    models.py              # Document/Version/Chunk/KnowledgeRecord/Operation
    policies.py            # 生命周期、权威级别、状态机
  auth/
    identity.py
    acl.py
  ingestion/
    service.py
    chunking.py
    registry.py
    parsers/
      markdown.py pdf.py docx.py xlsx.py pptx.py html.py image.py code.py log.py
  retrieval/
    query.py
    exact.py
    lexical.py
    vector.py
    fusion.py
    rerank.py
    service.py
  sync/
    service.py
    connectors/git.py
    outbox.py
    worker.py
  repositories/
    interfaces.py
    sqlite.py
    postgres.py
  api/
    schemas.py
    routes.py
  mcp/
    tools.py
  migrations/
  observability/
    metrics.py audit.py
```

现有文件处理建议：

- `knowbase/index.py`：保留 V1 兼容，逐步把 lexical/统计拆入新模块。
- `knowbase/store.py`：保留 Markdown codec；领域状态迁入 repository/service。
- `knowbase/server.py`：变成兼容适配器，不直接承担事务和 Git 副作用。
- `knowbase/gitops.py`：拆为审计镜像 writer 和 Lite sync connector。
- `knowbase/hooks.py`：改为调用统一 retrieval service，不再全表子串扫描。
- `knowbase/__main__.py`：新增 source/sync/worker/migrate/doctor 命令。
- `knowbase/config.py`：引入 profile、数据库、模型、同步、ACL 和 feature flag 配置。

## 12. 分阶段实施

### Phase 0：基线冻结与契约测试（1 周）

- 固化现有 6 MCP 工具契约和 32 条迁移清单。
- 扩充每项目真实 query golden set，区分 exact、keyword、semantic、no-answer、ACL。
- 记录 V1 延迟、HitRate、零命中和索引时间。
- 增加 feature flags：`v2_ingestion`、`hybrid_search`、`central_sync`、`acl_v2`。

交付：基线报告、契约测试、数据清单、回滚快照。

### Phase 1：统一文档模型与增量摄取（2～3 周）

- 建表/迁移和 repository 抽象。
- 实现 source/document/version/chunk/operation/outbox。
- 先支持 Markdown、TXT、PDF、DOCX；建立 parser registry。
- content hash 幂等、版本切换、tombstone 和任务恢复。
- V1 import 转为新 ingestion service 的兼容调用。

Gate：G1 基础格式通过；旧版检索仍工作。

### Phase 2：混合检索（2～3 周）

- 抽出 lexical service，新增 exact、dense、RRF、rerank。
- 建立 embedding/reranker 模型版本管理和双索引切换。
- Shadow 模式同时跑 V1/V2，只记录差异，不改变用户结果。
- 使用 golden set 调整 Top K、阈值和 chunk 策略。

Gate：semantic 集显著提升，must/exact 不回退，无答案误召回受控。

### Phase 3：ACL 与多人项目模型（2 周）

- 接入组织、项目、成员、角色、ACL。
- 所有 search/read/stats/history/dashboard 加服务端权限过滤。
- 完成跨项目串扰和越权攻击测试。
- scope 变为兼容别名，内部解析为 project_id。

Gate：G3 通过后才能开放多人员。

### Phase 4：同步和唯一写入者（2～3 周）

- Team Profile 上线中心 API、worker、outbox、Git mirror writer。
- Lite Profile 增加后台 fetch/rebase/push、失败队列和冲突状态。
- 提供 `sync_status`、`doctor`、运维告警。
- 演练断网、重复事件、进程崩溃、non-fast-forward 和 unknown outcome。

Gate：G4 通过后才能宣称远程自动同步。

### Phase 5：剩余格式、治理与运营（2～3 周）

- XLSX、PPTX、HTML、图片 OCR、代码和日志解析。
- bizrule/standard 审批 UI/API、有效期和冲突治理。
- 看板加入质量、freshness、权限拒绝和同步积压。
- 建立定期评测、模型/解析器升级流程。

### Phase 6：生产灰度和切换（1～2 周）

- 选 1～2 个项目 shadow → canary → 扩大。
- 双读比对、旧系统只读保留、每日质量报告。
- 满足 Gate 后切换默认 retrieval；保留一键回退窗口。

总工期建议：10～14 周，2～4 人并行；OCR、Office 复杂表格和企业身份接入可能扩大工期。

## 13. 测试与验收

### 13.1 单元测试

- 各格式 parser、chunk 边界和 metadata 保留。
- content hash、版本 CAS、tombstone、幂等 operation。
- ACL policy 和角色矩阵。
- Exact/BM25/vector/RRF/rerank 独立行为。
- 状态机、审批、supersedes/contradicts。

### 13.2 集成测试

- 上传 → 解析 → chunk → embedding → 索引 → 检索 → 引用。
- Git 新增/修改/删除/重命名的增量同步。
- 断网、重试、worker 崩溃和重复消费。
- embedding 服务失败后的词法降级。
- 旧版本在线直到新版本完整激活。

### 13.3 安全测试

- 跨组织、跨项目、直接 document ID、缓存污染、搜索历史越权。
- staging/stale/archived 绕过。
- prompt/文档中的指令不得改变权限和治理状态。
- zip bomb、超大文件、恶意 Office/PDF、路径穿越、符号链接。
- 密钥、个人信息和内部敏感字段检测。

### 13.4 检索评测

每个项目至少准备：

- 30 条精确/关键词问题；
- 30 条同义/口语问题；
- 10 条跨语言/缩写问题；
- 20 条无答案问题；
- 10 条版本/业务规则问题；
- 10 条应被 ACL 拦截的问题。

初始目标可设为：

- exact/keyword Recall@5 不低于 V1；
- semantic Recall@5 ≥ 85%；
- MRR ≥ 0.75；
- 无答案误返回率 ≤ 5%；
- 跨项目误召回率 ≤ 1%；
- ACL 泄露率 = 0；
- P95 检索延迟：Lite ≤ 500 ms，Team 不含生成 ≤ 800 ms。

这些阈值应在 Phase 0 用真实语料校准，不能直接视为已承诺生产 SLA。

## 14. 迁移方案

### 14.1 V1 数据迁移

1. 备份 `/Users/qc/knowbase` Git 仓库和 `memory.db` 一致性快照。
2. 导出 32 条记录及 ID、关系、confidence、status、feedback、usage。
3. Markdown 每条映射为一个 `knowledge_record + document + version`。
4. `reference` 原文按结构重新分块；其他经验类型默认保持原子记录。
5. staging 两条保持 staging，不能迁移时自动激活。
6. 对 migrated ID、hash、条数、关系做逐条 parity。
7. 建 V2 索引并 shadow 查询，旧系统继续提供正式结果。

### 14.2 冷知识库迁移

- 按项目分别注册 source，不允许把整个 `~/knowledge` 归为 global。
- 第一次只建清单、hash 和解析预览，不直接激活自动注入。
- 业务规则候选进入 staging 并补 provenance。
- 对混合目录建立路径到 project_id 的显式映射表。
- 大文件、重复文档和历史版本先出迁移报告再处理。

## 15. 部署与运维

### 15.1 环境

- dev：小样本、可删除索引。
- staging：脱敏生产规模语料、完整身份/ACL 和同步演练。
- production：独立数据库、对象存储、索引和密钥。

### 15.2 必须监控

- ingestion queue depth、各状态耗时、失败类型、poison document 数量；
- source freshness、remote/local revision、last successful sync；
- FTS/vector/rerank 延迟和错误率；
- zero-hit、low-confidence、degraded-mode 比例；
- ACL denied 和异常跨项目查询；
- outbox backlog、重试次数、lease 超时；
- 索引文档数与 metadata current version 数量 parity；
- embedding/parser/chunker 版本分布。

### 15.3 备份恢复

- PostgreSQL PITR；对象存储版本化；Git mirror；向量/FTS 可重建。
- 定期执行从“元数据 + 原始文件”全量重建到空索引的演练。
- usage/feedback/audit 不能只依赖索引备份。
- 明确 RPO/RTO，并在 staging 实测恢复时间。

## 16. 灰度、切换与回滚

### 16.1 灰度

1. V2 只摄取，不服务查询。
2. V1 正式返回，V2 shadow 查询并记录差异。
3. 内部 reviewer 查看 V2 结果。
4. 单项目 5% canary，再逐步 25%/50%/100%。
5. 每阶段至少覆盖一个完整同步周期和一次文档更新。

### 16.2 回滚触发条件

- ACL 泄露或权限过滤异常：立即全量回退并停止 V2 read。
- exact/keyword 核心用例回退超过阈值。
- 索引 current-version parity 异常。
- 同步积压超过 SLA 或出现不可恢复冲突。
- P95 延迟、错误率持续超过门限。

### 16.3 回滚动作

- feature flag 将默认检索切回 V1。
- 停止 V2 激活新版本，但继续保存原始文件和操作日志。
- 保留 V2 数据，不做破坏性删除；修复后重放 outbox。
- Git mirror 不回退历史，使用新 commit 表达撤销。
- ACL 问题回滚期间禁止直接绕过服务读取底层索引。

## 17. 主要风险与控制

| 风险 | 控制 |
|---|---|
| 向量召回带来相关但错误的内容 | Exact/BM25 并行、rerank、阈值、无答案、来源引用 |
| 文档越多噪声越大 | 项目/ACL 前置过滤、结构化分块、版本去重、质量评测 |
| OCR/表格解析错误 | 保存坐标和置信度、原文预览、低置信度进入复核 |
| 多人 Git 冲突 | Team 使用唯一写入者；Lite 使用 outbox 和显式 conflict 状态 |
| 业务规则被代码或文档污染 | provenance 强制、staging、人审、有效期、权威等级 |
| embedding 模型升级导致结果漂移 | 模型版本、双索引、shadow 评测、alias 切换 |
| 中心服务破坏离线能力 | 保留 Lite Profile 和 last-known-good 只读能力 |
| 日志或索引泄露敏感信息 | ACL 覆盖 stats/history/cache，日志脱敏和最小化 |

## 18. 决策清单

实施启动前需要由负责人确认，但不阻塞方案评审：

1. Team Profile 的身份源：公司 SSO、GitLab 身份还是独立用户目录。
2. 生产基础设施优先使用 PostgreSQL+pgvector，还是 OpenSearch+Qdrant。
3. OCR 使用本地引擎还是内网 OCR 服务，以及允许处理的敏感级别。
4. source freshness SLA：例如 1 分钟、5 分钟或按需同步。
5. 哪些项目作为 Phase 0/Shadow 的代表语料。
6. 业务规则最终审批人和各项目知识 owner。
7. 原始文档、解析文本、搜索日志和审计日志的保留期限。

## 19. 推荐的第一批工作包

为了避免一开始铺太大，第一迭代建议只启动以下工作：

1. 固化 MCP 契约和真实评测集。
2. 建立 source/document/version/chunk/operation/outbox 模型。
3. 完成 Markdown、PDF、DOCX 的增量摄取。
4. 在现有 FTS 旁加入本地向量召回和 RRF，先 shadow 不切流。
5. 把项目 scope 映射为 project_id，并在检索层预留 ACL filter。
6. 实现 `source_sync/document_status/sync_status/search_explain`。
7. 完成 V1 32 条数据迁移演练和一键回退。

这一批完成后再决定是否立即引入 OpenSearch/Qdrant；在真实 chunk 数量和延迟数据出来前，不应仅为“架构完整”提前部署所有重型组件。

