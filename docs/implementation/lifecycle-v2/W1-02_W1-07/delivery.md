# W1 接线批（W1-2/W1-3/W1-4/W1-5/W1-7）交付记录

- 日期：2026-11-05
- 基线：`e8ab132`（lifecycle v2 batch 1）
- 任务书：`docs/plans/knowbase-lifecycle-v2-implementation.md` W1 阶段
- 缺口清单：`reports/knowbase-lifecycle-capability-gaps.md`（含本批闭合附录）

## 用户确认的两个决策

1. **pytest 验证器默认注册**（DEFAULTS `knowledge_lifecycle.validators`）——validator 登记即
   registered-adapter 信任；解析器只做确定性文本解析，不执行命令，只在显式 CLI 调用时生效。
2. **worker 任务默认门**：`lifecycle_correlate`/`lifecycle_recompute` 默认开；
   `lifecycle_propose` 默认关，`publish` 二级门默认关（staging 首次激活的人工关卡节奏保留）。

## 实现

| 项 | 内容 |
|---|---|
| W1-2 | `observation.pending_runs(conn, limit)`（有递送 + 无证据事件的 run；无递送不列防空转）；worker `lifecycle_correlate`（300s，逐 run 容错，幂等） |
| W1-3 | `knowledge_lifecycle/validators.py`：`parse_pytest_text` 解析 pytest short summary 行（注意 `\s` 吞换行的正则坑已修，`[ \t]` 分隔 + SKIPPED 冒号后缀剥离）；CLI `lifecycle verify --memory-id --report-file [--run-id] [--claim-map]` |
| W1-4 | `governance.apply_decision` 去掉 `or True` 全量重写：投影字段（quality_state/evidence_set_digest/policy_revision）与现值比较后条件写盘；worker `lifecycle_recompute`（3600s）；CLI `lifecycle recompute` |
| W1-5 | `trust.public_from_private` / `trust.key_id_for_public`（key_id=公钥 PEM SHA256 前 12 位，与 team-keys 模板约定一致）；CLI `lifecycle keygen`（0600/0700，幂等读回）、`evidence-export`（identity.install + 签名包落盘）、`evidence-import`（registry 缺失指向 ENROLLMENT.md；import→`materialize_and_recompute` 显式链接；包格式错不重算） |
| W1-7 | worker `lifecycle_propose`（900s，双级门），委托 `changes.propose_admitted(publish=二级门)` |
| 测试隔离 | conftest 新增 autouse `_no_host_project_dir`：屏蔽宿主 `CLAUDE_PROJECT_DIR`/`ZCODE_PROJECT_DIR` 泄漏 |

## 回归

- 新增 5 个测试文件 16 项全过：`test_w1_02_correlate_wiring.py`、`test_w1_03_validator.py`、
  `test_w1_04_recompute.py`、`test_w1_05_evidence_cli.py`、`test_w1_07_propose_worker.py`
  （覆盖幂等、门控、tick 全链、信任语义、quarantine 投影）。
- 受影响既有回归 52 项全过（v2_02/v2_10_11/v2_12_13_15/lifecycle_v2_e2e/merge_boundary/p3/w1 系列）。
- 全量（一次性 uv 离线 editable venv，**故意保留宿主泄漏变量**验证隔离）：
  **1180 passed / 1 failed / 1 skipped**。唯一失败 `test_program_updates_integration` 在干净
  e8ab132 基线同错（更新器子进程 subprocess_failed，本机既有，与本批无关）。

## 测试隔离根因记录（漂移失败 8→1）

- 机制：宿主进程把 `CLAUDE_PROJECT_DIR`/`ZCODE_PROJECT_DIR` 注入 pytest 环境 →
  `_infer_scope`（hooks.py:55，2026-11-04 自动绑定新增 ZCODE 入口）据此解析 scope →
  hook_noise 系列 stop 捕获 `SCOPE_UNRESOLVED` 早退、unified_search 断言 `scope=global` 失败。
- 沙箱不泄漏该变量故三轮全量均过；本机泄漏故单跑也挂——"失败集合随环境漂移"的实体。
- 修法：conftest autouse delenv（测试需要项目目录时自行 setenv），不再依赖运行手册 `-u` 纪律。
- 沙箱记录的 5 个基线失败（concurrent_card_isolation/inflight_refresh/inflight_stdio/
  production_fixes×2）在本机单跑与全量均不复现，判为同族环境性漂移。

## 已知边界

- **verified 晋升仍不可达**：生产观测任务键诚实保留 `unknown:` 前缀（设计 §8.1 不静默填补），
  独立性计数不参与；本批打通的是 L3 反证门（trusted fail → 自动 quarantine，已有测试）。
  晋升解锁待任务身份设计（V2-09 未决项）。
- `evidence.export_package` 仍无增量水位（limit 截断，重复导出靠对端 event_id 幂等兜底）。
- worker 自动证据导出未做（exchange_v2 vs V2-13 统一属用户决策）。
- wide_recall 重定向：`server.py:616`（job 相似卡）与 `materials.py:163`（提炼素材召回）两处
  直调同样不跟随，与 evolution_recall 底座一起待语义决策（替换 vs 追加标注）。
- 运行时未部署：按流程等用户审后 wheel 原地安装 + kill worker + MCP 重连。
