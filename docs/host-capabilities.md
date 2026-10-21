# 执行中宿主能力记录

2026-10-21：本机 ZCode 官方 zcode-guide 0.3.0 的 diagnosing-hooks 文档列出七种事件：SessionStart、UserPromptSubmit、PreToolUse、PermissionRequest、PostToolUse、PostToolUseFailure、Stop。PreCompact 不受支持；SessionStart 的 compact 原因表示压缩后开场，不能当成压缩前事件。

工具事件按工具名匹配；process 使用 command+args 和 timeoutMs；输出 additionalContext 可注入，async 字段不提供异步执行。以上来自本机安装文档的静态取证，尚不证明本次 knowbase 处理器在真实会话中触发并被下一步消费。

| 宿主 | 规范证据 | knowbase 运行证据 | 当前验收 |
|---|---|---|---|
| ZCode | 本机 zcode-guide 0.3.0 七事件文档 | 用户级工具事件已注册；执行中注入/消费通过；headless MCP 提炼未过 | PARTIAL；整链待验 |
| Claude Code | 原三 Hook＋工具事件/PreCompact 已注册 | 本地 JSON 协议测试通过，真实宿主未探针 | external_pending |
| Trae | 既有用户规则/MCP | 执行中主动工作流待验证 | assisted，待真实验收 |

真实探针要求：隔离 scope/测试库，一条用户任务产生至少两个工具阶段；保留脱敏事件载荷、Hook stdout 和后续行动轨迹。仅手工给处理器喂 JSON 支持协议局部验证，不能升级为真实宿主验收。

## 真实探针（2026-10-21，ZCode CLI 0.17.0）

- 通过桌面内置 zcode.cjs headless 运行隔离工程，KNOWBASE_CONFIG/REPO_PATH 指向临时测试库；未使用真实业务库。原提示要求顺序读取两文件，只说明报告 KB_PROBE 标记，不包含其值。
- 首次失败：/var 与 /private/var 目录别名未匹配，以及本次 CLI 未加载工程级工具 Hook。已补规范路径映射；备份并注册用户级 PreToolUse/PostToolUse/PostToolUseFailure。真实 inflight 开关仍关闭，因此其他会话的新注册处理器为空操作。
- 第二次：实际 subtask_start 事件记录 executed，候选包含独立夹具标记 KB_PROBE_RULE_7C2A；宿主后续回复报告该完整值，并确认来源知识卡。这证明本机该路径的执行中递送和消费，不证明所有阶段或业务任务均覆盖。
- 该探针预置了当前任务的查询主题，标记值仅在知识卡正文；它不证明任意长方案都已由宿主正确拆解，更不等于三人真实业务验收。
- 同次 Stop 自动生成一个 awaiting_agent 的增量任务；headless --mode build 的 MCP claim/status 报 No permission client configured，尚未完成宿主提炼/提交验收。不能把 MCP 注册或入队当作自动成卡。
- 可执行入口需显式指定桌面打包的 provider 配置路径；普通 node 调用 bundle 的默认相对路径解析失败。测试不读取或打印密钥，不修改全局许可模式。
- 临时运行证据位置登记于 progress.md；保留结果摘要，不把完整聊天日志放共享 Git。
