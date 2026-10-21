# 执行中知识使用：升级、接入与验收

适用当前源码；新增机制复用原 memory.db、经验卡和 Git，不需要新模型 API。

## 1. 升级前

暂停当前机器的 knowbase 写入；记录源码版本与现有配置。使用 SQLite 在线 backup 备份 memory.db，保留 Markdown/Git revision 与本地运行材料；不要只复制活跃数据库主文件。配置备份留本机，不提交 Git。

新版本按 v9–v14 增量迁移：执行上下文、材料/批次、复合片段键、提交回执、应用租约、相似卡快照、脱敏事件观察。旧 usage/feedback/spool 保留。旧候选和截断材料保持 legacy_unverified/待复核，不自动大批成卡。

更新代码后重连宿主的 knowbase MCP，确认可以发现 `memory_context`、`memory_job_material` 等共 14 个工具。已启动旧服务不会因磁盘代码变化自动获得新协议；当前对话应重连 MCP 或在更新后开启新会话。

## 2. 配置

合并至现有 `~/.knowbase/config.json`，保留已有 repo_path、Git 配置等字段：

```json
{
  "scope_map": {"/你的项目绝对路径": "库中对应项目名"},
  "inflight": {
    "enabled": true,
    "capture_enabled": false,
    "mode": "assisted",
    "checkpoint_work_ops": 6,
    "checkpoint_minutes": 10
  }
}
```

新机制默认关闭；开启前先完成当前宿主的隔离检索、提炼和回滚验收。mode 是配置声明，不是验收证明；没有完整证据时保留 assisted。映射支持规范路径别名；不要把不同项目映射成一个 scope 来提高命中率。

首机先用上述配置开启自动检索/本地观察，capture_enabled=false 暂不强制自动入队；原收尾提醒和 Agent 显式的阶段提炼仍可用。完整宿主链路验证后再打开 capture_enabled。不要把只读试用当成全部自动沉淀或三人验收已通过。

正式使用建议安装经过测试的 wheel 到固定运行环境，MCP 与 Hook 同时使用该环境的 `python -I -m knowbase ...`；避免 editable 安装，且用 `-I` 防止工作目录里的同名源码覆盖发行包。运行环境仍指向原 repo_path 和同一 memory.db；固定发行包不是第二套知识库。切换前备份配置，回滚时关闭新机制并恢复之前的命令路径。

宿主接线见 [ZCode](hosts/zcode.md)、[Claude Code](hosts/claude.md)；Trae 使用[主动工作流规则](trae-rules.md)。

## 3. Agent 工作流

1. 先把长需求拆为目标、约束、子任务及原文位置，调用 memory_context(stage=task_plan)，以短查询表达当前阶段的问题。
2. 本轮执行中进入子任务、新错误和重要决策前调用 memory_context；命中后读卡并核对条件。不要等用户下一条消息。
3. 阶段完成检查技术经验与业务规则。较长材料用 memory_capture；领取时 memory_job_claim(protocol_version=2)，has_more 时 memory_job_material 续页。
   开启工具观察后，可沿用 Hook 提示的 session_id/scope/host_profile 直接调用 memory_capture 收集持久增量，无需模型重抄长原文。无观察来源时需显式提供材料，不假装已抓取。
4. 根据八项结构提炼，引用已完整交付片段的 ID/hash；有相似卡时读回并给 duplicate_evaluation。业务规则保留出处、业务确认/激活/实现状态，不把代码推断当业务真值。
5. memory_job_submit 后核对 results 中实际 memory_id、content_revision、治理与同步状态；校验失败不推进进度。bizrule/standard/preference 仍进 staging，由人工审核。
6. 任务未处理完保留 awaiting_agent/validating。memory_job_status 可恢复已确定的应用；没有活跃宿主时不生成新语义结论。

Hook 会先将可观察事件脱敏存本地数据库。transcript 不可得或已重写时可从已观察事件恢复，但 coverage_scope=observed_host_events 不代表完整聊天。未知输入/破损 transcript、缺证据和队列满均要明确报告，不能标成功。

## 4. 验收与能力记录

- 本地测试：完整长输入、尾部规则、续页、租约、版本、候选真实成卡、幂等恢复、默认检索隔离与正式 stdio。
- 待人工复核材料包：tests/fixtures/inflight/，含冻结的真实项目设计输入、来源哈希、6 个规则/反例锚点；human_approved=false 时不能计作人工金标准通过。
- 宿主：一次用户输入后，第二/第三子任务仍能获得经验；下一步行动实际消费候选；阶段完成可通过宿主 MCP 领取、提交并读回卡。
- 团队：每人至少一条实际多阶段任务，确认他人同步后能查到生效卡，待审规则不混入默认建议。
- 净提效：分别记录相关阶段覆盖、采用并验证的任务数、知识遗漏、额外时延及可得的总 token。没有对照不报节省百分比。

当前证据与限制见 [宿主能力记录](host-capabilities.md)。ZCode 隔离 build 模式已证实执行中注入和消费；MCP 提炼因该 headless 模式没有 permission client 被拒，整链未验收。应在正常有许可客户端的宿主会话中验证；不要为测试关闭团队的许可/审批策略。

## 5. 回滚

关闭 inflight.enabled，移除新工具事件配置即可恢复原三 Hook 路径；保留数据和待办，不删表。已保存卡按现有治理处理，不批量撤销正确知识。

回退旧代码前确认能忽略新字段和新表；如需恢复旧备份，先冻结写入并导出升级后卡片、回执、未完成材料，记录实际损失/重放窗口。原始日志不随 Git 分享，备份仍需包含数据库。
