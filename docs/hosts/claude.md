# Claude Code 执行中接线

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
