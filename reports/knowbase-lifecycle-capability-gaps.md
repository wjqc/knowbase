# knowbase 知识库生命周期能力缺口

- 日期：2026-11-05（第一批实施之后）
- 基线：远端 main `7e6f8cc` + 本地第一批改动（W0-1～W0-4、W1-6、W1-1、W1-8，未提交）
- 判定标准：按任务书 `docs/plans/knowbase-lifecycle-v2-implementation.md`（W0–W4，26 项）与设计文档 `docs/design/knowbase-knowledge-lifecycle-v2.md` 的 U1–U10 未决项；缺口 = 机制已存在但未接线、或根本未实现、或无法从代码核实。

## 已闭合（本批）

| 缺口 | 状态 |
|---|---|
| retry_plan 文档与代码不一致（W0-1） | 已修复，分类口径收紧 |
| evidence import 语义与文档不一致（W0-2） | 已修复（文档向代码对齐 + details 记录） |
| governance 独立性计数含 unknown 任务键（W0-2） | 已修复（正向事件过滤，负向仍触发隔离） |
| 工具数等过期表述（W0-3） | 已修正（17 个工具） |
| redirect_ids 检索不跟随（W0-4 → W1-8） | 已实现：index.search + evolution_recall 接线 |
| staging 卡无自动提案入口（W1-6） | 已实现：propose_new_card / propose_admitted + CLI |
| 宿主原生事件不落库（W1-1） | 已实现：hook + MCP 四工具观测接线 |

## 仍未闭合的接线缺口（机制在、无调用方）

### W1-2：attribution.correlate 未接线
`correlate` 仍无生产调用方。观测事件已能落库（W1-1），但事件 → 任务 → 卡片的归因链没有跑起来。后果：治理门收不到真实的正向/负向证据，decide() 的晋升/隔离判定仍只能靠手工喂入的证据。依赖：W1-1 已完成；下一步是确定 correlate 的触发点（hook 处理尾、worker 周期任务、或检索/使用时同步归因）。

### W1-3：verify_execution + 首个 pytest validator 适配器
`verify_execution` 无调用方；L2 验证仍无第一个真实 validator（pytest 结果解析器）。后果：执行验证证据无法自动产生，V2-02 的回归样本口径停留在设计。依赖：需一个真实任务的 pytest 输出样本作适配输入。

### W1-4：治理重算触发缺失
`governance.apply_decision` / `recompute_all` 仍无调用方。W0-3 已把交付记录改成「待 W1-4 接线」。后果：新证据入库后卡片状态不自动重算；晋升/降权/隔离仍需人工触发。依赖：W1-2 的归因输出；触发点候选：worker 周期任务、evidence import 之后、observation 批量落库之后。

### W1-5：证据包 worker / CLI 未接线
`evidence.export_package` / `import_package` 仍无 worker 与 CLI 调用方（W0-2 只修了 import 的文档与返回值）。后果：跨设备/跨成员的签名证据包交换无法实际发生；exchange_v2 目前只交换统计事件（见下）。依赖：V2-13 签名包格式已实现；需定 worker 调度与 CLI 入口。

### W1-7：admission → publish 无调度
`propose_admitted` 已实现（W1-6），但**没有任何 worker 任务调用它**。后果：staging 卡的提案→评估→发布链仍需人工在 CLI 上跑 `lifecycle propose-admitted --publish`；自动闭环的最后一公里仍是手动。依赖：worker 任务注册 + 调度策略（多久扫一次、limit、失败处理）。

## 仍未实现的能力

### W2：八关系自动识别 / 生成全部缺失
八种关系（MERGE / SPLIT / SUPERSEDES / CONTRADICTS / DEPENDS_ON / EXAMPLE_OF / PREREQUISITE / RELATED）的自动识别与生成**全部缺失**；唯一 `propose_change` 调用方仍是 rollback。后果：关系演进只能由人工在提案时声明；V2 机制层最核心的「内容演进闭环」没有生产入口。W2-1（内网模型八关系 POC，需真实脱敏样本）**待用户授权**。

### L4：无任何生产者
L4（最高层归因/验证）没有任何赋值点。后果：四层归因的顶层是空的；治理门的三层证据（L0–L3）之上没有更高级别的综合判定。

### wide_recall 未跟随重定向
W1-8 只接了 `index.search` 与 `evolution_recall`；`wide_recall`（evolution_recall 内部的宽召回底座）刻意未接线（去重语义未定）。后果：经 wide_recall 直出的候选若只含旧卡，不会因重定向带出新卡；evolution_recall 的重定向覆盖依赖候选先进入前 10。需决策：wide_recall 的重定向是「替换旧卡」还是「追加新卡并标注」。

### exchange_v2 与 V2-13 两套并存
`exchange_v2` 只交换统计事件（默认双门关闭）；治理证据走另一条未接线的 V2-13 签名包。两套机制并存且都不通。后果：多成员/多设备之间的证据同步没有一条走得通的路；P5 真实试点的前提不成立。需决策：统一走 V2-13 签名包（推荐，带签名与吊销），exchange_v2 降级为统计心跳或废弃。

## 真实验证缺口（W3）

- **双设备验证未做**：真实 Agent 会话、两台设备、跨设备同步均未验证。
- **样本量不足**：派生样本下限 60、CI95 下限 ≥ 0、latency 门 1000ms、Recall@5 ≥ 0.5 等门禁均未在真实数据上跑过；freeze_report 五门验收未在真实库上执行。
- **最小闭环数未验证**：全链路最小会话数 3、最小实例数 2 的阈值是设计值，未经真实闭环验证。
- **72/4/8 积压数字无法从代码核实**（W0-5 待授权复核）：72 条 failed_permanent（64 material_budget_exceeded + 8 index_changed）、4 条 needs_review、8 条 requeue_ready——其中 8 条 index_changed 按代码只能来自 embedding_refresh 任务，不是提炼积压；数字本身来自另一会话的报告，未在真实库上复核。

## 治理与清理缺口（W4）

- **W4-2**：material_budget 超限的分拆重试未实现（64 条 material_budget_exceeded 积压的出路）。
- **W4-3**：简化（simplification）——需真实验收通过后才可动，未开始。
- **W4-4**：stale 修订的 revalidation 路径未实现（stale 卡目前只标记 `revalidation_pending`、不可见，凭新版本证据重新晋升的回路没有入口；W1-6 的 propose_admitted 会跳过 stale 并记原因，但回路本身不存在）。
- **W4-5**：V2-17 生产迁移——真实 memory.db 已被 Hook 间接触发 `migrations.apply_all` 推到 v31，v32–v34 未应用；**不回滚、迁移需用户授权**。
- **V2-17 遗留**：源码树即生产（Hooks 每次触发都 import 当前源码）——开发期的一次 apply_all 就能推进真实库版本；需隔离开发与生产迁移路径，否则同类事故会重演。

## 配置与环境缺口

- **默认配置下 staging 首次激活仍依赖人工操作**：V2-01 门关闭时 decide() 的晋升出口是关的；本批未动默认配置（改默认需用户决策，涉及自进化定义的核心——人工关卡去除的节奏）。
- **配置曾被外部清空**：2026-11-05 11:34:37 `~/.knowbase/config.json` 的 automation/embedding/retrieval 三节被外部清空（含 secret_ref 执行器凭据），未恢复；distillation 被阻塞的原因是 `distillation_blocked_no_executor`。沙箱无法复核用户机配置状态。
- **测试隔离泄漏**：全量套件在基线即有偶发失败（本批最终全量跑：1159 passed / 2 skipped / 5 failed，5 项失败全部在干净基线 worktree 上单跑复现，确认为基线既有；失败集合随运行环境/顺序漂移，如 hook_noise 在本批三轮全量中均未失败）。测试隔离是回归可信度的底座，需修，否则「全量通过」本身不可信。
- **沙箱 /tmp 容量**：全量跑的 pytest 临时目录曾占满 512M tmpfs 导致套件崩塌；本次改用 `TMPDIR=~/workspace/.tmp-pytest` 绕过。沙箱长期跑法应固定 TMPDIR 到大磁盘。

## 授权待决（未执行，勿擅自做）

| 任务 | 内容 | 状态 |
|---|---|---|
| W0-5 | 真实库 72/4/8 积压只读复核 | 待授权 |
| W2-1 | 内网模型八关系 POC（真实脱敏样本） | 待授权 |
| W4-5 | V2-17 生产迁移（真实库 v31 → v32–v34） | 待授权 |

## 缺口优先级建议（按用户既定优先级：准确性与证据可信度第一）

1. **W1-2 + W1-4**（归因与重算触发）：没有这两步，W1-1 落库的观测事件是死数据，治理门收不到证据。
2. **W1-7**（admission→publish 调度）：把 W1-6 已实现的提案链跑起来，staging 首次激活才真正脱离人工。
3. **W1-3**（首个 pytest validator）：没有验证证据，晋升判定缺一半输入。
4. **W1-5 + exchange 统一**：跨成员同步的前提；否则 P5 试点无从谈起。
5. **W2 八关系 + L4**：内容演进闭环的核心能力，工作量最大，放后面。
6. **W4-4 stale 回路 + W4-2 分拆重试**：积压出路。
7. **W3 真实验证**：贯穿始终，每批接线后都应在真实库上跑一次 freeze_report 五门。
