# Trae knowbase 规则（统一生命周期版，2026-11-09）

本机已在原生设置启用 ~/.trae-cn/hooks.json，并关闭重复导入 Claude Hook；规则已在全局规则界面保存，不需要再次手动粘贴。接入与回滚见 [最新本机记录](hosts/latest-local.md)。

现行规则（以 MCP 初始化说明为准，此处为摘要）：

- 任务涉及具体项目/系统/报错时先 memory_search / memory_context（host_profile=trae，真实 session_id/scope）；检索命中先 memory_read 再动手；不整读 INDEX.md。
- 日常任务以完整的业务答复直接结束。已启用的 Hook 静默采集入队；主会话不等待、不补写、不重试或报告回执。仅在用户明确要求保存或维护经验时用 memory_save / memory_update（八项结构、来源与适用条件如实标注，不把原始文档或明文密钥入卡）。
- 实际消费 memory_use；任务结果验证后 memory_feedback（携带当前 content_revision/use_id/稳定 task_outcome_id/task_result）。正文变化后旧反馈不能验证新版本；工具标签不代表真实人数。
- **统一生命周期**：卡片类型只作分类。项目内有依据的新增可直接发布为 active+once；改变已发布结论、共享范围（global）或缺少依据的内容先待审。发布和可信晋升分开；普通 helpful 回填不会发布或晋升。
- Hook 采集是可选项。Stop 不因待办/未反馈阻断，不催办、不等模型。**日常任务不要求领取、补材料或续租**；只有明确整理知识才查询项目内待办、领取和提交，最终读回。入队、待审、发布和验证分别报告。
- `memory_update` 可能返回待审变更 ID，收到它时旧发布版本仍有效。授权人员使用 `lifecycle validate/publish` 审核发布；共享范围的变更需要权威确认。

## 历史版本说明

- rc4/rc5 时期的"粘贴用十条规则"已废弃：其中"standard/preference/bizrule 强制进 staging 待人审、Agent 不可 update"等按类型分流的条目自 2026-11-09 起被统一生命周期取代（见 docs/plans/knowbase-trae-mcp-unified-lifecycle.md）；"任务结束自动 capture→claim"的条目收敛为"明确整理任务才走提炼协议"。
- rc5 全文见 Git 历史（docs/trae-rules.md @ e753066 之前）。

## 与 Claude Code / ZCode 的能力差异（诚实说明）

| 能力 | Claude Code | ZCode | Trae |
|---|---|---|---|
| MCP 17 工具 | 支持 | 支持 | 支持 |
| 会话开始规则注入 | ✅ hook | ✅ hook | ✅ 原生 hooks.json（16 天零流量已修复：设置→Conversation→本地自动运行） |
| 每条输入自动检索注入 | ✅ hook（本地检索） | ✅ hook（本地检索） | ✅ UserPromptSubmit hook |
| 会话结束静默入队 | ✅ Stop hook | ✅ Stop hook（含有界尾持久化） | ✅ Stop hook |
| 来源标识 | claude-code | zcode | trae |
| 独立执行器（无兜底） | 复用宿主 CLI | 复用宿主 CLI | 同机其他宿主 CLI 执行 |

\* ZCode Stop 使用宿主提供的 transcript；宿主只提供当轮最终答复时，后台逐轮采集并按内容去重。临时文件删除后可用已持久化的有界尾部，采集失败不进入主任务。
