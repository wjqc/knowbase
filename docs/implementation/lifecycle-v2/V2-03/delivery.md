# V2-03 检索评测质量门与影子运行 — 交付记录

- 基线：`425f3fe`；实施日期：2026-11-05。
- 状态：**实现完成 + 隔离验证通过**。真实语义收益评测（≥60 真实任务样本 + 内网模型）external_pending：本轮无可用内网模型授权，未生成任何激活报告，检索保持 L2（这正是该任务的合格阴性交付之一）。

## 变更内容

| 文件 | 变更 |
|---|---|
| `knowbase/retrieval_evaluation.py` | ① `paired_interval` 任务簇 bootstrap：行带 cluster/task_id 时按簇重采样（同任务多条改写不再虚增置信度），无簇键回退并在 method 声明；② `freeze_report` 升 v2 schema：新增 `samples` 门（可判定=正+负+无关 ≥60 且正≥20/负≥20，**unknown 不计任何最低样本**，单独另报）、`latency` 门改为真实混合路径 `end_to_end_p95_ms ≤ 1000ms` 且 `latency_basis='end_to_end_hybrid'`（只交离线 `p95_lexical_compute_ms` 不再放行）；报告带 purpose/label_provenance/task_clusters/heldout_digest；③ 新增 `measure_end_to_end_p95`：生产 search 路径实测 + `hybrid_probe` 模式（激活前用真实远端 embed+cosine 实测混合路径网络耗时），只读不写 delivery/usage |
| `knowbase/semantic_index.py` | `quality()` 只接受 v2 报告（旧 v1 报告仍可读但无激活效力）：校验 evaluable 三计数、正/负例下限、`latency_basis` 与 `end_to_end_p95_ms`、五门全真；新增 `DERIVED_MIN_POSITIVE/DERIVED_MIN_NEGATIVE=20`、`END_TO_END_P95_BUDGET_MS=1000` |
| `knowbase/retrieval_metrics.py` | 新增 `shadow_observe`（影子诊断观测：声明 no_delivery/no_governance）；`report()` 把 origin=shadow_eval 的观测单列为 `shadow` 段，不计入 executed_queries/levels，不产生递送 |

## 验证证据

- `tests/test_v2_03_eval_gates.py`（4 项全过）：簇 bootstrap 确定性 + 无簇键回退声明；同簇改写不伪装独立任务；无模型时 `measure_end_to_end_p95` 如实返回 `lexical_only`（不能过 latency 门）；影子观测单列且 knowledge_uses/retrieval_deliveries 零增量。
- `tests/test_phase2_eval_gates.py` 迁移到 v2 契约（14 项全过）：新增断言——只交离线计算延迟 latency 门 FAIL；unknown=100 也不放行样本不足报告；quality() 拒绝正/负例单侧不足、拒绝 offline_compute_only、拒绝 v1 旧报告。
- `test_adaptive_embedding`（synthetic 报告 fixture 升 v2）、`test_adaptive_integration`、`test_local_retrieval_upgrade` 全过（58 项）。

## 语义保持

- 任务检索继续 L2：`dense()` 未变，无 v2 报告时 `quality()` 返回 None → 不激活，自动路径仍词法。
- 离线重放 `run()` 保持只读（mode=ro 连接），是影子对照的既有机制；新 `shadow_observe` 供 worker/MCP 显式标注。

## 未完成 / external_pending

- 真实内网模型 + ≥60 可判定任务样本的冻结报告与 hybrid-L2 配对 CI：需模型授权与真实材料脱敏副本（V2-16 协同）。在此之前 dense 保持未激活，属设计要求的阴性交付。
- 线上混合 p95 预算 1s 为初始待校准值；调预算须先按任务书说明代价。
