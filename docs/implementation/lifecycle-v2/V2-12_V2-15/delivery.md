# V2-12 治理 + V2-13 证据交换 + V2-14 一致性 + V2-15 适用范围 — 交付记录

- 基线：`425f3fe`；实施日期：2026-11-05。状态：**实现完成 + 隔离验证通过**。

## V2-12（`knowledge_lifecycle/governance.py`）

- `decide()`：确定性投影——只认 evidence_events 中未撤销、
  trust_basis=registered-adapter 的 L3/L4；L0/L1/L2 永远零治理票；独立任务按
  logical_task_key 去重（同任务 N 次重放仍 1 任务）；≥3 独立任务 + ≥2 独立组
  且零未裁决反证 → verified；有可信同版本反证 → quarantine；撤销剔除后重算，
  支持不足降回 validated（不自动清零）。输出含 input_set_digest/policy_revision
  （同输入同输出）。
- `apply_decision()`：兼容镜像落地（confidence/quality_state/quarantine→
  stale+原因）；治理字段不动 content revision。
- `recompute_all()`：worker/导入后重算入口。

## V2-13（`trust.py` + `evidence.py` + 迁移 v34）

- `trust.py`：cryptography Ed25519（50.0.2，两 venv 均在位）——`sign_envelope`
  覆盖 schema+event_id+完整信封哈希（含 key_id）；`verify_envelope`；
  `generate_keypair`（私钥永不入包）；`load_trust_registry`/`check_issuer`
  （unknown_issuer / key_revoked / scope_not_granted / trusted）。
- `evidence.py`：`export_package`（本地 trusted L3 → 签名信封包，白名单字段、
  无正文/命令输出）；`import_package`：信封校验→信任与验签→传输幂等（同 ID
  异 hash → conflict 双方隔离）→业务去重（同语义证据一次）→撤销物化（先撤销
  后原事件 → tombstone，原事件后到仍无效）；未信事件保留 external_unverified
  零治理影响。
- 迁移 v34：evidence_events 补 claim_id/key_id/scope 列。

## V2-14（`reconcile.py`）

- `detect_divergent_heads`：按 publication manifest before/after 检测并发版本头
  （两机各发 r2a/r2b → 隔离待显式合并，不 last-writer 获胜）。
- `sync_state`：本机证据/撤销/发布水位（离线服务最后确认版本 + as_of 语义）。
- `materialize_and_recompute`：导入后本地重算幂等入口（接收机必须重算，
  同步来的 decision 只是可核验声明）。

## V2-15（`applicability.py` + lifecycle 归档保护）

- `observe_environment`：environment.observed 事件 + 适用范围冲突卡生成待复验
  清单——**mismatch 只限制该环境推荐，不判卡错、不删内容**；无结构条件老卡
  unknown 不批量判过期。
- `evaluate_card`/`spec_matches`：可解析版本约束（>=2,<3 / 2.x）；不可解析回
  exact/unknown 不猜。
- `archive_protection` 接入 `lifecycle.auto_archive_sweep`：critical/keep_reason/
  复验中/隔离冲突阻止自动归档（时间衰减只表示"需复核"）。

## 验证证据（`tests/test_v2_12_13_15_governance_exchange.py` 5 项全过）

- 治理：2 任务 validated → 第 3 任务 verified；同任务 5 次重放任务数不增；
  撤 2 正证 → 降回 validated；可信反证 → quarantine（stale+原因，正文不被覆盖）。
- 交换：签名包导入 2；重发 10 次零增量；合法重签同 ID 异 payload → conflict；
  纯篡改 → 信封拒收；未登记密钥伪签发/未登记 issuer → external_unverified 零治理；
  撤销先到 → 原事件重导不复活，peer 库治理重算后独立任务=1 不晋升。
- 适用范围：2.1.0 vs >=1,<2 → 待复验单列、卡保持 active；版本判定正反例；
  critical 卡 200 天不归档、普通卡正常归档。

**external_pending**：两台真实设备的 Git 分支交换与 ACK、真实异机一致性行程
（同机双库只证明机制）、密钥登记/轮换真实流程、≥80 对八关系模型 POC。
