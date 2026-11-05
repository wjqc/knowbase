# knowbase Hook → 知识卡生成链路审计报告

- 审计基线：本地克隆 `~/workspace/knowbase-fix/knowbase-v2`，`git log -1` = `77209a2`（内容等于远端 main `e8ab132`），工作树干净
- 审计日期：2026-11-05
- 审计方式：只读代码审计，未改任何代码、文档、测试；每项结论标注「代码核实」或「推断」，核实不了的明确标注
- 对照设计：用户 2026-11-05 确定的两阶段分工（Hook 实时发现并生成知识卡；定期治理负责去重、合并、提炼、演进）与三引擎（Capture / Refinement / Governance）

## 执行摘要

1. **Hook 时不生成卡**：Hook 只做本地检索注入与材料入队；知识卡最早在 worker 提炼（会话结束后约 60 秒起）才出现，默认配置下整条链路是关闭的。
2. **会话被反复全量读取**：每次检查点都整文件重读、重解析 transcript；游标只防重复入队，不防重复解析。
3. **模型调用按批计费**：每批（≤32 片段）一次模型调用；同作业内有输入哈希缓存，但跨作业的相同内容会重复送模型。
4. **Hook 时无复用判断**：查重只在 worker 建批与提交时由模型做，且自动执行器被明文禁止 update/link_existing，重复只能走 review。
5. **定期治理没有提炼任务**：worker 八项任务中没有一项做跨卡去重、合并或精炼；治理投影函数存在但无任何触发器。
6. **三引擎现状**：Capture 有采集无成卡；Refinement 只有归档与冲突复验；Governance 有决策函数无触发器。
7. 最大的一致性冲突：新设计要求"Hook 阶段完成复用判断与成卡"，现状是"Hook 只入队，判断与成卡全在 worker 侧"。
8. 治理计分本身只认知识卡的证据事件，但 `recompute_all` 的写入会触及 R 卡与 staging 卡的 frontmatter。
9. 默认配置下 `inflight.enabled=False`、`automation.enabled=False`，且 V2-01 两道晋升门默认关闭——链路在默认配置下不工作。
10. 本报告只写审计结论，不写实施方案；缺口清单见末节。

---

## 一、知识卡到底在什么时刻生成？

### 结论

**代码核实：知识卡不在 Hook 时生成。** Hook 触发后只产生两样东西：一条本地检索注入（给宿主看的提示）和一个提炼任务（`jobs` 表里的一行）加材料片段（`job_spool`）。卡片文件第一次出现，是在 worker 提炼任务被领取、模型返回候选、提交校验通过之后。按默认调度，会话结束到成卡最快约 60 秒；积压或预算耗尽时无上界。

### 完整链路（按时间顺序）

**第 0 步：Hook 入口（无卡、无模型调用）**

| Hook 入口 | 位置 | 行为 |
|---|---|---|
| `cmd_session_start` | `knowbase/hooks.py:361` | 注入会话规则文本；inflight 开启时调 `host_events.handle(event="recover")` |
| `cmd_user_prompt` | `knowbase/hooks.py:374` | 本地检索注入（`user_prompt()`，`hooks.py:122`）；inflight 关闭时做中途 capture 检查 |
| `cmd_tool_event` | `knowbase/hooks.py:416` | 转调 `host_events.handle(event="tool-before"/"tool-after"/"tool-failure")` |
| `cmd_stop` | `knowbase/hooks.py:395` | inflight 开启 → `handle(event="stop")`；否则走 legacy `stop_event()`（`hooks.py:282`） |
| `cmd_before_compact` | `knowbase/hooks.py:435` | 转调 `handle(event="before-compact")` |

`host_events.handle()`（`knowbase/adapters/host_events.py:232`）每来一个事件只做三件事：记录观察（`context.execute`，`host_events.py:311,339`，本地检索 + 写 `context_events`）、W1-1 的 V2 观测落库（`host_events.py:298`，仅 tool 类事件）、以及**到检查点时**才调用 `capture_increment`。检查点条件（`host_events.py:346`）：事件是 `stop` / `before-compact`，或 `tool-after`/`tool-failure` 且自上次采集后新增工具事件数达到 `checkpoint_work_ops`（默认 6，`knowbase/config.py:91`），或距会话开始超过 `checkpoint_minutes`（默认 10 分钟）。`tool-before` 与 `user-prompt` 永远不直接触发采集。

**第 1 步：采集入队（只有材料，没有卡）**

`capture_increment()`（`host_events.py:142`）：读 transcript（或回退读 `context_events` 里的已持久化观察），经 `_is_self_event`（`host_events.py:57`，过滤 knowbase 自身工具调用）、`_drop_distiller_echo`（`host_events.py:90`，提炼会话的 assistant 回复不入队）、`hook_policy.filter_capture`（`knowbase/hook_policy.py`，确定性噪声过滤）三道过滤后，`materials.capture_payload()`（`knowbase/materials.py:67`）把事件切成 ≤4000 字符的片段（每个片段有 `text_hash`），再 `jobs.enqueue(KIND_CAPTURE)`（`knowbase/jobs.py:120`）入队。`server.capture_impl`（`knowbase/server.py:482`）返回给宿主的文案写明了这一点："尚未成卡"。legacy 路径 `hooks.py:293` 的 `_try_enqueue_capture` 同理，且只在"有 work 操作、无 memory_write"时才入队（`hooks.py:282`：宿主已自行存卡就不重复采集）。

**第 2 步：worker 提炼（卡片第一次出现在这里）**

worker 常驻循环每 5 秒 tick（`knowbase/automation/worker.py`，`tick_seconds: 5.0`），`distillation` 任务每 60 秒到期一次（`TASK_DEFS`，`worker.py:26-35`）。`run_due()`（`knowbase/automation/distillation.py:323`）每次最多取 2 个到期任务（`max_batches` 默认 2），`_one()`（`distillation.py:208`）走完：领取 → 建批（≤32 个待处理片段，`materials.create_batch`，`materials.py:135`）→ 分页读材料（每页 8000 字符）→ 一次模型调用 → `materialize.submit()`（`knowbase/extraction/materialize.py:101`）校验 → `_apply_candidate()`（`materialize.py:201`）→ `writer.create/update` 或 `link_existing`。**卡片文件（`.md`）只在 `_apply_candidate` 里由 `writer.create` 写出时才第一次存在。** 受治理类型（`standard` / `preference` / `bizrule`）落 staging 目录，其余类型落正式目录、confidence 为 `once`（`knowbase/commit_adapter.py:183`）。

**第 3 步：staging → 发布（纯 CLI，无自动调度）**

staging 卡需经过 `admission.evaluate_admission`（`knowbase/knowledge_lifecycle/admission.py:40`，结构/来源/权威门）、`changes.propose_new_card`（W1-6，`changes.py:173`）、`validation.validate_change`、`publication.publish`。入口只有 CLI（`lifecycle confirm / propose / propose-admitted / publish`，`knowbase/__main__.py:1135-1141`）。`propose_admitted`（`changes.py:198`）没有任何 worker 任务调用（代码核实：grep 仅命中 `__main__.py:1497` 的 CLI 调用）。

**另一条路：宿主直接存卡（Path A 的现实形态）**

宿主 Agent 可直接调 MCP `memory_save`（`server.py:107` → `writer.create`），卡片立即同步生成；或领取提炼任务自己当执行器（`memory_job_claim` / `memory_job_submit`，`server.py:555/745`），在提交时成卡。但这两条路都没有"统一 Skill/协议"——`memory_save` 是自由格式的 type/title/body 直接写入，不走任何结构化候选校验之外的知识识别；`memory_job_*` 是执行器协议，不是面向宿主主动上报知识的 Skill。**代码核实：仓库内不存在宿主按统一协议主动提交结构化知识候选的入口。**

**默认配置下链路不工作（代码核实）**

`config.py` 默认值：`inflight.enabled=False`（`config.py:89-100`），`automation.enabled=False`、`executors=[]`、`allowed_scopes=[]`（`config.py:102`）。`handle()` 在 inflight 关闭时直接返回 `{"status": "disabled"}`；`run_due` 在无执行器时返回 `distillation_blocked_no_executor`（`worker.py:370-376`）。即：默认配置下 Hook 只剩 legacy 采集入队，且入队的任务永远等不到提炼。

---

## 二、是否反复读取会话？

### 结论

**代码核实：是，反复全量读取。** 游标与内容哈希防止的是"重复入队、重复提炼"，不是"重复读取"。每次检查点、每次 Stop、每次中途检查都会把整个 transcript 文件从头读一遍、逐行重解析一遍。

### 证据

1. **`parse_transcript_auto` 每次全量读文件**（`knowbase/adapters/normalizer.py:181`）：第 197 行 `content = f.read()` 一次性读入整个文件，strict 模式下逐行 `json.loads` 校验全部行，再逐行 `normalize_events` 归一化。`capture_increment`（`host_events.py:142`）每次到检查点都调它。游标（`_hook_capture.cursor` + `prefix_hash`，`host_events.py:162-166`）只决定"从第几个事件开始取增量"，不减少解析量——文件越大，每次检查点的解析成本越高。10 MB 上限（`host_events.py:144-146`）是唯一护栏。
2. **`_transcript_stats` 每次全文件扫描**（`knowbase/hooks.py:212-231`）：逐行找 `"tool_use"` 子串计数。legacy 路径每次 Stop（`stop_event`，`hooks.py:282`）和每次 user-prompt 中途检查（`_maybe_trigger_mid_session_capture`，`hooks.py:249`，inflight 关闭时）都调一次。无增量状态，每次都是 O（文件大小）。
3. **提炼侧重复读 spool**：`materials.payload_for_job`（`materials.py:119`）每次从 `job_spool` 重读整份 `input_json` 并做校验和；`manifest_for_job`（`materials.py:129`）每次重解析。`_one` 分页时每个 `page()` 调用都会重新走 `validate_batch` + `payload_for_job` + `manifest_for_job`（`materials.py:190-237`）。同一批的多次分页、同一任务的多次领取都会重复读整份材料。
4. **相似快照算两次**：`similar_snapshot` 在建批时算一次（`materials.create_batch`，`materials.py:135`），提交时为保新鲜度**刻意重算一次**（`materialize.submit`，`materialize.py:133-141`，注释说明 2026-11-04 曾改用存储值后被测试否决）。这是有意为之的正确性换成本，但属于重复检索。

### 已有的去重/水位机制（只防重复入队与重复提炼，不防重复读取）

- 会话内游标：`context_sessions.coverage_json._hook_capture`（cursor + prefix_hash），`host_events.py:128` 写检查点；`_settle_session_cursor`（`materialize.py:15`）按连续结算段推进 `settled_cursor`。
- 内容级去重：`_enqueued_text_hashes`（`host_events.py:42`），同一会话已入队片段的 `text_hash` 集合；新 capture 的 payload 会滤掉已入队片段（`host_events.py:186-201`）。
- 幂等键：capture 三类键——inflight `capture:{host}:{session}:{scope}:{cursor}:{input_version}`（`host_events.py:207`）、显式 capture `capture:{session}:{scope}:{input_version}`（`server.py:482` 附近）、legacy hook `capture:legacy-hook:{session}:{scope}:{input_version}`（`hooks.py:319`）；`jobs.enqueue` 同键同内容返回既有任务、不同内容报 `IDEMPOTENCY_CONFLICT`（`jobs.py:120-165`）。
- 段级结算水位：`materials.contiguous_cursor`（`materials.py:244`）只统计连续终态段；`create_batch` 只取未结算段（`materials.py:135-156`），已结算段永不重提。
- 导入去重：`import_impl` 按文件用键 `extract:{scope}:{file}:{input_version}`（`server.py:932`），未改动的文件重导入命中既有任务。
- 背压：`max_pending_jobs` 默认 200，触顶返回 `BACKLOG_FULL` 并记 `capture_gaps` 可补采游标（`jobs.py:143-160`），不静默丢弃。

### 推断（未在代码中核实）

- 长会话下每次检查点的全量重解析是否已成为可观测的性能问题：**无法从代码核实**，需真实会话的耗时数据。代码只证明"每次都全量解析"，不证明"这已经慢到不可接受"。

---

## 三、是否重复调用模型？

### 结论

**代码核实：模型调用点只有一个（按批），但相同内容可以在三处被重复送模型：跨作业、跨领取、探针复验。** Hook 路径本身零模型调用。

### 调用点

| 调用点 | 位置 | 粒度 |
|---|---|---|
| 后台提炼 `_one` → `execute_text` | `knowbase/automation/distillation.py:208-290` → `knowbase/automation/execution.py:18` | 每批一次 |
| 执行器复验探针 `isolated_probe` | `distillation.py:323`（`run_due` 在回执需刷新时调用） | 每次复验一次（≥32 KiB 隔离材料） |
| 宿主自行领取提炼（MCP `memory_job_claim`） | `knowbase/server.py:555` | 宿主自己的模型调用，knowbase 不计数、不控预算 |

`execute_text` 把整批材料拼成一个请求发给执行器：命令式走子进程 stdin 一次性传入 JSON（`knowbase/automation/executor.py:364-384`），内网文本式走一次 `/chat/completions` 调用、整份 payload 作单条 user 消息（`knowbase/automation/text.py:20-44`，temperature 0，`response_format: json_object`）。即：**一批 = 一次模型调用**，输入为该批全部片段文本（默认上限 128000 字符，`config.py:105`）+ 建批时的相似卡（`[RELATED_KNOWLEDGE_DATA]`，`distillation.py:240-244`）。Hook 入口（`hooks.py`、`host_events.py`）全文无任何模型调用——检索是本地 FTS（`index.search`），过滤是确定性规则（`hook_policy.filter_capture`）。

### 幂等与预算（代码核实）

- **作业内缓存**：`executor_runs` 按 `input_hash = digest([job_id, input_version, segments, related, config_hash])`（`distillation.py:246`）查已持久化的响应；命中则直接复用，不调用模型、不扣预算；响应在提交前已持久化（"After a crash, reuse the response rather than calling again"，`distillation.py:271`）。
- **预算硬 cap**：日调用 `daily_calls` 默认 20、日输入字符 `daily_input_chars` 默认 512000（`config.py:106`），`_reserve`（`distillation.py:173`）在调用前原子扣减；超限进 `retry_wait` 到次日零点（`distillation.py:183-195`）。单批输入上限 128000 字符、输出上限 64000 字符。
- **重试**：`jobs.fail`（`jobs.py:511`）指数退避（5×2^attempt 秒，上限 300 秒），`max_attempts` 默认 3；`TRANSIENT_ERRORS`（`receipts.py:10-11`：timeout、rate_limited、process_exited、executor_error、auth 类）可重试；`material_budget_exceeded` 不可重试（`distillation.py:190`，`retry=False`），直接进 `failed`。`run_due` 对 transient 失败的任务在执行器复验通过后自动改回 `awaiting_agent`（`distillation.py:323`）。
- **提交幂等**：`materialize.submit` 按 `(job_id, request_id)` + `payload_hash` 重放（`materialize.py:101-112`）；相同 request 不同内容报 `IDEMPOTENCY_CONFLICT`；`jobs.submit`（legacy 路径）按 `request_id` 判 `duplicate_request`（`jobs.py:357`）。

### 会重复送模型的三种情形（代码核实）

1. **跨作业**：`input_hash` 含 `job_id`（`distillation.py:246`），同一内容分属两个作业时缓存永不命中。同一会话内容若因 `input_version` 变化被拆进多个 capture 任务，每个任务都会单独调模型（段级 `text_hash` 去重只保证同一段不进两个任务，不保证模型只看一次）。
2. **跨领取**：租约过期换执行器接管、或重试后重新领取时，`create_batch` 重新建批（新 `batch_id`、重算相似快照），新执行器重新分页读材料、重新调模型。`materials.page` 明确允许重复读取旧页（`materials.py:222` 注释）。
3. **探针复验**：`run_due` 在执行器回执需刷新时调 `isolated_probe`（`distillation.py:323`），每次复验都是一次完整模型调用，与提炼调用分开计数。

---

## 四、Hook/捕获时是否已有「复用或更新已有卡」的机制？

### 结论

**代码核实：Hook/捕获时完全没有。复用判断只在 worker 建批与提交时发生，且决策者是模型；自动执行器被明文禁止 update/link_existing，重复只能进 review。** 唯一在写入时生效的查重是标题相似度（0.82），命中即报错，不自动更新、不自动链接。

### 分环节核查

**Hook 与捕获阶段：零查重。**

`capture_increment`（`host_events.py:142`）与 `_try_enqueue_capture`（`hooks.py:293`）的过滤只有三类：自身调用、提炼会话回声、确定性噪声（`hook_policy.filter_capture`）。没有任何一步检索已有知识卡、没有任何相似度计算。`capture_payload`（`materials.py:67`）只切分、不比对。`server.capture_impl`（`server.py:482`）同样只入队。

**建批阶段：相似卡只作为模型的参考输入。**

`materials.create_batch`（`materials.py:135`）调 `similar_snapshot`（`materials.py:162`）：用"每片段前 200 字符拼接、截断到 2000 字符"作查询，经 `extraction.recall.wide_recall`（`knowbase/extraction/recall.py:13`，FTS + 中文短词 + 标题 + code_refs 四路，默认含 staging）取前 10 张卡，附在批次上。模型在 `_one` 里收到 `[RELATED_KNOWLEDGE_DATA]`（`distillation.py:241`）。注意：`wide_recall` 的 `_all_ids`（`recall.py:105`）**没有类型过滤**——R 卡（reference）与 staging 卡都可能出现在相似列表里，而默认检索 `index.search` 是排除 R 卡的（`knowbase/index.py:482`，`type != 'reference'`）。即相似快照的覆盖面比用户日常检索更宽。

**提交阶段：模型必须逐张评估，否则拒绝提交。**

`materialize.submit`（`materialize.py:101-156`）在提交时**重算**相似快照，并强制要求每个候选的 `duplicate_evaluation` 满足三点：`snapshot_hash` 与现算快照一致、`rationale` 非空、已评估卡覆盖全部相似卡且 revision 一致；否则返回 `NEEDS_REASSESSMENT`。候选的 `action` 只能是 `create` / `update` / `link_existing` / `review`（`materialize.py:52-53`）。

**但自动执行器的动作被锁死为 create/review。**

`executor.py:322-323` 的 `PROMPT_INSTRUCTIONS` 明文规定："执行器只使用 create 与 review 两个动作，禁止 update/link_existing（它们要求目标卡当前的 content/governance 双版本号，执行器无法可靠持有）"。这是 1fa0b05 提交（"executors create/review only"）引入的约束。后果：

- 模型判断为"重复"时，唯一合法动作是 `review` → 候选进 `needs_review`（`materialize.py:201`，`_apply_candidate` 对 `review` 直接返回 `needs_review`，不成卡），等人或宿主 Agent 用 `resubmit_review`（`materialize.py:340`，须换新 `candidate_key`、action 不得为 review、segment_ids 须与原候选一致）重提交。
- 模型判断为"补充"时，自动路径同样无法 update，只能 create 新卡或 review——"补充"语义在自动路径上没有出口。

**宿主 Agent 当执行器时，update/link_existing 才可用。**

MCP `memory_job_claim` 的 v1 路径给宿主的提炼指引（`_build_distillation_guide`，`server.py:701-724`）明确写着："相同规则用 link_existing 附 target_id/expected_content_revision；补充用 update 附 content/governance 两种期望 revision"。即复用/更新能力只存在于"宿主 Agent 亲自领取提炼任务"这条路上，且依赖宿主先 `memory_read` 逐张核对相似卡。后台 worker 走 `execute_text` 时用的是另一份指令（`PROMPT_INSTRUCTIONS`），不含这条。

**写入层最后防线：标题查重，命中即失败。**

`commit_adapter.adapter_create`（`knowbase/commit_adapter.py:198`）在写锁内调 `store.find_similar`（`knowbase/store.py:304`：归一化标题 + SequenceMatcher，阈值 0.82，scope 过滤，含 staging）；命中即返回 `{"error": "DUPLICATE", "existing_id": ...}`，不写文件、不更新旧卡、不自动链接。调用方（提炼提交或 `memory_save`）拿到的是一个错误。注意这是**标题级**查重，不是内容级——正文相同但标题不同时，此处不拦截；内容级去重只依赖模型在提交时的 `duplicate_evaluation`。

**`propose_admitted`（W1-6）与 Hook 时复用无关（代码核实）。**

`propose_admitted`（`knowbase/knowledge_lifecycle/changes.py:198-260`）扫描的是 `admission.pending_admissions`（staging 卡 + stale 复验候选），对每张 staging 卡立 NEW 变更候选并做准入评估；其路径（`propose_new_card → evaluate_admission → [publish 时] validate_change → publish`）中**没有任何一步做相似度比较或查重**。它处理的是"已经成卡的 staging 卡如何发布"，不是"Hook 时要不要复用旧卡"。两者无交集。

---

## 五、定期治理/worker 是否真正只针对知识卡运行？

### 结论

**代码核实：worker 的八项任务里没有一项是"针对知识卡做去重、合并、提炼、演进"。** 唯一生产知识卡的任务是 `distillation`（新建卡）；其余任务分属同步、归档、冲突复验、向量维护、统计交换。治理投影函数存在，但没有任何 worker 任务调用。

### worker TASK_DEFS 全表（`knowbase/automation/worker.py:26-35`，handler 在 `worker.py:279-376`）

| 任务 | 间隔 | 操作对象 | 是否触及知识卡 |
|---|---|---|---|
| `lease_recovery` | 60s | jobs 租约、write intents、未完成提交、Hook 恢复 | 间接触及：`materialize.recover` 会完成中断的提交（即把已提炼的候选落卡），但这是收尾，不是治理 |
| `knowledge_sync` | 60s | git 同步（`gitops.sync_before_read`） | 否（仓库级） |
| `evidence_exchange` | 900s | 价值统计事件交换（`value.exchange_v2`） | 否；且 `evidence_role` 默认 off，默认跳过 |
| `team_sync` | 900s | 团队统计服务（`team.service.run`） | 否；默认 team 未启用则跳过 |
| `archive_sweep` | 24h | stale 卡 → archived（满 90 天无成功证据）；`needs_review` 候选超 30 天丢弃 | 是，但只做归档/丢弃，不做合并提炼 |
| `conflict_recheck` | 24h | `card_conflicts` 中 open 的冲突，最多 20 条，重裁 | 是，但只做冲突重裁（`quarantine.readjudicate_card`，`knowbase/quarantine.py:270`） |
| `vector_maintenance` | 30s | 语义索引生成/激活（`retrieval_state.maintenance`） | 否（只动索引）；且语义清单明确排除 R 卡与 staging 卡（`semantic_index.manifest`，`knowbase/semantic_index.py:27-39`：`status='active' AND staging=0 AND type!='reference'`） |
| `distillation` | 60s | kind 为 `capture`/`extract` 的到期任务 → 模型 → 新卡 | 是，唯一生产卡的任务；只新建（或 review），不合并已有卡 |

### 治理门的计分范围与调用情况（代码核实）

- **计分范围是知识卡的证据事件**：`governance.decide()`（`knowbase/knowledge_lifecycle/governance.py:72-109`）只读 `evidence_events` 里 `trust_basis=registered-adapter` 的 L3/L4 事件，按 `logical_task_key` 去重；阈值 ≥3 独立任务且 ≥2 独立组晋升 verified，任一未裁决负向触发隔离。L0/L1/L2 永远零票。`apply_decision()`（`governance.py:110-152`）把投影写回卡片 frontmatter（confidence、quality_state、`evidence_set_digest`）。
- **但没有任何触发器**：grep 全库，`apply_decision` / `recompute_all` 的生产调用方为零；唯一调用 `recompute_all` 的是 `reconcile.py:84`（证据导入的 reconcile 路径），不是 worker 任务、不是证据到达时的回调。`attribution.correlate` / `verify_execution` 同样零调用方（grep 全库无命中）。
- **晋升出口双重关闭**：`config.py:55-56`，`legacy_promotion_enabled=False`、`trusted_promotion_enabled=False`（V2-01 门默认关闭）；`governance.py:108` 注释"verified 晋升从本函数走（V2-01 已关反馈旁路）"——旧反馈旁路已关，唯一晋升函数又无调用方。
- **`recompute_all` 的写入范围超出知识卡**：`recompute_all`（`governance.py:153-157`）用 `store.iter_all(repo, include_staging=True)` 遍历全部目录（`ALL_DIRS`，`knowbase/store.py:26`，含 `references` 与 `staging`），并对每张卡调 `apply_decision`；`apply_decision` 无条件写 `quality_state` / `evidence_set_digest` / `policy_revision` 并回写文件与索引。**代码核实：R 卡没有证据事件，`decide()` 对其恒为 `unverified`，但仍会被写入这三个治理字段。** 计分本身不认 R 卡，写入却不排除 R 卡。
- **`archive_sweep` 同样无类型过滤**：`auto_archive_sweep`（`knowbase/lifecycle.py:91-131`）遍历 `iter_all` 默认集（含 `references`、不含 staging），只按 `status == "stale"` + 90 天 + 无成功证据 + 无 `archive_protection` 判定；**代码核实：一条 stale 的 R 卡满足条件时会被归档。**（推断：R 卡很少会是 stale 状态，实际触发概率低，但代码层面无排除。）
- **`KIND_EMBED`（`embedding_refresh`）不属于知识提炼**：由 `semantic_index.py:170` 入队，操作对象是语义索引的生成（manifest 仅 active / 非 staging / 非 R 卡）；`jobs.claim` 明确拒绝 Agent 领取此类任务（`jobs.py:213`，`KIND_EMBED → NO_WORK`）。它不读、不写知识卡内容。

---

## 六、三引擎现状对照表

| 引擎 | 今天已有什么（代码核实） | 缺什么 | 与新设计冲突处 |
|---|---|---|---|
| **Knowledge Capture**（事件过滤、知识识别、实时成卡、增量更新，Hook 触发） | Hook 入口齐全（`hooks.py:361-443`）；确定性噪声过滤（`hook_policy.py`）；增量游标 + 内容哈希去重 + 幂等键（`host_events.py:42,128`；`jobs.py:120`）；显式采集入口 `capture_impl`（`server.py:482`）；宿主直接存卡 `memory_save`（`server.py:107`） | Hook 时不识别知识、不查重、不成卡；无"统一 Skill/协议"供宿主主动提交结构化候选；无 Hook 内轻量队列（队列就是 SQLite 的 `jobs` 表）；无增量更新路径（自动执行器禁 update/link_existing） | **核心冲突**：新设计要求 Hook 阶段完成"检索已有卡 → 判断重复/补充/全新 → 新卡创建或已有卡增量更新"；现状是 Hook 只入队材料，判断与成卡全在 worker 侧，且自动执行器不能更新旧卡 |
| **Knowledge Refinement**（去重、合并、精炼、冲突处理、演进，定期或触发） | 冲突复验（`conflict_recheck`，每日，最多 20 条）；自动归档（`archive_sweep`，stale 满 90 天）；`needs_review` 候选 30 天丢弃；变更机制 `propose_change` + `publish` 存在（MERGE/SPLIT/REPLACE）；矛盾裁决打平双降、不自动选边（`writer.py:230`） | 无任何定期去重/合并/精炼任务；无"变更检测 → 候选聚类 → 组内比较 → 模型提炼"管线；无关联提炼（高层组合）与质量完善任务；八关系自动识别/生成全部缺失（`propose_change` 唯一生产调用方是 `rollback`，`publication.py:336`）；合并时证据重算无生产逻辑（`claim_map` 只有结构）；`propose_admitted` 无 worker 调度 | 新设计要求定期提炼处理四类情况并保留版本/引用/合并映射；现状是定期任务只有归档与冲突复验，合并能力只存在于未接线的变更机制里 |
| **Knowledge Governance**（可信证据、任务独立性、晋升、隔离、失效判定，证据变化时及定期复核） | `decide()` 确定性决策（≥3 任务、≥2 组、负向隔离，`governance.py:72`）；`apply_decision` 落地（晋升/降权/隔离，`governance.py:110`）；版本绑定证据（`evolution.evidence_for_revision`，`knowbase/evolution.py:46`）；修订即降权（`writer.py:86` / `commit_adapter.py`）；准入门（`admission.py:40`）；隔离与矛盾裁决已打通 | `apply_decision` / `recompute_all` 无任何触发器（无 worker 任务、无证据到达回调；唯一调用方是证据导入的 `reconcile.py:84`）；`correlate` / `verify_execution` 零调用方，L0–L3 证据无法自动产生；证据包交换无 worker/CLI 调用方；V2-01 两道晋升门默认关闭 | 新设计要求"证据变化时及定期复核"触发治理；现状是治理投影只有函数、没有触发器，证据产生、治理决策、状态落地三段全断 |

---

## 七、缺口清单（按优先级）

按用户既定优先级排序（准确性与证据可信度第一；其次性能、维护性与成本）。只列审计确认的缺口，不含实施方案。

### P0——与新设计直接冲突，成卡主链路不成立

1. **Hook 时无知识识别与成卡**：Hook 只入队材料，卡片最早在 worker 提炼后出现（默认约 60 秒起，默认配置下链路关闭）。新设计要求 Hook 阶段完成知识识别与成卡，现状与之相反。
2. **Hook 时无复用/更新判断**：捕获阶段零查重；重复判断只在 worker 建批与提交时由模型做，且自动执行器被禁 update/link_existing。长期运行下重复卡只能靠标题查重报错（`DUPLICATE`）或进 review，没有自动复用出口。
3. **治理无触发器**：`apply_decision` / `recompute_all` 无 worker 任务、无证据到达回调；`decide()` 的晋升出口因 V2-01 门默认关闭且无调用方而实际不通。证据产生（`correlate` / `verify_execution` 零调用方）同样断开。

### P1——定期提炼能力缺失

4. **无定期去重/合并/精炼任务**：worker 八项任务中没有任何一项做跨卡比较、合并、归纳或质量完善；"变更检测 → 候选聚类 → 组内比较 → 模型提炼"管线不存在。
5. **八关系自动识别/生成缺失**：`propose_change` 唯一生产调用方是 `rollback`；MERGE/SPLIT 的自动识别与生成无入口。
6. **合并证据语义未实现**：`claim_map` 只有结构字段；合并时按原始任务身份、结论一致性、验证对象一致性重算可计证据的逻辑不存在；`publication.publish` 对新版本置 `once`（`publication.py:244`，不继承 verified）这一点符合约束，但只是单点正确，不是合并证据的重算。
7. **`propose_admitted` 无调度**：staging 卡的提案→发布链只有 CLI 入口，没有任何 worker 任务调用；staging 首次激活仍依赖人工跑命令。

### P2——正确性边界与范围泄漏

8. **治理投影写入超出知识卡**：`recompute_all` 遍历含 `references` 与 `staging` 的全部目录，`apply_decision` 会给 R 卡写入 `quality_state` / `evidence_set_digest` / `policy_revision`；`archive_sweep` 同样可能归档 stale 的 R 卡。计分不认 R 卡，但写入不排除 R 卡。
9. **相似快照覆盖面宽于默认检索**：`wide_recall._all_ids` 无类型过滤，R 卡与 staging 卡可进入建批相似列表；默认检索 `index.search` 则排除 R 卡（`index.py:482`）。执行器可能基于用户日常看不到的卡做重复判断。
10. **无统一 Skill/协议**：宿主主动提交结构化知识候选的协议不存在；现有两条路（`memory_save` 直接写、`memory_job_*` 执行器协议）都不承担"宿主识别、Knowbase 校验查重成卡"的分工。Path A 在设计层面是空白。

### P3——成本与重复劳动

11. **会话反复全量读取**：每次检查点整文件重读、重解析 transcript（`normalizer.py:197` 的 `f.read()`）；每次 Stop 与中途检查全文件扫描（`hooks.py:212`）；提炼分页时每次重读整份 spool（`materials.py:119`）。游标只防重复入队，不防重复解析。
12. **相同内容可重复送模型**：执行器响应缓存的键含 `job_id`（`distillation.py:246`），跨作业不命中；租约过期换执行器、重试重领都会重建批、重调模型；执行器复验探针是额外的独立模型调用。
13. **相似快照算两次**：建批与提交各算一次 `similar_snapshot`（提交侧重算是有意为之的新鲜度门，正确性优先，但成本翻倍）。

### 待用户裁决（审计中发现、需用户定夺，非代码问题）

- W0-5 / W2-1 / W4-5 三项授权仍未决（真实库积压复核、内网模型八关系 POC、V2-17 生产迁移），与本审计无关，列此备忘。
- `~/.knowbase/config.json` automation/embedding/retrieval 三节 11:34:37 被清空一事：【增量复核勘误：配置已于当日 16:05 由并行会话恢复，第二批的缺口文档勘误已确认 automation 全节 + intranet-deepseek 执行器 + team 提交身份均在。】

---

## 八、增量复核（2026-11-05，第二批 `5d064ad`）

- 复核基线：远端 main `5d064ad`（"feat: lifecycle w1 wiring batch 2 — correlate/verify/recompute/evidence CLI/propose scheduling"，2026-11-05 17:19 推送），本地克隆已同步至该提交；复核方式：读 `e8ab132..5d064ad` diff + 沙箱复跑 5 个新测试文件 **16 passed**（`/tmp/kb-test-venv` 曾被清空，已重建）。
- 本节只裁决受第二批影响的结论；Hook 侧（§一～§四、P0-1、P0-2、P3-11/12/13）第二批未碰 `hooks.py`、`host_events.py`、`materials.py`、`distillation.py`、`executor.py`、`extraction/materialize.py`、`index.py`、`store.py`，**全部维持**。

| 原缺口 | 复核裁决 |
|---|---|
| P0-3 治理无触发器 | **已闭合（部分）**：worker 新增 `lifecycle_correlate`（300s，默认开）、`lifecycle_recompute`（3600s，默认开）、`lifecycle_propose`（900s，默认关）；`correlate` / `verify_execution` / `recompute_all` / 证据包 export-import 均已有调用方（worker handler 或 CLI）。但 **verified 晋升仍不可达**：V2-01 两门默认仍关（`config.py:55-56`），且生产观测任务键诚实保留 `unknown:` 前缀、不计入独立性；打通的是 L3 反证门（trusted fail → 自动 quarantine，已有测试）。晋升解锁待任务身份设计（V2-09 未决项） |
| P1-7 `propose_admitted` 无调度 | **已闭合（带保留）**：worker `lifecycle_propose` 默认关，`publish` 二级门默认关——staging 首次激活的人工关卡节奏保留，与自进化定义之间的张力未决，需用户定夺何时放开 |
| W1-3 `verify_execution` 无 validator | **已闭合**：`knowledge_lifecycle/validators.py`（pytest-text 解析器，只做确定性文本解析，不执行命令）+ DEFAULTS 默认注册 + CLI `lifecycle verify --memory-id --report-file [--run-id] [--claim-map]` |
| W1-5 证据包无 CLI | **已闭合（CLI 侧）**：`lifecycle keygen / evidence-export / evidence-import`；import 后经 `materialize_and_recompute` 显式链接；未信/伪签零治理影响。仍缺：export 无增量水位；worker 自动导出未做（exchange_v2 vs V2-13 统一待用户决策） |
| W1-4 前置：`apply_decision` 每轮全量重写 | **已修复**：投影字段条件写入，无变化零写盘（调度无 git churn） |
| P2-8 治理投影写入超出知识卡 | **部分缓解，未根除**：条件写入消除了每轮重复 churn，但 `recompute_all` 仍遍历全部目录（含 `references` 与 `staging`，`governance.py:160-164`），R 卡首次重算仍会被写入 `quality_state` / `evidence_set_digest` / `policy_revision`（字段缺失时 `None ≠ 现值` 即触发写入）；`archive_sweep` 同样未加类型过滤 |
| P2-9 相似快照覆盖面宽于默认检索 | **未变，范围扩大确认**：第二批交付记录新增确认——`server.py:616`（job 相似卡）与 `materials.py:163`（提炼素材召回）两处直调同样不跟随重定向，需与 `evolution_recall` 底座一并做语义决策（替换 vs 追加标注） |
| 测试隔离泄漏 | **根因已定位并修复**：宿主 `CLAUDE_PROJECT_DIR` / `ZCODE_PROJECT_DIR` 经 `_infer_scope` 泄漏造成随宿主漂移的假失败；conftest autouse 屏蔽后本机 8→1（余 1 项 `test_program_updates_integration` 为本机更新器子进程既有故障）。**勘误**：第一批报告与本审计初版所称「5 个基线既有失败」实为沙箱环境性漂移——在本机单跑与全量均不复现，不是代码基线失败 |
| W4-5 生产迁移 | **勘误**：真实库 `schema_migrations` 已至 **v34**——v32–v34 已随 7e6f8cc 运行时应用，迁移已完成，余下只读核验迁移结果即可 |
| 配置清空（11:34:37） | **勘误**：配置已于当日 16:05 由并行会话恢复（automation 全节 + intranet-deepseek 执行器 + team 提交身份均在；`max_input_chars=128k` 仍为默认值，64 条 material_budget 积压根因未变） |

### 修订后的缺口状态（第二批后）

- P0 剩余 2 项：Hook 无识别成卡、Hook 无复用判断（治理触发器已闭合）。
- P1 剩余 3 项：无定期提炼任务、八关系缺失、合并证据重算缺失（propose 调度已闭合，带保留）。
- 新增确认项：**verified 晋升依赖任务身份设计**（V2-09 未决）；wide_recall 重定向语义待决策（3 处直调一并收口）；exchange_v2 / V2-13 统一待用户决策；W4-2 / W4-4、W3 仍未动。
