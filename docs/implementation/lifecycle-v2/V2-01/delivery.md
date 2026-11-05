# V2-01 治理安全门与观测口径 — 交付记录

- 基线：`425f3fe`（0.2.0rc9）；实施日期：2026-11-05。
- 状态：**实现完成 + 隔离验证通过**。生产运行包未更新（默认策略未部署到真实库，需 V2-17 授权）。

## 变更内容

| 文件 | 变更 |
|---|---|
| `knowbase/config.py` | `evolution` 新增默认：`legacy_promotion_enabled=false`、`trusted_promotion_enabled=false`、`feedback_ranking_factor_enabled=false` |
| `knowbase/evolution.py` | 新增 `POLICY_REVISION`、`trusted_promotion_available()`；`decide()` 在可信验证来源不可用时返回 `promote=retire=False` + `blocked_reason=trusted_validation_unavailable` + `policy_revision`，证据计数照常返回供展示 |
| `knowbase/versioned_feedback.py` | ① incorrect/outdated→stale 由 trusted 门控制；② 无 task_result 两工具标签晋升由 `legacy_promotion_enabled` 控制（默认关）；③ 回执新增 `observed`（outcome/task_result/verification_source/evidence_basis/policy_revision）、`governance_applied`（实际治理是否生效，不再是"绑定即 true"）、`unknown_reason`；④ read+completed 的 staging 激活随 decide 门自动关闭（原路径经 decide promote） |
| `knowbase/index.py` | `_feedback_factor` 默认中性（1.0）；frontmatter 反馈计数不再参与检索排序，`feedback_ranking_factor_enabled` 为显式回滚开关 |
| `knowbase/retrieval_metrics.py` | report 新增 `use_receipts_delivery_linked` / `use_receipts_unlinked`（未关联回执单列，不混入有效采用分母） |

未删除任何历史数据通道：4 条历史 feedback、6 条 use、3 条 agent_report task_outcome 保留为观察；`verification_source` 仍为来源声明枚举，不新增认证语义。

## 保留的显式人工渠道

`lifecycle.human_verify` / `human_archive` / CLI `verify/promote/archive` 未动（V2-08 统一权威门）。

## 验证证据

- 新契约测试 `tests/test_v2_01_governance_gate.py`（9 项，全过）：
  - read+completed ×3 跨实例 → staging 不激活，`governance_applied=false`，`unknown_reason=trusted_validation_unavailable`，helpful_count 照常积累；
  - adopted/read + 自报 completed ×3（含 `verification_source=independent` 自填）→ once 不晋升；
  - 无 task_result 两工具标签 → 默认不晋升；
  - 自报 incorrect → active 不置 stale，unhelpful_count 记观察；
  - 同请求重放 10 次 → 1 条 feedback_events / 1 条 feedback_log / 计数 1；
  - 同卡 helpful 0→40 后同查询得分不变（排序中性）；
  - 正式 context→read→feedback 后 MCP 形状读回治理字段未变；
  - 执行派生数据集：无 adopted use 时 positive=0（unknown 不凑样本）。
- 既有机制测试迁移：`test_phase0_self_evolution`、`test_auto_evolution`、`test_versioned_feedback`、`test_phase1_retirement`、`test_p4_maintenance_onboarding` 的晋升机制断言改为在 `evolution.trusted_promotion_enabled=true`（过渡开关，模拟可信验证器已接入）下运行；`test_versioned_feedback.py::test_unverified...` 改名并断言新契约（负反馈为观察）。脚本式 `test_e2e.py`/`test_governance.py`（pytest 排除、单独进程执行）通过。
- 全套 pytest：1103 passed / 7 failed——7 个失败为基线既有（`test_hook_noise` 6 + `test_production_fixes` 1，均在未改动 HEAD 上复现，环境相关），与本任务无关。

## 回滚方案

任一开关置 true 即恢复对应旧行为（配置文件级，无需回代码）：
- `evolution.legacy_promotion_enabled=true` → 恢复两工具标签晋升；
- `evolution.trusted_promotion_enabled=true` → 恢复反馈驱动晋升/降级（过渡语义）；
- `evolution.feedback_ranking_factor_enabled=true` → 恢复计数参与排序。

历史回执协议（feedback_events 表结构、request_id 幂等、崩溃恢复 `_finish`）未变，旧数据可读。
