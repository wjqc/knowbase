# Trae 接入 knowbase 使用规则（粘贴用）

> Trae 不支持 hook，也不读 AGENTS.md，把下面内容粘贴到 **Trae → 设置 → 规则 → 用户规则**（一次，全局生效）；
> 或放进某个项目的 `.trae/rules/project_rules.md`（仅该项目生效）。
> 另：Trae CN 已开启 `AI.rules.importClaudeMd`，把本文件内容放进项目根目录 `CLAUDE.md` 同样生效。

```text
【knowbase 经验库使用规则】
1. 任务涉及具体项目/系统/报错/环境配置时，先调用 memory_search（knowbase MCP，用 ≥3 字技术词，
   如 "EasyConnect 死锁"、"Docker 镜像加速"），命中后先 memory_read 再动手；
2. 任务结束：产生了踩坑（现象/原因/正确做法）、技术决策、固化流程、用户约束 → memory_save；
   返回"已存在相似记忆"时改用 memory_update；
3. 按某条记忆实际行动并验证任务结果后，memory_feedback 回填 helpful / not_helpful / outdated / incorrect，携带 expected_content_revision、memory_use 返回的 use_id、稳定 task_outcome_id。旧调用只保留未绑定观察，不改变治理；重试同一结果保持ID。正文变化后旧反馈不能验证新版本，工具标签不代表真实人数；
4. stale / once 状态的记忆采信前先在当前环境验证；不要整读 INDEX.md，检索一律走 memory_search；
5. standard / preference / bizrule 保存时强制进 staging 待人审；正式规则 Agent 不可 memory_update，人工使用 promote/revise CLI；
6. body 里不要出现明文密钥。
7. 长任务先列目标、约束、子任务；进入新子任务、新错误和重要决策前主动调用 memory_context，
   对实际候选调用 memory_use 记录 referenced/adopted/irrelevant/unverified；context_event_id 是返回该卡的检索事件，沿用会话、host_profile与content_revision。需要任务关联的全文读取时，memory_read 同时传 session_id/event_id/context_event_id/host_profile，得到独立阅读回执；不把命中或阅读自动变成helpful。
   指定 session_id/event_id/scope/stage/topics（当前主题+短查询），不要等下一条用户输入。
8. 阶段结束同时识别技术经验及业务规则。材料较多时 memory_capture → memory_job_claim(protocol_version=2)，
   has_more 时 memory_job_material 续页；查重、按八项结构 memory_job_submit 后核对实际 memory_id/保存状态。
9. 业务规则记录条件、结果、例外、provenance 和 rule_context，确认/激活/实现状态分开；
   代码推断无业务确认时保留待确认候选，不伪造出处，不自动激活 staging。
10. 只处理已读完整的片段；无可复用结论给明确理由。候选已提交或任务入队不代表知识卡已保存。
```

---

## 与 Claude Code / ZCode 的能力差异（诚实说明）

| 能力 | Claude Code | ZCode | Trae |
|---|---|---|---|
| MCP 14 工具（重连新版本后） | 支持 | 支持 | 支持 |
| 会话开始规则注入 | ✅ hook | ✅ hook | ❌ 靠本规则 |
| 每条输入自动检索注入 | ✅ hook（本地检索） | ✅ hook（本地检索） | ❌ 靠模型自觉 search |
| 会话结束漏存再判断 | ✅ Stop hook | ✅ Stop hook* | ❌ 无 |
| 来源标识 | claude-code | zcode | trae |
| 执行中使用 | 工具事件适配待真实验收 | 工具事件注入/消费局部通过，提炼待验 | 主动工作流，待真实验收 |

\* ZCode Stop 钩子依赖 stdin 载荷里的 transcript 路径；若 ZCode 不提供该字段，该钩子会静默放行（不报错），此时任务后沉淀退回"靠规则第 2 条"。
