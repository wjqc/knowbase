# Trae 执行中接线

> 当前本机已更新至 rc5：见 [最新接入、验证与回滚](latest-local.md)。下文 rc4/历史能力快照保留作为历史记录，当前配置以最新记录为准。

宿主版本基线：Trae CN 3.3.103（`/Applications/Trae CN.app`，2026-10-22 取证）；IDE 形态，本机无 trae CLI。接入走两条路径，**MCP 必选**；Hook 待真实探针，通过前为辅助模式。

## MCP（已接线）

knowbase 服务已注册于用户级 `~/Library/Application Support/Trae CN/User/mcp.json`：

```json
{
  "knowbase": {
    "command": "/Users/qc/.knowbase/runtime/venv/bin/python",
    "args": ["-I", "-m", "knowbase", "serve"],
    "env": {"KNOWBASE_AGENT_NAME": "trae"}
  }
}
```

原配置备份为 `mcp.json.bak-knowbase`（2026-09-17）。变更 mcp.json 前先备份，不删除其他服务器条目，不改工具审批策略。

## Hook（未接入，辅助模式）

官方文档（见[团队价值方案](../knowbase-team-value-implementation-plan.md) 附录 A）声明 Trae 支持 Hook 自动化；同名事件不保证参数与 Claude/ZCode 一致，必须按 3.3.103 实际版本探测输入输出后适配，不照搬 Claude 配置。本机 `User/settings.json`（2026-10-22 取证顶层键）无任何 hooks 配置，即当前未接入。

在 Hook 通过真实探针前，Trae 保持辅助模式：依赖[用户规则](../trae-rules.md)引导 Agent 在阶段检查点显式调用 `memory_context` / `memory_use` 及提炼作业 MCP 工具；不得宣称 Trae 自动执行中接入通过。

## 标识与用量

会话 ID、原生工具调用 ID、任务/提示关联标识：unknown，待真实会话探针。Token/成本：企业 API 仅在具备权限时使用并核对粒度，不假定任务级 Token 存在；本地暂无可验证的请求级用量来源（H0 快照结论见[宿主能力记录](../host-capabilities.md)）。

## 验收门槛

按方案 §3.3/§11：Trae 优先取得首个完整真实任务样本（真实输入 → Hook 或规则生效 → 模型消费 → 知识读回），才可从辅助模式升级为自动模式。探测样本使用隔离材料与测试知识库，不默认修改现有会话。执行手册与判据见[三宿主真实样本验收协议](../team-value-real-acceptance.md)。
