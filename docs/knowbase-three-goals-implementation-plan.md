# knowbase 三项核心目标实施方案（审核稿 v1.3）

日期：2026-09-23。代码基线：`3c63534`。状态：待用户审核，未授权据此实施。本文提出的模块、接口、数据库表和指标均为设计目标，不表示已经实现或验收。

v1.1 用户确认：复用宿主 Agent 的模型能力。knowbase 不新增模型 API、endpoint、密钥或模型调用后台；总结、提炼及语义判断由当前宿主 Agent 完成。此次确认仅固定这一架构选择，其余方案仍待审核。

v1.2 修订：增加 M0.5 独立止血包及架构决策门、开发期隔离、内容/治理版本分离、反馈基线规则、宽召回和提交复查、SQLite 并发与队列灾备、同步参数及显式验收工时。本次授权为修补方案，未启动实施。

v1.3 修订：根据复审补齐路线无关的本地任务组件、M0.5 同步观测与跨进程 TTL、Agent revision 指令迁移。记录收到的 v1.2 复审结论为“通过，三个非阻塞项随 M0 冻结”；本次只补文档，不表示实施或运行验收完成。

## 1. 目标与交付定义

1. Agent 对话结束或到达检查点后，自动识别、提炼、查重、保存值得复用的经验，并提供可追踪结果。允许结果为“没有可沉淀经验”。
2. 用户明确指定目录和项目后，已有知识库经过解析、定位、切分、提炼、查重，形成可检索知识；原始材料与知识卡均保留来源。
3. 多个人、多台机器同时新增或更新经验，独立写入能够持续同步；同一卡片的并发修改能够检测、保留并单独解决，不能阻塞其他卡片。

“可复用”包括项目内可复用和跨项目通用两类，不强制把项目经验泛化为 global。默认保存到明确项目 scope；升级为 global 需要明确的适用边界和人工确认。

自动保存与自动采信分开：普通经验通过质量校验可自动成为 active/once 卡；证据不足的候选进入待审核。人员标准、业务规则和用户偏好继续经过人工审核。自动注入保持 active + verified + exact scope/global，检索返回 once 时明确提示未验证。

## 2. 基线核对与差距

| 目标 | 当前代码证据 | 缺口 |
|---|---|---|
| 自动沉淀 | `hooks.py:_transcript_stats/stop_event` 统计工具调用并提醒宿主 Agent 保存 | 未解析完整语义、没有持久提炼任务、缺少失败恢复与候选管理；已有一次 save 会掩盖会话后续新经验 |
| 导入解析 | `__main__.py:cmd_import` 调用注册解析器；`sources.py:import_source` 保存解析文本和 manifest | 已有解析，不应重做一套；缺少稳定原文定位、切分、提炼、版本关联与知识卡生成；解析在 RepoLock 内执行 |
| 多人更新 | `gitops.py:push/sync_before_read` 支持 ff/rebase/重试 | Git 文本合并没有卡片级并发语义；流水号冲突、公共 INDEX 冲突和无关卡片阻塞仍存在 |
| 身份与审核 | `config.py:agent_name` 读取环境变量，人工 CLI 执行治理操作 | Agent 名是归因标签，不是认证；Git 共享仓库意味着成员能读取共享内容，scope 不是保密 ACL |
| 检索 | `index.py` 维护 FTS、字符 n-gram 向量、使用日志；Hook 已限制注入置信度 | 标题相似去重不是语义去重，字符向量不是经过验证的语义模型；新提炼流程必须保持 scope 隔离 |

本轮复读还发现以下需要纳入实施的明确风险：

- `store.alloc_id` 与 `sources.alloc_id` 使用本地最大序号加一，跨机器可分配同一个 ID。
- `sources.find_by_sha` 只按文本哈希去重，同内容跨 scope 导入会直接返回先前 manifest，不能表达独立归属。
- 所有保存重写并提交 `INDEX.md`，两人写不同卡也可能冲突；此前双克隆测试仅改不同普通文件，不能证明真实 memory_save 场景通过。
- `update_impl` 未提供 expected revision，读后更新期间别人已经修改时无法识别旧版本提交。
- `_rebase_onto` 把所有 rebase 异常都归为真实内容冲突，并忽略 abort 失败，当前告警不能证明一定已恢复。
- `push` 合入远端后未重建索引；读路径可能在本地已领先时调用 push，而没有显式遵守 auto_push=false。
- 当前同步推送仍在写请求里等待网络；`failed to push some refs` 被视为竞态的范围过宽。进程内 TTL 和吞掉同步状态记录异常也不适合做多人可靠性保证。

补充核对：`lifecycle.apply_feedback` 增加共享 frontmatter 计数，但晋升读取本机 `feedback_log` 的 helpful 总数和 distinct 工具数，另有 human 前缀分支。因而跨机累计与晋升依据可能不一致；“两人各反馈一次一定晋升”并非现行规则。工具标签和 human 前缀不证明独立人员或人工权限。

以上风险为静态代码审查发现；未新跑故障注入，不描述为已在生产复现。INDEX 是共同修改热点，并非每次必然冲突。现有 Stop Hook 返回 `decision: block` 是可复用的继续执行雏形，但仍需逐宿主验证。

## 3. 架构选择与边界

沿用 Python MCP、现有解析器、Markdown/YAML 共享文件、同一个本地 memory.db 和 Git 远端。增加统一写入服务、持久任务队列、宿主提炼协议、独立同步工作进程及不可变操作记录。宿主 Agent 承担语义工作，MCP 提供材料与确定性校验。暂不部署中心数据库或另建一套平行 V2 运行时。

数据流：

```text
宿主会话适配器 / 显式目录导入
  → 本地持久任务 → 解析与分段 → 宿主 Agent 领取材料并提炼/判断重复
  → MCP 接收候选 → 证据引用/适用范围/结构检查
  → 统一写入服务 → 不可变操作记录 → 卡片与检索索引
                              ↓
                      同步工作进程 → Git → 其他成员重建视图
```

本地 `.knowbase/local/` 保存脱敏前会话、导入绝对路径及任务输入，不进入 Git。共享 `operations/` 保存已脱敏的知识变更，`sources/` 保存允许共享的材料和定位信息。Markdown 卡片、INDEX 和 SQLite 检索表逐步变为可重建视图；不可变操作及 source 对象成为新版本知识的权威记录。

上述操作记录模型为目标架构候选，涉及数据权威迁移，必须有版本闸门和导出回滚。先独立交付 §10 的 M0.5，再经过架构决策门决定是否启动 M1/M2。止血包继续以 Markdown 为权威，不引入操作日志权威切换；§4 的操作记录设计仅在选择该路线后生效。

决策门可以选择：继续操作记录路线；或暂留轻量形态，先验证宿主提炼和导入。后者必须保留“跨机同卡修改可能阻塞同步”的未完成项，不能关闭目标 3。两机离线时本机 CAS 可分别成功，Git 无文本冲突也不保证语义正确。任何替代方案只有通过同卡并发检测、双方保留、无关实体持续同步的验收，才能替代操作记录路线。

适用团队：同一个受信任 Git 访问域、小型团队、允许最终一致和离线写入。若必须做跨部门保密、不可绕过的审核权限或实时强一致，改用中心写入服务与认证授权；不能用 scope 或环境变量伪装权限控制。该扩展不在本次默认实施范围。

## 4. 数据模型与并发协议

### 4.1 标识与版本

- 新卡、新 source、新候选、新操作采用带类型前缀的 UUID，例如 `P-<uuid>`、`SRC-<uuid>`、`OP-<uuid>`；旧 ID 原样保留，所有校验、查找、CLI、Hook、测试统一兼容两种格式。
- `content_revision` 仅覆盖 baseline/create/update/resolve 的内容操作；title/body/tags/scope/relations/source_refs/code_refs 均属于内容。普通 feedback、review、archive 不改变它。规范化固定键排序、UTF-8、JSON 版本，并对参与的操作 ID 排序，排除本机路径及处理时间。
- `governance_revision` 覆盖审核、归档、验证和改变可写性的状态；archive、stale 化反馈使该版本改变。update 的 `expected_revision` 是 content_revision 的兼容名，同时必须携带 expected_governance_revision。锁内重新检查状态/权限；普通 helpful 不使内容更新冲突，归档不能被旧内容更新复活。
- read 返回两种 revision；审核必须引用待审核内容 revision，内容变更后旧审批不能自动套用到新版本。resolve 分别检查内容 heads 和治理状态；反馈关联被评价的 content_revision，旧版本反馈不自动验证新版本。
- 更新操作携带 `base_revision` 和父操作 ID；同一父版本上有两个不同内容更新即形成分支，不能以时间戳或机器顺序选赢家。
- 每个操作记录 `schema_version/op_id/entity_id/scope/kind/parents/base_revision/payload/actor/created_at/request_id/payload_hash`。身份用于审计，可信度上限遵守第三节。
- 操作类型：create、update、feedback、review、archive、resolve、baseline。反馈按 op_id 唯一计数，不合并可被覆盖的累计数。

### 4.2 文件与 SQLite

共享文件：`operations/<entity-id>/<op-id>.json`、`sources/objects/<hash>`、`sources/manifests/<source-id>/<version>.yaml`；新增文件通常无共同编辑热点。数据仓库的 `INDEX.md` 不再跟踪，卡片视图迁入忽略目录；源码仓库的文档不受此规则影响。

在现有 memory.db 增加下列表，通过事务迁移，不创建第二套数据库：

| 表 | 必要字段与约束 |
|---|---|
| schema_migrations | version 主键、applied_at、checksum |
| jobs | job_id、kind、scope、input_hash、idempotency_key 唯一、state、attempt、next_run_at、lease_owner、lease_until、fencing_token、batch_cursor、result、error |
| candidates | candidate_id、job_id、scope、body_json、evidence_json、decision、duplicate_of、version；job+candidate_key 唯一 |
| applied_operations | op_id 主键、payload_hash、entity_id、applied_at；重建可清空 |
| entity_heads | entity_id 主键、revision、heads_json、conflict_id、view_hash；重建可清空 |
| conflicts | conflict_id、entity_id、base、heads_json、status、resolution_op；从操作可重建 |
| sync_outbox | op_id 唯一、state、attempt、next_run_at、last_error、remote_ack_revision |
| source_versions | source_id、version、scope、raw_hash、text_hash、parser_version、locator_hash；source_id+version 唯一 |

usage_log、feedback_log 和既有统计保留；队列和未共享输入不是检索派生物，备份必须包含。entity_heads 同时存 content_revision/governance_revision，不能只存一个混合 revision。

反馈迁移由指定迁移负责人在冻结远端 revision 上执行一次，输出共享 migration_id 和每卡基线哈希，其他机器只导入同一份。frontmatter 计数保存为 legacy_reported_count，仅供历史展示；各机原日志单独备份为 legacy evidence，不相加、不猜测跨机重复，也不伪造新反馈操作。

已 verified 的旧卡保留迁移时状态并标明 legacy provenance；未知计数不把 once 自动升级。新晋升只按同一内容版本的共享反馈操作去重统计，保留现有总数/不同工具阈值的政策含义，明确“不同工具”不是“独立人员”。人工晋升只能走人工 CLI 的明确动作，禁用 Agent 标签 human 前缀绕过。历史与新增计数分开展示；发生基线差异停止迁移该卡并列入核对清单。

### 4.2.1 同机数据库并发与灾备

沿用 WAL；每连接显式设置 busy_timeout=5000ms。写事务用 BEGIN IMMEDIATE，事务外完成解析、网络和宿主推理，锁冲突仅做最多三次短抖动重试，耗尽返回可重试错误。涉及仓库和数据库时固定 RepoLock→SQLite 顺序；job-only 事务不取 RepoLock，禁止持 SQLite 写锁等待仓库锁。迁移由单实例启动锁执行。

SQLite 事务不覆盖文件系统。卡片视图写临时文件后原子替换，数据库记录 applied revision；崩溃恢复根据权威操作重建视图，读路径校验视图 revision 后才提供内容。

接受任务前，将最小输入 manifest、来源快照引用和幂等键持久保存到本地 spool；返回 queued 前确认落盘。spool 使用原子文件与校验和，提交回执记录批次/request_id。使用 SQLite 在线备份每日及升级前快照，保留最近七份；不同于直接复制活跃 WAL 文件。检测 DB 损坏时暂停写入，保留损坏库，恢复备份后重放操作、spool 和回执。恢复时轮换 lease epoch，使旧租约失效。未完成任务仅能恢复到已持久批次；缺失输入明确 needs_input。没有备份的本机历史 usage 无法凭 Git 重建，必须报告实际损失窗口。

### 4.3 写入与恢复

1. 校验 scope、引用、治理类型、请求幂等键及内容/治理 expected revisions。
2. 在 RepoLock 内重新检查当前 revision；将规范化操作写临时文件，fsync 后原子改名。
3. 同一 SQLite 事务登记操作、更新索引及同步 outbox，按 §4.2.1 更新文件视图；一致后返回 local_saved。Git 提交失败单独记录 pending，不抹除已经持久化的操作。
4. 若写文件后崩溃，启动扫描未入账 op_id 并补齐；SQLite 已提交而应答丢失，按 request_id 返回同一结果。同 key 不同 payload 返回 IDEMPOTENCY_CONFLICT。
5. 跨机器重复执行同一来源任务，以确定性的 candidate_key 和 create 幂等标识防重；同 key 不同提炼输出保留变体进入审核，不静默覆盖。

### 4.4 同一卡片冲突

本机过期版本更新立即返回 REVISION_CONFLICT。离线两端各自成功写入后，同步检测分支，生成 conflicts 视图；普通检索暂时排除冲突卡，人工调查可读取共同基线和双方内容。其他实体继续物化、推送和检索。

`resolve` 必须引用全部已知 heads 和当前冲突 revision，生成新的解决操作；解决时又收到新分支则返回冲突，不能覆盖它。删除采用 archive/tombstone 操作，禁止靠删文件表达共享删除。

## 5. 对话自动沉淀

### 5.1 接入能力

MCP 本身无法被动看到宿主完整对话。新增 `adapters/` 归一化事件：session_id、message_id、role、text、tool_call_id、tool_result、timestamp、project_scope。角色和工具结果保留，防止把模型自述“成功”当作实际验证。

Claude/ZCode 优先基于当前 Hook 及 transcript 入口实现并用真实录制样本验收；不能仅凭配置声称支持。Hook 在宿主允许继续执行时返回待处理 job_id 和领取指引，让当前 Agent 完成提炼后结束；Hook 自身只入队。Trae 或其他无可用结束 Hook/导出通道的客户端提供显式 `memory_capture`，接入状态显示 assisted，不能标成自动。配置化 transcript 轮询只能发现并入队任务，不能唤醒或替代宿主推理。禁止默认扫描所有聊天数据。

### 5.2 触发与任务

- Stop Hook 和长会话检查点只持久入队，目标 1 秒内返回；宿主收到任务后继续调用 MCP 完成总结。同一检查点的继续提示有上限，宿主未完成则保留 awaiting_agent，避免无限阻断结束。
- 幂等键：host_profile+session_id+checkpoint_hash+scope+extractor_version。会话继续追加生成新 checkpoint；已有一次 save 不阻止后续检查。
- `queued → preparing → awaiting_agent → claimed → validating → succeeded/no_candidate/needs_review`；预处理失败进入 retry_wait/failed，依赖缺失进入 blocked。宿主退出或租约到期回到 awaiting_agent，不算已总结或失败。
- 本地 worker 只做解析和校验；宿主通过 MCP 原子领取批次，获得 lease_token、到期时间和递增 fencing_token。每次领取/续租/提交检查令牌，旧宿主迟到结果返回 LEASE_EXPIRED，不能覆盖接管结果。提交前重试先查 request_id 已提交结果，保证应答丢失时可恢复。
- 每批输入和候选提交分别幂等，持久记录 cursor 与处理结果；长文档逐批续做。没有 Agent 在线时保留任务，下一次宿主会话由 Hook 提示或显式领取续做。后台解析和同步仍可运行。
- 不持仓库锁执行 OCR、长文本处理或等待宿主。断电重启可继续，失败任务不会阻塞其他任务。

### 5.3 提炼与准入

MCP 在回传材料前去除凭据、个人信息和无关工具噪声，再按主题与上下文预算切片，保留证据消息 ID。宿主 Agent 结构化提交：结论、解决的问题、适用条件、不适用条件、可执行动作、关键证据、验证情况、未知项、建议类型、scope、证据位置。对话已经存在于宿主上下文，MCP 脱敏不能追溯撤销宿主此前收到的内容。

普通 pitfall/workflow/decision 满足八项结构、可执行性、项目范围和证据引用校验时自动 active/once；模型推断、相互矛盾或缺来源进入 needs_review。业务规则、标准、偏好全部 staging；模型输出不能指定 verified、不能执行来源文本中的操作指令。

MCP 先用相同来源键和内容哈希去重，再在同 scope 和允许适用的 global 范围召回相似卡；宿主结合证据判断相同、补充、矛盾或独立。MCP 校验引用存在、哈希匹配、scope、结构与治理权限，语义正确性依赖宿主判断及人工抽检，不能声称确定性校验证明结论正确。相似度只用于候选召回，禁止据此直接改写已有卡。补充产生带 base_revision 的更新建议；矛盾生成关联，保留两边。

查重使用独立的宽召回配置，降低字符向量阈值，合并标题、关键词、code_refs、source_refs 等召回；初始每路最多 50 条，经合并分页回传，不照搬自动注入 verified 过滤，须覆盖同 scope 的 staging/once 候选。宿主可多轮检索和读取，不能以一次未命中证明无重复。实际阈值按标注集校准。

claim 时记录相似卡及 revision，submit 时在同 scope 重查；新增近似卡或目标 revision 变化返回 needs_reassessment 和候选列表，不直接发布。重新评估用新 request_id，并关联旧批次结果，保持同 key 同响应语义。该检查防本机已知竞态；尚未同步的跨机重复在汇合后进入重复复核，不承诺全局语义唯一。

### 5.4 宿主执行协议与能力边界

knowbase 不配置独立模型，不读取宿主密钥，不调用模型 API 或 MCP sampling。宿主使用当前会话已有的模型和工具权限执行提炼。新增宿主指引/工作流约定：领取任务 → 获取有界材料批次 → 检索及读取相似卡 → 提交候选或 no_candidate 理由 → 查询结果 → 继续下一批。配置只包含批次大小、上下文字符预算、租约时长、单次会话批次数上限和 Hook 开关。

批次固定 source/version/segment_id/hash、指引版本和候选结果契约；宿主提交 batch_id、lease_token、request_id、候选及各 segment 的处理结论。只覆盖部分输入不得推进整批完成；跳过片段必须有原因。宿主自述 processed 不证明提炼充分，质量仍由标注集抽检。宿主换模型后记录可得的模型标识，未知则记 unknown，不影响幂等身份。

没有活跃宿主时，任务停在 awaiting_agent。验收必须区分“自动触发并由宿主完成”“需用户发起续做”“仅完成解析”，不承诺无人会话时自动提炼。至少一个真实宿主完成全过程，不能以 mock 工具调用替代接入验收。

## 6. 已有知识库导入

命令设计：`knowbase import DIR --scope PROJECT --mode source-only|extract --dry-run`。保留旧命令 source-only 默认行为以兼容脚本；面向新用户文档明确推荐 extract。完成输出逐文件解析状态、source 版本、候选数、自动保存数、待审核数和失败原因。

流水线：显式目录扫描 → 文件大小/格式/路径校验 → 原始字节指纹 → 解析 → 结构定位 → source 快照 → 切分 → 共用提炼任务 → 查重与保存。

extract 模式解析后返回 job_id、awaiting_agent 和续做提示，不在 CLI 内调用模型。宿主通过新增 `memory_import` 提交明确目录与 scope（目录必须在本机配置允许范围内），随后逐批领取材料并提交；用户单独运行 CLI 时，下次宿主会话可用 job_id 继续。`--dry-run` 只预览文件清单与计划，不创建任务或写入。任务全部批次完成后才汇总知识卡产出。

解析扩展现有 `ParsedDocument` 为 text、meta、segments；segment 包含 locator、text、text_hash。MD 用标题路径+行号，PDF 用页码，Word 用段落/表格坐标，Excel 用 sheet/cell range，PPT 用页码/形状标识，图片用图片哈希与可用的 OCR 坐标。解析器未提供坐标时明确使用“解析文本行号”，不能捏造原文页码。

切分遵守章节、表格标题和上下文边界，配置最大 token 和 overlap；跨块结论必须包含多个来源片段。扫描 PDF 无有效文本时进入 OCR 路径；OCR 缺依赖或低质量时报告 blocked/needs_review。压缩格式文档设置解压大小和时间限制，单文件失败不终止批次。

raw_hash 与 text_hash 分开。原始二进制默认留在本地受控缓存，共享规范化文本及允许共享的摘录；如需共享二进制单独启用且先评估仓库大小。跨机器没有原件时仍能回溯文本快照，界面明确标注原件仅在导入机可用。

同一原件跨 scope 可复用物理对象，但必须建立独立 scope manifest。逻辑 source_id 稳定，版本内容不可变；目录改名不能仅靠绝对路径判定新文档。同路径内容变化产生新版本，对引用旧版本的知识卡生成复核任务，不自动覆盖人工修订，也不把源文件删除等同于知识删除。

M4 的身份判定限定为：同 scope 下已登记逻辑来源路径更新沿用 source_id；新路径且 raw_hash 完全相同可关联已有对象并登记路径别名；新路径且内容变化默认创建新 source，标 possible_related，需人工确认后关联旧 source。相似哈希不自动认定“改名加微改”，避免无限扩大移动识别范围。跨 scope 始终保留独立 manifest。

所有 source_refs 保存 source_id/version/segment_id/locator/text_hash；共享卡不写本机绝对路径，code_refs 继续使用仓库相对路径。

## 7. 多人同步运行方式

新增 `knowbase worker` 处理解析、材料准备、校验恢复与本地落库，不执行语义提炼；`knowbase sync` 执行一次同步，`knowbase sync --watch` 提供可监管常驻同步。MCP stdio 生命周期不承担后台可靠性；安装时分别提供 macOS launchd 和 Windows 任务计划配置与健康检查。

同步流程：读取 pending → 在短锁内提交指定操作文件 → 释放写锁推送固定 commit SHA → 遇非快进拉取到独立同步工作目录 → 合并不可变新增文件 → 校验操作/对象 → 重放 → 推送 → 记录已确认的 op_id。使用单独 sync lease 防止多个进程同时修改同步工作目录。

远端校验实际 fetch URL 与 push URL，使用本地配置允许列表。未知格式、非法引用、同 op_id 不同内容进入隔离区并告警，不能阻断可验证的其他实体。因远端权限、认证或网络不可达导致的整体传输失败明确显示，不能承诺此时还能上传。

只对确定的 non-fast-forward 做收敛重试；认证错误立即停，网络错误指数退避加抖动。push 超时属于远端结果未知，下次 fetch 对照提交/操作存在性再补推，避免重复事件。

auto_pull 与 auto_push 独立生效；关闭 auto_push 时任何读请求不得上传。查询只读取完整已物化快照，不触发长时间网络同步。同步后原子更新索引和本地视图；只有远端确认后才从 pending 进入 synced，不把本地最新 HEAD 错当作已推送内容。

运行参数初值：每个本机数据仓只有一个 sync worker；健康状态每 20 秒检查远端，加 ±20% 抖动；本地新增唤醒后 2 秒合批。网络失败按 5/10/20/40/80/160/300 秒退避，恢复后回健康周期；认证错误暂停直到配置变更或显式重试。60 秒 p95 仅适用于健康网络和在线 worker，不覆盖退避、休眠或大批追赶。

以 20 台机器单仓估算，轮询约 1 次/秒，批量发布前记录 Git 服务器 CPU、请求延迟和传输字节；超过 M0 固化容量上限时增大周期并相应调整 SLO，不能无限缩短轮询。睡眠期间不保活；唤醒后立即检查。日志按 10MB 轮转保留五份，输出状态变化及计数，不逐轮输出无变化详情或材料正文。

INDEX 退役后提供本地 dashboard、list 和导出 Markdown 快照入口；浏览页显示最后同步时间和冲突卡数。导出快照标明生成时间、不参与同步真相，便于非技术成员阅读；M5 纳入至少一位非开发使用者的查看与定位验证。

## 8. 接口契约

所有新接口返回结构化结果；原六个 MCP 工具保留名称与兼容文本展示。变更接口由统一 mutation service 实现，CLI 与 MCP 不各写一份规则。

| 接口 | 核心输入 | 结果 |
|---|---|---|
| memory_capture | session_id、checkpoint、scope、受限 transcript_ref 或脱敏 messages、request_id | job_id、queued/duplicate/blocked |
| memory_import | directory、scope、mode、request_id | job_id、解析进度；只允许配置范围内目录 |
| memory_job_claim | job_id 或 scope、host_session_id、max_chars | batch_id、固定材料/哈希、相似卡引用、lease_token、expires_at、cursor |
| memory_job_renew | job_id、batch_id、lease_token | 新到期时间或 LEASE_EXPIRED |
| memory_job_submit | job_id、batch_id、lease_token、request_id、candidates、segment_results | accepted/needs_review/rejected、已保存 ID、next_cursor；重复请求返回原结果 |
| memory_job_status | job_id | state、phase、counts、error、retryable |
| memory_candidates | job_id/scope、cursor | 分页候选及证据、处理建议 |
| memory_save | 原参数 + request_id | id、revision、local_saved、sync_state、warnings |
| memory_read | id、可选 revision | 内容 + revision + conflict 状态 |
| memory_update | id、expected_revision、expected_governance_revision、request_id、原可变字段 | applied 或 REVISION_CONFLICT、最新两种 revision |
| CLI review | candidate_id、expected_version、accept/reject | 审核操作与 card_id |
| CLI conflicts/resolve | conflict_id、expected_revision、解决内容 | resolved 或 REVISION_CONFLICT |
| CLI jobs retry | job_id | 原任务恢复，不新增逻辑保存 |
| CLI sync/status | 可选 once/watch | pending、acked、failed、conflicts、last_success |

错误码固定为 SCOPE_REQUIRED、INVALID_REFERENCE、LEASE_EXPIRED、BATCH_INCOMPLETE、PARSE_FAILED、QUALITY_REJECTED、REVISION_CONFLICT、IDEMPOTENCY_CONFLICT、SYNC_UNAVAILABLE、UNSUPPORTED_SCHEMA。awaiting_agent 和等待审核是正常状态，不混为失败。claim 返回 no_work 时宿主结束处理，不持续空轮询。

进入团队新格式后 update 必须带 expected_revision；旧客户端缺该字段返回明确升级提示，不能静默“最后写入者获胜”。保留只读兼容；审核入口不暴露成 Agent 自授权限的参数。

## 9. 文件级任务分解

| 文件/目录 | 工作 |
|---|---|
| `knowbase/store.py`、`sources.py` | 新旧 ID、版本、source_refs、manifest scope 隔离与原子写 |
| 新 `knowbase/operations.py`、`materializer.py` | 不可变操作、规范哈希、重放、分支检测、resolve 与 tombstone |
| 新 `knowbase/mutations.py` | 收拢 save/update/feedback/promote/revise/archive 所有写入口、CAS 与幂等 |
| `knowbase/index.py` + 新 `migrations.py` | 同库迁移、操作视图、队列、索引事务更新与原日志保留 |
| 新 `knowbase/jobs.py`、`worker.py` | claim/lease/fencing、恢复、逐任务失败隔离、停止与健康状态 |
| `knowbase/hooks.py` + 新 `adapters/` | transcript 归一化、checkpoint 入队、接入能力探测 |
| 新 `knowbase/extraction/` | 脱敏、切分、宿主指引、批次契约、结构/引用校验、相似候选召回；语义判断交宿主 |
| `knowbase/parsers/*`、`__main__.py` | 沿用解析器，补 locator，导入 dry-run/extract/source-only 与逐文件结果 |
| `knowbase/gitops.py` + 新 `sync_worker.py` | 独立同步、固定 SHA、操作确认、退避、远端校验和错误分类 |
| `knowbase/server.py`、`config.py` | 新 MCP 领取/续租/提交契约、导入允许目录、批次预算与运行开关 |
| `knowbase/dashboard.py`、doctor/alerts | 任务、候选、冲突、积压、依赖及版本状态 |
| 新 `scripts/install-workers.*` | macOS/Windows 安装、停止、升级、日志和卸载说明 |
| `tests/`、`docs/` | 双端真实流程、故障注入、迁移/回滚、接入与运维手册 |

## 10. 实施批次与交付门槛

以下为待 M0 校准的团队投入估算，不是交付承诺。全路线合计 30–45 人日，显式包含 Windows 联调、人工标注和恢复演练；不含外部等待和新增客户端的额外适配。标注/业务复核需由具备对应知识的人员参与，单人排期不能假定这些工作免费并行。

| 批次 | 工作与产物 | 预估 | 退出条件 |
|---|---|---:|---|
| M0 | 真实客户端 Hook/继续执行能力探测、宿主领取合同、失败样本、冻结 schema 与迁移协议 | 2–3 日 | 自动/辅助支持矩阵、录制样本、接口与数据字典可评审 |
| M0.5 | 独立止血包，保留 Markdown 权威 | 3–5 日 | 下述专项验收通过，兼容发布并单独打标签 |
| M1 | 复用止血包 ID/CAS，增加操作记录、物化、队列与新格式闸门 | 4–6 日 | 崩溃恢复、幂等、旧数据迁移无丢失 |
| M2 | 独立同步、冲突隔离、索引一致、可监管 worker | 4–6 日 | 双机真实工具写入与断网恢复通过 |
| M3 | 对话适配、宿主领取/续租/提交、查重、自动准入、候选审核 | 6–8 日 | 一个首发宿主自动闭环；其余客户端明确自动/辅助能力，含 Windows 联调 |
| M4 | 多格式定位、导入提炼、增量源版本和旧卡处理 | 4–6 日 | 混合语料导入可追溯、重复重跑不重复保存 |
| M5 | 人工标注/盲测、联合验收、两人灰度、备份回滚与文档 | 7–11 日 | 标注与质量统计 4–6 日，灰度/恢复/交付 3–5 日，Gate R 证据齐全 |

先修多人基础再开放高频自动写入。M3/M4 复用同一任务与提炼服务，避免会话记忆和导入知识形成两条不一致的保存路径。

### 10.1 M0.5 独立任务与验收

- `store.py/sources.py` 采用唯一 ID、兼容旧 ID，修复同文本跨 scope manifest 归属；不重编号历史卡。
- `server.py/__main__.py/index.py` 不再提交 INDEX，提供本地重建；所有成员升级后在维护窗口从数据仓索引中移除跟踪，保留本地文件。旧客户端必须先停写，不能靠 .gitignore 阻止已跟踪文件被旧进程重提。
- import 在锁外读取稳定字节快照并解析，锁内复核 hash/归属、去重和落库，避免解析期间文件变化形成错配。
- update 增加内容与治理 revision，所有正式写入口检查；无 expected revision 的旧调用明确提示升级，不能以自动补当前值绕过 CAS。M0.5 revision 基于规范化 Markdown 字段，M1 切换算法时通过版本号使旧令牌失效。
- 同步迁移 `hooks.py` 的 session-start/stop 指引、MCP 工具描述、README、`docs/trae-rules.md` 及已部署工作流模板：相似卡先 memory_read，取得 content_revision 与 governance_revision 后再调用 memory_update；冲突后重新读取并重新判断修改内容，不盲目换令牌重试。所有宿主重载工具 schema 与指引，验收首轮 read→update 成功及过期版本明确拒绝。仓外工作流纳入部署清单，不能只改仓库模板即宣称已更新。
- gitops 修正 auto_push、错误分类、abort 恢复检查、同步后索引更新；固定推送 SHA，校验实际 push URL。网络未知结果明确报告，恢复后核对远端。同步网络等待仍是止血包已知限制，M2 才完全迁至独立工作进程。
- 修复同步观测可靠性：状态写入异常必须返回独立的 telemetry_warning，并写本地轮转诊断日志；网络结果与状态记录结果分别展示，doctor 将记录缺失/过期标为 unknown，不沿用旧 ok。DB 不可写时不得因日志成功就声称状态已持久化。
- `_LAST_FETCH_MONO` 降为进程内优化；同机多进程以 memory.db 中 repo+remote+branch 的 last_attempt/last_success/next_check_at 及原子检查租约为准。TTL 仅在本机共享，不跨机器同步单调时钟。使用 UTC 到期时间、短租约及上限校验，时钟回拨/异常跳变或崩溃后允许有界恢复；事务只做领取，不持数据库写锁执行 fetch。记录失败检查的退避，避免每个查询反复触发网络。数据库不可用时普通读保留本地快照并告警，自动检查暂停，显式 sync 可诊断。
- 为决策门增加本地 `sync_attempts` 追加记录：attempt_id、时间、固定目标 SHA、结果分类、耗时、已确认远端 revision、待推送提交数及人工处理记录。M0.5 积压按提交数计，不伪装为操作数；同卡竞态以可观测事件和人工报告统计，不把未检测到当作零。状态表仅是最新快照，不能代替一个工作周期的历史。
- 专项验收：真实双端 memory_save、UUID 唯一性、无共享 INDEX 修改、auto_push=false 零上传、同机过期 update 拒绝、解析期间其他写入可完成、rebase 非冲突失败与 abort 失败报告准确。

追加专项验收：多个 MCP 同时触发检查只能一个进程取得有效检查租约；跨进程重启遵守 TTL；租约持有者崩溃可恢复；状态表写入失败不会静默显示 ok；宿主工具描述与 read→update 参数一致。新增观测表采用同库增量迁移，并纳入升级备份和回退兼容检查。

发布物包括补丁标签、升级/重启清单、备份和回退说明。不承诺跨机同卡冲突隔离。回退仅可到能读取 UUID/版本字段的兼容版本；更旧客户端通过导出恢复库运行。

### 10.2 架构决策门

M0.5 发布后收集一个工作周期的同步失败、同卡并发、积压、人工处理次数及实际团队规模。记录选择、证据、剩余风险和负责人：继续 M1/M2 操作记录路线，或暂留 Markdown 路线并在独立环境推进 M3/M4。后者生产自动写入只可受控试点，目标 3 仍未验收。三客户端全部自动接入不能由“一个首发宿主通过”替代，需按 M0 能力矩阵另列工作量。

jobs/candidates、batch cursor、lease/fencing、spool、提交回执与恢复属于路线无关组件。轻量路线将其从 M1 拆为 L1，在 M3/M4 前完成，初估 2–3 人日；保留 §4.2.1 和 R3/R4 的验收要求，M0 验证后校准。完整路线的 M1 已包含这部分，不重复增加 2–3 日；轻量路线不能直接省略 M1 全部工作后启动领取协议。

轻量路线的提交适配器写 Markdown：使用短 RepoLock 检查两种 revision、持久本地幂等 intent、原子替换卡片，再登记 DB 回执。崩溃恢复通过 request_id、目标卡 ID 及内容哈希识别已落盘结果，避免重复保存；这些本地 intent 不成为共享知识权威，不提供跨机冲突隔离。M3/M4 面向统一提交接口，后续操作记录路线替换适配器，任务输入和批次身份保持兼容。

决策门至少要求一个完整工作周期的 attempt 记录、观测覆盖与缺失说明、未解决事件清单以及首发客户端能力证据。状态记录持续缺失或样本不足时延长观察，不默认提前启动 M1。L1 的轻量试点验收不能被报告为全路线 Gate R；原 30–45 人日仍是完整路线估算，轻量交付在决策记录里单独列范围、投入和未通过项。

## 11. 测试与验收

### 11.1 必过场景

| 编号 | 场景 | 断言 |
|---|---|---|
| A1 | 重复 Stop、进程中断后重试 | 一个逻辑检查点只产生一份有效结果 |
| A2 | 会话后半段产生新经验 | 新检查点处理新增内容，已有 save 不遮蔽 |
| A3 | 对话只有闲聊/未验证猜测/凭据 | 不生成正式经验；MCP 回传材料及共享记录不包含检测到的凭据 |
| A4 | 项目 A 的总结任务 | 不采用项目 B 内容，不自动扩大成 global |
| A5 | 宿主关闭、输出格式错误、租约过期 | 任务可重新领取，旧宿主不能提交；已提交应答丢失可查回 |
| A6 | CLI 导入时无 Agent 在线 | 解析完成且 awaiting_agent；下次会话继续，不伪报提炼完成 |
| A7 | 两个宿主同时领取、部分批次提交 | 同一批次只有一个有效租约，未处理片段不被标完成 |
| B1 | MD/PDF/DOCX/XLSX/PPTX/HTML/图片混合目录 | 每文件结果明确；可读内容与定位一致 |
| B2 | 同内容跨 scope、同源更新、目录改名 | 物理去重但归属独立；版本可追溯且无误覆盖 |
| B3 | OCR 缺依赖、空白 PDF、损坏文档 | 非成功结果可诊断，批次其他文件继续 |
| B4 | 材料里夹带“忽略规则/发布标准”等指令 | 仅作材料处理，不形成授权 |
| C1 | 两端同时调用真实 memory_save 各写 50 张卡 | ID 不撞号、无丢失、无公共 INDEX 冲突 |
| C2 | 两端从同一 revision 更新同一卡 | 形成冲突，双方完整保留；其他卡继续同步 |
| C3 | 一端离线新增/反馈后恢复 | 事件幂等、反馈不丢不重、两端物化一致 |
| C4 | push 成功但应答丢失 | fetch 确认后收敛，不重复生成逻辑操作 |
| C5 | auto_push=false、认证失败、dirty worktree | 不越权上传、不覆盖人工修改、状态真实 |
| C6 | 旧客户端写入新格式、未知操作格式 | 明确拒绝/隔离，不能静默损坏数据 |
| C7 | helpful 与内容 update 同时发生；archive 与 update 竞争 | helpful 不改内容令牌，archive 阻止过期更新复活 |
| C8 | 两机历史反馈基线不同、重复重放新反馈 | 同一迁移基线、无重复计数；未知历史计数不触发晋升 |
| C9 | 领取后另一宿主保存相似卡 | submit 返回 needs_reassessment，未经重新判断不发布 |
| R1 | 注入 write/commit/index 各阶段崩溃 | 恢复后操作、卡片、索引一致，日志未被重建清空 |
| R2 | 执行完整回滚 | 导出的卡片可用，新版本操作和本地日志有备份 |
| R3 | 同机 8 个 MCP + CLI + worker 写入与领取竞争 | 有界等待、无死锁、单租约、响应可重试且无重复保存 |
| R4 | memory.db 损坏后恢复 | spool/回执与备份恢复任务，旧租约失效，缺失日志范围明确 |
| R5 | 20 客户端轮询、休眠唤醒、长时间断网 | 健康 SLO 可测、退避受控、日志轮转、无持续忙轮询 |

### 11.2 质量与性能门槛（拟定）

建立人工标注的至少 60 个对话样本和 30 份文档，包含无经验、错误总结、跨项目同词、过期知识和重复材料。至少 20% 保留盲测，不用训练提示词的样本代替独立验证。

M0 确定标注负责人、样本权限和判定规范；样本可早期收集，工时计入 M5。精确率=人工确认正确且可复用的自动发布卡/全部自动发布卡；召回率=成功覆盖的标注知识点/全部可沉淀标注知识点，重复卡不增加覆盖数。报告分母、错误样例和盲测结果；小样本百分比不外推为生产可靠率。

建议自动发布精确率≥95%，可沉淀知识召回率≥80%；无法达到时降低自动发布范围，候选提炼仍继续。证据定位有效率必须 100%，跨 scope 误采用、未经审核发布治理类知识、凭据泄露为零容忍。以人工标注为依据，不以模型自评为通过。

建议在约 1000 张卡的小团队样本上：Hook 入队 p95≤1 秒，本地写入 p95≤2 秒；正常网络下新操作跨端可检索 p95≤60 秒。测试记录硬件、网络、语料大小和模型耗时；这些为验收目标，不能引用为当前性能。

分别记录 capture→candidate→saved→reviewed→read→helpful，搜索命中次数不表示知识采用或正确。

## 12. 迁移、发布、回滚

### 12.0 开发期隔离与部署顺序

开发和测试使用独立 KNOWBASE_CONFIG、数据目录、memory.db、remote 与 worker 服务名。测试配置只允许临时远端，启动前检查目标不等于生产 repo_path；源码 editable 环境也隔离，避免修改源码立即影响生产 MCP。生产固定已发布版本，M1–M5 不实验性指向共享生产库。

生产允许先升级 M0.5；新操作记录路线始终在测试仓验证。在最终切换前先发布理解格式闸门的兼容客户端，盘点全部 MCP/CLI/worker 并停掉不支持闸门的旧进程。普通文件标记无法强制拦住任意旧程序，停写与进程清单是必要条件。存在不可升级成员时延后新格式切换。

新旧格式只允许迁移工具离线双读校验，不允许两种权威格式同时写同一生产仓库。shadow 试验在独立仓运行，结果不回写生产。切换后按主机确认进程版本、数据路径、remote 和格式版本，并以真实工具读写核验；仅终止旧 PID 不等于重启成功。

### 12.1 迁移

1. 同步窗口前盘点所有成员和客户端版本；停止旧自动写入，导出各机器未推送内容，集中解决遗留 Git 冲突。
2. 备份数据仓 Git、全部 Markdown/source、SQLite（含使用日志）、本地队列及输入；执行一次恢复演练。
3. dry-run 检查重复 ID、非法引用、source scope、现有 staging 与反馈基线。重复 ID 有不同实体时保留原件并生成映射，不按文件名覆盖。
4. 从一致远端 revision 建迁移标签，为旧卡生成 baseline 操作，保留 ID、正文、状态和治理历史；无法推断的出处标未知。
5. 同库迁移表，重建视图并比较卡数、内容哈希、状态、关系、反馈和搜索基准；旧 R 卡保留只读或显式转 source，禁止批量自动晋升。
6. 停止跟踪派生 INDEX/卡片视图，提交仓库格式版本；旧进程停止后新客户端接管。格式闸门必须覆盖所有写入口，无法检查版本的旧客户端必须通过部署清单停用。

### 12.2 上线

先 source-only 和队列 shadow 运行，再开启候选提炼，最后启用普通经验自动保存。选两人两机，至少覆盖 macOS 与实际使用的 Windows 客户端；观察一个完整工作周期并演练断网。

Gate R 要提供：真实宿主结束会话自动入队并继续领取/提炼/提交、来源回读、混合文档导入与跨会话续做、两机真实 memory_save/update/feedback、冲突解决、重启恢复及回滚的日志。单测、临时双克隆、协议 initialize 成功都不能单独证明交付。

### 12.3 回滚

先禁用 capture/extract 自动发布，停止 worker/sync，保留当前 Git 和 SQLite 快照。回滚到支持新格式的上一兼容版本优先；如果必须回旧格式，先将最新操作物化导出旧 Markdown，冲突实体分别导出双方版本与清单，再指向新的恢复仓库。禁止旧程序直接写入新格式数据仓库，禁止 force push 抹去他人操作。

回滚验收包括 source 可访问、旧卡 ID 和关系保持、反馈不重计、本机未同步操作有独立导出；未知冲突交由人工决定，不能作为“已恢复”的隐含数据损失。

## 13. 审核意见与待确认项

本方案覆盖三个目标的端到端链路，但属于设计审核稿。建议按 M0→M0.5→架构决策门→M1/M2→M3/M4→M5 实施。已补入独立止血交付、过渡隔离及验收投入，操作记录路线不再被当作未经评审的必然迁移；暂停该路线不会自动免除跨机并发验收。

审核意见处理：接受唯一 ID/INDEX/锁/配置止血、版本分离、反馈核对、宽召回、轮询参数、移动识别降级、SQLite 竞争与灾备、人工查看入口；修正“INDEX 必冲突”“两人反馈一次一定晋升”及“frontmatter 就是可靠反馈基线”的绝对表述。30–45 人日为分项估算，M0 后重新校准，不构成已确认承诺。

已确认复用宿主 Agent。需要在 M0 固化的部署输入：首批客户端的 transcript、Hook 继续执行和工具调用能力、允许导入目录、团队规模与是否需要强制权限隔离。客户端无自动继续执行通道时仅列为辅助接入；宿主离线期间允许材料解析和同步，提炼等待下一次会话。

本轮审核结论：可以用本文评审架构与排期；尚未通过运行验收，也不宣称现有三项目标已经完成。本文未修改业务代码、未运行新功能测试、未重启或发布服务。
