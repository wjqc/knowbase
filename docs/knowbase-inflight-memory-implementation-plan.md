# knowbase 执行中知识使用与沉淀：实施方案 v1.0

日期：2026-10-21。源码基线：`2049e1f`。依据：[执行中检索与增量沉淀设计](knowbase-inflight-memory-plan.md)、[三项核心目标方案](knowbase-three-goals-implementation-plan.md)及本轮源码取证。

**本方案已获用户实施授权，正在执行。** 以下保留原批次和验收契约；实际实现/迁移/测试及尚未通过的门槛以 progress.md 和 docs/host-capabilities.md 为准。不能将原计划中的接口存在、测试通过或本机注入证据升级为自动提炼与三人真实业务验收。

## 1. 最终交付及完成定义

交付一套融入 Agent 连续执行过程的知识使用机制：用户只发一次长任务，Agent 在进入各子任务、遇到新问题时使用相关经验与业务规则；阶段结束及时提炼新增知识；长输入尾部、压缩前的待办及进程重启后未完成材料可继续处理。

继续使用当前宿主模型、Python MCP、现有检索、同一个 memory.db、Markdown 卡片和内网 Git。不新增模型 API、中心数据库或第二套队列，不切换既有知识权威，不新建业务规则库。本期无新界面，不涉及前端实施及 F1–F5。

| 编号 | 必须交付的用户结果 | 失败判据 |
|---|---|---|
| U1 | 一次用户输入后的多个子任务中持续查询知识 | 等待下一条用户消息才查；只在开场注入 |
| U2 | 新错误发生后，下一次相关尝试前取得适用候选或明确无结果 | 只记录错误、入队而不递送给执行者 |
| U3 | 长方案中部、尾部规则均被识别和处理 | 只读开头、截断后仍报告全部完成 |
| U4 | 技术经验及业务规则实际成卡或形成可追溯待审核提案 | candidates 表有记录即报“已入知识库” |
| U5 | 后续阶段可继续沉淀，崩溃/重试不会遗漏或重复写卡 | 一次 save/feedback 遮蔽后续；应答丢失产生重复卡 |
| U6 | 其他成员同步后可按项目检索已生效知识，待审规则保持隔离 | 只证明本地可读；待审规则成为默认执行依据 |

业务规则与技术经验同等纳入 U3/U4，且规则的业务确认、人工激活、代码实现验证分别记录。命中次数、通过测试总数、保存数量均不代替以上结果。

## 2. 现状、可复用能力与必须补齐的断点

静态取证不代表以下问题已在真实宿主复现。

| 当前位置 | 可复用内容/实际行为 | 实施要求 |
|---|---|---|
| `hooks.py:user_prompt/_maybe_trigger_mid_session_capture` | 用户消息触发；中途 capture 默认关闭、每会话一次 | 新增执行中适配；按增量覆盖判断，不依赖下一条输入 |
| `hooks.py:stop_event` | 会话内任何 memory_write 可抑制漏存提醒 | 改查未处理片段/阶段，Stop 仅兜底 |
| `adapters/normalizer.py`、`checkpoint.py` | 多格式归一化、多 checkpoint；存在单事件截断 | 原文先保真，模型材料分页，超长单事件继续切分 |
| `server.py:capture_impl/job_claim_impl` | capture 构建单 checkpoint；claim 可能截断输出 | 统一 capture/import 的完整 manifest 与分页，不能靠 renew 重复同一截断头部 |
| `jobs.py:claim/renew/submit` | 已有 CAS、token、租约和候选持久化 | 补批次实际成员、到期校验、幂等回执及连续覆盖；复用队列，不重建 |
| `server.py:job_submit_impl` | 校验后存 candidates；没有调用写卡路径 | 增加候选到卡片的应用服务和读回确认 |
| `jobs.py:submit` | 按非 pending 数量或候选数累加 cursor | 改成 manifest 成员校验和连续完成前缀，不让重复/乱序推进超前 |
| `migrations.py:v8` | segment_results 以 segment_id 单列为主键 | 修正跨 job 的同名片段覆盖风险，保留存量记录 |
| `extraction/validator.py` | 有候选基本校验，但不等同 save 的完整 lint | 八项正文、bizrule provenance、scope、引用及治理共用写前校验 |
| `server.py:save_impl`、`commit_adapter.py` | 已有 lint、幂等 intent、卡片写入、索引/Git 路径 | 提取共享写服务供 save 与候选应用调用，不解析 save 返回文本判断成功 |
| `store.py`、`mutations.py` | bizrule、provenance、code_refs、内容/治理 revision | 保留现有类型与治理，新增元数据必须贯穿读写/哈希/同步路径 |

核心顺序：先验证宿主能否执行中递送；同时以完整材料和真实成卡闭环作为自动沉淀放量的前置条件。

## 3. 实施批次与依赖

以下为单名熟悉项目的开发者的初估，包含开发及本地回归；不含宿主不支持事件时的产品改造、外部等待及业务方审核排期。估算待 E0/E1 后校准，不作为已承诺日期。

| 批次 | 可交付结果 | 主要落点 | 前置/退出闸门 | 估算 |
|---|---|---|---|---|
| E0 接入探针与基线 | 确认每个宿主能监听什么、能否中途注入；冻结独立验收样本 | 新增 `tests/fixtures/inflight/`、`docs/host-capabilities.md`；现有 Hook 配置的隔离副本 | 真实 ZCode 一条用户任务中完成一次中途递送，或明确 assisted；不凭事件名假定支持 | 0.5–1 人日 |
| E1 原文、分页和覆盖 | 长输入无静默丢失；可领取尾部，游标可靠 | `adapters/normalizer.py`、`checkpoint.py`、新增 `materials.py`、`jobs.py`、`migrations.py` | 长单事件/多主题/乱序/重试反例通过，旧日志和队列保留 | 2–3 人日 |
| E2 执行中检索 | 当前 Agent 按阶段提交查询，新错误补查并获得有界上下文 | 新增 `context.py`、`server.py`、`index.py`、`config.py`、宿主工作流 | U1/U2；新错误不被重复缓存挡住；scope 不串项目 | 2–3 人日 |
| E3 增量提炼与真实成卡 | 候选可成为 active/once 卡或 staging 提案，结果可读回 | 新增 `extraction/materialize.py`、`extraction/validator.py`、`server.py`、`jobs.py`、`commit_adapter.py`、`mutations.py`、`store.py` | U3/U4/U5；未生效规则不进入默认检索；应答丢失不重复保存 | 3–4 人日 |
| E4 宿主正式接线 | 从真实工具事件到检索/提炼，再到 Agent 下一步消费 | `hooks.py`、新增 `adapters/host_events.py`、`__main__.py`、拟新增 `docs/hosts/*.md`、`docs/trae-rules.md` | E0 确认能力；E1–E3 通过；无事件的宿主明确 assisted | 1–2 人日 |
| E5 升级、故障与团队试点 | 可回滚，三位成员实际复用，记录使用效果及成本 | `migrations.py`、`__main__.py:doctor`、README、回归集与验收记录 | 下文正式入口/故障验收通过，U6 完成 | 2–3 人日，另观察 3–5 个工作日 |

合计初估 **10.5–16 人日**，加真实试点观察窗口。E1/E2 中不依赖宿主事件的工作可先做；E0 未通过不得宣称全自动，E1/E3 未通过不得扩大自动写入。每批单独形成可审查变更，避免 Hook、长文本和写入恢复一次性混发。

## 4. E0：宿主能力与贯穿样本

### 4.1 宿主能力清单

每个宿主记录版本、工具事件及实际字段、transcript 格式、工作目录来源、session/tool_call 标识、注入格式、下一步消费时机、超时、阻断语义、压缩事件、工具名前缀。

先核对本地安装的事件规范及录制样本，再生成配置；事件名称尚未确认前不写入真实配置。ZCode 优先；Claude/Trae 各自验收，不能照搬。能力标记为：

- `event_driven`：真实执行中事件能在下一相关行动前递送上下文。
- `assisted`：仅由宿主工作流在子任务节点主动调用 MCP，不能声称强制覆盖。
- `unsupported`：关键输入/工具不可用，明确原因与受限能力。

探针不采集所有历史聊天。使用隔离 scope、测试库和脱敏样本；保留事件原始字段的测试夹具、回传内容和后续工具轨迹，证明实际被消费。无活跃宿主时只允许材料入队，任务停 awaiting_agent。

### 4.2 独立验收样本

实施前由维护者与团队使用者标注：长方案的子任务、应检索知识 ID/版本、规则原文位置、可沉淀结论、不可当事实的推测。至少包括长方案头/中/尾三类规则、连续两阶段经验、新错误、用户纠正规则、代码与需求冲突、无可复用结论。

样本应混入无关卡与同词异项目卡。预期由人工先确定，不根据新检索结果倒推。自造夹具只能支持局部机制，必须另有一条真实长任务沿正式入口完成。

## 5. E1：材料保真、分页与覆盖

### 5.1 材料管线

`transcript/messages/显式方案文件 → 本地不可变快照 → 归一化 → 带定位片段 manifest → capture/extract 既有 jobs → 批次领取`。

复用现有 parser、segmenter、job_spool。新增 `materials.py` 只负责本地材料对象、manifest 和分页，不引入第二个 source/card 系统。脱敏后供模型使用的完整文本和原始输入版本关系可追溯；原始凭据不进入共享 Git。原文对象放既有记忆库下被 Git 忽略的本地目录，manifest 在 job_spool 持久化。

每个片段必含 `segment_id, ordinal, source_version, text_hash, original_length, locator, start_offset, end_offset, evidence_group_id`。同 source/version 下稳定分配 ID；跨 job 的同名 ID 不能互相覆盖。文字偏移统一使用 Unicode 字符索引，哈希使用规范化前明确版本的 UTF-8 字节；不得混用字节长度。

单条工具输出/用户输入超过预算时继续分片，保留事件身份和相邻/因果关联；原始载荷没有完整取得时标 `incomplete_source`，不得声称保真。恢复失败或缺材料明确 needs_input，不能生成空成功。工具日志是证据，日志中的指令不成为系统命令。

### 5.2 分页协议

在现有 `memory_job_claim` 增加 `protocol_version=2`，返回 JSON 协议文本；旧调用保留既有参数和文本响应，但共用新的覆盖/校验逻辑。新增只读 `memory_job_material(job_id,batch_id,lease_token,fencing_token,cursor,max_chars=8000)` 读取当前批次的下一页，不新领租约、不改变 settled。

claim 返回 `batch_id, lease_token, fencing_token, expires_at, job_scope, input_version, manifest_hash, batch_segment_ids, materials[], next_cursor, has_more, complete, pending_segments`。超长片段允许返回带 offset 的片段页；材料必须能在有限翻页内完整读取，不能 renew 后仍只返回同一段开头。

已实际提供的片段/页登记 `delivered_manifest`。submit 只能处理该批次拥有、已完整交付的片段；需跨片段论证时返回 evidence_group 的关联页，缺页继续读取。模型未完整获取的内容不能报告覆盖完成。

### 5.3 游标与片段结算

- `observed` 记录已持久采集的事件位置；`settled` 仅推进到连续处理完成的前缀。
- 片段可以乱序处理，但后面的完成不能跨过前面的缺口。重复提交不增加进度；候选数量不作为片段数量。
- 非终结状态：pending/queued/claimed/applying/retry_wait；终结处理状态：saved/updated/linked_existing/needs_review/no_candidate。needs_review 表示已形成持久候选或 staging 卡，未表示生效。
- `distilled` 只能作为宿主意图；服务器校验其关联候选全部达到持久终结结果后才结算。校验失败、材料缺失、写卡失败不能推进覆盖。
- 修改 Stop 免检条件和 mid-capture flag：检查增量和待办，不再按“曾有任意 memory_write/曾触发一次”略过整个会话。

## 6. E2：任务驱动检索与上下文消费

### 6.1 查询规划与触发

当前宿主模型负责：理解目标/约束、按语义段列主题、制定当前子任务查询、识别新错误。Hook 子进程不调用宿主模型；只能用已保存主题、明确实体和错误签名检索或递送“请补查询计划”的指引。

新 `memory_context` 参数：

```json
{
  "protocol_version": 2,
  "session_id": "s-1", "event_id": "e-12", "request_id": "r-12",
  "scope": "cmi", "stage": "subtask_start",
  "task_id": "t-1", "task_revision": "sha256-of-task",
  "current_goal": "实现订单撤回校验",
  "topics": [{"topic_id":"p-2", "source_segment_ids":["seg-2"],
    "entities":["订单","审批状态"], "queries":["订单 撤回 审批状态 权限"], "status":"active"}],
  "evidence_refs": [], "observed_cursor": "event:12"
}
```

stage 枚举：task_plan/subtask_start/new_failure/decision/verified_result/checkpoint/before_compact/recover。new_failure 必须提供可定位实际工具结果；verified_result 必须给验证引用，宿主自述“已通过”不自动提高卡片 confidence。

响应为 `event_id, capability_mode, retrieval_status, candidates, next_topics, capture_due, job_ids, pending_segments, resume_cursor, budget_used, warnings`。候选携带 `id, content_revision, summary, applicable_when, confidence, status, topic_ids`。摘要来自卡片正文，缺内容标 unknown，不能凭标题生成未在卡片中的结论。

### 6.2 行为规则

1. scope 解析顺序：显式参数与已绑定会话一致性校验 → 会话绑定 → 项目路径映射。冲突报错；未知项目不扩成全库。首次绑定可来自合法的显式 scope，scope 不是 ACL。
2. 只查询 active 主题；其他主题 pending/deferred，进入阶段后再查。短输入“继续”恢复现有任务，不以长度为由丢弃。
3. 复用 `index.search` 与既有排序。主动调查可返回 once 及验证提示；自动候选保持 verified，staging/stale/冲突只用于明确复核/查重。
4. 初始默认：每主题 3 个方向、首批最多 6 次检索、合并最多 3 条候选、摘要注入预算约 800 token；零命中允许一次改写。预算达到后返回 deferred 主题，不丢输入。
5. 查询结果按卡片版本去重。缓存键含 scope、任务/主题版本、实体/错误签名、环境及知识版本；新错误、scope/环境变化、相关知识更新必须失效。知识版本暂以 Git HEAD 与本机卡片写入修订标识组合，不以数据库文件 mtime 代替。
6. 返回内容必须到达当前执行 Agent；读取和实际行动轨迹分别记录。采用时核对适用条件，反馈仅在实际使用/验证后填写，不因命中自动 helpful。

## 7. E3：技术经验、业务规则与真实写卡闭环

### 7.1 每阶段双路提炼

维护每阶段新增的“技术经验”和“业务知识”候选。业务知识包含术语/对象含义、状态转换、准入校验、角色权限、计算口径、例外及跨系统约束。需求澄清、用户纠正、方案决定均可触发，不以发生故障或完成代码为前提。

扩展 `_build_distillation_guide`，同步调整 MCP instructions、Hook 指引及 Trae 规则，避免入口继续只强调踩坑。普通经验按现行政策保存 active/once；bizrule/standard/preference 一律 staging。未获业务出处的推断保留待确认候选，不伪造 provenance。

### 7.2 候选契约

`memory_job_submit` 增加 `protocol_version=2, fencing_token`；批次/租约参数沿用，所有提交都执行新成员和状态检查。V2 候选：

```text
candidate_key, type, title, scope, action=create|update|link_existing|review,
target_id?, expected_content_revision?, expected_governance_revision?,
body={conclusion,problem,applicable_when,not_applicable_when,
      actionable_steps,key_evidence,verification,unknown_items},
evidence_refs[], source_refs[], code_refs[], provenance?,
segment_ids[], rule_context?, duplicate_evaluation
```

rule_context（bizrule 必填）包括 business_object、trigger、required_or_forbidden、exceptions、applicable_version、business_confirmation、implementation_status。枚举分别为 `explicit_source|code_inferred|unconfirmed` 和 `not_implemented|implemented_unverified|verified|unknown`；这些是证据分类，不是审批授权，不能设置 active/verified。

已有 provenance 字符串继续保留；rule_context 为可选 frontmatter 扩展，存量卡未填为 unknown，不回填虚假确认。必要内容同时写入八项正文，使共享卡自足。用户确认引用脱敏消息摘录/日期及定位，PRD 引用文件版本与条款；共享引用必须可在现有 source 机制中定位或正文自足，本机 transcript 绝对路径只留本地。

将 type/title/provenance/rule_context/source_refs/code_refs/action/segment_ids 等完整保存在 candidate payload 中；不能只存 body/evidence 后丢失治理类型。rule_context/provenance/source_refs 纳入统一内容 revision，并同步修正 create/update、读回、哈希、lint、Git 往返和索引序列化；生效权限仍由既有人工 CLI 控制。

### 7.3 校验、查重和应用

拟新增 `extraction/materialize.py`，调用从现有 save/update 提取的共享写服务。共享服务返回结构化结果；save 的既有文本响应仅作为外层展示，不用于内部判定。不得绕过完整 lint、RepoLock、commit_adapter、关系副作用、索引和 Git。

提交顺序：

1. 查询同 job/request_id 的持久回执，同 key 同 payload 返回原结果；不同 payload 拒绝，幂等恢复先于租约校验。
2. 对新提交校验 batch_id、当前 token/fencing、绝对到期时间、job scope、已交付 manifest；拒绝伪造片段、跨项目候选和未知 decision。
3. 校验八项内容、provenance、source 哈希/定位、code_refs 及治理；错误返回逐候选/片段结果，不能过滤掉候选却仍把其片段标 distilled。
4. 既有宽召回供宿主判断重复/补充/矛盾。提交前同 scope 复查 revision 与新候选；发现变化返回 needs_reassessment。标题相似只作候选线索，不证明两个业务规则相同。调整共享查重路径，避免跨项目同标题误拒；所有 save 与 submit 行为一致。
5. 短 SQLite 事务持久保存完整候选及应用意图/回执，job 进入 validating。此时租约交接已提交，不允许其他宿主再次 claim 该批次。
6. 锁外组织逐候选应用；共享写服务内部采用既有短 RepoLock 与 intent 恢复，稳定写入幂等键为 job_id+candidate_key+candidate_version，不随重试 request_id 变化。
7. 对卡片文件、content_revision、索引状态读回。普通新卡记录 saved_active_once；规则记录 saved_staging；link_existing 核对目标版本；待确认材料记录 review_pending。读回失败保留 applying/retry，不报成功。
8. 同库事务回填候选结果与片段覆盖；一个片段关联多候选时必须全部获得终结结果才能结算。保存与 Git 推送状态分开，本地成功/同步待办不能冒充其他成员已收到。

单批部分写卡成功时保留其确定结果，失败部分可独立续做，不回滚已发布的正确卡。跨文件/SQLite 无法一个事务覆盖，依靠应用回执+既有 write_intents 做对账，不能假称全部原子完成。启动恢复及 job_status 的有界恢复路径可接续 validating；没有宿主时仅能重放已确定的应用意图，不能生成新语义结论。

规则修正不得直接 Agent update 已激活的治理卡。新规则形成关联旧卡及其 revision 的 staging 提案，人工按现有 revise/promote 流程裁决；旧规则不因一个未审核候选自动失效。规则确认、激活、实现验证在返回结果和默认检索中分别可见。

### 7.4 完成响应

逐候选返回 `candidate_id, action, outcome, memory_id, content_revision, governance_status, local_state, sync_state, error, retryable`。批次返回 `processed_segment_ids, pending_segment_ids, next_contiguous_cursor, saved_active, saved_staging, linked_existing, review_pending, failed, state`。

只存 candidates 时不得返回 saved；job 的 succeeded 表示所有片段已可靠处理，可能含 staging，必须同时返回有效卡/待审卡/无候选数量。needs_review 和 no_candidate 的含义不变为“全部已生效”。

## 8. 数据迁移、幂等与兼容

基线 schema 为 v8；固定rc1采用v9–v14，当前开发继续到v18：v12应用回执租约、v13相似卡快照、v14脱敏观察事件；v15同步指纹、v16消费回执、v17版本/任务结果反馈、v18命令遥测。禁止修改已运行的旧迁移，正式部署与开发状态分别记载。

| 迁移 | 表/字段与约束 | 兼容策略 |
|---|---|---|
| v9 执行上下文 | context_sessions：PK(host_profile,session_id)，scope、task_revision、topics_json、observed_cursor、settled_cursor、coverage_json、last_checkpoint_at、updated_at；context_events：UNIQUE(host_profile,session_id,event_id)，task_id、trigger、payload_hash、status、reason、segment_refs_json、job_ids_json、latency_ms、created_at | JSON 默认空对象/数组；老使用日志不回填成 Hook 事件；同事件不同内容冲突 |
| v10 材料和批次覆盖 | job_segments：PK(job_id,segment_id)，UNIQUE(job_id,ordinal)，source_version/hash、定位/offset、evidence_group_id；job_batches：batch_id PK，job_id、input_version、manifest_hash、segment_ids_json、delivered_json、owner、fencing_token、expires_at、state | 从可完整还原的 spool 建 manifest；不能还原的旧截断材料标 legacy_incomplete/needs_input，不伪造原文 |
| v10 覆盖修正 | 事务重建 segment_results，PK(job_id,segment_id)，增加 settlement_state/receipt_id/linked_ids_json；保留 decision/reason、旧时间字段 | 旧行原样保留；无法证明与 manifest 一致的旧 distilled 不自动算成已成卡，单列待对账 |
| v11 应用与回执 | candidates 增 payload_json/payload_hash、materialization_state、memory_id、memory_revision；job_submissions：PK(job_id,request_id)，batch_id、payload_hash、state、result_json、created_at、updated_at | 旧候选不自动大批写卡；逐条显式复核恢复缺字段/证据，保留旧决策供审计 |
| v15 刷新协调 | sync_lease.target_hash，明确绑定配置/HEAD，跨进程冷却与租约 | 本地检索不等网络；过期工作不能释放新租约，目标改变不采纳旧结果 |
| v16 消费回执 | knowledge_uses，UNIQUE(host_profile,session_id,event_id)，任务/方案版本、实际递送事件、卡版本、使用状态 | 阅读与采纳分开；内容改版不得继续采纳旧候选；不自动helpful |
| v17 反馈回执 | feedback_events及feedback_requests；版本+任务+稳定结果去重；before/after文件哈希 | 历史和旧调用仅作未绑定观察；新内容重新验证；回执恢复不重复累计，工具标签不当人工权限 |
| v18 命令遥测 | command_calls，仅命令、传输、结果类别、时间与时长，不存参数 | 新旧日志并列不相加；metrics只读导出不迁移/触网，缺表为null |

所有表位于原 memory.db，沿用迁移事务、WAL 与短写入；副作用锁顺序 RepoLock→SQLite。应用意图先独立提交，不持 SQLite 事务等待 RepoLock；后台网络和模型不持两种写锁。

同 job 的 manifest 固定；来源变化新建 input_version/任务，不覆写已 claim 的输入。statement、candidate 与写卡幂等使用各自明确作用域。幂等检查还需比对 job/batch/payload，不能用他人 request_id 取回无关结果。

旧 MCP 工具和文本展示保留；新指引显式要求 V2。旧协议缺少完整候选信息时允许保存 review_pending，并返回缺字段，不能继续宣称已入库。原 save/read/search 等绕过任务入口仍调用同一校验/写服务；治理 CLI 不被 Agent 的新字段绕过。

## 9. E4：正式事件接线和资源预算

### 9.1 工具事件适配

新增 `adapters/host_events.py` 将已验证宿主载荷转换成 `host_profile, session_id, event_id, tool_call_id, project_path, trigger, tool_name, payload_ref, timestamp`。event_id 优先使用宿主稳定 ID；没有时以会话内可重放序列/载荷哈希构造，不能以当前时间生成导致重投重复。

拟新增 CLI `knowbase hook tool-before|tool-after|before-compact --style <宿主>` 作为内部规范名；实际宿主事件映射必须来自 E0。现有 session-start/user-prompt/stop 继续兼容。适配器先记录事件，再执行轻量 context 内核；解析失败/超时同样记录原因。

- 开始新子任务/模块：优先由宿主主动 memory_context 提供语义主题；工具路径变化只作提示信号，不能每个文件都算新阶段。
- 新错误：使用实际返回状态与脱敏错误签名，同签名同环境限频；语义判断由宿主确认，日志中出现单词 error 不足以认定任务失败。
- 阶段验证/规则纠正：宿主主动提交 evidence_refs，触发增量 capture 并领取，不必再等 UserPromptSubmit。
- 排除 knowbase 自身工具、其 Git/索引/日志活动；支持批量工具结果时逐项保留身份，不能丢失败项。
- 无可用中途事件时仅启用主动工作流并显示 assisted；MCP 无法强制宿主每个思考步骤调用工具。

### 9.2 配置草案

以下配置键待实现后才能生效；不修改用户当前配置，不擅自扩大聊天扫描范围。

```json
{
  "inflight": {
    "enabled": false, "mode": "assisted", "protocol_version": 2,
    "checkpoint_work_ops": 6, "checkpoint_minutes": 10,
    "max_queries_per_topic": 3, "max_queries_per_event": 6,
    "max_candidates": 3, "context_token_budget": 800,
    "material_page_chars": 8000, "max_batches_per_opportunity": 2,
    "fast_path_timeout_ms": 1000, "max_pending_jobs": 200,
    "host_profile": "zcode-local", "member_label": "configured-label"
  }
}
```

数值均为可调初值：token 字段在缺少真实 tokenizer/宿主用量时标 estimated，不伪造精确成本。快路径不等待 Git 网络；超过 1 秒返回 deferred 警告、保留待办。6 次操作或 10 分钟只在下一个事件检查，非独立定时唤醒；新错误和阶段结束不等待阈值。连续提炼预算耗尽后把 job 留待下个宿主机会，不占用无限对话。

单输入对象初始上限 10 MiB、单批最多 32 个片段、单片段目标 4,000 字符，实际按语义边界切分；超上限显式返回需分批及原文定位，禁止头部截取后宣称完整。队列满保留已接受任务，拒绝新任务并记录 backlog_full；用户原始任务可继续，沉淀缺口可追踪。是否满足真实输入规模由 E1/试点校准。

## 10. 验收矩阵与检查命令

拟新增测试均按真正用户结果验证，不写仅镜像函数实现的断言。已有 regression 集保留；项目基线并未在本轮重跑，下述全部为实施时检查计划。

| 用例 | 独立预期/正式入口 | 断言与故障点 | 对应结果 |
|---|---|---|---|
| A01 一次输入三个子任务 | 人工标注各阶段相关卡；真实宿主 | 第二、第三阶段无需新用户消息仍检索，下一行动体现适用约束 | U1 |
| A02 执行中出现新错误 | 实际失败工具结果；工具事件→Agent | 新错误补查、相同错误限频、环境改变重新查 | U2 |
| A03 超长单消息与方案 | 头/中/尾标注，超过 8,000/16,000 字符及跨页证据 | 完整分页、尾部规则保留；缺页不可 settled | U3 |
| A04 双阶段沉淀 | 第一阶段已 save，第二阶段产生新结论 | feedback/save 不免除第二阶段；有新卡/提案的 ID 与读回 | U4/U5 |
| A05 三类业务规则 | 状态、权限、计算规则和原文出处 | 各自成 staging；可读全文，默认检索不出现，人工激活后可检索 | U3/U4/U6 |
| A06 代码推断及用户纠正 | 未实现需求、代码冲突、用户明确修订 | 确认/激活/实现不混淆，旧规则不被未审候选覆盖 | U4 |
| A07 候选应用恢复 | 写卡后杀进程/丢响应、索引前失败、push 失败 | 恢复返回相同卡；本地/索引/远端状态分别报告；无重复 helpful | U5/U6 |
| A08 批次和覆盖 | 两 job 同名 segment；乱序/重复/非法片段 | 不跨 job 覆盖、不越过缺口、无凭空进度 | U3/U5 |
| A09 租约与并发 | 过期未接管提交、接管后旧提交、相同 request 重放 | 新提交过期拒绝；已持久提交可幂等读回；stale 更新不覆盖 | U5 |
| A10 无候选/无命中/失败 | 预先标注无关材料；scope 缺失/队列满/索引错 | no_candidate/no_hit/deferred/error 分开；开发不无限阻断 | U1/U5 |
| A11 自递归与压缩 | 实际 memory_* 调用和可用压缩事件 | 不自触发；压缩后主题、采用版本和待办恢复 | U1/U5 |
| A12 三机共享 | 两个隔离克隆的 Git 回归＋三人真实环境 | 生效卡同步后可查；staging 保持隔离；本地 usage 不冒充团队统计 | U6 |

测试落点建议：新增 `test_inflight_context.py`、`test_material_paging.py`、`test_capture_coverage.py`、`test_candidate_materialization.py`、`test_inflight_bizrules.py`、`test_inflight_host_contract.py`；在 `test_m3.py`、`test_p1_migrations_jobs.py`、`test_bizrule.py`、`test_code_refs.py` 中保留历史反例并补相邻入口。真实宿主录制证据与 mock 分开登记。

实施阶段运行：先用项目 `.venv/bin/python -m pytest` 执行该批定向测试；每批落地前跑相关既有回归，完整合入前跑全套、compileall 与 diff 检查。MCP 验收必须通过 stdio 正式工具注册调用，不能只调用 impl；Git 验收至少使用两个克隆，真实团队试点另行完成。纯文档阶段不以运行无关全套测试冒充实现验证。

## 11. 观测、试点与回滚

### 11.1 指标和试点记录

所有触发记录 executed/skipped_cache/unsupported_event/scope_unresolved/deferred_budget/backlog_full/failed/no_hit 等原因。现有 usage_log 保持原含义；新增 context_events 记录完整事件，候选应用状态用于核对真正入库率。标签保留 member/device/host，未设置记 unknown，不视为可信身份。

试点逐任务记录：标注的应检索阶段数、实际检索/采用阶段、采用卡版本及验证结果、标注的应沉淀结论数、保存/待审/遗漏/重复、额外延迟、总 token 与总用时的可得性。分母由预先标注提供，不能用系统检测到的事件自证覆盖率。

本地实验的 A01–A12 必需断言全满足；真实效果指标由首批标注任务形成基线后设门槛，不提前承诺节省百分比。若要测 token/工时净收益，使用同模型配置、相近难度和独立任务样本的开启/关闭对照，注明模型缓存、熟悉度和样本限制。反馈次数不折算工时。

三位成员各完成至少一条实际多阶段任务；维护者核对“查询影响了哪一步”和“别人能否找到新知识”。三人试点仅证明该样本接入，不能直接声称部门全面提效。待审规则的积压需有人工审核责任角色，不能通过自动 promote 降低积压。

### 11.2 升级顺序

1. 记录代码/配置/宿主版本；SQLite 在线一致性备份，备份本地材料对象和 Markdown/Git revision。不得直接复制活跃 WAL 的单个主文件冒充完整备份。
2. 维护窗口暂停相关本机 MCP/Hook 写入，执行同库增量迁移和一致性核对；任何失败回滚迁移事务，保留备份与错误。
3. 发布兼容运行代码，新 inflight 默认 false；重启 MCP，确认新旧协议、旧 save/search、候选和使用日志计数保持。
4. 隔离 scope 完成协议回归；按 E0 的证据为一个宿主开启 assisted 或 event_driven。先执行中只读检索，E3 门槛通过后启用自动候选应用。
5. 单成员真实任务通过后推广至三位成员，逐机检查版本、scope_map、身份标签及能力状态；不把 Git 拉取当作本机 Hook 已更新。

### 11.3 回滚与事故恢复

- 先关闭 inflight 和新事件适配，恢复原三 Hook；保存队列和原文，不删库、不清空已接受任务。
- 已保存正确卡不批量撤销；错误卡按 revision 和既有治理流程修正/归档。待审规则未生效，仍保留待审。
- schema 为增量，禁止在线反向删表。回退代码前确认旧版本可忽略新表/元数据；如必须恢复旧数据库，先冻结写入并导出升级后新卡、回执及待办，明确恢复窗口和未重放数据。
- 原文对象丢失进入 needs_input；卡已存在而回执未完成，按稳定写键和 write_intents 对账；Git 离线保留 local_saved/sync_pending，联网后走现有同步重试。

## 12. 实施准入、责任与分层状态

| 风险/决定 | 处理与责任角色 | 未通过时限制 |
|---|---|---|
| ZCode 执行中事件及注入可用性 | 宿主接入者在 E0 录制正式证据 | 只能交付 assisted，禁止全自动宣传/批量配置 |
| 原文尾部与跨页因果完整性 | 内核开发者以人工定位样本完成 E1 | 禁止大规模自动总结/写入 |
| 候选到卡片的治理与恢复 | 内核开发者和复核者完成 E3/A05–A09 | 可以保留待办，不声称知识已保存 |
| 业务规则确认与审核 | 业务规则负责人确认来源，按人工 CLI 激活 | 未审不得默认注入或自动替换旧规则 |
| 长文本宿主语义提炼效果 | 团队使用者预标注并评审 E5 的真实任务 | 保留遗漏与待确认，不能用格式校验代替正确率 |
| 预算及效果 | 维护者按试点调参，记录实际成本和负担 | 参数为默认值，不能宣称已优化或净省 token |

本期取舍已明确，无须用户再指定技术细节：复用宿主模型/现有队列与写服务、分阶段检索、增量提炼、业务规则保留人工激活。新的中心服务、模型 API 和知识权威迁移不属于本实施方案。

代码和局部验证已开始落地，实际范围及结果见 progress.md；ZCode 执行中注入/消费局部通过，headless build 模式的 MCP 提炼被许可客户端缺失阻断。三人试点和净效果测量仍待执行，保留原准入条件。
