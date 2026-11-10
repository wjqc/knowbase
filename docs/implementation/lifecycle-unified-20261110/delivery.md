# 统一生命周期实施交付（T0–T4）——2026-11-10

依据 [Trae MCP 主线与统一知识生命周期实施计划](../../plans/knowbase-trae-mcp-unified-lifecycle.md)（2026-11-09）。T0 生产清理与 rc10 安装见历史执行记录；真实 Trae 主线、无独立模型的真实 Trae 验收与前台性能对照仍未完成。下表区分隔离测试、生产协议验收和真实宿主验收。

## 1. 交付矩阵（按验收口径分别标明）

| 任务 | 口径 | 状态 | 证据 |
| --- | --- | --- | --- |
| T0 盘点+清理 | 生产只读盘点 | ✅ 完成 | §2.1（2026-11-09 22:52 水位） |
| T0 隔离演练 | 合成（隔离库） | ✅ 通过 | §2.2（快照副本：2 失败补回执、spool 155→74、九项账本不变、二次执行全零） |
| T0 生产执行 | 生产 | ✅ **已执行**（2026-11-10 01:14） | 备份 `~/.knowbase/backups/t0-retire-20261110-011127/`；8 条失败 checkpoint 补 retired 回执、spool 162→75（8+79）、candidates/feedback/evidence/task_outcomes 不变、二次执行全零 |
| T1 基线链路 | 合成（stdio MCP 真实契约，隔离库） | ✅ 4/4 | tests/test_t1_trae_baseline_chain.py（分页/无候选/重放防重/无模型证据） |
| T1 真实 Trae | 真实宿主 | **未验收** | Trae 接线层：三 Hook exit 0、桥接到 rc10。deploy-verify 是另一客户端的生产协议验证，只能作支持证据，不能替代 Trae 的实际提炼、复用和更新。 |
| T2 类型分流合并 | 代码+回归 | ✅ | §3.1；全量 1221 绿 |
| T2 验证绑定修复 | 代码+回归 | ✅ | §3.2（执行戳+执行记录+三重校验） |
| T3 宿主流程收敛 | 代码+文档 | ✅ | §4（有界尾持久化 4/4；trae-rules.md/MCP 描述已收敛） |
| T4 隔离库全量验收 | 合成（隔离库） | ✅ | tests/test_t4_acceptance.py（样例卡贯穿全链，§5 矩阵） |
| T4 部署 | 生产 | ✅ **已部署**（2026-11-10 01:13） | 提交 d9c1c77 已推 origin/main；wheel 0.2.0rc10 原地安装（16 模块逐字节一致）；worker 重启（launchd 新进程）；stdio 冒烟：统一生命周期说明 ✓、17 工具 ✓、检索命中 ✓ |

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

## 6. 审批件与执行记录

> 2026-11-10 01:11–01:16 用户批准后执行：§6.1 生产清理 ✅、§6.3 提交部署 ✅；§6.2 Trae 接线层 ✅、GUI 两条任务因环境输入守卫（Trae 窗口不在在屏列表，三类注入路径全拒）待用户在 Trae 界面完成。
>
> **01:26 部署验收补充（deploy-verify 标识，非 Trae）**：rc10 生产全链两条任务通过——任务 A：capture→claim→查重门 NEEDS_REASSESSMENT→逐卡真读 10 张相似卡→duplicate_evaluation 重提交→succeeded（saved_active=1）→读回（P-b4506b0ae008）；任务 B：search 命中→context→read→use(adopted)→feedback(helpful, task_outcome_id=deploy-verify-task-b)→update 修订（修订清验证 once）。生产轨迹：knowledge_uses host=deploy-verify 两条；内容库自动提交 3b4ef0e/8645097。新语义防线（本机路径 lint 拒绝、NEEDS_REASSESSMENT 查重门）在真实链路中各拦截一次并按协议放行。
>
> **01:40 收口记录**：当时以 deploy-verify 作为替代口径关闭交付。复核区分证据等级：该记录可证明生产 MCP 协议连通，Trae 专属接线证据可证明注册、启动和 Hook 消化；均未证明 Trae 在无独立模型条件下完成两项实际任务。GUI 输入受环境守卫阻断，仍记录为未验收。

### 6.1 T0 生产清理 runbook

```bash
backup="$HOME/.knowbase/backups/t0-retire-$(date +%Y%m%d-%H%M%S)"
mkdir -m 700 "$backup"
sqlite3 -readonly "$HOME/knowbase/memory.db" ".backup '$backup/memory.db'"  # 1. SQLite 一致性备份
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

## 7. 复核发现与修正

- **版本登记**：本机可测得 rc10 安装包，实际模块哈希为 `34d24a7db7c2743c`，固定运行清单仍标记上一版本 `e753066 / 04d4b5c50500220d`。源码此前也出现分发版本 rc10、模块版本 rc9 的差异。已统一源码版本并修正升级集成测试；当前复核未改安装清单、未重新部署。
- **统一治理**：MCP 与后台提炼的实质修订走同一待审提案，原发布版本保持；已确认的发布才会换版本并清空旧版本的验证镜像。普通使用反馈不充当高风险变更的可信验证。执行报告对齐运行 ID、验证器与卡片版本，失败命令不得产生正面证据。
- **宿主恢复**：按项目查询可领、待审任务；按项目领取可发现到期重试和过期租约。可选 Hook 只持久化已脱敏的有界尾部，完整与兼容入口均可回退读取。修复以隔离回归为证，未验证 Trae 或 ZCode 的真实宿主操作。
- **清理边界**：失败项也受截止水位约束；未完成提交和有效租约受保护。仅未成卡的旧待审材料纳入清理，保留回执。默认不删除其他已结算正文；显式传入 `--spool-days` 才运行已结算正文的保留期清理。本次未执行生产清理。
- **尚未验收**：实际 Trae 无独立模型完成生成与复用、前台延迟/注入量对照，以及 MCP 不可用时真实主任务的正常结束。生产配置在复核时仍启用了独立执行器和采集；不能把当前生产协议样本记作关闭模型与采集的 Trae 样本。

复核验证：全量回归 1229 通过、1 跳过；末轮针对生命周期、回退采集、清理、MCP 契约与两成员升级的定向回归 65 通过、1 跳过。真实 Trae 会话与前台对照仍未执行。本轮没有重新部署、更新生产配置或清理生产知识库。
