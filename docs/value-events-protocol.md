# knowbase 价值事件协议 knowbase.value.events.v1

状态：**H0 冻结基线**（2026-10-22，随[团队价值实施方案](knowbase-team-value-implementation-plan.md) §5–§7 形成）。V1 实现本地持久记录，A1 实现文件汇总，S1 可选服务沿用同一协议；与既有 `knowbase.metrics.v1` 并存，不合并口径、不相加。

破坏性变更必须新增 schema 版本并保留共存窗口，所有启用实例确认前不下线旧版本（方案 §10.2）；接收端对未知 `schema_version` 明确拒绝，不静默忽略必需字段。

## 公共字段

事件生成即固定 `event_id` / `schema_version` / `event_type`；本地汇总库或可选服务端按 `(team_id, event_id)` 唯一。

| 字段 | 约束 |
|---|---|
| `event_id`, `schema_version`, `event_type` | 事件生成即固定；重传保留原 ID，禁止生成第二个“等价”事件 |
| `operation_id`, `attempt_id`, `native_call_id` | 逻辑操作、执行尝试、宿主原生调用分别记录；无原生 ID 时明确缺失，不伪造 |
| `installation_id`, `emitter_instance_id`, `sequence` | 安装与进程不同；恢复会话不能仅按会话＋进程序号去重 |
| `host`, `host_version`, `adapter_version`, `capability_id` | 归因及解释采集边界；不能替代人员身份 |
| `member_binding_id` | 引用有来源的成员/设备确认，保留人工确认或认证绑定的证据等级；不能凭自报字段获得成员或管理权限 |
| `task_id`, `task_run_id`, `session_id`, `task_revision`, `stage` | 一项任务可跨会话/宿主；运行重试与新业务任务区分 |
| `memory_id`, `content_revision`, `context_event_id`, `use_id` | 沿用既有版本及消费回执协议；按事件类型条件必填 |
| `outcome_id`, `feedback_source`, `evidence_ref` | 结果稳定标识、来源类别（Agent 声明/用户反馈/独立验证）、受权限控制的证据引用 |
| `occurred_at`, `received_at` | UTC；发生与接收时间分别保存，检查时钟偏移 |
| `payload_hash`, `payload` | 规范化哈希；payload 按事件类型白名单字段，禁止任意正文扩展 |

查询原文不进入团队交换文件或上传内容；检索类事件只报告主题类别/数量、结果版本及必要关联。证据正文留在原系统，汇总只存受控引用；不可访问时显示不可复核。

## 事件类型

首批 15 种点分类型，可分批启用；未启用的类型不宣称覆盖：

`capability.snapshot`、`task.started`、`task.checkpoint`、`task.completed`、`task.abandoned`、`knowledge.created`、`knowledge.revised`、`knowledge.activated`、`retrieval.completed`、`knowledge.delivered`、`knowledge.read`、`knowledge.used`、`feedback.recorded`、`model.usage`、`collection.gap`

各类型的 payload 白名单字段与条件必填矩阵在 V1 实现时随契约测试冻结（见下“待补充”）。

## 本地状态机与 outbox

- 状态：`pending → leased → acknowledged`；超时重试，永久拒绝进入 `quarantined`。
- 建议 outbox 字段：`event_id PK, schema_version, event_type, payload_json, payload_hash, state, attempt, next_attempt_at, lease_owner, lease_until, last_error_code, created_at, acknowledged_at`。租约需 fencing/版本约束；迁移编号实施时取下一可用版本。
- 首版无后台发送器（S1 启用才接线）：持久记录供显式文件导出，本机看板直接读取允许范围记录。
- 关键业务事件由对应业务事务写入 outbox；跨文件恢复以既有回执/哈希为准，恢复最终成功后才生成唯一完成事件。同一关键业务记录只能生成一个对应事件，靠唯一约束与稳定键兜底。

## 文件汇总（首版 A1）

- `knowbase value export --output <事件包>`：显式导出最小事件包及来源清单，不自动上传。
- `knowbase value import <事件包>`：汇总机按事件 ID 幂等导入，冲突隔离；未确认身份保持未知。汇总库为独立 SQLite 分析库（知识 Git 仓库外），禁止导入他人事件进本机 `memory.db`。
- 接收回执：导出成功≠已接收；汇总机导入后生成含事件 ID/哈希的接收回执，源端显式导入并核对后才置 acknowledged。
- `knowbase value snapshot --output <快照>`：冻结时间窗与允许范围的最小看板快照；快照只带授权范围内的结果和证据摘要，不是完整原始事件包。解析前校验 schema、大小和类型，拒绝可执行内容与任意本地路径引用。
- 事件包/快照经团队已有授权渠道传递；文件哈希仅校验完整性，不能认证人员。

## 可选服务模式（S1）

后台上传与 HTTP 接口仅在启用 S1 时接线，统一 `/api/value/v1`：`POST /events:batch`（逐事件 `accepted/duplicate/retryable_error/rejected`）、`GET /collection-status`、`GET /metrics`、`GET /reuse-cases`、`GET /tasks/{task_id}/evidence`；报告首版由本地共享内核生成，不要求 `POST /reports`。

批上限默认 200 事件/未压缩 1 MiB，超限拆批、单事件超限隔离；网络总超时 5 秒、指数退避最大 15 分钟；401/403 暂停上传提示重授权；409 同 ID 不同内容隔离告警；部分成功只逐项确认服务端持久接受/重复的事件。边界默认值须在 H0/V1 试点后冻结（方案 §6.3）。

批示例（简化结构，省略安装/会话等公共字段）：

```json
{
  "schema": "knowbase.value.events.v1",
  "batch_id": "batch-example",
  "events": [{
    "event_id": "evt-example",
    "event_type": "knowledge.used",
    "operation_id": "op-example",
    "attempt_id": "attempt-example",
    "task_id": "task-example",
    "task_run_id": "run-example",
    "occurred_at": "2026-10-22T02:00:00Z",
    "payload": {
      "memory_id": "card-example",
      "content_revision": "revision-example",
      "context_event_id": "context-example",
      "use_id": "use-example",
      "state": "adopted"
    }
  }]
}
```

团队与成员权威取自认证及绑定，不信任客户端覆盖。查询默认 UTC 半开区间 `[since,until)`，周起点周一；文件模式在生成和分发前裁剪授权范围，不能仅靠 HTML 隐藏成员名。

## 待 V4/S1 补充（附件，不阻塞字段冻结）

- 各宿主原生调用 ID → `native_call_id` 映射表；宿主能力以 [host-capabilities.md](host-capabilities.md) H0 快照为准（ZCode/Trae 为 unknown，Claude Code 注册待真实触发核对）。
- `model.usage` 字段映射：Claude Code 本地 transcripts 键名已实证（`input_tokens/output_tokens/cache_read_input_tokens/cache_creation_input_tokens/service_tier`）；ZCode/Trae 保持 unknown，缺失不填零。
- 本构建已启用发射点（8 种）：`knowledge.created/revised/activated`（写路径与人工激活同事务）、`knowledge.used/read`（消费回执同事务）、`feedback.recorded`（反馈状态迁移同事务）、`task.completed/abandoned`（任务结果账本同事务，`verification_source` 区分 agent_report/user_confirmed/independent）。其余类型的 payload 白名单随接入批次在 `knowbase/value/events.py` 冻结。
