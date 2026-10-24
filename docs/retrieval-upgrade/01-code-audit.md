# 01 · 前置代码调查报告（Phase 0）

核对日期：2026-10-23。源码版本 0.2.0rc7（工作区），生产运行时 0.2.0rc6（~/.knowbase/runtime/venv）。
所有结论关联实际源码位置；"现状"指当前已实现行为，"缺口"为本升级需新增内容。**设计预期不等于已实现能力。**

## 1. init 现状与 Agent 接入缺口

**现状**（`knowbase/__main__.py:72-116` cmd_init）：配置文件不存在才写（幂等不覆盖）、补齐仓库目录、git init + 远端白名单校验（allowed_remote_prefixes）、可选 `--import-from` 导入为 source。幂等纪律已达标。

**缺口**：完全不发现/不写入 Agent 侧配置。宿主配置落点已有文档（`docs/hosts/latest-local.md:11-13`）：

| 宿主 | MCP 配置 | Hook 配置 | 文档 |
|---|---|---|---|
| ZCode | `~/.zcode/cli/config.json` mcp.servers.knowbase | 同文件 hooks.events（6 事件，--style zcode） | docs/hosts/zcode.md |
| Claude Code | `~/.claude.json` mcpServers.knowbase | `~/.claude/settings.json`（7 事件，含 PreCompact） | docs/hosts/claude.md |
| Trae CN | `~/Library/Application Support/Trae CN/User/mcp.json` | `~/.trae-cn/hooks.json`（5 事件，工具失败从 PostToolUse 识别） | docs/hosts/trae.md |
| Codex | **未文档化——待调查**（docs/hosts/ 无 codex.md） | 同左 | — |

已知环境陷阱（findings.md「P4 收尾」）：TRAE sandbox 曾拦截 `~/knowbase/.lock` 与 `~/.knowbase/` 写入——init 写宿主配置时需三层兜底（先探测可跑性，失败本身是诊断信息）。

## 2. doctor 现状与缺失项

**现状**（`__main__.py:837` 入口，检查逻辑 `:640-670`）：index_parity（markdown==meta==fts 行数）、governance（staging/stale 计数）、embedding_index 行数==meta 行数、inflight 队列/事件、sync_state。已有"运维命令三层兜底"原则（findings.md P4 收尾）。

**缺口**：当前实际检索等级与降级原因、Embedding 服务健康、模型/索引版本兼容性、Agent MCP/Hook 配置有效性。注意：L3 落地按 model_version 过滤后，现有 `embedding_index 行数==meta` 检查语义需改为"当前激活版本的向量覆盖率"。

## 3. Hook 自动检索入口与无关注入路径

**现状**（`knowbase/hooks.py:66-105` user_prompt）：
- `:82` **原始 prompt 直接作为查询**（无长度上限；`MIN_PROMPT_LEN` 只有下限）——长文噪音根源；
- `:84-85` 自动注入仅取 confidence=verified（`hooks.inject_min_confidence`）；`:87-88` 过滤 contradicts 关系卡；`:90-96` 版本安全过滤（verified_content_revision CAS）；`:97` 截取 MAX_HITS 条；
- 2026-10-23 hook 降噪交付（docs/hook-noise-reduction.md）新增**自动提示预算** `inflight.hook_notice_budget=2 / hook_notice_chars=900`（硬上限 2/900）——预算机制已有先例可复用于注入，但 auto_search 注入当前**不在**该预算内。

**误召回归因**（2026-10-23 DB 副本重放，本会话原始数据）：3648 字查询命中的无关卡 channels 标记为 `['lexical','dense']`——长文 split 出 Agent/git/knowbase 等通用词走 OR 召回+命中数加权（`index.py:189-226`）是词法侧根因；字符哈希 dense 侧同时贡献假相似度。**只关 dense 不止血。**

## 4. memory_search / memory_context 完整调用链

- `server.py:203-221` search_impl → `index.search()`（同一引擎，无第二实现）。
- `context.py:142-243` memory_context 逐 topic 逐 query 调 `index.search(conn, query, scope=scope, limit=10)`（`:174`），每条记 `inflight_search` 事件；**预算三件套已存在**：`fast_path_timeout_ms` 时间预算（`:172`）、`context_token_budget*2` 字符预算（`:147`）、`max_candidates`（`:196`）；coverage 缓存以 topic 签名+知识版本指纹为键（`:159,199`）。
- **重要复用点**（`context.py:138-141`）：`knowledge_version` 摘要 = 对 embedding_index 的 `content_hash+content_revision` 全量 digest——索引内容变化会自然使 Agent 端 topics 覆盖缓存失效。向量重建后此机制自动生效，无需新代码。
- 幂等：context_events 按 payload_hash 去重（`:250-254`），失败不伪装 no_hit（`:264`）。

## 5. FTS5 / 字符哈希 dense / RRF / 阈值

| 组件 | 位置 | 现状 |
|---|---|---|
| FTS5 | index.py:39-40 | trigram 分词；≥3 字词 MATCH OR 召回+命中词数加权（:189-226）；≤2 字词 LIKE（:229-246）；无命中整体 LIKE 回退（:342） |
| 同义扩展 | index.py:287-312 | `_QUERY_ALIASES` 仅 4 条硬编码（连不上/很卡/怎么写/问一下）+中文 run 抽取——**L2 现状不合格，0/4 实测** |
| 字符哈希向量 | index.py:249-263 | 512 维 char n-gram 哈希，注释自认非语义模型 |
| dense 通道 | index.py:266-284 | 阈值 `index.dense_threshold=0.25`（config.py:27），**无模型版本过滤**，与 lexical 经 RRF（:315-321）融合 |
| 排序 | index.py:324-385 | RRF 分 × scope(2.0/1.5) × verified(1.5) × 新鲜度(半衰期180天) × 反馈(0.3-1.5)；结果带 channels 字段 |
| scope 过滤 | index.py:354 | scope in (请求scope, 'global')——治理边界，检索升级不得绕过 |

## 6. embedding_index 读写/版本/增量

- 写入：`index.py:88-97` 每次 upsert 全量重写，model_version 取 config `index.vector_model`（默认 char-ngram-v1），content_hash=sha256(title+tags+body)。
- **读取只有三处且均不做版本校验**：`_dense_ids`（index.py:273，`SELECT memory_id,vector` 无 WHERE model_version）；context.py:139（变更指纹用途）；doctor 行数（__main__.py:658）。**查询侧向量硬编码 `_fallback_vector`（index.py:268）**——裸换模型必混空间。
- 增量：content_hash 已存但**无任何"内容未变则跳过重嵌"逻辑**。

## 7. Git 同步后重建/事务/失败恢复

- 同步为后台子进程：refresh.py:57-60 拉起 `python -I -m knowbase sync-once`（每事件短生命周期）。
- gitops.py 同步主体：fetch timeout=10s 不阻塞本地读写；RepoLock+sync_lease 双保护；多处防迟到写（指纹冻结、分支/远端/配置变化即 abort）；工作区脏且本地≠远端时保留 last-known-good（`:513-514`）。
- **`:523-525`：远端有变更 → `index.rebuild(repo)` 全量重建**（rebuild 本身单事务，index.py:131-141，含孤儿向量清理）。真模型时代每次同步重嵌全部卡片——增量跳过必做。失败路径：sync_state 记 error/conflict，sync_attempts 留痕，本库继续 last-known-good。

## 8. usage_log / feedback / team-value 复用能力

- usage_log（index.py:41-42）五个工具事件全覆盖；`record_search`（:520-527）payload={search_id,query,scope,hits[id,title,rank,score,confidence,status]}——**加 mode/degrade_reason 字段向后兼容**；`search_history`（:559-570）可挖历史查询；stats 漏斗（:420-443）含 auto_search。
- feedback_log（:405-417）；value 链路：`knowbase value export/import/snapshot/report`（__main__.py:869-902，显式离线聚合不自动上传，协议见 docs/value-events-protocol.md）；team collect 读 search 事件（team/collect.py:85）；value/overview.py:39 消费 usage_log detail。
- 遥测只读契约（既有约束）：指标扩展不得改变既有事件语义，只加字段。

## 9. golden set 与检索质量基线

`tests/test_retrieval.py`（打真实库，可直接执行）：
- 基线 2026-09-17 v1.3：must 8/8、**gap 0/4**、HitRate@3=8/12。
- 4 条 gap 查询：内网连不上怎么办→P-2026-0001、系统很卡→P-2026-0006、日报怎么写→W-2026-0001、部署要问一下吗→PR-2026-0001（:29-32）。
- 文件头已预埋验收口径："gap 类 HitRate 从基线提升且 must 类不回退"——**评测协议直接继承此红线**。
- 1 条 must 为负例（随机字符串应返回空）——正确拒绝测试已有先例。

## 待验证项（不得宣布已具备）

1. Codex 宿主 MCP/Hook 配置方式（无文档）。
2. 公司内网 Embedding 网关真实协议：base_url、模型清单、认证方式、是否返回不可变模型修订号、限流行为——**需用户提供或 Phase 2 前实测**。
3. 生产 rc6 与源码 rc7 检索行为差异（本报告重放用 rc6；Phase 1 前须以 rc7 重新冻结基线）。
4. `~/.knowbase/config.json` 现值 `vector_model=char-ngram-v1`、`dense_threshold=0.25`——升级时需迁移为分层配置。
