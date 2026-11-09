# Trae knowbase 规则（统一生命周期版，2026-11-09）

本机已在原生设置启用 ~/.trae-cn/hooks.json，并关闭重复导入 Claude Hook；规则已在全局规则界面保存，不需要再次手动粘贴。接入与回滚见 [最新本机记录](hosts/latest-local.md)。

现行规则（以 MCP 初始化说明为准，此处为摘要）：

- 任务涉及具体项目/系统/报错时先 memory_search / memory_context（host_profile=trae，真实 session_id/scope）；检索命中先 memory_read 再动手；不整读 INDEX.md。
- 有明确可复用结论时 memory_save（八项结构、来源与适用条件如实标注；无出处、不完整材料如实标注，不把原始文档或明文密钥入卡）。
- 实际消费 memory_use；任务结果验证后 memory_feedback（携带当前 content_revision/use_id/稳定 task_outcome_id/task_result）。正文变化后旧反馈不能验证新版本；工具标签不代表真实人数。
- **统一生命周期**：类型只作检索分类。是否直接发布由统一风险规则判定——项目内新增且有出处直接发布（active+once）；改变已发布结论、共享范围（global）或来源不足进入 staging 待审，凭独立任务验证证据达标后自动激活（当前版本 3 个独立完成任务、2 会话，负面证据失效并保留历史）。不得凑任务或伪造出处/人工确认。
- Hook 自动采集静默入队（临时 transcript 已有有界尾持久化，宿主删除后仍可后台解析）；Stop 不因待办/未反馈阻断，不催办、不等模型。**日常任务不要求领取、补材料或续租**；只有明确整理知识/维护任务才走 capture → claim(protocol_version=2) → material 续页 → submit → 读回；入队和普通阅读不当作成功。
- 待审卡人工放行：knowbase promote / lifecycle confirm（global 需权威确认 authority_ref）；修订已生效卡用 revise（不再限定类型）。

## 历史版本说明

- rc4/rc5 时期的"粘贴用十条规则"已废弃：其中"standard/preference/bizrule 强制进 staging 待人审、Agent 不可 update"等按类型分流的条目自 2026-11-09 起被统一生命周期取代（见 docs/plans/knowbase-trae-mcp-unified-lifecycle.md）；"任务结束自动 capture→claim"的条目收敛为"明确整理任务才走提炼协议"。
- rc5 全文见 Git 历史（docs/trae-rules.md @ e753066 之前）。

## 与 Claude Code / ZCode 的能力差异（诚实说明）

| 能力 | Claude Code | ZCode | Trae |
|---|---|---|---|
| MCP 17 工具 | 支持 | 支持 | 支持 |
| 会话开始规则注入 | ✅ hook | ✅ hook | ✅ 原生 hooks.json（16 天零流量已修复：设置→Conversation→本地自动运行） |
| 每条输入自动检索注入 | ✅ hook（本地检索） | ✅ hook（本地检索） | ✅ UserPromptSubmit hook |
| 会话结束漏存再判断 | ✅ Stop hook | ✅ Stop hook（含有界尾持久化） | ✅ Stop hook |
| 来源标识 | claude-code | zcode | trae |
| 独立执行器（无兜底） | 复用宿主 CLI | 复用宿主 CLI | 同机其他宿主 CLI 执行 |

\* ZCode Stop 钩子依赖 stdin 载荷里的 transcript 路径；若该文件已被宿主删除，采集回退到入队时的有界尾持久化与 DB 观察路径，不留失败在主任务里。
