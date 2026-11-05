# knowbase 知识卡片全生命周期 V2：实施任务书

> 状态：任务书 v1.0（2026-11-05）。基线：`7e6f8cc`（0.2.0rc9）。
> 配套：[架构设计](../design/knowbase-knowledge-lifecycle-v2.md)、
> [审计修订与设计决策记录](../reports/knowbase-lifecycle-v2-audit-reconciliation.md)。
>
> **本任务书的定位与 V2-01..V2-18 的关系**：V2-01..V2-18 的机制层已实施
> （65dfdcd，61 项新增测试，全量 1126 passed）。本任务书**不是**重做 V2-01..V2-18，
> 而是覆盖其**剩余工作**：生产接线、自动识别、真实验证、清理收尾。
> 每一项任务都标注了它在架构设计中的章节、精确的代码位置、验收标准与依赖。
> 任务编号 W0～W4，W = 剩余工作（Work remaining）。
>
> 执行纪律：
> - 代码先于文档；每项任务先写测试、再写实现、最后更新本文档的状态栏。
> - 只改本任务范围内的文件；不碰运行配置、不碰真实库、不擅自回滚迁移。
> - 所有声称必须标注证据等级（E0～E4，见审计修订文档 §1.1）；「函数存在」不等于「闭环已通」。
> - 每项任务完成后在本文档的状态栏更新状态与提交哈希；未完成的任务不得标记完成。

## 任务总览

| 阶段 | 目标 | 任务 |
|---|---|---|
| W0 收口 | 修复已核实的文档/代码不一致，补齐未确定项的核查 | W0-1～W0-5 |
| W1 接线 | 把已实现的 V2 机制接入生产路径，打通自动闭环 | W1-1～W1-8 |
| W2 内容演进闭环 | 八关系自动识别与自动生成，补齐循环 A 的缺失层 | W2-1～W2-4 |
| W3 真实验证 | 把全部 external_pending 逐项转为真实验收 | W3-1～W3-5 |
| W4 治理与清理 | L4、积压根因、简化清单、生产迁移 | W4-1～W4-5 |

依赖关系：W1 → W3（接线完成后才能做真实验证）；W2-1 → W2-2/W2-3/W2-4；
W3-5 依赖 W1 + W2-2；W4-3 依赖 W3-5（V2-16/17 真实验收后才能删代码）。

---

## W0 阶段：收口

### W0-1 修复 retry_plan 的文档与代码不一致

- **状态：已完成（2026-11-05，第一批实施）**——按 docstring 补齐代码检查（spool 存在性、error 内容分类、lease 状态区分）+ docstring 重写六类口径；测试扩展 4 个种子场景，7 passed。详见 `reports/knowbase-lifecycle-v2-implementation-batch1.md`。

- **目标**：消除 `retry_plan` docstring 与代码实现的三处不一致。
- **位置**：`knowbase/automation/worker.py:519-560`（`retry_plan`）、
  `tests/test_v2_02_runtime_health.py:114-150`。
- **事实**（E2，代码核查已确认）：
  1. docstring 称 requeue_ready 要求「租约已释放、材料 spool 仍在」，但代码不检查
     `job_spool` 是否存在、也不检查 `lease_owner` 是否为空。
  2. docstring 称 blocked_config 是「`material_manifest_required` 等配置类原因」，
     但代码把所有 `blocked` 状态都归入此类，不论 `error` 为何。
  3. `failed_permanent` 的「permanent」只是命名：`jobs.retry()` 允许手动重试，
     `run_due()` 还会对 `TRANSIENT_ERRORS` 的 failed 任务自动改回 `awaiting_agent`。
  4. 存在第六类 `unknown_state`，文档未提及。
  5. `lease_stuck` 的判定不区分状态（`retry_wait` 任务若 `lease_until` 仍在未来会被判为
     `lease_stuck`；`claimed` 但租约已过期则落入 `unknown_state`）。
- **任务**：二选一并说明理由——（a）按 docstring 补齐代码检查（spool 存在性、error 内容
  分类、lease 状态区分）；（b）按代码修正 docstring，明确六类口径与 `failed_permanent`
  可恢复的语义。推荐（a）对第 1 项（spool 检查是分类正确性的关键），（b）对第 3、4 项
  （命名与兜底类是文档问题）。
- **验收**：新增测试覆盖 spool 缺失的 requeue_ready 任务、error 非配置类的 blocked 任务、
  `unknown_state` 任务；`worker retry-plan` 输出与文档口径一致；全量回归通过。
- **依赖**：无。

### W0-2 修复 evidence.py 与 logical_task_key 的文档/代码不一致

- **状态：已完成（2026-11-05，第一批实施）**——第 1 项选（b）修正 docstring 为「仅计数，不入库」+ details 记录 event_id 与原因；第 2 项选（a）在 decide() 中过滤 `unknown:` 前缀的正向事件（负向仍触发隔离）；新增治理测试，6 passed。

- **目标**：消除两处已核实的 docstring 与代码不一致。
- **位置**：
  1. `knowbase/knowledge_lifecycle/evidence.py:112-119` vs 同文件 docstring 第 9 行：
     docstring 称未验签事件「保留 external_unverified 观察」，但代码该分支无 INSERT、仅计数。
  2. `knowbase/knowledge_lifecycle/contracts.py` `logical_task_key()` docstring vs
     `knowbase/knowledge_lifecycle/governance.py:57-82` `decide()`：
     docstring 称 unknown 不参与独立性计数，但 `decide()` 未过滤 `unknown:` 前缀。
- **任务**：第 1 项——二选一：（a）按 docstring 补 `external_unverified` 观察落库
  （需明确表结构与治理隔离）；（b）修正 docstring 为「仅计数，不入库」。
  第 2 项——二选一：（a）在 `decide()` 的 `pass_tasks` 计算中过滤 `unknown:` 前缀；
  （b）修正 docstring 说明实际行为（折叠为单键，最多贡献 1 任务 + 1 组）。
  两项都需在任务书中记录选择与理由。
- **验收**：对应测试断言修正后的行为；全量回归通过。
- **依赖**：无。

### W0-3 修正 server.py 工具数 docstring 与 V2-12 交付记录表述

- **状态：已完成（2026-11-05，第一批实施）**——docstring 改为 17 个工具（已核实注册数）；交付记录改为「可调用的重算函数，待 W1-4 接线」；两份共享文档 16 → 17。

- **目标**：消除两处陈旧/误导表述。
- **位置**：
  1. `knowbase/server.py:1-5` docstring 称「6 个工具」，实际注册 17 个 MCP 工具
     （`server.py:910-927`）。
  2. `docs/implementation/lifecycle-v2/V2-12_V2-15/delivery.md` 称 `recompute_all()`
     为「worker/导入后重算入口」——函数存在且可调用，但仓库内无任何 worker handler
     或导入流程调用它。
- **任务**：修正 docstring 为 17 个工具（或改为动态计数）；修正 V2-12 交付记录表述为
  「可调用的重算函数，待 W1-4 接线」。
- **验收**：grep 确认无残留「6 个工具」表述；交付记录表述与代码接线状态一致。
- **依赖**：无。

### W0-4 核查 redirect_ids 是否被检索跟随

- **状态：已完成（2026-11-05，第一批实施）**——结论：**不跟随**。写仅 publication._materialize，读仅 contracts manifest 校验；index/extraction/recall/consumption/delivery/server 零读取；evolution_recall 邻接只跟 relations。缺口已转 W1-8 并完成。

- **目标**：确认 `_materialize()` 写入的 `redirect_ids` 是否被检索路径跟随。
- **位置**：`knowbase/knowledge_lifecycle/publication.py:197-243`（`_materialize` 写入）、
  `knowbase/index.py:445-574`（`search()`，未见 redirect 解析）、
  `knowbase/extraction/recall.py:13`（`wide_recall`）。
- **事实**（E2）：`_materialize()` 把 redirect 信息写入 `published_heads` 与旧卡 frontmatter，
  但 `index.search()` 未见 redirect 解析逻辑。宽召回 / relations 邻接是否覆盖，未核查。
- **任务**：读 `wide_recall` 与 relations 邻接路，确认 redirect 是否在任何检索路径中被跟随；
  若无，记录为缺口并转 W1-8；若有，记录路径与测试位置。
- **验收**：一份核查结论（跟随 / 不跟随 + 证据行号）；若不跟随，W1-8 任务定义随之确定。
- **依赖**：无。

### W0-5 独立复核授权跟进报告的 72/4/8 积压数字

- **目标**：在真实库上重跑 `worker retry-plan`，确认积压分类的真实数量与 kind 分布。
- **位置**：`knowbase/automation/worker.py:519-560`（`retry_plan`，只读，不修改任何状态）。
- **背景**：`docs/implementation/lifecycle-v2/authorized-followup/report.md:43-48` 称
  72 failed_permanent（64 material_budget_exceeded + 8 index_changed）、4 needs_review、
  8 requeue_ready。但代码核查已确定：8 条 `index_changed` 按代码只能来自
  `embedding_refresh` 向量索引任务（`retry_plan` 不过滤 `kind`），解读为「提炼积压」
  是代码不支持的归因；报告未列 `lease_stuck` / `blocked_config` / `unknown_state` 三类，
  不能推断其余三类为零。
- **任务**：在真实库只读重跑 `worker retry-plan`，按 `kind` 分列数量；确认 72 条中
  capture/extract 与 embedding_refresh 各占多少；确认其余三类是否为零。
- **验收**：一份复核记录（SQL + 输出 + 时间戳），修正报告中的归因表述。
- **依赖**：需用户授权访问真实库（只读）。**未授权前不得执行。**

---

## W1 阶段：接线（第一优先）

> 本阶段是 V2 剩余工作的核心。机制已实现，接线缺失是「自动闭环未打通」的直接原因。
> 每一项接线任务都必须包含：生产调用方、触发条件、失败降级、测试覆盖。

### W1-1 接线 observation.ingest：宿主原生事件落库

- **状态：已完成（2026-11-05，第一批实施）**——新模块 `knowledge_lifecycle/observe.py`；接线 host_events.handle()（hook 路径）与 server.py 四工具（read/context/use/search）；决策：不新增 observation.batch MCP 工具；feedback_impl 刻意不接线。4 passed。验收中的「真实宿主会话」一项仍待用户 Mac 实测（沙箱无法跑真实会话）。

- **目标**：让真实宿主会话的原生事件进入 `native_events` / `task_runs`。
- **位置**：生产调用方待新增。候选插入点：
  - `knowbase/adapters/host_events.py` `handle()`（inflight 路径的事件总入口；
    内含 event → stage 映射与 `capture_increment` 触发条件）；
  - `knowbase/hooks.py:361-445`（`cmd_session_start` / `cmd_user_prompt` /
    `cmd_stop` / `cmd_tool_event` / `cmd_before_compact`）。
- **事实**（E2）：`observation.ingest()`（`knowledge_lifecycle/observation.py:26`）
  在 `knowbase/` 内无任何生产调用（仅测试调用）。表结构存在（迁移 v32）。
- **任务**：
  1. 选定插入点（推荐 inflight 路径的 `host_events.handle()`，因其已有事件归一化与
     capture 触发条件；Hook stdin 路径作为备选）。
  2. 实现调用：宿主事件 → `normalizer.normalize_events()` → `observation.ingest()`，
     失败时只记 gap、不阻断宿主任务（Hook 绝不阻断是硬约束）。
  3. 处理 identity_basis：有原生任务键 → native；无则 local_segment + `unknown:` 前缀。
- **验收**：一次真实宿主会话（Codex/Claude/Zcode/Trae 任一）运行后，`native_events`
  出现该会话的 tool_call/tool_result 配对行，`task_runs` 出现对应 run；
  未配对事件进 gap_flags 不猜；Hook 失败不阻断宿主（测试覆盖）。
- **依赖**：无。**前置于 W3-3。**

### W1-2 接线 attribution.correlate：L0/L1/L2 自动派生

- **目标**：任务运行结束后自动产生 L0/L1/L2 证据事件。
- **位置**：worker 新增 handler。注册点：`knowbase/automation/worker.py:29` `TASK_DEFS` +
  `:378` `_HANDLERS` + handler 函数。候选触发：任务结束事件（stop / checkpoint）或
  周期扫描已完成的 `task_runs`。
- **事实**（E2）：`correlate()`（`attribution.py:58`）在 `knowbase/` 内无任何生产调用；
  worker 全文无 `attribution` / `correlate`。
- **任务**：
  1. 新增 worker 任务 kind（如 `evidence_attribution`），间隔与触发条件由设计定
     （推荐：任务结束时触发 + 兜底周期扫描，避免遗漏）。
  2. 调用 `correlate(conn, repo, run_id)`，对每个已完成且未归因的 run 派生 L0/L1/L2。
  3. 幂等：`stable_event_id` 保证重跑零增量；ambiguous 不发奖；通用命令进 generic_rejected。
- **验收**：一次真实任务结束后，`evidence_events` 出现对应的 L0/L1/L2 行；
  同一 run 重跑零增量；歧义 case 不发 L2（测试覆盖）。
- **依赖**：W1-1（需要 `native_events` / `task_runs` 有数据）。

### W1-3 接线 verify_execution 与首个 validator 适配器

- **目标**：让 L3 从「机制存在」变为「能产生真实 L3 事件」。
- **位置**：
  - `knowbase/knowledge_lifecycle/attribution.py:159` `registered_validators()`、
    `:169` `verify_execution()`；
  - 配置 `knowledge_lifecycle.validators`（`config.py` 默认配置中无 `validators` 键）；
  - 新建首个 validator 适配器（推荐 pytest 报告适配器：读 pytest JSON/XML 报告，
    核对 artifact_hash + checks + claim_id）。
- **事实**（E2）：`verify_execution()` 零生产调用；仓库内无任何 validator 适配器实现；
  默认配置无 `validators` 键，故 `registered_validators()` 默认返回空字典。
- **任务**：
  1. 实现 pytest 报告适配器（最小：解析报告、提取 artifact_hash 与 checks、
     映射 claim_id、返回标准报告结构）。
  2. 在配置中登记该适配器（`knowledge_lifecycle.validators`），默认关闭、显式开启。
  3. 接线调用点：任务结束时对相关卡的 claim 运行验证（或由 worker 任务触发）。
  4. 未登记验证器时返回 `VALIDATOR_NOT_REGISTERED`，不产生事件（已有行为，保持）。
- **验收**：一次真实 pytest 运行后，对应卡的 claim 产生 L3 事件（pass 或 fail）；
  ghost claim → unknown；空报告 → 错误且不记录（测试覆盖）。
- **依赖**：W1-1（需要任务与卡的关联）。**前置于 W3-1。**

### W1-4 接线治理重算：apply_decision / recompute_all

- **目标**：证据到达后自动重算治理决策。
- **位置**：`knowbase/knowledge_lifecycle/governance.py:84` `apply_decision()`、
  `:94` `recompute_all()`；调用方：`reconcile.materialize_and_recompute()`
  （`reconcile.py:80`）或 worker 新增 handler。
- **事实**（E2）：`apply_decision` / `recompute_all` 的唯一调用方是
  `reconcile.materialize_and_recompute()`，而后者本身在 `knowbase/` 内零生产调用。
- **任务**：
  1. 选定触发点：证据导入后（W1-5 的 import_package 之后）+ L3 事件产生后（W1-3 之后）
     + 周期兜底重算（worker 任务，间隔待定，推荐与 evidence_exchange 同频或更低频）。
  2. 实现调用链：`materialize_and_recompute()` → `recompute_all()` →
     `apply_decision()`，幂等（同输入集合 digest 输出一致）。
  3. 治理字段落地不产生新 content revision（已有行为，保持）。
- **验收**：3 个独立任务的 L3 正证据到达后，卡自动从 once → verified（无需人工操作）；
  可信反证到达后自动 quarantine（status=stale + 原因，正文不被覆盖）；
  撤销后重算降回 validated（测试覆盖）。
- **依赖**：W1-2、W1-3（需要 L3 事件输入）；W1-5（跨机证据输入）。

### W1-5 接线 V2-13 签名证据包：export / import 的生产调用

- **目标**：让跨机治理证据真正流动起来。
- **位置**：`knowbase/knowledge_lifecycle/evidence.py` `export_package` / `import_package`；
  调用方待新增：worker handler（推荐，与 evidence_exchange 同频 900 秒）+ CLI 子命令
  （`lifecycle` CLI 目前只有 changes/show/validate/publish/rollback/admissions/confirm，
  无 export/import 入口）。
- **事实**（E2）：`export_package` / `import_package` 在 `knowbase/` 生产代码中零调用；
  无 CLI 子命令，无 worker handler。`reconcile.py:4` 的 docstring 提及 evidence 模块，
  但该文件代码本身未 import evidence。
- **任务**：
  1. 新增 worker 任务 kind（如 `lifecycle_evidence_exchange`），复用 evidence Git 分支
     机制（与 exchange_v2 的分支分离，或复用同一分支但不同包类型——需设计决策并记录）。
  2. 新增 CLI 子命令：`lifecycle evidence-export` / `lifecycle evidence-import`
     （手动触发与排障用）。
  3. 导入后触发 W1-4 的重算（接收机必须重算，同步来的 decision 只是可核验声明）。
  4. 密钥登记流程：Ed25519 设备密钥对生成（`trust.generate_keypair`）+
     信任登记模板 + ENROLLMENT.md（授权跟进报告已创建模板，需确认是否已分发）。
- **验收**：两台真实设备之间，一台的 L3 事件经签名包到达另一台并进入其 evidence_events，
  治理重算后独立任务计数正确；伪签发 / 未登记 issuer → 仅计数、不入库、零治理影响；
  撤销先到 → 原事件后到仍无效（测试覆盖 + 双机实测）。
- **依赖**：W1-4（导入后重算）。**前置于 W3-2。**

### W1-6 接线 admission → 发布链：staging 卡自动提案

- **状态：已完成（2026-11-05，第一批实施，手动路径 + 自动提案函数）**——新增 propose_new_card / propose_admitted + `lifecycle propose` / `propose-admitted` CLI；附带 publication 两处安全修复（SOURCE_MOVED 重验、staging 清理）；权威门仍成立。5 passed。剩余：W1-7 的 worker 调度（无任何 worker 任务调用 propose_admitted）。

- **目标**：staging 卡准入通过后，不依赖人工 CLI 就能进入发布流程。
- **位置**：
  - `knowbase/knowledge_lifecycle/admission.py:40` `evaluate_admission()`（生产调用方
    仅 `lifecycle confirm` CLI）；
  - `knowbase/knowledge_lifecycle/changes.py:71` `propose_change`（生产调用方仅
    `publication.rollback()`）；
  - 新增：自动提案方（worker 任务或 admission 通过后的回调）。
- **事实**（E2）：没有任何自动提案方把 staging 卡转为 NEW 变更候选。
  提案一步目前甚至没有 CLI 入口，只有 rollback 能建 change。
- **任务**：
  1. 新增 CLI 入口：`lifecycle propose`（手动提案用，补齐 `confirm → propose →
     validate → publish` 链路的手动路径）。
  2. 新增自动提案方：admission 通过的 staging 卡自动立 NEW 变更候选
     （base 为空、outputs 为该卡内容、claim 映射按整卡单 claim `C-whole` 处理，
     或按卡片已有结构化 claim 映射）。
  3. 提案后自动走 `validate_change` → `evaluate_admission` → `publish`，
     或进入待发布队列由 worker 任务批量处理（需设计决策：即时发布 vs 批量发布）。
  4. 发布后新版本 confidence 为 once（不继承 verified），进入使用后验证流程（循环 B）。
- **验收**：一张 staging 卡在 admission 通过后，无需人工操作即发布为正式可见内容；
  全过程有 manifest 与快照可查；失败时置 needs_review 不静默丢弃（测试覆盖 + 真实卡实测）。
- **依赖**：无。**前置于 S1 真实层。**

### W1-7 接线 shadow_observe：在线影子观测

- **目标**：让影子评测从「接口存在」变为「在线运行」。
- **位置**：`knowbase/retrieval_metrics.py:47` `shadow_observe()`；调用点待新增：
  `knowbase/index.py:445` `search()` 或 `consumption.py` / `delivery.py`。
- **事实**（E2）：函数存在且测试通过，但生产代码中无任何调用点，仅一项测试调用。
  `report()` 单列影子段、不计入 executed_queries 的隔离性已有保障。
- **任务**：在 `search()` 的 dense 尝试路径上调用 `shadow_observe`，记录诊断字段
  （level / reason / semantic_status / channels / vector_coverage / generation_id），
  origin=`'shadow_eval'`，`mode='shadow'`、`no_delivery=True`、`no_governance=True`。
- **验收**：在线检索产生 shadow 观测行；`report()` 的 shadow 段与 executed_queries 严格分离；
  影子观测不产生递送、不产生治理影响（测试覆盖）。
- **依赖**：无。**前置于 W3-4。**

### W1-8 确认并修复 redirect_ids 检索跟随（依赖 W0-4 结论）

- **状态：已完成（2026-11-05，第一批实施）**——W0-4 结论为「不跟随」；实现 redirect_targets / follow_redirects，接线 index.search 与 evolution_recall；wide_recall 刻意未接线（见能力缺口文档）。3 passed。

- **目标**：MERGE/SPLIT 后的旧卡重定向在检索中生效。
- **位置**：`knowbase/index.py:445` `search()`、`knowbase/extraction/recall.py:13`
  `wide_recall`、relations 邻接路。
- **任务**：若 W0-4 结论为「不跟随」，实现 redirect 跟随（旧卡命中时返回新卡或同时返回
  并标注 redirect）；若结论为「已跟随」，补充测试覆盖并关闭本任务。
- **验收**：MERGE 后搜索旧卡标题/关键词能到达新卡（或明确标注已重定向）；测试覆盖。
- **依赖**：W0-4。

---

## W2 阶段：内容演进闭环

> 本阶段补齐循环 A 缺失的「自动识别与自动生成」层。这是 V2 设计中最难的部分，
> 必须先 POC、再接线，绝不凭阈值自动合并。

### W2-1 八关系 POC：真实脱敏样本盲测

- **目标**：验证当前内网文本模型能否可靠识别八关系及 claim 回归。
- **位置**：POC 脚本（新建，`scripts/` 或 `tests/` 下独立目录，不进生产代码）；
  模型：现内网文本模型（`IntranetTextRunner`，`automation/text.py`）。
- **任务**：
  1. 构造 ≥80 对真实脱敏样本，每关系 ≥10 对，含正例与易混淆负例（如 ENRICH vs CORRECT、
     MERGE vs REPLACE 的边界 case）。
  2. 盲测：模型输出关系 + claim 映射，与人工标注对照，计算每关系的 precision/recall。
  3. 输出 POC 报告：通过 / 不通过 + 分关系数据 + 失败模式分析。
- **验收**：POC 报告完整；若通过（阈值由 POC 报告定，推荐每关系 precision ≥90%），
  则 W2-2 可开放自动提案；若不通过，则 W2-2 保持「候选召回 → LLM 判关系 → 确定性核验 →
  验证候选」的人工确认环节，不开放自动发布。
- **依赖**：需用户授权内网模型调用与真实脱敏样本。**未授权前不得执行。**
- **注意**：本任务不开放自动 ENRICH 或其他内容发布（审计修订 §6.2）。

### W2-2 自动关系判定与自动提案

- **目标**：新经验到达时自动判断关系、自动立案变更候选。
- **位置**：新增模块（推荐 `knowbase/knowledge_lifecycle/proposer.py`）；
  调用方：提炼提交后（`materialize.submit` 之后）或 worker 任务；
  判定输入：新材料 + `evolution_recall` 的候选集（含 relations 邻接）。
- **任务**：
  1. 实现判定流程：候选召回（V2-05）→ LLM 按 claim 与条件判关系（复用 W2-1 的 prompt，
     提示词位置 `executor.py:315` PROMPT_INSTRUCTIONS 需扩展或新建独立常量）→
     确定性核验（claim 保真、base heads 冻结、快照漂移）→ 立案 `propose_change`。
  2. 判定失败 / 低置信度 → 立 NO_CHANGE 或进 needs_review，不猜测关系。
  3. 自动提案的候选进入正常验证-准入-发布流水线（不绕过任何一环）。
- **验收**：W2-1 通过后，在隔离环境中对 20 组新材料运行自动提案，人工抽查关系判定
  准确率；错误提案可被验证环节拦截（测试覆盖）。
- **依赖**：W2-1（POC 通过）；W1-6（发布链路接线）。

### W2-3 MERGE/SPLIT 内容合成策略

- **目标**：明确 MERGE/SPLIT 的内容合成规则，不再完全依赖提案方自由发挥。
- **位置**：`knowbase/knowledge_lifecycle/publication.py:197` `_materialize`（通用机制，
  不动）；新增合成策略模块或在 proposer 中实现。
- **任务**：
  1. 定义合成规则：MERGE 时取哪些 claim、如何去重、冲突 claim 如何处理（进 CONFLICT
     还是按证据强度选）；SPLIT 时拆分边界如何划分、共享 claim 如何处理。
  2. 合成结果仍须通过 claim 保真校验（全部旧有效 claim 必须有映射）。
  3. 合成失败 → 提案置 needs_review，不产不完整输出。
- **验收**：S3 场景（3 卡 MERGE）的合成结果人工可接受；合成规则文档化；测试覆盖。
- **依赖**：W2-1、W2-2。

### W2-4 语义矛盾自动发现

- **目标**：发现两卡结论相反但文本不冲突的语义矛盾。
- **位置**：新增模块（推荐 `knowbase/knowledge_lifecycle/conflict_scan.py`）；
  调用方：worker 周期任务（低频，如每日一次）或新卡发布后触发。
- **任务**：
  1. 实现扫描：对新发布 / 新修订的卡，用演进召回找候选矛盾卡（relations 邻接 +
     词法 + 语义），LLM 判定是否矛盾。
  2. 发现矛盾 → 立 CONFLICT 提案（占位，state 直接 needs_review，不产内容），
     不自动选边。
  3. 误报处理：CONFLICT 提案可被后续验证裁决后另立 ENRICH/CORRECT/MERGE 候选。
- **验收**：在隔离环境中注入 10 对已知矛盾卡，检出率与误报率记录在案；
  误报不产生内容变更（测试覆盖）。
- **依赖**：W2-1（判定能力）。**本任务为 P1，不阻塞 W1/W3。**

---

## W3 阶段：真实验证

> 本阶段把全部 external_pending 逐项转为真实验收。每一项都必须有真实宿主 / 设备 /
> 模型 / 样本，隔离 fixture 不算通过。

### W3-1 L2 采用识别精度验收

- **目标**：≥60 独立人工标注 task-card 样本，L2 precision ≥95%。
- **位置**：`attribution.correlate`（`attribution.py:58`）；样本集（新建，需人工标注）。
- **任务**：收集 ≥60 个真实任务的（任务日志、递送卡、人工标注的采用关系）样本；
  运行 correlate，对照人工标注计算 precision/recall；输出验收报告。
- **验收**：precision ≥95%；失败模式分析完整；若不通过，调整 `GENERIC_COMMANDS`、
  动作签名提取或歧义规则，重新验收。
- **依赖**：W1-1、W1-2、W1-3。

### W3-2 两台真实设备 Git 分支交换与 ACK

- **目标**：V2-13 签名证据包在两台真实设备之间完整行程。
- **位置**：`evidence.export_package` / `import_package`；worker handler（W1-5）。
- **任务**：两台真实设备（用户本机 + 另一台）配置信任登记，交换签名包，
  验证：导入 2、重发零增量、伪签发零治理、撤销先到不复活、peer 治理重算正确。
- **验收**：双机实测报告（含时间戳、包 ID、ACK 记录）；与 V2-12/13/15 交付记录的
  隔离测试结论一致。
- **依赖**：W1-5。需用户提供第二台设备与授权。

### W3-3 真实宿主原生日志接线

- **目标**：Codex/Claude/Zcode/Trae/OpenCode 各一次真实原生任务的事件接线实测，
  ≥3 宿主 tool/result 配对准确率 100% 样本集。
- **位置**：W1-1 的接线点。
- **任务**：每个宿主跑一次真实任务，核对 native_events 的 tool_call/tool_result
  配对准确率；记录各宿主可提供的字段差异（工具调用 / 退出码 / 产物身份）。
- **验收**：≥3 宿主配对准确率 100%；各宿主字段差异记录在案；缺失字段的宿主
  明确标注「最多 L1/L2，不伪造 L3」。
- **依赖**：W1-1。

### W3-4 真实语义收益评测（freeze_report v2）

- **目标**：产出第一份可接受的冻结 v2 报告，或确认语义无收益、保持 L2。
- **位置**：`retrieval_evaluation.freeze_report`；`semantic_index.quality()`；
  样本：真实任务检索日志（需 ≥60 可判定样本，正/负各 ≥20）。
- **任务**：
  1. 收集真实检索样本（execution_derived 标注，正/负/irrelevant 分类，unknown 单列）。
  2. 运行 freeze_report，五门逐项判定；测量真实混合路径端到端 p95。
  3. 若通过，`quality()` 激活 dense，进入影子观测（W1-7）与在线对照；
     若不通过，记录阴性结论，保持 L2，不预设向量收益。
- **验收**：冻结报告文件 + 五门判定明细；或阴性结论报告。
- **依赖**：W1-7（影子观测）。需用户授权真实检索日志使用。

### W3-5 六场景真实层验收

- **目标**：S1～S6 的真实层逐项验收（见架构设计 §10）。
- **任务**：
  - S1：2 真实宿主 + 冻结代码副本回放。
  - S2：真实反例注入与评审。
  - S3：三实例真实并发 + 崩溃点回放。
  - S4：真实宿主日志 + 独立人工标注 ≥60 样本（与 W3-1 复用样本集）。
  - S5：两台真实设备、真实 Git 分支（与 W3-2 复用）。
  - S6：两个真实依赖环境副本回放。
- **验收**：每场景一份真实层验收记录；任一场景未通过不影响其他场景的状态；
  全部通过后 V2-16 标记「业务验收通过」。
- **依赖**：W1（全部接线）、W2-2（S1/S3 的自动提案）。

---

## W4 阶段：治理与清理

### W4-1 L4 的定义、判定条件与生产者

- **目标**：补齐 L4 的定义与实现，或明确决定不实现 L4。
- **位置**：待定（设计补充）。
- **事实**（E2）：`level='L4'` 在全仓库中无任何赋值点，仅在两处过滤条件中出现
  （`evidence.py` 导出过滤、`governance.py` 过滤）。L4 的定义、判定条件、生产者
  无法从代码确定。
- **任务**：设计决策——（a）定义 L4 为「同一结论由独立任务/独立环境复现」
  并实现生产者（如跨机同一 check_id 的独立复现事件）；或（b）明确 L4 暂不实现，
  治理只认 L3，直至 L4 有明确判定。无论选哪项，**不伪造 L4 事件**。
- **验收**：设计决策记录 +（若选 a）实现与测试。
- **依赖**：W1-5（跨机证据是 L4 的主要来源）。**本任务为 P1。**

### W4-2 material_budget_exceeded 拆分重试

- **目标**：超预算的提炼批次拆成更小的批再试，而不是整批失败。
- **位置**：`knowbase/automation/distillation.py:208-322`（`_one`）、`:229-230` 与
  `:244-245`（两个超预算抛点）。
- **事实**（E2）：单批材料 + 相似卡合计超 `max_input_chars`（默认 128000）即整批失败，
  失败路径为不可重试（`retry=False` → `jobs.fail(retryable=False)` → 直接 `failed`），
  无拆分重试分支。`material_budget_exceeded` 不在 `TRANSIENT_ERRORS` 中，
  `run_due()` 的自动恢复逻辑不适用。
- **任务**：实现超预算时按片段拆分重提（每批 ≤ max_input_chars，相似卡按需裁剪并记录），
  拆分后的子批独立结算；仍超预算的单片段进 needs_review 不静默丢弃。
- **验收**：构造超预算批次，验证拆分重提成功；单片段超预算进 needs_review；
  历史 64 条 material_budget_exceeded 任务可手动重试后按新逻辑处理（测试覆盖）。
- **依赖**：无。**本任务为 P1（积压根因修复）。**

### W4-3 V2-18 简化清单执行

- **目标**：在 V2-16/17 真实验收覆盖后，逐项执行简化清单。
- **位置**：见 V2-18 交付记录。
- **候选移除项**（16/17 未完成前不删代码，均保留只读兼容）：
  1. `versioned_feedback.py` legacy 两工具标签晋升段（V2-01 已默认关，开关可退）；
  2. `evolution.decide()` 的 trusted_promotion_enabled 过渡开关（V2-12 证据治理接管后移除，
     改为只消费 evidence_events）；
  3. `index._feedback_factor` 计数排序分支（默认已中性；保留回滚开关一轮）；
  4. read+completed 派生 helpful 的历史路径（数据只读保留）；
  5. `test_phase0/auto_evolution` 中依赖过渡开关的机制测试改为证据治理 fixture。
- **验收**：逐项执行，每项有独立提交与回归验证；全量回归通过。
- **依赖**：W3-5（V2-16 真实验收）与 W4-5（V2-17 生产迁移）。**未满足前不得执行。**

### W4-4 stale 非治理卡复验路径

- **目标**：给非 GOVERNED 的 stale 卡一条复验路径。
- **位置**：`knowbase/server.py:359-366`（`context_impl` 的 validation_candidates 查询，
  目前只查 staging GOVERNED/active）；`knowbase/versioned_feedback.py:148-167`（验证分支）。
- **事实**（E2）：`status=stale` 不在 validation_candidates SQL 中，非 GOVERNED stale 卡
  没有同等复验路径。
- **任务**：设计并实现 stale 卡复验路径（推荐：复用 validation_candidates 机制，
  扩展查询含 stale 卡，或新增独立的复验候选通道；复活仍走 ENRICH 修订发布，新版 once）。
- **验收**：一张非治理 stale 卡能通过复验路径复活；测试覆盖。
- **依赖**：W1-6（发布链路）。**本任务为 P1。**

### W4-5 V2-17 生产迁移（需授权）

- **目标**：在真实库上执行 V2 迁移与部署。
- **位置**：`docs/implementation/lifecycle-v2/V2-16_V2-18/delivery.md` §V2-17
  （部署准备 5 步 + 回滚方案）。
- **步骤**（未执行，需授权）：
  1. 备份真实 SQLite / 类型目录 / staging / spool / 配置（updates.backup 机制可用）；
  2. 真实库上 `legacy_import`（幂等可重入）+ 全量 doctor 校验；
  3. 固定 runtime 原地更新（`uv pip install --python ~/.knowbase/runtime/venv/bin/python`）
     后核对 installed_build_id = 源码 build（V2-02 已强制实测写入）；
  4. 宿主重连验证（sessions.stale → current）；worker 重启后 build_loaded 刷新；
  5. 观察期两周（V2-02 告警 + doctor automation_health）。
- **回滚**：迁移仅新增表/列（无数据改写）；回退 = DROP 新表 + 重建 automation_schedule
  （去 5 列）或整库恢复备份（updates 的 `rollback_would_overwrite_new_data` 门保留）。
- **注意**：真实库 schema 已被会话间接推进到 v31（见架构设计 §13.3「源码树即生产」风险）；
  v32～v34 尚未应用。迁移前必须在副本上完整演练（V2-17 已示范），并把「源码树即生产」
  列入部署评审。
- **验收**：5 步逐项打勾 + doctor 全绿 + 观察期无告警。
- **依赖**：用户明确授权。**未授权前不得执行。**

---

## 附录：任务状态跟踪

| 任务 | 状态 | 负责人 | 提交 / 备注 |
|---|---|---|---|
| W0-1 | 待开始 | | |
| W0-2 | 待开始 | | |
| W0-3 | 待开始 | | |
| W0-4 | 待开始 | | |
| W0-5 | 待授权 | | 需真实库只读访问 |
| W1-1 | 待开始 | | |
| W1-2 | 待开始 | | 依赖 W1-1 |
| W1-3 | 待开始 | | 依赖 W1-1 |
| W1-4 | 待开始 | | 依赖 W1-2、W1-3、W1-5 |
| W1-5 | 待开始 | | 依赖 W1-4；前置于 W3-2 |
| W1-6 | 待开始 | | |
| W1-7 | 待开始 | | |
| W1-8 | 待 W0-4 | | |
| W2-1 | 待授权 | | 需内网模型与脱敏样本授权 |
| W2-2 | 待 W2-1 | | |
| W2-3 | 待 W2-1 | | |
| W2-4 | 待 W2-1 | P1 | |
| W3-1 | 待 W1 | | |
| W3-2 | 待 W1-5 | | 需第二台设备 |
| W3-3 | 待 W1-1 | | |
| W3-4 | 待 W1-7 | | 需真实检索日志授权 |
| W3-5 | 待 W1、W2-2 | | |
| W4-1 | 待开始 | P1 | |
| W4-2 | 待开始 | P1 | |
| W4-3 | 待 W3-5、W4-5 | | 未满足前不删代码 |
| W4-4 | 待 W1-6 | P1 | |
| W4-5 | 待授权 | | 需用户明确授权 |

状态取值：待开始 / 进行中 / 待授权 / 待依赖 / 已完成 / 已阻塞（注明原因）。
