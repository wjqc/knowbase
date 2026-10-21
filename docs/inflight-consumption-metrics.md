# 执行中知识消费、反馈与团队统计协议

本地开发实现，尚未进入32ad7f0固定运行包。仍共用原memory.db与Git知识库，无新增模型调用。

## 消费链

1. `memory_context(session_id,event_id,task_id,task_revision,scope,current_goal,topics)` 返回候选的 `id/content_revision` 和实际任务身份。`freshness` 说明本地版本及后台刷新状态；当前调用不等待网络。后台只接收远端变更，推送仍由写入链路负责。
2. 需要全文时，`memory_read(id,session_id,event_id,context_event_id,host_profile)` 可获得与任务关联的独立阅读回执。`context_event_id` 必须指向实际返回该候选的检索事件；缓存跳过的事件没有递送新候选，应沿用原递送事件。
3. `memory_use(session_id,event_id,context_event_id,id,content_revision,state,host_profile)` 接受 `referenced/adopted/irrelevant/unverified`，返回 `use_id`。每次状态变化使用独立稳定事件ID；同事件重试保持ID和内容。记录不会自动产生helpful。
4. 实际执行并验证后，`memory_feedback(id,outcome,expected_content_revision,use_id,task_outcome_id,request_id)` 回填结果。`use_id`须是该卡版本的参考/采纳回执；`task_outcome_id`表示本任务的一项稳定验证结果，重试不能重新生成。去重粒度为卡版本+会话/任务/方案版本+结果ID；更换请求ID或工具标签不能重复累计同一结果。

旧feedback调用仍可用，但仅记 `legacy_unbound` 观察，不改变有效验证计数或治理状态。旧历史日志保留，不追认成新版本验证。正文等内容字段变更后清除旧验证及本版本反馈计数；新版本重新验证。人工修订规则不等于人工确认有效，可信人工确认走CLI verify。工具标签用于归因，不认证人数，human前缀也不构成人工权限。

反馈写前持久回执及文件before/after哈希，落盘后通过索引/日志事务完成；中断后重放核对实际文件，内容冲突返回明确错误，不凭文件存在宣称恢复。旧回执重放不会给新版本加计数或提交新正文。

## 本机汇总导出

新版CLI示例（在安装新版的环境执行）：

```bash
python -I -m knowbase metrics --source-label member-a-device-1 \
  --since 2026-10-22T00:00:00+08:00 --until 2026-10-29T00:00:00+08:00 \
  --naive-timezone Asia/Shanghai
```

或调用 `memory_metrics(since,until,source_label,naive_timezone)`。输出schema为 `knowbase.metrics.v1`，只读现有SQLite快照，不迁移、不触网、不自动上传，导出本身不记新的命令遥测。未初始化或不能读取返回错误；旧库缺表返回null，不能解释成零。

内容仅含命令/状态计数、执行中检索与消费状态、版本绑定反馈计数、库存类型数量和日志覆盖时间。不包含查询、参数、对话、业务正文、卡ID、项目scope名、真实工具/人员/设备标签或本机路径。`source_label`是操作者主动填入的报告标签，不验证身份。未知数据库维度值统一归other，不原样导出。

`command_calls`从新版接线后记录MCP和CLI/Hook调用，包括重试与可观察错误，不记录参数和异常正文。`legacy_usage_records`是旧代码留下的非完整日志，可能含历史重复，与新计数并列展示，禁止相加。进程被杀或本地遥测写入失败仍可能遗漏，日志计数不是完整聊天轮数。阅读、参考、采纳和helpful各有口径，不混为同一漏斗人数。

时间窗为含起点、不含终点，边界无时区时按UTC。旧无时区日志按 `naive_timezone` 解释，建议显式IANA时区；默认local是导出时本机固定偏移，历史夏令时可能有偏差。`logging_coverage_all_time`和 `invalid_timestamp_records`是全量日志口径，`inventory_snapshot`是当前库存，其他活动计数在时间窗内。消费汇总按窗口内任务-卡版本的最后明确状态去重，不能把参考→采纳算两个采用任务。

## 三人收集与验收

三位成员在各自设备用相同新版和相同时间窗分别导出，给每台设备唯一标签。报告留在本机或按团队既有渠道提供；不需要复制memory.db或原始对话。汇总前由成员确认设备归属、是否克隆过数据库及日志覆盖，排除同源副本/重复报告；一人多个工具或多个设备不能当多个人。同一设备不同重叠时间窗也不能直接相加。

报告可说明记录到多少调用、哪些阶段查询了、多少任务声明采纳/完成验证。跨成员真实复用仍需实际任务和接收端生效卡证据；单靠标签和计数不能证明。净token、用时和代码质量收益使用独立同类任务对照，不能把helpful条数折算成工时。
