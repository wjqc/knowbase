# ZCode 执行中接线

以下对象合并到用户级 `~/.zcode/cli/config.json` 的 `hooks.events`，保持 hooks.enabled=true，不覆盖现有 SessionStart/UserPromptSubmit/Stop 和其他插件。替换 Python 路径为本机 knowbase 虚拟环境。

```json
{
  "PreToolUse": [{"hooks": [{"type": "process", "command": "/你的路径/knowbase/.venv/bin/python", "args": ["-m", "knowbase", "hook", "tool-before", "--style", "zcode"], "timeoutMs": 5000}]}],
  "PostToolUse": [{"hooks": [{"type": "process", "command": "/你的路径/knowbase/.venv/bin/python", "args": ["-m", "knowbase", "hook", "tool-after", "--style", "zcode"], "timeoutMs": 5000}]}],
  "PostToolUseFailure": [{"hooks": [{"type": "process", "command": "/你的路径/knowbase/.venv/bin/python", "args": ["-m", "knowbase", "hook", "tool-failure", "--style", "zcode"], "timeoutMs": 5000}]}]
}
```

当前本机官方 zcode-guide 0.3.0 仅列七种事件，不支持 PreCompact；不要添加该事件。压缩后的恢复依赖保留下来的上下文状态/原文观察和 SessionStart 或主动 memory_context(stage=recover)。

工具 Hook 输出严格顶层 additionalContext。本机真实测试采用用户级注册才获得执行中候选；不要仅凭项目配置文件存在认为已经接入。接线后要检查真实事件日志及下一步消费，详见[验收](../inflight-rollout.md)。

## 2026-10-22 快照增补

桌面 3.16.2 上以当前会话活体复核：六事件（SessionStart/UserPromptSubmit/Stop/PreToolUse/PostToolUse/PostToolUseFailure）经 runtime `ef4189a` 注册于用户级配置且实际触发——SessionStart 规则注入与工具后 additionalContext 均到达模型上下文，hook 载荷含 `session_id`。PreCompact 不支持的结论维持。Token 粒度 unknown：UI 使用统计不作为稳定导出依据。真实任务整链验收与 headless MCP 提炼缺口见[宿主能力记录](../host-capabilities.md)。
