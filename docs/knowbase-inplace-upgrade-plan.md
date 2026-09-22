# knowbase 单库原地升级实施方案

> 日期：2026-09-18  
> 状态：已实施（本机静态、回归与真实库验证完成；远程网络和真实 OCR 仍需环境验收）  
> 核心约束：不建设第二套知识库，不迁移 ID，不引入 V1/V2 双运行时。

## 1. 目标

在现有架构上直接升级：

```text
Markdown + frontmatter（权威内容）
          │
          ▼
memory.db（FTS/向量派生索引、统计、反馈、同步状态）
          │
          ▼
现有 memory_search/read/save/update/feedback/stats
```

升级目标：

1. 现有 FTS5 增加模糊/语义召回和 RRF 融合。
2. import 支持 Markdown、PDF、Office、HTML、图片 OCR、代码和日志。
3. scope 在候选生成前过滤，避免多项目串扰。
4. Git 同步具备超时、非交互、定时拉取、失败状态和冲突提示。
5. 业务规则、标准、经验继续使用现有 staging、provenance、confidence 和 feedback 治理。
6. 保持现有 ID、Markdown 路径、Git 历史和 MCP 参数兼容。

治理补充：MCP 的 `source` 参数不具备身份权威；standard/preference/bizrule 始终进入 staging，激活后的修订只能通过本机可信人工 CLI `knowbase revise`。

## 2. 明确不做

- 不创建 `.knowbase/v2.db`。
- 不复制 Markdown 到 Document/Version/Chunk 第二套权威模型。
- 不做 V1/V2 双读、shadow 数据面或数据迁移。
- 不引入 PostgreSQL、OpenSearch、Qdrant、中心 API 或 Git mirror writer。
- 不要求同事修改 `repo_path`、MCP 工具名或现有记忆 ID。

## 3. 原地数据模型

### 3.1 保留

- Markdown/frontmatter：正文、类型、scope、来源、关系、治理状态。
- `meta`：派生元数据和统计。
- `mem_fts`：标题、标签、正文全文索引。
- `usage_log`、`feedback_log`：使用和反馈证据。

### 3.2 增量新增

在同一个 `memory.db` 内按需新增：

- `embedding_index(memory_id, model_version, content_hash, vector)`；
- `source_state(source_path, source_hash, modified_at, indexed_at, status, error)`；
- `sync_state(remote, local_revision, remote_revision, last_fetch_at, last_push_at, status, error)`。

以上均为派生或运行状态，不改变 Markdown 权威边界。

## 4. 检索链路

```text
query
  ├─ 原始 FTS5/BM25 召回
  ├─ 短词/标题/ID 精确召回
  ├─ 受控同义短语扩展
  └─ embedding/模糊向量召回
             ↓
          RRF 融合
             ↓
scope/type/tag/status 前置候选约束
             ↓
confidence × freshness × feedback 排序
             ↓
去重、阈值、无答案判断
```

要求：

- 原查询永远参与检索；扩展词不能覆盖原词排序。
- 无 embedding 模型时明确降级为字符 n-gram 模糊召回，不宣称真实语义模型。
- dense-only 结果必须达到阈值，避免无答案问题被随机填满。
- 返回结果显示命中通道，便于调试。
- 每个项目维护 exact、semantic、no-answer、cross-project 四类 golden set。

## 5. 多格式导入

解析器只负责把文件转换为规范文本和结构元数据；最终仍调用现有保存管道：

```text
文件 → parser → ParsedDocument → Markdown memory → FTS/embedding → Git
```

支持：

- Markdown/TXT；
- PDF；
- DOCX；
- XLSX；
- PPTX；
- HTML；
- PNG/JPEG OCR；
- Python/Java/JS/TS 等代码；
- 日志。

导入必须保留 `import_path`、SHA-256、格式、页码/sheet/slide/符号等可用元数据。相同路径和 hash 幂等；源内容变化时提示复核，不静默覆盖。

## 6. 多项目和人员

一期继续使用 scope，但收紧为服务端规则：

- 有项目上下文：只检索当前 scope + global。
- 无项目上下文：默认只检索 global，不扫描所有项目。
- scope 过滤在 dense/FTS 候选生成前应用。
- 项目目录名和 scope 使用显式映射，不能只靠目录 basename 猜测。

人员级 ACL 后续直接扩展 `meta` 表和服务端过滤，不另建知识库；在身份源未确定前不把 scope 宣称为安全权限。

## 7. Git 同步

### 7.1 写后推送

- commit 与 push 解耦；本地保存成功不因远程失败回滚。
- `GIT_TERMINAL_PROMPT=0`，禁止 MCP 后台等待凭据输入。
- commit/push 必须有超时。
- push 失败记录状态并返回明确警告，不能返回 `undefined`。
- 本地 INDEX/SQLite/Markdown 完成并提交后释放 RepoLock，再执行远程 push。
- push 前 fetch；双方均有新提交时自动 rebase，成功后推送；push 被其他客户端抢先时最多重试 `git.push_retries` 次（默认 3）。
- rebase 只在工作区干净时执行；真实内容冲突必须 `rebase --abort`，保留本地提交并列出冲突文件，禁止把普通分叉或网络超时表述为内容冲突。

### 7.2 检索前更新

- 后台或 TTL 到期时执行 fetch，不在每次查询无限阻塞。
- fetch/merge/reindex 全程持 RepoLock，remote 名称和分支从配置/当前 Git 分支解析，不写死 origin/main。
- 仅在工作区安全且可以 fast-forward 时自动更新。
- 更新成功后对变化文件增量 reindex；初期允许全量 reindex。
- 真实内容冲突、dirty worktree、认证失败均保留 last-known-good，并在 doctor 中展示；可自动重放的普通分叉不再要求人工处理。

## 8. doctor 与 alerts

只检查现有系统：

- `memory.db` 是否存在、可打开、WAL 和 FTS5 是否正常；
- Markdown 条数和 meta/FTS 条数是否一致；
- staging、stale、未验证条数；
- Git remote、分支、ahead/behind、dirty 状态；
- 最近同步错误；
- golden set 最近结果；
- parser 依赖和 OCR 二进制。

禁止再检查 `v2.db`、outbox、mirror.git 或第二套 schema version。

## 9. 文件级实施

- `knowbase/index.py`：混合召回、RRF、阈值、embedding 表。
- `knowbase/server.py`：保持 MCP 契约，输出通道和降级状态。
- `knowbase/parsers/`：正式多格式解析器。
- `knowbase/__main__.py`：多格式 import、单库 doctor/alerts。
- `knowbase/gitops.py`：超时、非交互、fetch/push 和同步状态。
- `knowbase/hooks.py`：调用同一检索服务，不保留另一套搜索实现。
- `tests/`：保留现有 E2E，加入 parser、混合检索、同步和 doctor 测试。

## 10. 清理清单

迁移可复用 parser/算法/测试后删除：

- 第二套 domain/repositories/schema；
- V2 ACL、governance、contracts、features；
- outbox/worker/mirror/Lite sync；
- DocumentVersion/Chunk ingestion service；
- V1/V2 shadow adapter；
- 只验证第二套运行时的 tests/v2。

清理完成后必须满足：

```bash
rg 'v2\.db|V2Repository|document_version|outbox_event|mirror\.git' knowbase
```

除历史说明外无运行时代码命中。

## 11. 实施顺序

1. 重写方案并冻结单库边界。
2. 重写 doctor/alerts。
3. parser 正式归位并接入原 import。
4. 混合检索在现有 memory.db 上完成。
5. 补 Git fetch/push 状态和超时。
6. 删除第二套运行时和测试。
7. 跑全量回归、真实库 golden set、wheel 和死代码扫描。
8. 提交后让同事 pull、重装 editable package、重启 Trae MCP。

## 12. 验收与回滚

验收：

- 现有 ID 和 Markdown 路径不变；
- 原 E2E/Hook/import/governance 全通过；
- exact 召回不回退；
- semantic/gap 召回提升；
- no-answer 不出现伪命中；
- 多格式导入进入现有 repo；
- Git 认证或网络异常在限定时间内返回明确错误；
- 安装产物包含 parsers 和检索模块；
- 运行时不存在第二套数据库引用。

回滚：保留 Git commit 边界，回滚代码即可；`memory.db` 和 Markdown schema 不发生破坏性迁移，无需数据回迁。
