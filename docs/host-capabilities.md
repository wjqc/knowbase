# 执行中宿主能力记录

2026-10-21：本机 ZCode 官方 zcode-guide 0.3.0 的 diagnosing-hooks 文档列出七种事件：SessionStart、UserPromptSubmit、PreToolUse、PermissionRequest、PostToolUse、PostToolUseFailure、Stop。PreCompact 不受支持；SessionStart 的 compact 原因表示压缩后开场，不能当成压缩前事件。

工具事件按工具名匹配；process 使用 command+args 和 timeoutMs；输出 additionalContext 可注入，async 字段不提供异步执行。以上来自本机安装文档的静态取证，尚不证明本次 knowbase 处理器在真实会话中触发并被下一步消费。

| 宿主 | 规范证据 | knowbase 运行证据 | 当前验收 |
|---|---|---|---|
| ZCode | 本机 zcode-guide 0.3.0 七事件文档 | 用户级工具事件已注册；执行中注入/消费通过；headless MCP 提炼未过 | PARTIAL；整链待验 |
| Claude Code | CLI 2.1.168；七类 Hook（含 PreCompact）注册于用户 settings.json | MCP（~/.claude.json）与 Hook 均已注册（2026-10-21 备份在案）；transcripts 含请求级 usage；真实会话消费未证 | external_pending；注册已证 |
| Trae | Trae CN 3.3.103；MCP 必选 | knowbase MCP 已注册于 mcp.json；Hook 官方支持但本机未配置 | assisted；MCP 已接线 |

真实探针要求：隔离 scope/测试库，一条用户任务产生至少两个工具阶段；保留脱敏事件载荷、Hook stdout 和后续行动轨迹。仅手工给处理器喂 JSON 支持协议局部验证，不能升级为真实宿主验收。

## 真实探针（2026-10-21，ZCode CLI 0.17.0）

- 通过桌面内置 zcode.cjs headless 运行隔离工程，KNOWBASE_CONFIG/REPO_PATH 指向临时测试库；未使用真实业务库。原提示要求顺序读取两文件，只说明报告 KB_PROBE 标记，不包含其值。
- 首次失败：/var 与 /private/var 目录别名未匹配，以及本次 CLI 未加载工程级工具 Hook。已补规范路径映射；备份并注册用户级 PreToolUse/PostToolUse/PostToolUseFailure。真实 inflight 开关仍关闭，因此其他会话的新注册处理器为空操作。
- 第二次：实际 subtask_start 事件记录 executed，候选包含独立夹具标记 KB_PROBE_RULE_7C2A；宿主后续回复报告该完整值，并确认来源知识卡。这证明本机该路径的执行中递送和消费，不证明所有阶段或业务任务均覆盖。
- 该探针预置了当前任务的查询主题，标记值仅在知识卡正文；它不证明任意长方案都已由宿主正确拆解，更不等于三人真实业务验收。
- 同次 Stop 自动生成一个 awaiting_agent 的增量任务；headless --mode build 的 MCP claim/status 报 No permission client configured，尚未完成宿主提炼/提交验收。不能把 MCP 注册或入队当作自动成卡。
- 可执行入口需显式指定桌面打包的 provider 配置路径；普通 node 调用 bundle 的默认相对路径解析失败。测试不读取或打印密钥，不修改全局许可模式。
- 临时运行证据位置登记于 progress.md；保留结果摘要，不把完整聊天日志放共享 Git。

## H0 能力快照（2026-10-22）

按[团队价值实施方案](knowbase-team-value-implementation-plan.md) §3 执行的只读探测：读取本机安装版本（Info.plist / `--version`）、宿主配置文件与目录结构，并以当前 ZCode 会话做活体观察；未修改任何配置、会话或数据库。三宿主适配器统一为 knowbase runtime `ef4189a`（源码工作区基线 `7ce499c`，`0.2.0rc4`）。

| 快照字段 | ZCode | Trae | Claude Code |
|---|---|---|---|
| 宿主/版本/形态 | 桌面 3.16.2；CLI 0.17.0（10-21 探针） | Trae CN 3.3.103，IDE；本机无 trae CLI | CLI 2.1.168（`~/.local/bin/claude`） |
| 接入注册 | 用户级六事件 Hook 全注册（`~/.zcode/cli/config.json`，备份 2026-10-21） | `~/Library/Application Support/Trae CN/User/mcp.json` 注册 knowbase serve（`KNOWBASE_AGENT_NAME=trae`，备份 2026-09-17）；Hook 未配置 | `~/.claude/settings.json` 七类 Hook 全注册（备份 2026-10-21）；`~/.claude.json` 注册 knowbase MCP |
| 采集模式 | 自动（部分验收） | 辅助（MCP＋显式阶段工作流） | 自动，待真实验收 |

能力判定（supported / unsupported / unknown / failed，附证据）：

| 能力项 | ZCode | Trae | Claude Code |
|---|---|---|---|
| MCP 连通/工具许可/规则加载 | supported：本会话 `mcp__knowbase__*` 可调用，SessionStart 规则注入到达模型 | supported（注册层面）：mcp.json 条目在案；会话内真实消费待 Trae 首个真实样本 | supported（注册层面）：`~/.claude.json` 用户级 mcpServers 含 knowbase；真实消费未证 |
| 用户消息/工具前后/失败/停止事件 | supported：六事件注册且本会话实际触发（SessionStart 与工具后附加上下文均到达模型） | 未接入：官方文档称支持，本机 settings.json（2026-10-22 取证顶层键）无任何 hooks 配置 | 注册 supported：七类已注册；`PostToolUseFailure` 是否为 2.1.168 有效事件名待官方清单核对（失败语义或需走 PostToolUse `is_error`） |
| 上下文压缩事件 | unsupported：仅七事件，无 PreCompact（zcode-guide 0.3.0） | unknown | supported（注册）：PreCompact 已注册；真实触发未证 |
| 会话/原生调用/任务标识 | `session_id` supported（本会话 hook 载荷实证）；原生调用 ID、任务关联 unknown | unknown | transcripts 含会话/请求标识（uuid 文件名实证）；任务关联 unknown |
| Hook 输出进入模型上下文 | supported：本会话两类注入实证；工具事件超时 5s、生命周期 15s 在案，输出上限与重入未测 | n/a（未配置） | unknown：注册在案，真实会话注入未证 |
| 请求/会话级 Token 与成本 | unknown：UI 使用统计存在，稳定导出协议未证，不由 UI 推导 | unknown：企业 API 需授权，粒度待确认 | supported-本地：`~/.claude/projects/*/*.jsonl` 行内 `message.usage` 实证键名 `input_tokens/output_tokens/cache_read_input_tokens/cache_creation_input_tokens/cache_creation/service_tier`；OTel 路径未启用 |
| 数据导出/观测授权 | 本机 `knowbase.metrics.v1` 只读导出已有；宿主用量导出 unknown | unknown | 本地 transcripts 可读；OTel 白名单与授权待启用验证 |

结论与限制：

- 三宿主均取得版本绑定的注册证据，但仅 ZCode 具备真实递送消费证据（2026-10-21 隔离探针＋本会话活体观察）。按 §3.3，Trae、Claude Code 仍需真实任务贯通样本，不允许宣称三宿主自动接入通过。
- Token 粒度：当前仅 Claude Code 具备本地请求级证据；ZCode/Trae 保持 unknown。V4 计量探针自 H0 起持续记录，分析后置。
- headless MCP 提炼缺口（"No permission client configured"）维持 10-21 结论，归 E1 处理；本节静态取证不改变该状态。
- Trae mcp.json 中存在与本方案无关的第三方服务器条目（含禁用项），本次未读取其凭据、未做变更；探测原则为只读取证、修改前备份。
