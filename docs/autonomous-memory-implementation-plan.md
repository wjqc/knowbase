# knowbase 无人值守多 Agent、多人共享实施方案

日期：2026-10-24。源码基线：`d1eef60be091f7d6090af9f9a8b0eb000636cbcb`。

状态：设计交付；新增能力未实施、未接线，工程验证 `NOT_RUN`，真实跨成员验收 `external_pending`。本方案不构成生产配置、模型调用或升级动作。

## 1. 目标、边界和完成定义

目标：成员完成一次安装、身份登记和必要授权后，日常无需手工执行 capture、claim、submit、push、反馈补录和清理命令；各 Agent 的有效经验能够自动沉淀、共享、检索，并依据实际使用证据演进。

“无人值守”不意味着任何输入都必须成卡或自动判为正确。没有材料、无可复用知识、证据不足、预算耗尽、授权失效时，系统必须明确保留相应状态，自动处理其他任务。业务真值不可由完成率倒推。一次性配置/授权和确实需要业务责任人裁决的例外，与日常逐条人工审核分别计量。

沿用以下硬约束：

- Markdown + Git 仍为共享知识唯一真相；SQLite 检索索引可重建。本地队列、回执、使用历史不可由 Markdown 重建，必须独立备份。
- 保留现有 MCP 工具及用户数据，新增字段可选；正式提炼共用现有租约、材料分页、候选校验、writer 和提交恢复链，不另建写库旁路。
- 不引入公共模型默认依赖、本地推理框架或独立向量数据库。Embedding 服务与文本提炼执行器是不同能力，不假设 Embedding 网关同时支持文本生成。
- UserPromptSubmit Hook 不远程推理；只做规则、本地检索、观察和有界入队。后台提炼独立于前台任务。
- 只有一个固定运行环境，升级原地完成；禁止并行版本运行、保留旧 wheel；数据与配置备份独立保留。
- scope 是项目过滤，不是访问控制。首期适用于同一受信任 Git 访问域；跨部门权限隔离需另行设计认证服务。
- 本期无新增网页或看板交互，复用 CLI、doctor JSON、现有报告。前端 F1–F5 不适用，不扩展页面建设。

### 用户任务与合格结果

| 用户任务 | 输入与触发 | 可观察结果 | 不合格结果 |
|---|---|---|---|
| A 正常完成工作后自动沉淀 | 获准采集的会话增量、工具结果、项目身份 | 有价值结论成卡，或有证据的 no_candidate；完整材料覆盖可查 | 只有 queued、只读取了前半段、Agent 自述保存成功 |
| B 自动复用 A 的经验 | 新任务、已同步知识、明确项目范围 | 递送当前版本、实际采用可关联到任务结果 | 只命中、跨项目误用、采用旧版本却验证新版本 |
| 离线后自动恢复共享 | 待提交/待确认数据、网络恢复 | 无新增用户动作即可推送、拉取、重建视图、确认远端 | 需要再保存一张卡才触发重试、推送成功未读回就 ACK |
| 知识自动维护 | 当前版本反馈、独立任务结果、到期时间 | 晋升/失效/归档有证据；冲突可隔离并继续补证 | 提炼作业给自己 helpful、未知当正确、冲突拖停整库 |

## 2. 当前证据与缺口

以下为本轮只读审查快照，不能充当实施后的验收报告：

| 编号 | 已确认事实 | 源码落点/运行证据 | 需要补齐 |
|---|---|---|---|
| G1 | Hook 持久入队且前台不催办；serve 维护循环只维护向量 | `adapters/host_events.py:356`、`server.py:927`；本机 awaiting_agent=22 | 后台执行者及无人值守能力探针 |
| G2 | 派生评测按 search_id 匹配采用事件 event_id，实际二者不同 | `execution_eval.py:92`、`consumption.py:13`、`context.py:195` | 正式检索→递送→采用关联 |
| G3 | 没有采用被判 irrelevant；负面采用被推为查询不可回答 | `execution_eval.py:110–119`；真实库派生259条全部 irrelevant | 未知标签、独立相关性评测 |
| G4 | 跨成员验收按实例建链，完成事件未逐采用任务绑定 | `acceptance.py:160`；纯内存反例：同人三设备/无关完成事件仍通过 | 真实成员去重、任务/版本/use_id 同链 |
| G5 | 写后 push 和读时 refresh 已实现；周期任务仅执行 team sync | `writer.py:166`、`gitops.py:574`、`refresh.py:114`、`team/worker.py:122` | 无前台操作时的知识同步和证据交换 |
| G6 | 自动归档/候选清理主要由独立 CLI 触发 | `__main__.py:737–760`、`lifecycle.py:91` | 统一维护调度、重试与审计 |
| G7 | 真实库知识同步为 error，统计快照待确认11项；汇总事件库尚不存在 | 本机只读 SQLite/统计快照 | 运行根因定位、恢复验收；统计计数不能代替采用证据 |
| G8 | 当前源码与已安装 rc8 有31个 Python 文件不同或缺失 | 包文件逐项比较 | 发布构建/会话指纹一致性 |

真实库快照另有203张卡、5条使用记录、3条版本任务绑定反馈、2条完成任务结果；数量不证明相关性或实际净收益。历史宿主文档存在过期描述，实施时以版本绑定探针更新，不能将旧证据覆盖为新版本已通过。

## 3. 总体接线与复用决策

```text
获准的宿主事件 / 显式资料导入
  → 已有 adapters + materials + jobs
  → 本机后台 supervisor（每库租约；按 kind 调度）
      ├─ 提炼：claim v2 → 执行器 → 材料分页 → submit → writer → 独立读回
      ├─ 同步：知识 Git 收敛/推送/拉取 → 索引更新 → SHA 确认
      ├─ 证据：value_outbox → 最小包 → Git → import/receipt → 聚合报告
      ├─ 治理：租约回收、回执恢复、归档、候选清理、冲突复验
      └─ 可选向量：复用现有代际/Canary/熔断/恢复

任务检索 → search_id → 递送 delivery_id → memory_use/use_id
  → 同任务执行结果/反馈 → 当前版本治理与团队验收
```

### 决策与取舍

| 决策 | 选择及原因 | 不采用/延期项 | 验证状态 |
|---|---|---|---|
| 调度 | 一个本机 supervisor，复用 jobs 与现有 OS 调度实现；MCP 多连接只唤醒，不各自启动执行池 | 第二套任务队列、分布式调度平台 | 待验证 |
| 推理 | 优先适配已安装、获授权且实测可无人值守的 Agent；可选显式配置内网文本 Provider | 默认公共 API；复用 Embedding 凭据猜测文本协议；绕过宿主权限 | 先做 P1 探针 |
| 写入 | 执行器仅访问受限的提炼协议，所有候选走现有 writer | 让生成文本直接改 Markdown/Git/SQLite | 复用机制已存在，接线待实施 |
| 证据 | 区分直接观测、Agent 报告、独立验证；不以采用推断相关性真值 | 将行为样本直接升级为完整 Gold | 规则待修复 |
| 跨机 | 复用 Git/ACK 及 value exchange；统计与证据分通道传输 | 从每日总计反推逐任务采用链 | 待接线 |
| 形态 | 先完成 CLI/MCP/后台闭环，继续现有看板 | 新门户、Reranker、向量数据库、多版本 runtime | 本期决定 |

无文本模型/无可用执行器时，检索、保存与同步仍可用，自动提炼标为 `assisted` 或 `blocked_executor`。该环境不得宣称全自动沉淀。执行器发现失败不会请求用户在每个任务授权；只在首次接入或权限变化时给出一次明确诊断。

## 4. P0：先修复证据关联及验收口径

### 4.1 检索、递送、采用身份

不得简单把现有 SQL 改成按最近时间、标题或单个 context_event_id 匹配。一个 context 可以有多条查询，查询候选也不一定全部递送。

新增本地关联表（名称为拟定，迁移号在落地时从当前最大号后分配，不修改旧迁移）：

| 表 | 关键字段/约束 | 用途 |
|---|---|---|
| retrieval_deliveries | delivery_id PK；search_id；host_profile、session_id、context_event_id、task_id、task_revision、scope；memory_id、content_revision、rank；source_delivery_id 可空；created_at | 保存经过过滤/预算后真正递送的卡及来源查询；缓存递送引用原来源 |
| use_retrieval_links | use_id、delivery_id 联合唯一；search_id；link_basis；created_at | 采用回执与实际递送关联；多查询命中同一卡允许多来源，但有效采用按 use_id 去重 |
| task_evidence | evidence_id PK；task_outcome_id、task_run_id、use_id、memory_id、content_revision；kind、source、result、artifact_hash、locator、observed_at | 记录脱敏工具结果/检查证据，不把正文或密钥写进共享事件 |

`index.record_search` 返回的 search_id 必须在 `context.execute` 留存；完成最终候选过滤后再写 delivery。`consumption.record` 校验原有 context 候选及当前版本，再在同事务追加关联。直接 search/read 的旧调用继续可用；缺 task/context 的旧观察保留未关联，不自动晋升为有效采用。

历史只按现存明确关联回填；无法准确关联记 `legacy_unlinked`。不删除 usage/feedback、不重写历史 helpful、不以内容相同猜版本。缓存命中仍产生新递送身份，但复用查询来源，不虚增远程模型调用。

### 4.2 自动反馈的证据等级

| 状态 | 自动生成依据 | 对治理/评测的作用 |
|---|---|---|
| delivered/read | 正式协议递送/读取回执 | 仅曝光、阅读 |
| adopted | Agent 在同任务中显式引用采用，并通过服务端版本和递送校验 | 采用证据，不等于有效帮助 |
| completed | 宿主结束/Agent 任务报告；保留 verification_source | 任务结束证据，不等于知识正确 |
| helpful + completed | 同任务、同 use_id、同知识版本的结果与可追溯验证材料 | 按现有政策累计验证；证据来源分列 |
| independent_verified | 冻结断言/测试产物或独立验证器确实支持对应主张 | 可用于高可信验收，不能由提炼执行器自封 |
| unknown/unresolved | 未采用、没有结果、关系不明确、证据冲突 | 不作负例、不晋升、不以零补齐 |

Hook 可以观察工具结果并生成证据候选；判断“这个结果支持哪条知识主张”由受限验证器完成，并保存依据。Stop、退出码0、测试通过均不能单独生成 helpful；不支持验证的任务保持 unknown。普通用户不需要补录；缺失率进入报告。

### 4.3 评测与验收修正

- 修复 search→delivery→use 的关联后，再从真实库重新派生行为样本。没有采用保留 unknown；单卡失败不推出该查询在整个语料库不可回答。
- 派生样本用于行为、采用和版本回归；独立相关性 Gold 继续采用原任务书约60–100条审核样本，覆盖未召回的正确卡、零结果、跨项目混淆、中英术语、错误码和长输入。一次性评测标注不进入日常运行流程。
- 保留 A原检索/B查询提取/C真实向量/D混合/E门控五组；固定语料、模型身份、向量缓存、代码指纹、split，避免用同一批样本校准又验收。
- 跨成员检查先将安装实例归并到 member_binding_id；同一人三设备只算一人。每条复用边同时核对贡献者、消费者、内容版本、采用 use_id 与完成任务，不能以设备上任意任务完成代替。
- full_chain 需独立递送事件，不再以 adopted 自身替代递送；Agent 报告与独立验证分列。
- 收益对照必须同模型/版本、工具、任务预算和结果质量标准；独立新会话、交替实验顺序、防止跨组记忆与缓存污染。真实任务有副作用时使用隔离副本，不在真实业务重复执行两遍。
- 收益统计纳入提炼、验证、同步处理的背景成本及摊销口径；供应商账单不可得则实际费用 null。负净收益如实报告，不因报告自洽就判定收益达标。

## 5. P1/P2：无人值守提炼执行器

### 5.1 先验证能力，再启动长链

`Executor.probe()` 输出以下能力，不返回凭据：

```json
{"executor_id":"configured-agent","version":"observed-version","headless":true,
 "noninteractive_auth":true,"bounded_tools":true,"structured_output":true,
 "mcp_v2":true,"usage_source":"observed|estimated|unavailable",
 "status":"verified|unsupported|blocked|unknown","reason_code":null}
```

第一轮真实隔离探针：读取≥32KiB材料的后半段规则及例外→分页提炼→正式 submit→从知识卡独立读回。另测拒绝权限、模型限流、进程退出和恶意材料。现有 ZCode headless 曾因权限客户端不可用失败，不能默认已解决，也不通过放开全局权限绕过。

默认选择首个通过探针且已配置授权的执行器。内网文本 Provider 为显式备选：独立 endpoint/model/secret_ref/允许发送范围/预算，实际协议另验。无候选通过则阻断自动提炼批次，不阻断其他实施。

### 5.2 内部接口与限制

计划新增 `knowbase/automation/{supervisor,dispatch,executor,protocol}.py`：

```python
probe_executor(config) -> CapabilityReport
tick(repo, now, budget) -> TickReport
run_distillation(job_id, executor, budget, cancel) -> ExecutionResult
# 结果：saved | no_candidate | partial | retry_wait | blocked | failed
```

提炼请求包含 job_id、scope、host_session_id、protocol_version=2、extractor_version、material manifest/hash、租约与 fencing_token、预算和允许工具列表。允许读取本批材料、分页、读取同 scope 的相似卡、续租及提交；不允许任意 shell、业务仓库修改、外部网址访问或直接数据库写入。日志不记录租约密钥/认证信息。

如果宿主无法保证工具受限，则该宿主不作为默认后台执行器；使用结构化输出模式：本机控制器分页并持有租约，模型只返回候选 JSON，控制器调用相同的候选提交服务。两种模式共用 validator/materialize/writer，不建立第二个校验实现。

拟定默认：每库同时1个提炼任务；单批120秒、整个作业单次运行10分钟、材料页8000字符；30秒心跳/续租；每次最多2批，剩余回 awaiting_agent，按 scope 公平轮转。数字为初始预算，需 P1 探针校准。Token 上限及每日额度由配置显式给出，未知计量以输入/输出大小和调用数限额，不能假装精准计费。

### 5.3 状态与恢复

复用 `queued → preparing → awaiting_agent → claimed → validating → succeeded/no_candidate/needs_review`。不重命名旧状态；新增执行记录保存 executor_id/version、attempt_id、started/finished、结果指纹、usage 与计量来源。

- 网络/429/5xx：有界超时，最多3次，建议30秒/2分钟/10分钟退避并加抖动；Retry-After 优先，达到预算后停止本轮。
- 认证/许可失效：blocked；凭据/能力指纹变化后重探测，不无限重试，也不把无许可当无知识。
- 结果格式错：一次有界纠正，仍失败保留材料与诊断；无价值须提交有理由的 no_candidate，不以空响应假成功。
- 超时但提交结果未知：先用原 request_id 查状态/回执，已应用则读回；只有未提交时才能重试。每批使用稳定 request_id，重试不生成新候选键。
- 材料分页、处理游标、候选版本由现有服务校验；未处理片段不标 distilled。模型没读完必须 partial。
- 进程崩溃由租约失效接管，递增 fencing；迟到提交拒绝。禁止持 RepoLock/SQLite 写事务等待推理或网络。
- 提炼会话打自调用标识，沿用 `_drop_distiller_echo`；维护输出不重新进入材料队列，提炼自身不产生 helpful/真实成员任务收益。

## 6. P3：统一调度、离线恢复和跨机证据

### 6.1 后台生命周期

新增入口（均为计划）：`knowbase worker run|once|status|install|disable`。安装复用 team/worker 的平台实现和固定解释器，迁移旧 team 定时任务为兼容别名，确保每库只有一个调度所有者。

OS 负责启动/保活，SQLite 每库 supervisor 租约负责互斥。服务空闲时不轮询模型；各任务按 next_run_at 唤醒。MCP/Hook 只追加任务或发本地唤醒信号，不各自开执行池。Windows 当前调度依赖用户登录，其无人值守范围明确为用户登录期间；系统未登录或电脑休眠不承诺运行，恢复会话后自动续做。

jobs 内部增加 maintenance kinds，与 capture/extract/embedding 明确分派。公开 memory_job_claim 只领取 capture/extract，不得让旧客户端误领内部同步任务。按 kind 保留维护容量，capture 达200待办上限不使租约回收/同步饥饿。达到材料容量时记录 capture_gap 并提供可补采游标，不能静默丢弃。

| 任务 | 初始频率/预算 | 失败时行为 |
|---|---|---|
| 租约/未结算回执恢复 | 启动及每60秒 | 幂等恢复，异常记录后处理其他项 |
| 提炼 | 有材料且预算可用时 | 按 P2 退避；待补输入保持可见 |
| 知识同步 | 本地写后唤醒；周期60秒 | 自动重试，不要求下一次写卡；读取使用本地已知版本 |
| 统计/最小证据交换 | 默认900秒 | 本地提交先完成，远端未读回不ACK |
| 自动归档/候选清理 | 每日/启动补跑 | 分批、可恢复，不影响前台检索 |
| 冲突复验 | 新证据或相关版本到达；最长每日检查 | 打平保持隔离，不猜赢家；重试受预算控制 |
| 可选向量维护 | 沿用30秒健康调度及现有退避 | L3失败回L2/L1，不启动Hook远程调用 |

### 6.2 知识同步

复用 Git 白名单、租约、`integrate_with_conflicts`、base trailers、逐卡隔离、索引一致性校验。网络前释放本地写锁，Git收敛/卡片物化阶段按既有锁序执行。推送返回后读回远端目标 SHA，确认已包含目标提交才清 pending；网络未知状态只重查，不重造提交。fetch 更新与索引恢复都有 checkpoint，崩溃后按当前 Markdown 和已确认 SHA 修复。

当前内网 Git 失败先分层诊断 DNS/TCP、代理/认证、协议/远端 refs、Git/索引。记录脱敏错误类别；不自动改 VPN、网络路由、白名单或认证。网络恢复后应在一个调度周期内启动重试，这是待验证目标。

### 6.3 统计与采用证据分别交换

已有 team daily summaries 继续用于计数；复用 `value/outbox.py`、`exchange.py`、`aggregate.py` 增加自动最小事件包交换，支持真正的逐任务证据链。

- 本地 package manifest 固定 event_id 列表、payload_hash、schema、source installation/member、cursor 和目标团队。每包建议≤1000事件/1MiB，采用现有协议更严格的限制，不修改已发布包。
- 仅白名单最小事件字段入 Git；无查询、知识正文、原始工具结果、机器硬件标识或凭据。存放单独版本化证据分支，例如 `knowbase-value-events-v2`，不混入知识检索目录。
- 指定聚合角色自动拉取→验 schema/hash→幂等导入现有本地 aggregate DB→发布receipt。发送者独立核验 package_id、事件集合、哈希、团队与接收角色后 ACK。接收角色来自一次性团队配置，本协议不冒充中心认证。
- 即使“上传返回成功”也不ACK；收据丢失重发同包。事件冲突隔离单条、错误数量可见，不使无关事件停滞。
- 新增 `retrieval.delivered` 等事件需要显式协议版本/能力门；旧接收者继续处理v1，v2证据留待发送，不能将未知事件丢弃或将不完整链称通过。
- 一人多机按成员绑定合并；无绑定为 unknown，不以设备数/宿主名算人数。聚合角色关闭时采集与同步继续、验收保持证据不足。
- 成员绑定来自一次性团队登记/已有可信身份映射，记录来源和纠错历史。自声明绑定只能按其证据等级统计；没有足够身份依据时仅报告实例覆盖，不宣称真实人数。首期不为此新建SSO平台。

## 7. P4：项目识别、维护和一站式接入

### 项目识别

init/首次任务从规范化路径、Git项目身份及既有 scope_map 建立有出处的本地绑定；明确映射优先。不同 checkout 可映射同一项目，嵌套仓库采用最具体已确认映射。项目相似名不自动合并；冲突/无法确定时保持 scope_unresolved，后台仅补充项目元数据，不读其他项目知识来猜归属。

### 治理维护

定期执行已有 archive_sweep、review_sweep、intent/receipt 恢复；写卡后的 Git 同步和审计走统一写入内核，禁止维护路径绕过 base 记录、索引核对或版本事件。冲突复验在受限环境核对相关代码/版本和既有任务证据；无可执行验证器则保留 open，报告其年龄与原因，不能为“零待办”强选赢家。

当前源码把受治理类型激活统一接到证据策略；实施时显式记录团队允许自动激活的类型/范围和policy_revision，沿用已有授权范围。未授权类型仍可收集证据和存候选，不因新worker隐式扩大权限。业务规则无出处不会因重复执行自动成为事实。

### 接入与观测

init 增量组织现有 Agent 选择、固定runtime检查、知识远端/同步、成员绑定、执行器探针、后台安装和可选Embedding配置。保持可跳过模型；需要的授权一次完成，不能把新成员默认标自动就绪。

拟定 doctor JSON 增加：source/runtime build、活跃服务build、各宿主加载状态、worker租约/心跳、executor能力、队列年龄/覆盖缺口、检索实际等级、scope_unresolved、同步目标/确认SHA、pending/ACK年龄、证据关联覆盖率、最近降级原因。

配置完成/协议通过/宿主已加载/真实任务已验证分别输出；旧会话重连前标 reconnect_required。非阻断性问题不持续向普通Prompt注入；只有首次状态变化、服务不可用或需授权才提示一次。

## 8. 数据、接口、并发和迁移

### 权威与新增数据

共享卡片仍以Markdown为准。retrieval_deliveries、use links、task evidence、executor runs、supervisor lease/schedule 是本机运行账本，0600保存，不进知识Git。聚合库沿用现有value分析库，只接收最小事件，不接收他人完整memory.db。

拟定 `executor_runs(run_id PK,job_id,batch_id,executor_id,executor_version,fencing_token,request_id UNIQUE,state,started_at,finished_at,error_code,usage_json,usage_source,result_hash)`；`automation_schedule(task_key PK,kind,next_run_at,last_success_at,config_fingerprint)`；`automation_lease(singleton PK,owner,lease_until,fencing_token)`。job生命周期仍只在jobs，不在runs复制第二套权威状态。

旧MCP参数保持兼容。新字段通过capability/version协商；旧客户端缺身份时仅普通访问，不隐式生成有效采用。新增内部错误：EXECUTOR_UNAVAILABLE、EXECUTOR_AUTH_REQUIRED、BUDGET_EXHAUSTED、TASK_EVIDENCE_UNRESOLVED、DELIVERY_LINK_MISSING、PEER_SCHEMA_UNSUPPORTED；可重试性及next_run_at必须结构化返回。

### 事务和恢复

- 沿用 RepoLock→SQLite；网络/模型调用在锁外。同一receipt与最小事件在同事务追加，跨文件阶段沿用intent/回执恢复。
- 调度租约为跨进程；领取、续租、提交同时校验owner/fencing。释放不能覆盖后续持有者。
- scheduler选中不等于claim成功；claim失败转下一项，不重复启动。scope公平轮转，前台锁繁忙短退避。
- 模型应答/submit重试检查幂等回执优先于版本过期判断；旧版本实际未应用才返回冲突。
- 磁盘不足/坏材料：停止受影响任务、保留引用和错误，基础读取继续；不能清空队列换取健康状态。

### 升级和回退

1. 冻结发布commit和构建指纹，先完成隔离回归；本次历史733通过不是d1eef60或后续版本的验收。
2. 在线备份 memory.db、队列/spool、team state、身份、配置和宿主/OS任务描述；核对备份可读。
3. 暂停后台领取，等待或取消已知维护作业；原MCP连接完成已进行操作后排空。宿主无法安全重连时明确会话pending，不能忽略旧进程。
4. 固定runtime原地安装；按顺序应用现有23/24及本期追加迁移，检查旧checksum。禁止把当前22直接当作已完成全部迁移。
5. 比对卡片内容、身份、历史账本数量和约束，隔离验证正式MCP/Hook与build后再启worker。
6. 单scope灰度→本机→真实三成员试点；worker可独立关闭，不关闭基础检索。重新同步按已确认ACK继续，不重建成员身份。
7. 故障优先关闭新增automation/策略并前向修复。旧代码对新增迁移不兼容时不得强行降级；使用兼容补丁在同一路径安装。整库备份恢复只作为明确灾备，并报告/恢复备份后新增历史，不能用整库回退处理向量故障。
8. 删除临时wheel/构建产物，保留Git源码与私有数据/配置备份；分别记录已安装、后台已启动、宿主实际已加载。

## 9. 分批实施、文件落点与退出条件

以下为顺序依赖，不以预计工期代替验收；实现由项目开发者负责，环境能力由对应Agent/网关维护者负责，真实试点由成员A/B/C正常任务产生证据。

| 批次 | 用户结果与主要落点 | 必做验证 | 退出条件/未通过限制 |
|---|---|---|---|
| P0 证据修复 | context/index/consumption/execution_eval/acceptance/value events；追加迁移；检索→递送→采用准确关联 | 正式MCP产生不同search/event ID；多query/缓存/预算过滤/旧版本；一人三机、无关任务、单卡失败反例 | 不再制造irrelevant/unanswerable；人数及任务链不误过；未过禁止用派生标签启用模型/宣称团队收益 |
| P1 执行器验证 | 新automation executor/protocol、integration探针；复用materials/job APIs | 获授权的真实headless读取长材料后半段、提交读回；恶意材料、认证失败、usage来源 | 至少一个真实执行器通过受限工具和正常许可；未过不得启动自动提炼，只继续独立开发 |
| P2 自动沉淀 | automation supervisor/dispatch、jobs、server唤醒与自调用抑制 | 无前台催办自动成卡/合理no_candidate；杀进程/断电恢复；两宿主争抢/迟到提交；预算与公平性 | 无重复卡/越权/假完成/自激循环，队列实际消化；失败不影响检索 |
| P3 同步与证据 | gitops/refresh、team transport/service、value exchange/outbox/aggregate；OS worker复用 | 断网后停止前台操作再恢复；SHA/ACK读回；重复包、收据丢失、旧peer协议、冲突无关卡同步 | 不靠新用户动作恢复；统计/证据口径分离；未过不得宣称多人自动共享 |
| P4 维护与上线 | lifecycle/materialize/quarantine、init/integration/doctor、构建发布与README统一 | 定时入口真实调用；scope绑定；错误回退；迁移22→最新版；固定路径stdio/Hook；重连指纹 | 所需维护自动触发，身份/卡片/历史保留，软件和会话状态可辨认 |
| P5 真实试点与质量 | acceptance/retrieval evaluation/benefit control；脱敏报告与独立预期 | 三名真实成员A→B→C、每宿主正式入口；冻结评测；同任务质量相当的成本对照 | 分项报告，不用一个PASS覆盖缺失任务；失败回退受影响能力，基础知识库继续可用 |

每批用独立提交记录实现和验证；代码修改时更新相邻正式入口/失败恢复测试，不能只加入直接调用impl的测试。设计当前不要求修改生产team-stats或真实数据。

依赖关系为 `P0 → P2`、`P1通过 → P2启用`、`P2/P3 → P4 → P5`；P1执行器探针及P3网络诊断可与P0并行，P3的本地开发可在执行器不可用时继续。不能把真实模型或网络阻塞扩散为所有工程工作停滞。

## 10. 验收矩阵及建议门槛

数值为初始建议，实施前冻结样本和测量方法；如调整须记录原因，不能看到结果后降低门槛。

| 项目 | 建议验收方式/门槛 | 不能替代它的证据 |
|---|---|---|
| 自动提炼 | 固定长材料、短材料、无价值、恶意输入共≥12例；自动处理覆盖应处理片段100%；无重复成卡/虚构出处 | queued、模型返回JSON、读完分页 |
| 日常无人值守 | 三名真实成员、至少7个自然日；每人≥3个正常任务；统计常规维护人工次数与异常次数，常规次数目标0 | 同人三设备、人工代替Agent调用claim/submit |
| 多Agent | 每个声称支持自动的宿主至少一条真实任务全链；能力不足宿主明确assisted | 修改配置、列出工具 |
| 共享恢复 | 离线保存后不再发用户请求，恢复网络，2个调度周期内启动并完成可用网络下同步；不重复ACK/记账 | 仅进程重启、push退出码0 |
| 采用证据 | 每条有效链关联任务、知识版本、递送、use_id、结果；未知单列；同人多机合并 | 命中率、阅读次数、设备数 |
| 检索质量 | 冻结60–100条独立审核样本；Recall@5、Precision@3、MRR@10、无关注入率、正确拒绝率、p95和Token均报告；无关/不可回答不得被强召回；模型上线要求质量及退化场景达标 | 仅执行派生标签、合成向量、单次人工演示 |
| 成本 | 建议≥20组配对任务、相同质量验收和模型身份；报告置信区间、背景成本、计量覆盖；净收益为负不通过收益门 | 调用次数、字符估算或未校验任务成功的少Token |
| 故障 | 认证、429、超时、模型漂移、索引坏、网络断、进程退出、磁盘满均有行为断言；基础检索不被破坏 | 全部测试总数、只测happy path |

### 核心反例清单

1. 提炼器只看前8000字符：不得把后半段标处理完成。
2. 模型超时但服务端已落卡：恢复只能读回同一结果，不能再造卡。
3. 任务完成但未采用知识：不得计有效帮助；未采用也不得自动判无关。
4. 某卡失败但库中另一卡可回答：不得将查询标unanswerable。
5. 同一成员三电脑：不得通过三人门槛；A→B→C的任务须与边逐条绑定。
6. 新版卡继承旧版helpful：拒绝晋升；缓存递送不得绕过版本校验。
7. 统计包上传成功但无远端确认：pending保留。
8. 知识冲突证据打平：隔离双方，其他卡继续同步；不因超期强选赢家。
9. 所有MCP宿主退出：OS worker仍处理已获准任务；无可用模型则保留blocked，不宣称成功。
10. 新包已安装但宿主仍旧build：doctor标不一致，不宣称该宿主生效。

## 11. 风险、假设和准入

| 风险/假设 | 责任与验证时点 | 不通过时的限制与恢复 |
|---|---|---|
| 现有Agent可无人值守且限制工具 | Agent适配实现者，P1真实探针 | 该适配器assisted；验证显式内网文本Provider后再放行P2 |
| 内网Git可用且权限正确 | 环境维护者，P3前诊断 | 本地继续；禁止修改路由/泄露原始错误；恢复网络/权限后自动重试 |
| 自动判断的helpful与真实帮助一致 | 验证器实现者，P0/P5独立样本 | 降为Agent报告或unknown，不用于高可信自动晋升/收益结论 |
| 行为数据覆盖不足/选择偏差 | 评测实现者，P0后和P5 | 继续收集独立Gold，不自动补造正负样本 |
| 多实例身份不等于多人身份 | 汇总实现者，P0反例/P5真实成员 | 未归属单列；不通过跨成员门槛 |
| 后台成本反超收益 | 执行器/计量实现者，P2预算/P5对照 | 降频/限量/暂停提炼，保留检索与同步；账单未知不填0 |
| 迁移后旧代码不兼容 | 发布实现者，P4隔离升级演练 | 使用兼容前向补丁；不得整库回退丢新反馈 |

没有需要当轮用户补充的骨架裁决；上述默认选择可以在实施前调整。真实执行器、内部网络和三成员试点能力尚待验证，不凭方案宣称具备。

## 12. 设计维度覆盖

| 维度 | 本方案落点 | 状态 |
|---|---|---|
| D1定位边界 | §1用户任务与原硬约束 | 已定义，结果待验 |
| D2概念状态 | §4身份链、§5状态与未知 | 已设计，未实施 |
| D3数据权威 | §1、§8Markdown权威/运行账本/迁移 | 既有约束沿用，新增待验 |
| D4依赖分层 | §3、§5执行器与Embedding分离、共享writer | 已设计，执行器待探针 |
| D5接口契约 | §4、§5、§8版本/错误/幂等 | 已设计，未接线 |
| D6并发一致 | §5租约/§8锁序与回执恢复 | 机制复用，组合待验 |
| D7失败模式 | §5/§6/§8故障恢复 | 已设计，故障注入待实施 |
| D8安全信任 | §1信任域、§5受限执行、§6白名单事件 | 已设计，适配器隔离待验 |
| D9有界性能 | §5预算、§6公平/限额、§10p95 | 初始默认，待校准 |
| D10可观测 | §4证据分级、§7doctor、§10覆盖率 | 已设计，未实施 |
| D11演进版本 | §6v1/v2协商、§8追加迁移 | 已设计，兼容待验 |
| D12部署运维 | §6OS生命周期、§8唯一runtime/恢复 | 已设计，发布演练待验 |
| D13测试验收 | §9–§11正式入口、独立预期与反例 | 可实施验证计划，NOT_RUN |
| D14人与流程 | §1一次授权/日常零维护、§9责任、§11例外 | 已定义，试点待验 |
| F1–F5前端 | 本期不新增/改造网页，只提供CLI/MCP与报告 | 不适用；日后页面实施另交设计稿 |

设计覆盖完成不代表实现就绪：P1执行器能力、P3网络/跨机接线、P5质量与收益仍是明确门禁。
