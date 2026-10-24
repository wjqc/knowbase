# 本机三宿主 knowbase 接入（唯一运行版本）

已原地安装 0.2.0rc7（凭据脱敏复核补修），62 个模块与源码一致；不是 editable 安装。启动 Python：

`/Users/qc/.knowbase/runtime/venv/bin/python`

三个 MCP 均使用 `-I -m knowbase serve`，共享原 `~/knowbase/memory.db`；env.KNOWBASE_AGENT_NAME 分别为 zcode / claude-code / trae。

| 宿主 | MCP 配置 | Hook 配置与事件 |
|---|---|---|
| ZCode | ~/.zcode/cli/config.json 的 mcp.servers.knowbase | 同文件 hooks.events：SessionStart、UserPromptSubmit、PreToolUse、PostToolUse、PostToolUseFailure、Stop；--style zcode |
| Claude Code | ~/.claude.json 的 mcpServers.knowbase | ~/.claude/settings.json：上述六项加 PreCompact；--style claude |
| Trae CN | ~/Library/Application Support/Trae CN/User/mcp.json | ~/.trae-cn/hooks.json：SessionStart、UserPromptSubmit、PreToolUse、PostToolUse、Stop；--style trae。工具失败从 PostToolUse 的结果识别 |

Trae 设置界面已确认：专用 Hook 开启，导入 Claude Hook 关闭，原沙箱运行方式保留；全局 knowbase 使用规则已替换为 rc5 并读回。它与 Claude 使用兼容输出封装，但宿主身份独立；没有照搬不受支持的 PostToolUseFailure/PreCompact。

本机 ~/.knowbase/config.json：inflight.enabled=true、capture_enabled=true、mode=assisted；value.events_enabled=true；evolution 启用（业务规则自动激活，3 个独立完成任务/2 会话，负面反馈失效）。团队统计另通过现有内部Git上传脱敏计数，不新增模型服务。详见 [团队统计接入](../team-stats-operations.md)。

## 接入后的使用
1. 长任务主动 memory_context 分阶段检索，沿用 Hook 给出的会话、scope、host_profile；实际消费 memory_use，验证后 memory_feedback 带版本/use_id/唯一稳定 task_outcome_id/task_result。
2. Hook自动观察并静默入队；普通主任务不负责清理提炼队列。明确整理知识或维护任务时，再capture→claim(protocol_version=2)→材料续页→submit→读回。高价值结论仍可直接memory_save，业务规则带出处。
3. 业务规则 validation_candidates 只用于验证，关联 memory_read 获得 read_use_id 后根据实际结果回填；服务按当前版本的独立反馈自动晋升/失效，不能为了阈值编造任务。标准/全局偏好保持原权限。
4. 现有会话保留旧 MCP 进程。任务结束后在宿主中重连 knowbase MCP；若宿主不支持单服务重连，重启该客户端后开启新会话。单纯改文件不证明旧会话已经加载新工具。

rc6 修复及 rc7 独立复核、历史清洗和验证见 [凭据修复记录](../credential-redaction-2026-10-22.md)。`duplicate_evaluation` 必须放在每个 `candidates[i]` 内部，不是 `memory_job_submit` 顶层参数。历史清洗撤销了受影响材料的旧租约，未完成作业需重新领取。

## 验证及边界
- 当前 rc7 源码全套：603 passed、1 skipped；凭据相关定向 125 passed。旧 rc5 上线时曾验证 496 passed、1 skipped。
- 固定包安装态 stdio 端到端 2 passed；三个实际配置启动命令分别初始化成功、列出 16 工具，反馈 schema 包含 task_result，instructions 含 validation_candidates。
- Hook 配置命令逐项在隔离测试库运行；正常退出及输出封装核对，结果见运行 manifest。
- 真实库迁移21兼容旧命令日志，宿主归属通过关联表补充；schema20及更早数据原地升级后可正常连接，完整性检查通过，凭据扫描覆盖字段无待修改项。191 卡/25 feedback/2 knowledge_uses 是 rc5 上线时的历史库存，本次不以其作为当前计数；没有批量激活历史候选。
- Trae 全局 Hook 开关与规则保存有原生 UI 证据；未向正在运行的聊天发消息，未终止任何宿主任务。三宿主新会话真实业务整链及三人收益仍待使用样本，配置/协议验证不替代这些验收。

## 单版本运维与数据恢复
唯一运行环境为 `~/.knowbase/runtime/venv`，所有宿主固定引用其 Python。升级在此路径原地安装；不创建 releases/<版本>，不保留旧运行包和旧构建产物。当前版本标识只用于核对来源，不对应并行安装目录。

数据和配置备份独立保留在 `~/.knowbase/backups/`。最近一次路径合并前备份是 `single-runtime-20261022-180225`；原 SQLite online backup 在 `hosts-rc5-20261022-174641`。恢复配置后也必须使用唯一固定路径，不恢复已删除的版本路径。需要修复代码时从源码 Git 取得所需内容，验证后原地安装，不保留第二套运行环境。

已关闭引用旧路径的 knowbase 服务连接，宿主和任务保留；在各宿主重连 knowbase MCP 后加载唯一运行包。

规范依据：[Trae Hook 配置](https://docs.trae.cn/ide_hook-configuration-reference)、[Claude Hooks](https://code.claude.com/docs/en/hooks)。
