# 统一生命周期实施交付（T0–T4）——2026-11-10

依据 [Trae MCP 主线与统一知识生命周期实施计划](../../plans/knowbase-trae-mcp-unified-lifecycle.md)（2026-11-09）。本交付完成全部代码与隔离验收；**生产清理、真实 Trae 复验、部署三项为人工审批件**（见 §6）。基线 1216→**1221 passed / 1 skipped**（一次性 editable venv 全量）。

## 1. 交付矩阵（按验收口径分别标明）

| 任务 | 口径 | 状态 | 证据 |
| --- | --- | --- | --- |
| T0 盘点+清理 | 生产只读盘点 | ✅ 完成 | §2.1（2026-11-09 22:52 水位） |
| T0 隔离演练 | 合成（隔离库） | ✅ 通过 | §2.2（快照副本：2 失败补回执、spool 155→74、九项账本不变、二次执行全零） |
| T0 生产执行 | 生产 | ⏸ **待审批** | §6.1 runbook |
| T1 基线链路 | 合成（stdio MCP 真实契约，隔离库） | ✅ 4/4 | tests/test_t1_trae_baseline_chain.py（分页/无候选/重放防重/无模型证据） |
| T1 真实 Trae | 真实宿主 | ⏸ **待用户** | §6.2 清单 |
| T2 类型分流合并 | 代码+回归 | ✅ | §3.1；全量 1221 绿 |
| T2 验证绑定修复 | 代码+回归 | ✅ | §3.2（执行戳+执行记录+三重校验） |
| T3 宿主流程收敛 | 代码+文档 | ✅ | §4（有界尾持久化 4/4；trae-rules.md/MCP 描述已收敛） |
| T4 隔离库全量验收 | 合成（隔离库） | ✅ | tests/test_t4_acceptance.py（样例卡贯穿全链，§5 矩阵） |
| T4 部署 | 生产 | ⏸ **待审批** | §6.3 步骤 |

## 2. T0 旧积压清理

### 2.1 生产只读盘点（2026-11-09 22:52 +0800，/tmp 快照取证）

- jobs 155 行全部终态：capture 70 no_candidate + 48 succeeded（已结算，排除）；capture_checkpoint 15 succeeded（当晚 no_delta，排除）+ **2 failed**（J-07aa542ee3e7 / J-f783d9eae70a，MATERIAL_PARSE_FAILED——spool 仅存已被宿主删除的 `/var/folders/.../zcode-hook-*/transcript.jsonl` 引用，永不成功，属"已失败旧材料"）；embedding_refresh 20 succeeded（维护，排除）。
- 无 queued/preparing/awaiting_agent/claimed/retry_wait/blocked 任务，无在途租约。
- job_spool 155 行 = jobs 全量（worker 停机期 retention 未跑）：118 行 capture 正文超 7 天保留期，属"可独立删除的临时正文"。
- candidates 113 行**全保留**：83 saved + 12 updated + 16 linked_existing（成卡回执，含 31 条 11-05 queue-purge 后的孤儿 job 引用）+ 2 needs_review（进统一复核入口，不删）。
- staging 34 张（当晚并行会话产物+存量待审）、references 20、归档 23：一律不动。
- 使用/反馈/证据/版本记录（usage_log 2562、feedback_events 4、evidence_events 2、task_outcomes 3）：不动。

### 2.2 隔离演练（生产快照副本，/tmp/kb-t0-rehearsal）

新增 `knowbase jobs retire-backlog`（jobs.py `retire_plan`/`retire_backlog` + CLI）：

- 默认只读清单（数量/原因/排除项/kept 分状态计数）；`--execute` 才写库。
- 规则：失败知识类任务补 `retired` 回执（保留 jobs 行，不可再领取）；未终态旧任务（cutoff 前、无有效租约）结算 `no_candidate`；spool 删"已撤销任务 + 超保留期终态正文"；needs_review/候选/staging/证据记录不动；在途租约跳过；幂等（retired 戳过滤，二次执行全零）。
- 演练结果：execute 后 jobs 155→155、spool 155→74（2 retired + 79 超期）、candidates/feedback/evidence/usage/task_outcomes 逐项不变、卡文件不变；二次执行全零。
- 回归：tests/test_jobs_retire_backlog.py 5 项。

## 3. T2 代码改造

### 3.1 类型分流合并（类型只作检索分类）

统一风险规则 `admission.classify_risk(scope, change_relation, *, has_basis)`：R1=项目内 NEW/ENRICH 有依据；R2=项目内 CORRECT/REPLACE/MERGE/SPLIT 或无依据 NEW；R3=global（共享范围）。`has_content_basis`=非 `:adhoc` 显式 source、provenance、evidence、code_refs、source_refs 任一。

| 变更点 | 旧行为 | 新行为 |
| --- | --- | --- |
| commit_adapter 落盘路由 (`_routes_staging`) | standard/preference/bizrule 一律 staging | 统一风险路由：global/无依据→staging；项目内+依据→直接 active+once |
| admission 权威门 | standard/global-preference 需 authority_ref | 仅 scope=global 需权威确认（validation.risk_authority 同步只看 R3） |
| admission provenance | standard/bizrule 缺失=hard fail | 全类型缺失=unknown（保持待审），不再按类型硬拒 |
| store.lint | bizrule 无 provenance 拒绝保存 | 移除类型硬拒（由路由进 staging；resubmit_review 补出处放行） |
| writer.update 治理字段防线 | 仅 GOVERNED 类型 | 全类型（staging 除外） |
| versioned_feedback 验证通道 | 仅 GOVERNED staged 卡 | 全类型开放（W4-4 缺口关闭） |
| evolution.promotable | staged 卡须 GOVERNED+provenance | staged 卡须 provenance（类型无关） |
| server validation_candidates SQL | `type IN ('standard','preference','bizrule')` | 全类型 staging 卡 |
| revise CLI | 仅三类型 | 全类型 |
| `GOVERNED` 常量/`GOVERNED_STRICT`/`PROVENANCE_REQUIRED` | 类型集合 | 移除（注释保留语义迁移说明） |
| 文案（save 提示/提炼指引/工具描述/MCP instructions） | 按类型分工 | 统一风险路由口径 |

### 3.2 验证绑定修复（校验报告必须绑定执行时的卡片版本）

- 执行戳：`validators.stamp_report_text` 在真实执行时写入 `knowbase-execution-v1: {json}`（memory_id/content_revision/run_id/validator_id/executed_at）；`parse_pytext` 解析进 `execution_stamp`，artifact_hash 覆盖含戳全文。
- 执行记录：迁移 **v35** 新表 `validator_executions`（memory_id+artifact_hash 唯一，绑定 content_revision）；`attribution.record_execution` 在执行时登记。
- 三重校验（verify_execution 顺序）：EMPTY_REPORT → EXECUTION_STAMP_REQUIRED（无戳拒绝）→ 戢内 memory_id/revision 与目标一致 → EXECUTION_RECORD_MISSING（无记录拒绝）→ 记录 revision==当前 revision，否则 **STALE_EXECUTION_RECORD**——旧报告改版后不能验证新正文。
- CLI `lifecycle verify`：新增 `--run CMD`（执行时打戳+登记+可选 `--report-file` 落盘带戳原件）；`--report-file` 单独投喂必须已有执行记录。治理消费端本就按 `subject_revision` 过滤（effective_evidence），闭环成立。
- 信任边界（如实）：受信 CLI 内报告原件的新鲜度仍属操作者责任；代码层已封死"旧原件绑定新版本"通道（戳+记录+版本三方一致）。

### 3.3 测试

- 改写 72 个旧类型分流断言（23 文件，三组并行复核，机制断言未弱化）；关键改写：bizrule 项目内+出处→直接 active（原强制 staging）、权威门按 scope、无出处→staging 待审。
- 新增 tests/test_t2_unified_lifecycle.py 7 项（同输入同转换/权威门按范围/provenance unknown/全类型验证通道/防线全类型/revise 全类型/风险规则矩阵）+ test_w1_03 绑定负例 2 项（无戳拒绝/旧记录不能验证改版正文）。

## 4. T3 宿主流程收敛

- **有界尾持久化**（checkpoint_queue）：入队时同步读 transcript 尾部 ≤256KB（行对齐、毫秒级，9MB 会话实测 <0.5s）随 spool 持久化；worker 在原件被宿主删除后用尾件解析（临时文件用后即删）；无原件且无尾部保持诚实 MATERIAL_PARSE_FAILED。这是 2026-11-09 生产 2 条失败 checkpoint 的根因修复（ZCode Stop 后删除临时 transcript，旧路径只存引用）。tests/test_t3_tail_persistence.py 4 项。
- 文档：docs/trae-rules.md 重写为统一生命周期版（修复"14 工具"→17、移除历史"领取/续租"粘贴块、能力差异表更新）；MCP instructions 与 memory_context/memory_save/memory_job_submit 描述收敛（日常任务不做积压领取提醒）。
- 三项归零（设计+测试证据）：Stop 静默入队不阻断（test_hook_noise 断言 stdout 为空、无 block）；独立模型调用 0（T1 断言 executor_runs 空且三开关关闭）；主任务内自动领取 0（前台 notify_job_ids 恒空）。

## 5. T4 隔离库验收（tests/test_t4_acceptance.py）

样例卡（global bizrule，PRD-T4 出处）全链通过：

1. 生成：capture→claim(v2)→submit → **needs_review 待审**（不以入队代替成卡）；回执链 job_submissions→memory_id+revision、candidates→payload_hash+证据片段可追溯。
2. 发布：propose(R3)→validate fail（权威缺失）→`authority_confirm(PRD-T4 §1 负责人确认记录)`→validate pass→publish → active+once；独立读回+默认检索命中。
3. 修订：受信 `revise` CLI → 降回 once、旧 verified 清零（证据不转嫁）。
4. 晋升：3 独立任务 × 2 独立组（g1: t4-a/t4-b；g2: t4-c）注册验证器 L3（执行戳+执行记录+任务身份）→ recompute → **verified**（绑定当前 revision）。
5. 反证隔离：第 4 任务可信 fail → **quarantine**（stale+quarantine_reason），退出默认检索，证据账本 4 条保留可追溯。
6. 归档时钟：受控时钟 stale 满 90 天无复验证据 → **archived**（archived_at 落 frontmatter，正文保留，退出检索）。

## 6. 审批件（人工动作，未执行）

### 6.1 T0 生产清理 runbook

```bash
cp ~/knowbase/memory.db ~/.knowbase/backups/t0-retire-$(date +%Y%m%d-%H%M%S)/memory.db   # 1. 备份
knowbase jobs retire-backlog --cutoff 2026-11-09T06:00:00+00:00                            # 2. 只读清单复核
knowbase jobs retire-backlog --cutoff 2026-11-09T06:00:00+00:00 --execute                  # 3. 执行
knowbase jobs retire-backlog --cutoff 2026-11-09T06:00:00+00:00 --execute                  # 4. 二次执行应全零（幂等核对）
```

- 预期：retired 2（J-07aa/J-f783）、spool 删除 ~81 行、其余账本不变；执行前按 §2.1 重盘水位（并行会话可能新增终态 spool）。
- 审批点：涉及生产 DB 写入；**用户确认后执行**。

### 6.2 真实 Trae 复验清单（T1 剩余，需用户在 Trae 操作）

1. 部署后重连 Trae MCP（旧连接不加载新代码）。
2. 任务 A（真实业务任务）：结束后用 memory_capture 显式提交一段真实材料 → claim(v2) → material 续页 → submit → memory_read 读回实际卡。
3. 任务 B（独立任务，同项目）：memory_context 检索命中任务 A 的卡 → memory_read → memory_use(adopted) → memory_feedback（真实 task_outcome_id/task_result）→ memory_update 提交一处可核对修订并确认旧验证清零。
4. 对照记录：前台延迟（Stop 无感）、Stop 强制续跑 0 次、主任务内自动领取 0 次；另跑一个与知识无关的业务任务确认知识维护失败不影响主任务。

### 6.3 部署步骤（生产，待审批）

1. 源码提交（本交付 52 文件，含测试；建议先人工审 diff）。
2. uv 离线构建 wheel → 备份 `~/.knowbase`（config+memory.db+team）→ 原地安装唯一 runtime（`~/.knowbase/runtime/venv`，wheel 原地装+kill worker；不建并行包）。
3. 验证：固定路径启动、MCP 桥接重连后 instructions 含"统一生命周期"、worker 加载新代码、`jobs retire-backlog` 干跑、hook 冒烟（含 transcript 删除回退）、golden 集。
4. 生产 T0 清理（§6.1）与真实 Trae 复验（§6.2）在部署窗口内完成并回填台账。

## 7. 边界与如实声明

- 生产库当晚有并行会话活动（303 张卡治理投影未提交、34 张今晚 staging）：本交付未触碰内容库，未提交任何 Git 变更（含源码），未部署生产。
- 报告新原件的"执行时"信任以受信 CLI 运行为界（操作者自律部分见 §3.2 末）；检索 golden 集与真实库语料漂移为既有信号（test_retrieval.py 重基线记录在案，非本次回归）。
- T1 的"真实 Trae 任务"与 T4 的"真实宿主复验/部署"未执行，达到门槛前不宣称全计划闭环。
