# knowbase（薪火）

2026-10-25：后台自动沉淀已补齐领取、分页、推理、提交、恢复与预算控制；必须显式授权范围且执行器探针通过后启用。无模型时基础检索仍可用。公司网络/模型与真实成员试点未在本轮验收，接入与边界见[本地能力补齐说明](docs/operations/autonomous-local-completion.md)。

凭据脱敏修复与历史清洗：见 [凭据修复记录及 rc7 复核](docs/credential-redaction-2026-10-22.md)。`knowbase privacy-scrub` 默认只读盘点；`--apply --backup-dir <仓库外新目录>` 先私有备份再清洗。

执行中检索与增量沉淀已进入实现验证：新增 `memory_context` 和 `memory_job_material`，支持分阶段查询、完整材料分页、技术经验/业务规则候选成卡与回执恢复。新机制默认关闭；升级与宿主配置见 [接入和回滚说明](docs/inflight-rollout.md)，真实验收边界见 [宿主能力记录](docs/host-capabilities.md)。

跨 Agent 共享的经验记忆库 MCP 服务：踩坑 / 决策 / 流程 / 偏好 / 业务规则沉淀为一个本地 git 知识库，
供本机所有 AI Agent 检索复用。**一个人的坑，所有 Agent 的经验。**
原始材料（文档/代码/日志）以 source artifact 分层保真留存，不混入知识检索（ADR-0002）。

- 存储：markdown + frontmatter（唯一真相）+ git 审计 + SQLite FTS5 派生索引（可重建）
- 检索：默认规则同义扩展+FTS5；可选内网真实Embedding通过质量、版本和覆盖检查后执行混合RRF检索。旧字符哈希向量不参与语义召回，不创建独立向量数据库。
- 治理：受治理知识先进入候选，按明确激活策略及独立任务证据演进；confidence/status由服务端状态机管理，人工CLI保留为例外纠正入口
- 完整设计：知识库 `通用/05-智能助手/Agent经验记忆库MCP服务-设计方案-v1.md`

> 团队程序升级：rc9支持首次接入后的独立定时升级、唯一运行环境原地安装与失败恢复；见[无人值守升级说明](docs/program-auto-update.md)。知识Git同步不会自动更新Python程序。

## 环境要求

- macOS / Linux / Windows（Windows 为实验性支持，见下方说明）
- Python ≥ 3.10、git、[uv](https://docs.astral.sh/uv/)（没有 uv 就用 `python3 -m venv` + pip）

### Windows 安装（PowerShell）

```powershell
# 前置：Python ≥3.10 与 Git（winget install Python.Python.3.12 Git.Git），uv 官方脚本安装
uv venv "$env:USERPROFILE\.knowbase\runtime\venv"
uv pip install --python "$env:USERPROFILE\.knowbase\runtime\venv\Scripts\python.exe" .
& "$env:USERPROFILE\.knowbase\runtime\venv\Scripts\python.exe" -I -m knowbase init
```

MCP 注册时 command 用唯一固定runtime的 `Scripts\python.exe` 完整路径（JSON 里写 `\\`），
推荐 ZCode 用 `type: "process"`（参数数组，免转义）。钩子同样指向 Scripts 下的 python.exe。

## 五分钟接入

```bash
# 1. 获取代码
git clone <内网 GitLab 地址>/knowbase.git ~/work/knowbase && cd ~/work/knowbase
#    （内网 pip 慢就加镜像：-i https://pypi.tuna.tsinghua.edu.cn/simple）

# 2. 安装
uv venv ~/.knowbase/runtime/venv
uv pip install --python ~/.knowbase/runtime/venv/bin/python .

# 3. 初始化（显式、幂等；创建 ~/.knowbase/config.json 与 ~/knowbase 记忆库）
~/.knowbase/runtime/venv/bin/python -I -m knowbase init

# 4. init 可选择Agent并安全写入配置；手工注册时也固定使用同一runtime
```

**Claude Code**（`~/.claude.json` → mcpServers）：

```json
"knowbase": { "command": "<用户目录>/.knowbase/runtime/venv/bin/python",
  "args": ["-I", "-m", "knowbase", "serve"],
  "env": { "KNOWBASE_AGENT_NAME": "claude-code-你的名字" } }
```

**ZCode**（`~/.zcode/cli/config.json` → mcp.servers）：同上，`"type": "stdio"`。

**Trae CN**（`~/Library/Application Support/Trae CN/User/mcp.json` → mcpServers）：同上；
并核对 `docs/trae-rules.md` 与原生Hook开关。支持事件按宿主实际版本验证，不沿用“Trae无Hook”的旧结论。

**KNOWBASE_AGENT_NAME 约定：`工具-姓名`**（如 `trae-lisi`）——只作工具来源归因，不认证身份或计算真实人数。

```bash
# 5. 验证：重启 Agent 后问一句 "搜下 EasyConnect"，应命中 P-2026-0001
```

## 两种开局

| 模式 | 做法 | 适用 |
|---|---|---|
| **空库自己攒**（默认） | 什么都不做，init 建的是你自己的空库 | 个人使用 |
| **团队共享经验库**（推荐） | `git clone` 已有记忆库（如部门共享仓）到本机 → `~/.knowbase/config.json` 里 `repo_path` 指过去 → `knowbase reindex` | 想直接继承团队踩坑遗产 |

共享模式下写权限就是 git：保存即 commit，`git.auto_push` 开启后在释放本地写事务锁后执行同步推送（默认关；
远端白名单默认 `http://10.21.20.112/*`，误配公网会被拒）。推送前会先 fetch：远端抢先更新时，
不同文件/可自动合并的修改会通过 rebase 重放并有限重试（默认 3 次）；只有真实内容冲突才停止，自动
`rebase --abort` 并保留本地提交。网络超时只记录为同步失败，不会误报成内容冲突，也不会回滚本地保存。
索引可随时重建。

执行中检索、版本反馈与本机统计导出见[消费和统计协议](docs/inflight-consumption-metrics.md)。新版提供 `memory_use`、`memory_metrics` 和 `knowbase metrics`；历史本机日志不能代表全团队总量。

## 可选：任务前自动检索 / 任务后漏存兜底（hooks）

Claude Code（`~/.claude/settings.json`）与 ZCode（`~/.zcode/cli/config.json`，**必须 `"enabled": true`**）：
照抄本仓库维护者的接线，三事件 `SessionStart` / `UserPromptSubmit` / `Stop` 都调
`<venv python> -m knowbase hook <事件> --style claude|zcode`。Trae使用其原生Hook配置，事件能力按版本单独核验。

## Agent 使用规则（Team 通用，六句话）

1. 任务涉及具体项目/系统/报错 → 先 `memory_search`（≥3 字技术词），命中先 `memory_read`；
2. 任务结束产生踩坑/决策/流程/约束 → `memory_save`（提示相似就改 `memory_update`）；新卡正文须含八项小节：**结论 / 解决的问题 / 适用条件 / 不适用条件 / 可执行动作 / 关键证据 / 验证情况 / 未知与待确认**（"视情况而定"不算适用条件、"参见某文档"不算证据、"测试通过"不算验证，lint 会拦）；
3. 实际参考/采纳先用 `memory_use` 关联递送事件、任务和卡版本；验证结果后 `memory_feedback` 回填 helpful / not_helpful / outdated / incorrect，并传 `expected_content_revision/use_id/task_outcome_id`。同任务结果重试只应用一次；旧调用仅记录未绑定观察，不改变治理状态。正文变更会清除旧版本的验证；可信人工确认仍走 CLI `verify`，工具名不是人工身份；
4. stale / once 的记忆采信前先验证；检索走 `memory_search`，不要整读 INDEX.md；
5. 标准/偏好类保存先进入staging，按授权策略和证据流转，不绕过治理；
6. body 里禁止明文密钥；经验卡禁止本机路径和仓外文档引用。文档结论必须正文自足或使用 knowbase 仓内相对路径；代码证据写成 `code:<项目>/<仓库相对路径>`（可带行号，lint 会拦违规写入）。**整篇文档等原始材料不入 memory_save**，走 `knowbase import` 导入为 source（见下节）。

### 结构化代码定位 `code_refs`

`memory_save` / `memory_update` 支持在所有卡类型上携带 `code_refs`（业务规则卡最常用）：

```yaml
code_refs:
  - repo: order-center
    path: svc/order/OrderServiceImpl.java
    symbol: OrderServiceImpl#changeCard
    lines: 120-180
    note: 实名校验入口
```

| 字段 | 要求 |
|---|---|
| `repo` | 必填，仓库/项目标识，只允许 `[A-Za-z0-9_.-]+` |
| `path` | 必填，仓库相对路径；禁止绝对路径、`..` 穿越和反斜杠 |
| `symbol` | 可选，函数名或 `类#方法` |
| `lines` | 可选，单行或闭区间，如 `120` / `120-180` |
| `note` | 可选，简短说明该位置的业务意义 |

索引会把每项拍平为 `code:<repo>/<path>[#symbol][:lines]` 追加到现有 FTS 与向量文本，因此可按类名、方法名或路径片段反查规则；不增数据库列。正文出现旧式 `code:` 定位而 frontmatter 未填 `code_refs` 时，lint 仅告警、不阻断存量卡。

## 原始材料入库（source）与知识提炼（2026-09-21 起）

原始材料与可复用知识已彻底分层（ADR-0002）：**source 负责保真，card 负责复用**。

```bash
# 把现存文档批量导入为 source artifact（支持 Markdown/TXT/PDF/Office/HTML/图片 OCR/代码/日志）
.venv/bin/python -m knowbase import <文档目录> --scope 项目名
```

- import 只写 `sources/`（objects 文本快照 + manifests 元数据），按内容 sha256 去重；**不生成知识卡、不进入默认检索**；
- `--type/--staging` 参数保留仅为兼容，一律忽略；
- 可复用结论从 source 中提炼后用 `memory_save` 按八项小节结构保存；
- 旧 `reference`（R 卡）已退出默认检索与速览，可 `memory_read` 单条查看，待逐条提炼迁移（P3）。

防污染开关（`~/.knowbase/config.json` → hooks）：

```json
"hooks": { "enabled": true, "inject_min_confidence": "verified" }
```

`"verified"` 时任务前自动注入只取已验证经验（once 的新经验仍可被主动 search 检索到），
默认 `"verified"`；已有配置显式 `"any"` 会保留兼容行为。MCP 主动检索不受置信度开关影响，但默认排除 staging/stale/reference。

## 常用命令速查

未把虚拟环境加入 PATH 时，将 `knowbase` 替换为 `.venv/bin/python -m knowbase`。

**人工例外治理入口（不代表日常必须人工操作）**

| 命令 | 用途 |
|---|---|
| `knowbase verify <id>` | 人工确认有效（once→verified / stale 复活） |
| `knowbase promote <id>` | 激活 staging 提案（标准/偏好/业务规则生效） |
| `knowbase revise <id> --body-file <文件> [--title]` | 人工修订已生效的标准/偏好/业务规则 |
| `knowbase archive <id>` | 归档退役（检索与速览不再出现） |

**运维与观测**

| 命令 | 用途 |
|---|---|
| `knowbase init [--import-from <目录> --scope <名>]` | 初始化/补全（幂等），可顺带导入 source artifact |
| `knowbase import <目录> --scope <名>` | 批量导入为 source artifact（16 种格式，不产生知识卡、不进默认检索；`--type/--staging` 已废弃兼容保留） |
| `knowbase reindex` | 全量重建索引与 INDEX.md（markdown 唯一真相，坏了就重建） |
| `knowbase stats` | 数量分布 / 使用漏斗 / TOP |
| `knowbase list [type]` | 列出记忆（含 staging 标注） |
| `knowbase history [--limit N]` | 最近搜索命中记录（JSON） |
| `knowbase dashboard [--output <文件>] [--open]` | 生成离线统计卡片：调用、知识状态、命中、更新、Token 和自动演进；只读业务库 |
| `knowbase doctor [--json]` | 健康检查：索引一致性 / 同步 / Git / 解析器；P4 新增绑定、激活策略、维护、同步确认、待确认回执、证据覆盖、租约、会话与执行器能力检查 |
| `knowbase alerts [--dry-run]` | 输出异常状态，供调度器通知 |
| `knowbase scope resolve [路径]` | 解析一条路径的 scope 绑定（只读；无法确定时保持 scope_unresolved） |
| `knowbase scope bind --path <路径> --scope <名>` | 显式绑定一条路径到 scope（人工确认，优先级最高） |
| `knowbase maintenance run` | 执行一次定期维护：崩溃恢复→自动归档→review 清理→冲突复验 |
| `knowbase maintenance status` | 维护状态快照（只读） |
| `knowbase onboarding` | 接入就绪四项分别输出：配置/协议/宿主加载/真实任务验证；新提示只出现一次 |

**服务与接入**

| 命令 | 用途 |
|---|---|
| `knowbase serve` | 启动 MCP 服务（stdio，供各 Agent 配置） |
| `knowbase hook <session-start\|user-prompt\|stop> [--style claude\|zcode]` | Agent 钩子入口（接线见上文 hooks 节） |

**团队价值文件汇总（显式执行，不自动上传）**

| 命令 | 用途 |
|---|---|
| `knowbase value export --output <包>` | 导出本机最小事件包（不改变事件状态；默认关闭发射，先在 `value.events_enabled` 开启） |
| `knowbase value import <包> --agg-db <库>` | 汇总机按事件 ID 幂等导入独立分析库，生成含哈希的接收回执（拒绝把他人事件写进个人 memory.db） |
| `knowbase value receipt <回执>` | 源端核对回执哈希，通过才把本机事件置 acknowledged |
| `knowbase value snapshot --output <快照> --agg-db <库>` | 从汇总库生成最小团队快照（指标、来源覆盖、数据水位；覆盖率分母仅认登记名单） |
| `knowbase value usage [--source claude\|zcode\|trae] [--path <目录>]` | 提取本机宿主请求级用量为 model.usage 事件（当前仅 claude transcripts 支持，不采对话正文；zcode/trae 明确 not_supported 不估算） |
| `knowbase value report --output <报告> [--agg-db <库>] [--personal]` | 确定性生成周/月 Markdown 报告（无模型润色，数值与证据等级不可改写） |
| `knowbase dashboard --team-data <快照>` | 本机/团队统计卡片切换；不嵌入原始查询、知识正文或任务明细，缺失指标保留未知 |
| `knowbase value register-member / confirm-member` | 登记团队成员名单 / 人工确认安装实例归属（归并一人多宿主） |

协议与口径见[价值事件协议](docs/value-events-protocol.md)；导出成功≠已接收，以回执核对为准。汇总侧任务按（归属人, 运行来源＝会话优先/安装实例回退, task_id）区分运行，同名任务不跨安装/会话合并；反馈计入任务结论前须命中实际消费回执（含 use_id），未命中单列 `unlinked_feedback_events`。

## 已知边界（诚实版）

- Windows 为实验性支持：跨进程锁在 Windows 走 msvcrt 字节范围锁（标准模式，但未在 Windows 真机回归），首次使用建议先跑 `.venv\Scripts\python tests\test_e2e.py` 验证；真实语义提升须配置公司内网模型并通过质量验收，默认本地检索可用；
- Trae原生Hook具备接入实现；真实宿主/版本上的消费链仍需独立验收；
- 记忆库含内部系统经验，**只推内网 GitLab，永不推公网**。


## 导入、检索记录与治理看板（行为细则）

- `init --import-from` 要求明确 scope（通用资料填 global）；import 只生成 source artifact（sources/objects 快照 + manifests），不产生知识卡、不进默认检索。普通 init 仍为空库初始化，不扫描外部目录。
- source 快照按内容 sha256 去重（跨路径同内容只存一份）；同路径源文件变化产生新快照，不覆盖旧内容。源文件绝对路径仅留在本机 `memory.db.source_state` 做幂等，不写入共享内容。支持 Markdown/TXT/PDF/DOCX/XLSX/PPTX/HTML/图片 OCR/代码/日志。按项目分别导入，不把混合项目目录全部归为 global。
- 可复用知识用 `memory_save` 提炼（八项小节结构，lint 强制）；source→card后台提炼已实现，默认关闭，启用依赖执行器授权、有效探针回执及明确scope。
- 默认 MCP 检索仅 active 且非 staging，且排除 reference（原始材料卡）与 source；无项目上下文只查 global。有项目上下文时必须通过 `scope_map`、已存在的同名 scope 或显式参数确定项目，无法解析就拒绝检索，避免跨项目召回。Hook只使用本地检索；主动MCP可在内网模型与质量/索引条件满足时使用L3。
- 旧 `reference`（R 卡）只读不写不检索：`memory_save` 拒绝该类型，`memory_search` 不返回（显式 type=reference 也拒绝），`memory_read` 可单条查看并带"原始材料卡"提示；INDEX.md 速览不再列出，仅在头部计数标注。
- 新卡 `schema: card-v2` 强制八项小节与内容质量（空洞适用条件/纯引用证据/"测试通过"式验证/空小节均拦截）；存量卡不带标记沿用旧规则，`memory_update` 不受影响，待 P3 迁移。
- Hook 单词兜底要求命中标题/标签，或至少两个关键词命中正文；输出明确为候选，需检查适用项目、版本、证据和验证日期。词法分数不是正确率，verified 也不保证永远正确。
- 每次 MCP/Hook 搜索记录查询、scope、Agent、时间、返回 ID/顺序/分数/状态，包括零命中。旧记录缺少的命中明细无法追溯补齐。`stats.enabled=false` 关闭使用日志。
- 看板支持项目、标题搜索、有效反馈排行、疑似问题、staging 待审核、once 未验证及搜索明细。有效反馈数量不等于唯一任务数；负反馈/失效/矛盾属于复核线索，不自动判错。页面为静态快照，重新运行命令刷新。
- `init/reindex` 保留读取统计、usage_log、feedback_log。这些日志不是 Markdown 派生态；删除数据库或仅克隆 Markdown 仓库无法恢复，备份需包含 memory.db（SQLite 一致性备份）。看板包含本地查询和来源信息，不自动发布到网络。
- 记忆卡必须内容自足：禁止保存 `~/...`、`/Users/...`、`C:\Users\...` 等本机路径，也禁止指向 knowbase 仓库外的文档。文档若需独立维护，先纳入 knowbase，再用仓库相对路径引用；代码证据统一写成 `code:<项目>/<仓库相对路径>`，例如 `code:cmi-sales-assistant/agent-platform/app/service.py:42`。
