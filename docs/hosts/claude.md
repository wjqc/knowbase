# Claude Code 执行中接线

> 当前本机已更新至 rc5：见 [最新接入、验证与回滚](latest-local.md)。下文 rc4/历史能力快照保留作为历史记录，当前配置以最新记录为准。

合并到 `~/.claude/settings.json` 的 hooks，不覆盖现有事件和其他处理器：

```json
{
  "PreToolUse": [{"hooks": [{"type": "command", "command": "\"/你的路径/knowbase/.venv/bin/python\" -m knowbase hook tool-before --style claude", "timeout": 5}]}],
  "PostToolUse": [{"hooks": [{"type": "command", "command": "\"/你的路径/knowbase/.venv/bin/python\" -m knowbase hook tool-after --style claude", "timeout": 5}]}],
  "PostToolUseFailure": [{"hooks": [{"type": "command", "command": "\"/你的路径/knowbase/.venv/bin/python\" -m knowbase hook tool-failure --style claude", "timeout": 5}]}],
  "PreCompact": [{"hooks": [{"type": "command", "command": "\"/你的路径/knowbase/.venv/bin/python\" -m knowbase hook before-compact --style claude", "timeout": 5}]}]
}
```

工具事件使用 hookSpecificOutput.hookEventName/additionalContext，不能照搬 ZCode 顶层格式。PreCompact 仅持久保留待办，不靠其 stdout 进行会话注入；整理后 SessionStart 或主动 recover 继续任务。

契约参考 [Claude Code 官方 Hooks 文档](https://code.claude.com/docs/en/hooks)。当前仅有协议测试，尚未取得本机 Claude 真实消费与提炼的验收证据；安装模板不表示验收通过。

## 2026-10-22 快照增补

CLI 2.1.168 上取证：七类 Hook（含 PreCompact、PostToolUseFailure）已注册于用户 `~/.claude/settings.json`（备份 `settings.json.knowbase-inflight-20261021194917.bak`），knowbase MCP 注册于 `~/.claude.json` 用户级 mcpServers。两点待核：`PostToolUseFailure` 是否为 2.1.168 有效事件名（ZCode 专有名，失败语义或需走 PostToolUse `is_error`）；注册≠触发≠消费，真实会话注入与消费仍未验收。

请求级 Token 具备本地证据：`~/.claude/projects/*/*.jsonl` 行内 `message.usage` 实证键名 `input_tokens/output_tokens/cache_read_input_tokens/cache_creation_input_tokens/cache_creation/service_tier`，可作为 V4 计量来源之一；官方 OpenTelemetry 路径未启用，启用时按白名单过滤字段。快照矩阵见[宿主能力记录](../host-capabilities.md)。
