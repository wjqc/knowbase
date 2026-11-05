# V2-02 运行健康、升级窗口和积压可解释恢复 — 交付记录

- 基线：`425f3fe`；实施日期：2026-11-05。
- 状态：**实现完成 + 隔离验证通过**。真实主机上的健康状态需部署后观察（V2-17）。

## 变更内容

| 文件 | 变更 |
|---|---|
| `knowbase/migrations.py` | v30：`automation_schedule` 增 `last_attempt_at`/`last_effective_success_at`/`blocked_since`/`build_loaded`/`result_counts`；新表 `maintenance_alerts` |
| `knowbase/automation/worker.py` | `_reschedule` 区分每次尝试/真实产出成功/连续 blocked 起点，记录实际执行进程构建指纹与累计结果分布；`_maybe_alert` 结构化维护事件（连续两轮 blocked → `task_blocked`；持续 >1h → `task_blocked_persistent`；连续 ≥3 error → `task_failing`；同窗口去重）；`status()` 增 build 三层（loaded 指纹 + 各宿主会话 current/stale）、`oldest_pending`（按任务 kind 的最老非终态作业）、最近告警；新增 `retry_plan()` 只读积压分类（requeue_ready / lease_stuck / needs_review / failed_permanent / blocked_config） |
| `knowbase/updates/engine.py` | 安装后**实测**安装树 `module_hash`，不一致抛 `installed_build_mismatch_after_install` 走既有回滚；`active.json` 写入 `installed_build_id`+`installed_measured_at`；`status()` 输出登记构建 vs 实测构建，漂移显式标 `install_drift` |
| `knowbase/doctor.py` | 新增只读检查 `automation_health`：blocked 任务、各任务 loaded 构建指纹、告警总数与最近 5 条 |
| `knowbase/__main__.py` | `worker retry-plan` 子命令（只读） |

探针回执语义未变：`receipts.py` 一直只证明"执行器能力可用"（schema 名 executor-verification），不证明模型质量；`status().distillation` 字段输出原因码（如 `verified_executor_required`），不伪造 ready。

## 验证证据

- `tests/test_v2_02_runtime_health.py`（7 项全过）：
  - v30 迁移列与告警表存在；
  - attempt/effective-success/skipped 三分离 + build_loaded 写入 + result_counts 累计；
  - 连续两轮 blocked 出一次 `task_blocked`，第三轮不重复，恢复后 blocked_since 清空；
  - blocked >1h 出一次 `task_blocked_persistent`；
  - `retry_plan` 五类分类正确且只读（前后 jobs 表逐行相等）；
  - `status()` 输出 build.loaded/sessions/oldest_pending/alerts；
  - doctor `automation_health` warn 级呈现 blocked 与告警。
- 既有回归：`test_p2_autonomous`/`test_team_worker`/`test_autonomous_local_completion`/`test_init_doctor_upgrade` 49 项、`test_program_updates*`/`test_inplace_upgrade` 22 项全部通过（active.json 新字段不影响既有断言）。

## 未完成 / 外部待验证

- 真实升级窗口演练（安装→实测→登记一致→旧连接 needs_reconnect）需 V2-17 授权后在真实运行环境执行；本轮仅在隔离测试覆盖逻辑。
- 历史积压（真实库 140 capture）的重放执行不在本轮范围：`retry-plan` 已可对其出清单，执行需授权。
