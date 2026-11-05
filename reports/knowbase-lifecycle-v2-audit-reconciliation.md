# knowbase 生命周期 V2：审计修订与设计决策记录

> 日期：2026-11-05（Asia/Shanghai）。本轮仅调查与设计；未实施 V2，未迁移真实库，未修改运行配置或卡片，未创建 Git Commit。原审计 v1.1 保留原样，本文件是其后续纠正记录。
>
> 配套：[架构设计](../docs/design/knowbase-knowledge-lifecycle-v2.md)、[实施任务书](../docs/plans/knowbase-lifecycle-v2-implementation.md)。

## 1. 取证口径与基线

### 1.1 证据等级和陈述类型

沿用原报告定义，**等级数字不是由弱到强的评分**。

| 等级 | 含义 | 本轮使用边界 |
|---|---|---|
| E0 | 缺失、未执行或不能确认 | 不以猜测填空 |
| E1 | 本机真实运行数据、配置、文件或工具返回 | 仅支持明确快照和本机范围 |
| E2 | 实际运行的隔离测试 | 不等于真实模型、宿主或团队验收 |
| E3 | 当前源码实现 | 函数存在不等于正式路径闭环 |
| E4 | 文档、设计或历史报告声明 | 不自动升级为当前运行事实 |

全文使用四种陈述：**事实 F**（有来源）、**推论 I**（解释事实但不冒充直接观察）、**决策 D**（V2 方案）、**假设 H**（需实施阶段验证）。V2 的使用证据 L0～L4 是另一套业务分层，不与 E0～E4 混用；检索 L2/L3 也不表示证据等级。

### 1.2 版本核对

| 对象 | 本次观察 | 依据 |
|---|---|---|
| 基础报告 | `reports/knowbase-card-lifecycle-audit-20261105.md`，674 行，标题修订 v1.1 | E1 文件读取 |
| 报告历史源码 | `b6ac115`，`0.2.0rc9` | E4 报告声明 |
| 当前源码 | `425f3fe84f42e95f57548fc462800773b1b6f8f3`，版本仍 `0.2.0rc9` | E1 Git、`pyproject.toml:3` |
| 新增提交 | `1fa0b05` 执行器只 create/review；`425f3fe` code_refs 元素归一化 | E1 Git；E3 `automation/executor.py:319`、`distillation.py:36` |
| 固定安装包 | `~/.knowbase/runtime/venv/lib/python3.13/site-packages/knowbase` 的 115 个 `.py` 与当前源码逐文件 SHA256 一致 | E1 只读字节比对；不代表已存活进程全部重载 |
| 运行登记 | `runtime/active.json` 仍写 `556f8bf...`、build `6a0cf13e326078c4` | E1；与安装字节不一致，登记不能单独证明实际构建 |
| 初始工作区 | `?? reports/`、`?? uv.lock` | E1；原文件不覆盖，不清理、不提交 |

**F-01：同一个包版本号不能识别当前行为。** 实施验收必须同时记录源码提交、安装模块摘要、登记信息、worker/长驻 MCP 的加载构建和宿主新连接。当前仅确认前两项一致，不能宣称所有宿主加载了该构建。

### 1.3 读取方法和影响边界

- 代码先经 codegraph 探索，再读取未覆盖的具体实现；图谱“未发现测试”提示不作无测试结论，已按实际测试文件校正。
- 真实 `~/knowbase/memory.db` 直接使用 `sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)`、`PRAGMA query_only=ON`、`BEGIN` 读取；没有使用会自动迁移的 `index.connect()` 访问真实库。
- 主统计冻结于 **2026-11-05 09:32:48 +08:00 / 01:32:48 UTC** 的同一 SQLite 读事务。后续补充查询单独标时段，后台 worker 持续变化，不能把多个快照合并成一个总数。
- 运行配置仅白名单字段输出；没有读取或披露凭据正文，没有调用内网模型，没有重新探针、启用语义、重试历史任务或触发维护写入。
- 按会话要求调用了 `memory_context`；其返回属于运行观测，会由服务按既有流程记录检索/递送，亦可能引发既有刷新请求。**不能把本次观测称为全系统零写入**；本轮未通过工具提交卡片、采用、反馈或治理操作。后续真实库核验均使用直接只读连接；后台既有写入不属于本轮实施。
- 项目相关 `~/knowledge/通用/05-智能助手/knowbase-*.md` 与早期设计用于找约束；其中过时的 releases、Stop 阻断、人工晋升描述不作当前事实。本轮明确不写真实知识库、不提交，优先于一般知识同步约定。

## 2. 最新真实数据

### 2.1 同事务冻结快照（E1）

| 指标 | 09:32:48 快照 | 正确解释 |
|---|---|---|
| meta 卡片 | **246** | 包含 staging，不等于默认可检索库存 |
| 正式 active | once 194、verified 12，共 **206** | 未扣 reference 或其他过滤条件 |
| staging | **30**，均为 active/once | staging 是目录/投影维度，不能并入正式 active 口径 |
| archived / stale | 10 / 0 | 不能推出从未发生过中间状态变化 |
| capture | **140** | 六类明细下表求和一致 |
| knowledge_uses | 6：referenced 4、irrelevant 2；adopted/read 0；均 host=zcode | 记录不足不能推出没有实际采用 |
| feedback_events | 4：helpful / version_task / applied | 版本绑定不等于经过独立执行验证 |
| task_outcomes | 3：completed / agent_report | 均 task_run_id 为空（后续补查）；不证明真实运行与因果收益 |
| task_evidence | 1：task_outcome / agent_report / completed | artifact_hash 为空（后续补查），不是可复验执行产物 |
| 提炼候选 | saved 40、updated 2、needs_review 2 | 是候选行数，不能等同于新增卡数或当前执行器可自动更新 |
| embedding_refresh jobs | succeeded 18、failed 8 | 成功构建不等于生产启用 |
| embedding_generations | ready 18；累计 expected/completed 均 2719 | 是跨代计数，不能当作 2719 张当前卡 |
| retrieval_active | **0 行** | 当前无激活代际 |
| retrieval_observations | L2 241（reason 空 233、index_not_ready 8），L3 0 | local 44、maintenance 36 另列，不能混入任务检索分母 |
| context_events / job_segments | 14925 / 5790 | 不是独立任务数或经验价值数 |
| 活跃 capture 关联片段 | 2899 | 固定 SQL 状态集合口径，非所有历史段；不同于 context 返回的 pending_segments 业务口径 |

capture 六类：

| 状态 | 数量 | 最早创建（UTC） |
|---|---:|---|
| awaiting_agent | 43 | 2026-10-23T04:09:00Z |
| failed | 19 | 2026-10-23T03:00:17Z |
| needs_review | 4 | 2026-10-26T09:06:10Z |
| no_candidate | 60 | 2026-10-22T10:00:16Z |
| retry_wait | 1 | 2026-11-05T00:33:54Z |
| succeeded | 13 | 2026-10-22T10:26:36Z |
| **合计** | **140** | 43+19+4+60+1+13 |

`60/140=42.9%` 仅为作业终态占比，**不是低价值材料比例、错误率或提炼效率**：作业内材料量不同，且还有未完成任务。

### 2.2 运行状态与时间解释

主快照中 distillation 为 `blocked / verified_executor_required`（updated 01:31:56Z）。但 capture 成功任务更新至 11-05T00:02:23Z，executor_runs 的 settled 更新至 00:02:24Z；因此不能复用“从 11-04T14:28 一直没有成功”的判断。调度表 `last_success_at` 仍为 11-04T14:28:59Z，也不能替代任务级成功证据。

executor_runs 按状态/错误统计：settled 37；calling 6；failed 中 candidate_validation_failed 75、timeout 15、structured_output 14、daily_budget_exhausted 9、connection_error 7、authority_changed 6、process_exited 2、rate_limited 1。**一次任务可能多次调用、一次调用可能多候选，不能用这些计数计算作业成功率**。

知识同步和 vector_maintenance 最近为 success；evidence_exchange 为 skipped。success 仅说明 handler 的返回口径；`worker._h_vector_maintenance()` 返回 `ran=True` 并不证明 L3 激活或远端模型健康。

后续 09:34 左右补查：两个 executor 私有回执均 `passed=false, had_verified=true`；更新器为 `deferred_or_failed`、failures=5。未在本轮重跑探针或修复。原报告“恢复了”与本次“blocked”是不同时间切片，可同时为真；**不能用本次 blocked 否定历史恢复，也不能用历史恢复宣称当前健康**。

原 v1.1 的“升级后 blocked 约17分钟、成功间隔约2h17m”只保留为历史报告记录（E4）：本轮没有逐条重建那一窗口的调用日志。两次成功之间的间隔也不能直接视为全时段服务不可用时长。

### 2.3 语义配置和历史实验

真实配置为 embedding enabled=true，模型 `AI/Qwen3-Embedding-4B`，quality_report=null，allowed_scopes 为 knowbase/cmi/cmi-sales-assistant/global。当前无 active 指针；本会话 context 返回 L2/lexical/index_not_ready、remote_calls=0（E1）。

`semantic_index.quality():47` 不接受简单 approved=true；要求冻结报告、execution_derived≥60、Recall@5≥0.5、CI 下界≥0、四门通过、模型与代码指纹等。`activate():136` 还检查 manifest/覆盖率。`retrieval_state.dense():88` 在证据到来后可自动激活 ready 代际，因此“激活只能人工执行”不准确。

**F-02：有历史评测文档，当前没有配置可接受的冻结报告。** `docs/retrieval-upgrade/experiments/results-phase2.md` 记录 2026-10-23 的小样本阴性结果：混合没有证明优于加强词法，正例仅6，不能外推当前语料。这推翻“从未评测过任何语义能力”的绝对表述；也不证明当前版本已经评测或可以激活。

新增问题：`freeze_report()` 的 derived_sample_count 含 unknown，latency 门测的是 `p95_lexical_compute_ms`，`paired_interval()` 注明正式验收仍需 task-cluster bootstrap。不能以 unknown 凑足60条，也不能以本地计算延迟替代真实混合路径延迟。这些属于 Phase 0 必须收口的评测契约。

## 3. 核心链路重新核验

| 编号 | 代码事实（E3，测试另列） | 可以复用 | 尚未成立的能力 |
|---|---|---|---|
| F-03 | `writer.update():86 → commit_adapter.adapter_update():246` 可改正文，内容变化清 verified/计数；CAS 参数当前可省略 | 版本计算、单卡原子落盘、intent、读回 | 先候选、保留已发布版本服务、验证后切换的事务 |
| F-04 | `materialize._apply_candidate():201` 支持 create/update/link_existing/review；当前自动 executor 提示只允许 create/review | 候选/片段/租约、严格提交核验 | 自动 ENRICH/CORRECT/REPLACE/MERGE/SPLIT 内容闭环 |
| F-05 | `evolution.decide():67` 聚合当前版本 helpful/负面任务，返回 promote/retire | 纯决策函数、版本限定、策略开关 | 生成、对比、测试、发布新正文；任务独立性认证 |
| F-06 | `versioned_feedback.apply():89` 接受 referenced/read/adopted 回执；task_key 来自 host/session/task/revision；verification_source 是调用方枚举 | 回执幂等与恢复、版本绑定 | 相同真实任务跨宿主/会话/机器去重，执行结果认证 |
| F-07 | 无 task_result 的兼容 helpful 仍可按两工具标签晋升（`:207` 附近） | 只读保留历史 | V2 应撤销此晋升旁路；工具标签不是独立证据 |
| F-08 | `server.context_impl():341` 从 staging GOVERNED/active 按 updated/id 选3条 validation_candidates；`read_impl`/consumption 支持读回执，feedback 有验证分支 | 单独验证入口雏形 | 与当前任务相关的准入调度、stale 非治理卡复验路径、验证结果可信性 |
| F-09 | `writer._relation_side_effects():173` 有 supersedes/contradicts 等副作用与强度裁决 | 关系格式、冲突快照和隔离工具 | 自动发现事实冲突；多目标发布原子性；按环境分歧治理 |
| F-10 | `exchange_v2` 有白名单事件包、哈希、事件ID幂等、冲突返回、Git读回与逐事件ACK | 传输/队列/收据骨架 | 撤销、治理验证事件、独立性标识、授权认证、各机器治理投影 |
| F-11 | worker evidence_role 只有 sender/aggregator/off；接收落独立 aggregate DB | 现有Git分支、后台调度、离线 pending | 每机把远端证据安全物化到治理决策；启用开关不等于已实现 |
| F-12 | `store.parse():180` read_text 归一行尾；`index.source_current():218` bytes.decode 后 parse_text 不归一 | 固定 LF 写路径、来源核验 | 导入/外部编辑 CRLF 读视图仍不一致，不能只修一张存量卡 |

**staging 结论修正：** 不是绝对不可达死锁：已有 context→validation_candidates→read→绑定反馈→自动移动通道，当前测试覆盖 standard。真实问题是准入与发布后验证耦合、候选按时间选取而非任务相关性、三条上限可能长期遮挡其他候选、依赖 Agent 主动协议及自报结果。`status=stale` 不在该候选 SQL 中，非 GOVERNED stale 卡也没有同等专用复验路径；因此“有 revalidation_pending 就已闭环复活”同样不能成立。

**治理权限纠正：** 当前 `GOVERNED={standard,preference,bizrule}`，三类在有 provenance 和开关时都可自动激活，当前 activation_policy 两个 allowlist 均 null。不能沿用旧文档“standard/global preference 永远人工”的事实声明。V2 选择恢复其权威确认门，是新设计决策，而非现状描述。

**存储权威纠正：** Markdown 保存卡片内容和治理投影；反馈、task_outcomes、write_intents 等持久账本不是从 Markdown 可完整重建的缓存。V2 明确“内容资产与证据资产各有权威源”，不能沿用“删 SQLite 一律可无损重建所有状态”。

## 4. v1.1 勘误和整改建议去留

| 原位置/主张 | 重新核查后的结论 | 决定与替代 |
|---|---|---|
| §1/附录C capture 总数132；明细10+59+28+17+15+2 | **仍只合计131**，补 needs_review 并未消除算术不一致；本次140为另一快照 | 撤回旧明细的“已对账”结论，所有汇总绑定一个事务/时点 |
| §2.3、图、专项I仍写当前停摆/blocked旧积压 | 历史与当前混写；本次状态亦再次blocked，但不能追认连续时长 | 保留升级窗口风险；替换为本次快照，不覆盖历史 |
| §3、§9、P0-1等残留99.3%、0.66%、150倍坍缩 | 调用级和卡片回执级分母不同 | 全部撤回；采用率只在同一 task-run/card-revision/delivery 集合内计算，未观测保留unknown |
| §11仍使用 read+completed→derived_helpful、0.5票 | 阅读和任务成功均不证明该知识有效或被采用 | 撤回；L0/L1只分析，L2采用关联，L3独立执行验证后才影响治理 |
| §11/§14仍建议frontmatter计数/max跨机 | 无撤销、独立性、版本和因果去重能力 | 撤回；扩展exchange_v2承载版本化事件，治理从有效事件集合重算 |
| P0-1 ≥20条反馈/周、首张晋升作为成功 | 可通过伪反馈满足，不能证明内容或价值改善 | 替换为采用识别精度、错误晋升反例、前后任务效果与回归验证 |
| P0-2 差一次运行、评测从未发生 | 当前无可用报告为真；历史有阴性实验 | 保留质量门；不预设向量收益；unknown不计有效样本，真实延迟入门 |
| P0-3 双机各2条反馈至少一机晋升 | 两机可能复述同一任务或结果全自报 | 改为去重/撤销后同输入同决策；数量只是最低样本支持 |
| P1-2 cosine>N强制评审/相似即重复 | 向量衡量相似，不证明等价、冲突或可合并 | 候选召回→LLM按claim与条件判关系→确定性核验→验证候选 |
| P1-4 当前12张verified直接补当前版本锚点 | 无法证明其历史验证针对当前正文 | 默认迁入legacy_unanchored、兼容显示once；仅有可核实旧版本证明才补锚 |
| P2-2 staging只需补反馈流 | 存在专用入口，仍把准入绑定使用验证 | 提前做类型/风险分级 admission；不要求先被真实采用 |
| 自动晋升=自我演进，只需改反馈来源 | decide只动状态；update缺候选验证；自动执行器现禁update | 保留治理内核、重构输入，新增完整内容候选/修订差异/回归/发布机制 |
| 2个独立会话=2次独立任务 | 现task_key含host/session，真实任务可重复计数 | 跨机logical_task_key+origin_run_key+independence_group，未知身份不计独立 |
| needs_review 30天TTL等同材料丢失 | `sweep_review_candidates`处理候选，不等于所有spool物理删除 | 保留审阅超时告警；改为expired可重开与有引用材料保留，不直接指控已丢原材料 |
| 原验收状态变化0⇒decide从未调用过 | 4条绑定反馈足以调用decide，只是没有达到晋升条件 | 撤回“从未被调用”；本机无可核实自动晋升结果≠未执行决策函数 |
| 存在模块即可“E3+E2全覆盖/工程成熟” | 本轮测试只覆盖明确范围；多卡事务、跨机治理尚缺 | 用复用矩阵和逐项测试范围代替笼统成熟度分数 |

## 5. V2 设计决策及依据

| ID | 生效设计决策（D） | 保留/重构/替代 | 依据及验证门 |
|---|---|---|---|
| ADR-01 | 三闭环：内容演进、可信使用、持续治理；分别有输入/输出 | 替代九阶段线性模型 | 状态变化不能补正文；任务V2-04～11 |
| ADR-02 | Markdown已发布内容 + 不可变版本/候选manifest；SQLite是本地物化与运行账本 | 保留SQLite+Markdown+Git，澄清权威 | 当前表并非全可重建；V2-04/14 |
| ADR-03 | 八种知识关系属于变更提案语义，不建设八套服务/状态机 | 新增小型协调层 | 复用candidates/jobs/writer，V2-05/06 |
| ADR-04 | 候选不覆盖已发布版本；变更集验证/CAS后统一切换，多卡用manifest可见性门 | 重构写路径 | 单卡atomic_write无法保证MERGE/SPLIT；V2-07 |
| ADR-05 | L0/L1不影响可信度；L2只可限幅排序；L3才准入治理，L4证明独立复现/效果 | 替代derivedHelpful和自报晋升 | verification_source字符串不能认证；V2-01/10/11 |
| ADR-06 | admission与usage验证分离；standard/global preference需权威确认；bizrule核对出处和实例 | 重构首次准入 | 当前通道非死锁但不安全且打扰；V2-08 |
| ADR-07 | 扩展exchange_v2传版本证据、撤销、决策依赖；每机验证并重算 | 保留传输、替代仅统计聚合消费 | ACK≠可信、hash≠签名；V2-13/14 |
| ADR-08 | 同任务重放不增加独立任务数，复制产物不增加L4；不同环境可保留不同结论 | 新增业务去重而非新基础设施 | 当前task_key不能跨机去重；V2-09/13 |
| ADR-09 | 任务检索与演进检索分开过滤/评测，Provider和存储共享 | 重构检索用途 | 当前manifest排staging/stale；V2-03/05 |
| ADR-10 | 无模型走词法与规则，难判关系留审；有模型也不凭阈值合并 | 保留降级 | 历史阴性实验；真实POC通过前自动写入默认关闭 |
| ADR-11 | 低频重要卡不按低点击淘汰；失效作用到版本/适用范围 | 简化纯计数与时间治理 | 使用频率不是正确性；V2-12/15 |
| ADR-12 | 继续唯一固定运行环境，升级前备份、影子迁移和加载构建核对 | 保留运维约束 | 本轮发现登记漂移；V2-02/17 |

详细接口、状态、失败处理及采纳条件见架构对应章节；上述均为设计而非已实施结果。

## 6. 验证记录和未确认事项

### 6.1 本轮已执行

- 源码/报告/相关知识文档核验、Git历史及安装115模块只读比对。
- 一致读事务统计及单独补充查询；敏感配置只读白名单输出。
- 实际context回包观察：L2、无远程模型调用；当前知识同步刷新告警仍存在。
- 现有测试在隔离配置、临时知识目录、临时runtime下执行：**62 passed in 13.13s**。范围为`test_phase0_self_evolution.py`、`test_auto_evolution.py`、`test_versioned_feedback.py`、`test_phase2_eval_gates.py`、`test_value_exchange.py`、`test_autonomous_local_completion.py`；禁pytest缓存，固定临时KNOWBASE_CONFIG/REPO_PATH/RUNTIME_ROOT，清除宿主项目目录环境。没有改测试实现，没有执行V2新功能或六个真实场景。结果只支持已有机制，不证明LLM判断、原生多宿主或多机真实效果。

### 6.2 尚未确认（E0）及阻断条件

| 项 | 推荐默认方案 | 责任/验证里程碑 | 未通过时限制 |
|---|---|---|---|
| 当前模型能否可靠识别八关系及claim回归 | 现内网文本模型先做真实脱敏样本POC | 实施者+知识评审者，V2-05/06 | 不开放自动ENRICH或其他内容发布 |
| 各宿主可提供哪些工具调用/退出码/产物身份 | 本机原生事件优先，缺失返回unknown | 宿主适配者，V2-09 | 该宿主最多L1/L2，不伪造L3 |
| 跨机任务/成员独立性 | 项目稳定标识+原生任务键；无键不计独立 | 团队维护者，V2-13/16 | 不启跨机自动verified |
| 真实语义收益/覆盖率/在线p95 | 词法继续生产，影子任务与演进分别评测 | 检索维护者，V2-03/16 | L3任务检索保持未激活 |
| 团队信任名单和scope访问策略 | 默认同一授权团队内协作；非公开项目分独立Git远端/分支ACL | 团队负责人，V2-13前 | 未确认来源只观测，禁止治理影响；scope本身不当安全ACL |
| 内容复制、任务重复的自动归并是否准确 | 显式origin键优先；不确定先合为一组、可申诉拆分 | 证据维护者，V2-10/13 | 不根据机器数/会话数扩票 |
| 已存活MCP与worker加载构建 | 使用正式加载回执并重连验证 | 运维，V2-02 | 不能以文件一致宣称宿主升级通过 |

所有默认策略可由真实POC调整并版本化，但不允许降低“派生未知不晋升、旧证据不转嫁新版本、无权威不激活规范”的底线。

## 7. 可复现只读取证查询

以下仅是重取证模板，**执行时会得到新快照，不能拿新数覆盖本文件冻结快照**。不得把数据库正文、完整工具载荷或凭据粘进报告。

```sql
-- 使用 mode=ro 连接，先 PRAGMA query_only=ON; BEGIN;
SELECT status, confidence, staging, COUNT(*) FROM meta GROUP BY 1,2,3;
SELECT state, COUNT(*), MIN(created_at), MAX(updated_at)
  FROM jobs WHERE kind='capture' GROUP BY state;
SELECT COUNT(*) FROM jobs WHERE kind='capture';
SELECT state, host_profile, COUNT(*) FROM knowledge_uses GROUP BY 1,2;
SELECT outcome,binding,state,COUNT(*) FROM feedback_events GROUP BY 1,2,3;
SELECT result,verification_source,COUNT(*) FROM task_outcomes GROUP BY 1,2;
SELECT kind,source,result,COUNT(*) FROM task_evidence GROUP BY 1,2,3;
SELECT state,COUNT(*),SUM(expected_count),SUM(completed_count)
  FROM embedding_generations GROUP BY state;
SELECT * FROM retrieval_active;
SELECT level,reason,COUNT(*) FROM retrieval_observations GROUP BY 1,2;
SELECT kind,last_status,last_success_at,last_error,failure_count,updated_at
  FROM automation_schedule ORDER BY kind;
SELECT state,error_code,COUNT(*) FROM executor_runs GROUP BY 1,2;
SELECT COUNT(DISTINCT delivery_id),COUNT(DISTINCT use_id) FROM use_retrieval_links;
-- COMMIT 仅结束读事务，不作业务写入。
```

后续09:33左右独立补查 retrieval_deliveries=12、use_retrieval_links=1，task_outcomes中3条task_run_id均为空；usage_log 的 search316/auto_search311/inflight_search314、read102、feedback48。这些是不同粒度且不完整的观察，不计算检索转化率。

## 8. 结论边界

当前需要调整核心生命周期设计，范围包括内容候选与版本发布、验证准入、自动采用证据及跨机可信合并。可以复用多数底层组件，但不能只开几个配置就宣称升级完成。V2交付物定义了实现路径和验收门；**本轮交付为设计文档，真实业务能力仍 NOT_RUN**。

---

# 附录：核验记录（2026-11-05，基线 7e6f8cc）

> 本附录由另一核查会话（Muse 沙箱）追加，非原文作者所写。核查基线为 V2 已实施后的
> `7e6f8cc`（0.2.0rc9，含 65dfdcd V2 实现 + 7e6f8cc V2-01..V2-18 交付记录）。
> 核查方式：只读代码核查（4 组并行），未修改业务代码，未运行测试，未访问真实数据库。
> 原文基线为 `425f3fe`（V2 实施前），故原文 §8 结论「V2 = 设计文档，真实业务能力 NOT_RUN」
> 在本基线下**部分过时**——机制已实现，但接线状态需按本附录重新判定。

## A.1 原文 F-01～F-12 在 7e6f8cc 下的状态

| 编号 | 原文主张（425f3fe） | 7e6f8cc 核查结果 | 状态 |
|---|---|---|---|
| F-01 | 同版本号不能识别行为；登记 build 与安装字节不一致 | 仍成立。另：V2-02 已强制实测写入 installed_build_id（`V2-02/delivery.md`），但「宿主是否加载新构建」仍需加载回执 + 重连验证 | 保留 |
| F-02 | 有历史评测文档，无当前可接受冻结报告；freeze_report 含 unknown、latency 门测词法延迟、paired_interval 缺簇 bootstrap | **已修复**。`retrieval_evaluation.py:156` freeze_report v2 五门：unknown 不计样本；latency 门要求 `latency_basis='end_to_end_hybrid'` 且端到端 p95 ≤ 1000ms；paired_interval 按任务簇 bootstrap。`semantic_index.quality():47` 15 项校验只认 v2 报告 | 已修复，待真实报告验收 |
| F-03 | writer.update 可改正文但无候选-发布事务 | **已补机制**。`knowledge_lifecycle/changes.py:71` propose_change + `publication.py:60` publish：候选不覆盖已发布版本，manifest 原子 rename 为 durable commit point，发布前重读 base heads，独立 readback | 机制已实现，未接自动提案方 |
| F-04 | materialize 支持 create/update/link_existing/review，但自动 executor 提示只允许 create/review | 仍成立。`executor.py:315` PROMPT_INSTRUCTIONS 仍只允许 create/review；ENRICH/CORRECT/REPLACE/MERGE/SPLIT 无自动生成——全仓库唯一 `propose_change` 调用方是 `rollback()`（仅产 REPLACE） | 保留，属剩余工作 |
| F-05 | evolution.decide() 只聚合当前版本 helpful/负面，不生成内容 | 仍成立。decide() 为纯判定函数（无写操作），唯一生产调用方 `versioned_feedback.apply()`。内容生成/发布由 V2-06/07/08 的 changes/publication 承担，与 decide() 无调用关系 | 保留；V2 治理已另建 decide |
| F-06 | versioned_feedback task_key 来自 host/session/task/revision；verification_source 为调用方枚举 | 仍成立。另：V2-09/13 以 `contracts.logical_task_key()` + `origin_run_key` + `independence_group` 实现跨机去重；`verification_source=independent` 仍只是标签，不产生 L3 资格 | 保留；V2 链已另建 |
| F-07 | 无 task_result 的两工具标签 helpful 仍可晋升（旁路） | 仍存在但默认关闭。`versioned_feedback.py:218` legacy 旁路受 `legacy_promotion_enabled`（默认 False）控制，且要求 `not staged`。V2-18 列为待删项（16/17 未完成前不删） | 保留，待 V2-18 清理 |
| F-08 | staging 有 validation_candidates 通道，非绝对死锁；但准入耦合使用验证、按时间选 3 条、依赖 Agent 自报 | **设计已解耦，执行链未接通**。`admission.evaluate_admission()` 不检查使用流量；但 admission→发布链路的生产调用方只有 CLI（`lifecycle confirm/validate/publish`），无 MCP、无 worker、无自动提案方把 staging 卡转为 NEW 候选。默认配置下 V2-01 门关闭 decide() 晋升出口，staging 首次激活实际依赖人工 `knowbase promote` | 保留；属剩余工作（接线） |
| F-09 | writer 有关系副作用与强度裁决，但无自动冲突发现 | 仍成立。语义矛盾无自动发现；CONFLICT 提案占位后无自动裁决器；`reconcile.detect_divergent_heads` 检出并发头后待显式 MERGE/CORRECT，无自动跟进 | 保留 |
| F-10 | exchange_v2 有白名单包/哈希/幂等/冲突/ACK，缺撤销、治理事件、独立性、认证 | **部分补齐，位于另一层**。V2-13（`knowledge_lifecycle/evidence.py` + `trust.py`）提供 Ed25519 签名信封、三层去重、撤销 tombstone；但与 exchange_v2 **零代码连接**（两者目标表不同：agg_events vs evidence_events），且 V2-13 零生产调用（无 worker、无 CLI、无导入流程） | 部分实现，未接线 |
| F-11 | worker evidence_role 只有 sender/aggregator/off；远端证据落独立 aggregate DB，不进治理 | 仍成立。exchange_v2 目标仍为 agg_events（只做统计，治理不读）。V2-13 的 evidence_events 是另一条未接线的链 | 保留；V2-13 需接线 |
| F-12 | store.parse 与 index.source_current 行尾归一不一致 | **已修复**。V2-04：`store.parse_text` 入口统一 CRLF/CR→LF；CRLF 回归测试通过；快照保留磁盘原字节 | 已修复 |

## A.2 原文 §8 结论的更新判定

原文：「本轮交付为设计文档，真实业务能力仍 NOT_RUN。」

在 7e6f8cc 基线下，分三档重新判定：

| 档位 | 内容 | 依据 |
|---|---|---|
| **已实现 + 隔离测试通过** | V2-01 治理门、V2-02 运行时健康（retry_plan、build 实测写入）、V2-03 评测五门、V2-04 版本资产与 schema、V2-05 演进召回、V2-06/07/08 变更-验证-发布、V2-09 原生观测、V2-10/11 归因与 L3 验证、V2-12 治理 decide、V2-13 签名证据包、V2-14 一致性、V2-15 适用范围 | 61 项新增 V2 测试；全量 1126 passed / 1 skipped（7 项基线既有环境失败除外） |
| **已实现但未接入生产路径** | `observation.ingest`、`attribution.correlate`、`attribution.verify_execution`、`governance.apply_decision`/`recompute_all`、`evidence.export_package`/`import_package`、`reconcile.materialize_and_recompute`、`admission.evaluate_admission`→publish 链：生产代码中**零调用方**（仅测试调用）；仓库内无 validator 适配器实现；默认配置无 `validators` 键；`shadow_observe` 同样零生产调用 | 全仓库调用点 grep |
| **未实现 / 未验证** | L4 无任何赋值点（定义与生产者无法从代码确定）；八关系自动识别与自动生成（无代码从新经验判断关系）；MERGE/SPLIT 内容合成（依赖提案方）；语义矛盾自动发现与裁决；`redirect_ids` 是否被检索跟随（未确定）；真实语义收益评测、真实宿主接线、双机真实交换（全部 external_pending） | 代码核查 + 交付记录自述 |

**更新后的结论**：V2 的机制层已实现且隔离验证通过，但**自动闭环未打通**——从「观测→归因→治理→发布」的生产触发链缺失，默认配置下治理出口关闭，staging 首次激活与跨机证据同步仍依赖人工操作或尚未接线。原文「NOT_RUN」应修正为「机制 RUN（隔离），生产闭环 NOT_RUN」。

## A.3 本轮新发现的文档与代码不一致（E2）

1. `evidence.py` docstring 称未验签事件「保留 external_unverified 观察」，但代码该分支无 INSERT、仅计数（`evidence.py:112-119`）。
2. `contracts.logical_task_key()` docstring 称 unknown 不参与独立性计数，但 `governance.decide()` 未过滤 `unknown:` 前缀（实际影响有限：折叠为单键，最多贡献 1 任务 + 1 组，无法单独达到 3 任务 + 2 组阈值）。
3. `retry_plan` docstring 称 requeue_ready 要求「租约已释放、材料 spool 仍在」且 blocked_config 为「配置类原因」，但代码不检查 spool、不区分 blocked 的 error 内容；另存在第六类 `unknown_state`；`failed_permanent` 可手动/自动恢复（命名误导）。
4. `server.py:1-5` docstring 称「6 个工具」，实际注册 17 个 MCP 工具（`server.py:910-927`）。
5. V2-12 交付记录称 `recompute_all()` 为「worker/导入后重算入口」——函数存在且可调用，但仓库内无任何 worker handler 或导入流程调用它，「入口」目前只是可调用的函数，不是已接通的流程。
6. V2-13 交付记录称未验签事件「保留 external_unverified」——与第 1 项同源，代码仅计数不入库。

## A.4 授权跟进报告数字的核验边界

`docs/implementation/lifecycle-v2/authorized-followup/report.md` 称真实库上 `worker retry-plan`
结果为 72 failed_permanent（64 material_budget_exceeded + 8 index_changed）、4 needs_review、
8 requeue_ready。**本轮无法从代码核实这组数字**（无真实数据库访问权限）。

但代码核查确定两点归因修正：

- 8 条 `index_changed` 按代码只能来自 `semantic_index.build()` 的 `embedding_refresh` 任务
  （代码里唯一把 `index_changed` 写进 `jobs.error` 的路径）；`retry_plan` 不过滤 `kind`，
  会把它们与提炼失败混在同一类。把这 8 条解读为「提炼积压」是代码不支持的归因，
  应解读为「向量索引任务失败」。
- `material_budget_exceeded` 的根因是单批材料 + 相似卡合计超 `max_input_chars`（默认 128000），
  且失败路径为不可重试、整批失败、无拆分重试分支（`distillation.py:208-322`）。

另：报告未列 `lease_stuck` / `blocked_config` / `unknown_state` 三类，代码会产出这三类，
不能从报告推断其余三类为零。

## A.5 本轮核查方法与证据等级

- 4 组并行只读代码核查，基线 7e6f8cc；核查报告存于工作区（非仓库）：
  `knowbase-v2-factcheck-semantic-lifecycle.md`、`knowbase-v2-factcheck-exchange-attribution.md`，
  及 evolution/staging、distillation/扩展点两组（见会话记录）。
- 本附录采用的证据等级：E2 = 代码静态核查（已读实现，附文件行号）；E3 = 代码 + 测试验证；
  E4 = 真实生产数据验证（本轮无）。与原文 E0～E4 定义**不同**（原文 E3=源码实现、E2=隔离测试），
  阅读时注意口径差异。
- 未确定项：生产 dense 激活状态（取决于用户本机配置与数据库）；`redirect_ids` 是否被检索跟随；
  V2 相关测试是否通过（本轮未执行）；72/4/8 积压数字的真实性。
