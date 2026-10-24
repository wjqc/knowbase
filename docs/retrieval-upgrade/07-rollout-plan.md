# 07 · 分阶段实施计划、风险、回滚与复用清单（Phase 0）

## 1. 阶段计划（按已冻结的 Phase 0–5）

| 阶段 | 内容 | 发布条件 | 预估 |
|---|---|---|---|
| **Phase 0（本次交付）** | ADR、代码调查、评测集协议+候选、实验协议、设计定稿 | 基线清楚、方案批准 | 已完成草案 |
| Phase 1 | 查询卫生（hooks.py）、L2 词典文件化、相关性门控骨架、遥测 mode 字段、B 组实验 | 06 号 §3 Phase1 门槛全过；无关注入得到控制 | 1–2 周 |
| Phase 2 | Embedding Provider、模型指纹+canary、增量索引、L3 状态机、C/D/E 实验 | 06 号 §3 Phase2 门槛全过（A/B 验证实际收益） | 2–3 周（依赖内网服务实测） |
| Phase 3 | init 向导、doctor 扩展、降级恢复演练 | 04 号 §5 验收全过；不配模型一键接入 | 1–2 周 |
| Phase 4 | 三人真实试点（跨宿主/跨机）、效果统计 | 质量、可靠性、价值证据均通过 | 2 周 |
| Phase 5 | 发布与团队推广 | 明确发布裁决及回滚方案 | — |

排序不变式：H0–V4 人工验收主线（Trae 样本/三人跨机/效果稿）不因本计划暂停；Phase 1 不得与 team-stats 未收口改动（当前工作区未提交文件）混在同一提交。

## 2. 风险与对策

| 风险 | 等级 | 对策 |
|---|---|---|
| 内网网关协议与假设不符（无批量/无模型清单/鉴权私有） | 高（Phase 2 前置） | 03 号§7 待验证清单先行实测；不符即修订适配器设计，不影响 Phase 1 |
| 网关静默换底座 | 高 | 指纹+多维 canary（03 号§4）+ doctor 周期巡检；漂移冻结不删数据 |
| 门控误杀真命中 | 中 | 06 号§3-4 零命中误杀率 ≤5% 红线；门槛 config 可调可回滚 |
| 长文门控规则误伤粘贴的真错误日志 | 中 | 保留错误码/异常栈提取通道（02 号§5.2），标注集长文负例含真日志样本验证 |
| hook 延迟回退 | 低（V1 无远程调用） | Phase1 增幅 ≤20ms 红线；预算机制复用 hook_notice 先例 |
| 索引重建与同步竞态 | 中 | 复用 RepoLock+sync_lease；向量补嵌走后台 jobs 模式不占同步临界区 |
| 多机索引版本不一致 | 中 | 指纹校验在每机探测时进行；覆盖率不足自动补嵌（02 号§3） |
| Codex 宿主接入方式未知 | 低 | 首期支持 ZCode/Claude/Trae；Codex 标待验证（04 号§2） |

## 3. 回滚方案

- Phase 1：查询卫生与门控均 config 开关（`retrieval.gate.*`、hook 卫生开关）；回滚=关开关回现状行为，git revert 单提交。
- Phase 2：`embedding.provider=null` 即整体回 L2/L1；embedding_index 数据保留不删；旧字符哈希向量退役但源码保留可重建（03 号§5）。
- Phase 3：init 写入前全量备份（04 号§1），`knowbase init --restore <时间戳>` 恢复宿主配置。
- 任何阶段：生产运行时原地升级前先备份数据与配置（AGENTS.md 政策），升级后验证固定路径启动/MCP/Hook 并要求旧连接重连。

## 4. 现有代码复用清单（新增代码最小化）

| 需求 | 直接复用 | 新增 |
|---|---|---|
| RRF/双通道融合 | `_rrf_scores`/`_dense_ids`/channels（index.py:266-321） | 版本过滤参数、门控 |
| 向量表/内容指纹 | embedding_index + content_hash（index.py:45-97） | 指纹读取校验、增量跳过 |
| 排序与治理过滤 | search 排序链/scope/staging 过滤（index.py:324-385） | 无改动 |
| 覆盖缓存失效 | knowledge_version digest（context.py:138-141） | 无改动（自动生效） |
| 预算机制 | fast_path_timeout/token_budget/hook_notice 预算（context.py:147,172；hook-noise-reduction 交付） | 注入条数字符预算 |
| 幂等/事务 | rebuild 单事务（index.py:126-148）、context_events payload_hash 幂等（context.py:250） | 补嵌 jobs 化 |
| 同步防护 | RepoLock/sync_lease/防迟到写（gitops.py:495-545） | 增量重建钩子 |
| 遥测 | record_search/search_history/value export（index.py:520-570；__main__.py:869-902） | mode/degrade_reason 字段 |
| 评测 | golden set 可执行脚本（tests/test_retrieval.py） | 扩展为 60–100 条 |
| 宿主配置 | docs/hosts/*.md 落点矩阵 | 发现/写入/备份向导 |

## 5. 提交与工作区纪律

- 本工作区（docs/retrieval-upgrade/）为新增未跟踪文件，未改动任何既有文件、未提交；
- 各 Phase 代码在独立分支开发，一次性 venv 测试（既有惯例），生产运行时仅经批准后原地升级；
- 每阶段结束更新 progress.md 并核对并行会话改动（既有惯例）。
