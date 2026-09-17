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
3. 按某条记忆解决问题（或发现它失效）→ memory_feedback 回填 helpful / not_helpful / outdated / incorrect；
4. stale / once 状态的记忆采信前先在当前环境验证；不要整读 INDEX.md，检索一律走 memory_search；
5. standard / preference 类内容保存时自动进 staging 待人审，属正常治理流程，不要重试绕过；
6. body 里不要出现明文密钥。
```

---

## 与 Claude Code / ZCode 的能力差异（诚实说明）

| 能力 | Claude Code | ZCode | Trae |
|---|---|---|---|
| MCP 6 工具 | ✅ | ✅ | ✅ |
| 会话开始规则注入 | ✅ hook | ✅ hook | ❌ 靠本规则 |
| 每条输入自动检索注入 | ✅ hook（本地检索） | ✅ hook（本地检索） | ❌ 靠模型自觉 search |
| 会话结束漏存再判断 | ✅ Stop hook | ✅ Stop hook* | ❌ 无 |
| 来源标识 | claude-code | zcode | trae |

\* ZCode Stop 钩子依赖 stdin 载荷里的 transcript 路径；若 ZCode 不提供该字段，该钩子会静默放行（不报错），此时任务后沉淀退回"靠规则第 2 条"。
