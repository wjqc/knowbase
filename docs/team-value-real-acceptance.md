# 三宿主真实样本验收协议（E1 / 三人试点门槛）

状态：协议已定义，**未执行**。任一宿主未取得真实样本前，该宿主维持 H0 快照的模式声明（[host-capabilities.md](host-capabilities.md)），不得宣称自动接入通过。

依据：[团队价值实施方案](knowbase-team-value-implementation-plan.md) §3.3/§4/§12；本文件是其 E1 退出条件"Trae 优先完整真实样本，另两宿主同标准"的执行手册，并附本地契约对照审计。

## 1. 前置条件

- 三位真实成员 A/B/C（人，不是工具标签），各自安装实例与正常宿主许可；适配器统一版本（当前 runtime `ef4189a`）。
- 业务审核者一名（可与成员兼任，操作权限仍区分）：预先冻结材料并签核"应识别/不应生效"人工预期清单。
- 冻结材料（E0 永久材料包口径）：≥32 KiB 长输入、关键规则位于后半段、一个例外条款、一次可复现错误及其解决证据；记录 material 版本/哈希。
- 隔离 scope 与测试知识库；不默认修改现有会话（方案 §3.1）。

## 2. 每宿主执行步骤（Trae 优先，另两宿主同标准）

1. **真实任务开场**：Trae（无 Hook，辅助模式）由规则引导 Agent 显式调用 `memory_context(stage=task_plan)`；ZCode/Claude 走已注册 Hook 自动触发 + Agent 显式补充。核对规划上下文（`planning_context`）被 Agent 实际使用。
2. **阶段切换 ≥2 次**：核对主题增量——新主题补检索、同任务/同知识版本命中签名缓存不重复查询（`coverage_summary.queries_completed` 只增不重）。
3. **触发一次错误**：核对 `new_failure` 检索使用归一化错误特征（时间戳/ANSI/十六进制已脱敏为 `<volatile>`）、knowbase 自身调用被排除、无跨项目召回。
4. **长材料增量沉淀**：capture → claim(protocol_version=2) → material 续页读完 → submit；核对后半段关键规则被提炼、例外条款进入"不适用条件"、预算不足显示 partial 不假完成（`coverage_summary.queries_pending`>0 时不得宣称完整）。
5. **消费与反馈**：候选递送 → `memory_use(referenced/adopted)` → 任务结果验证后 `memory_feedback`（携带 expected_content_revision/use_id/task_outcome_id）；`use_id` 须来自本任务运行的 `memory_use` 回执（团队汇总端校验命中，未命中不计入任务结论）；Agent 声明与用户反馈分开记录。
6. **读回与治理**：新卡带完整身份读回；bizrule/standard 保持 staging 待人工 promote，Agent 不直接生效。
7. **证据留存**：`context_events`/`knowledge_uses`/`feedback_events` 落库记录 + 人工预期核对表；启用 `value.events_enabled` 的成员同时产生 `value_outbox` 事件（V1，默认关闭）。

## 3. 通过 / 不通过判据（每宿主独立判定）

通过 = 从真实输入、经正式入口（Hook 或 MCP）、到模型实际消费、再到知识读回的全链每一步都有独立证据；缺失环节按"未关联/缺失"如实展示。

不通过即方案 §1.1 不合格列：Hook 成功但模型没收到；命中错误项目；只有检索没有消费；分页读完即宣称提炼完成；待审规则被自动采纳；把工具标签当人数。

## 4. E1 本地契约对照审计（2026-10-22，源码 `7ce499c`+V1）

| 方案条款 | 实现落点 | 证据 | 状态 |
|---|---|---|---|
| §4.1 六类触发时机 | `hooks.py` cmd_*、`adapters/host_events.py` stage 映射与 checkpoint 节流 | tests/test_inflight_host_contract.py（16 项） | 本地已证 |
| 失败检索＋错误归一化＋自调用排除（T09） | host_events.handle：sanitize、`<volatile>` 归一、error_hash、self_call 跳过 | host_contract / context 测试 | 本地已证 |
| 相同主题/任务版本/知识版本缓存 | context.execute 签名缓存（topic+knowledge_version+scope+evidence+task_revision） | test_inflight_context::test_multiple_execution_stages | 本地已证 |
| 预算不足 partial＋未处理主题 | `deferred_budget` + `next_topics[].remaining_queries` | test_topics_over_budget_remain_deferred | 本地已证 |
| 覆盖诊断（§2 差距项） | context.execute `coverage_summary`（本轮新增） | test_coverage_summary_reports_increment_and_pending | 本地已证 |
| 提示正文有界 | `context_token_budget`×2 字符预算＋续读引用 | context 测试 | 本地已证 |
| 同任务/同触发类别冷却 30s | 未按字面实现：由签名缓存＋checkpoint_work_ops/minutes 节流替代（更强去重，不丢观察记录） | — | 设计替代；试点后决定是否补独立冷却配置 |
| §4.2 长材料三游标/manifest/租约 | materials.py、jobs.py（v10–v13） | test_material_paging / test_inflight_acceptance_materials | 本地已证 |
| 完成回执区分完整/未处理/无内容/阻断/失败 | settlement_state 五态＋legacy_unverified | 材料与作业测试 | 本地已证 |
| §4.3 规则待审与 code_refs/provenance | staging 强制＋bizrule 提交流程 | bizrule/governance 脚本回归 | 本地已证 |
| T07 长材料后半段关键规则 | E0 冻结材料包（32K/6 锚点） | ZCode 有界验收会话（辅助纠错后通过，不称零干预） | ZCode 有界样本已过；Trae/Claude 待真实样本 |
| T08 材料含提示注入 | sanitizer＋指令属材料不越权 | 材料/sanitizer 测试 | 本地已证 |

## 5. 剩余门槛（人工/跨机，不阻塞 A1）

- Trae 首个完整真实样本（最高优先，辅助模式标准即 §2 步骤）。
- Claude Code 真实会话消费样本（注册已证，消费未证）。
- 三人 A→B→C 跨机复用链（V2 验收）：A 沉淀→B 另一机器采纳→C 第三宿主验证修订版本不继承旧有效性。

完成前不宣称：三宿主自动接入通过、跨成员复用已发生、任何净节省收益。
