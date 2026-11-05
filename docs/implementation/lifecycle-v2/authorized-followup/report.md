# 授权后续任务执行报告（2026-11-05）

授权范围：①真实库 legacy 导入 ②原地更新安装+重连验证 ③外部验证采办 ④真实积压处置。
执行环境：源码仓 `425f3fe` + 本会话 V2 改动（41 个未提交文件）；runtime `~/.knowbase/runtime/venv`。

## 任务 1：真实库 legacy 导入 — ✅ 完成

| 步骤 | 结果 |
|---|---|
| 只读盘点 | 251 卡（active/once 229、verified 12、archived 10；staging 32）；写入 `~/.knowbase/backups/v2-legacy-import-inventory.json` |
| 一致性备份 | `~/.knowbase/backups/v2-legacy-import-20261105-041955/`：memory.db（backup API，407MB）、knowledge.tar.gz（405KB）、config/active/manifest/executor-receipts/sessions |
| 导入 | RepoLock 内执行：**251 快照、0 错误、0 drift、0 needs_review**；manifest `lifecycle/imports/legacy-51afd8340d6e.json` |
| 独立验证 | 全新只读连接 251 行（全 content-v1）；抽样 readback（verified/staging）hash_ok |
| 幂等 | 复跑 0 新增 / 251 existing |
| Git | lifecycle/ 本地提交 `bedd95a`（未推送） |

**执行中修复一个 P1 缺陷**：`legacy_import` 缺 `conn.commit()`——首两次执行的数据随连接关闭回滚（磁盘文件在、DB 行丢；隔离测试因同连接内断言未暴露）。已修复并补**跨连接幂等回归测试**（close→重开→existing=N/snapshots=0 + 只读连接行数）。真实库重跑后账实一致。

## 任务 2：原地更新安装 + 重连验证 — ✅ 完成

1. **机制修正（重要）**：runtime 是 site-packages 固定副本；此前"runtime=源码直跑"结论有误——真相是**无 `-I` 且 cwd=源码目录的入口会加载工作区源码**（真实库的 v30–v34 迁移即由此类子进程应用；常驻 serve/bridge/worker 均带 `-I` 安全）。已更新长期记忆与交付文档。
2. **安装**：`uv build --wheel --offline` + `uv pip install --python ~/.knowbase/runtime/venv/bin/python --offline --no-deps --reinstall`。安装后 `-I` probe：installed build = 源码 build = `ad213e8fc4923a3d`，14 个 V2 模块可加载（cryptography 50.0.2 已在位，无新增依赖）。
3. **登记对账**：active.json/manifest.json 写入 `build_id=installed_build_id=ad213e8f…` + `installed_measured_at` + source 状态说明；旧漂移登记（`6a0cf13e`）修正。
4. **正式入口验证**：MCP stdio 真实握手（initialize→tools/list→memory_search）经 runtime 入口通过：18 工具、真实检索命中（L2 词法，未触远端）。
5. **worker 重启**：TERM 旧进程（31668）→ launchd KeepAlive 拉新（88269）→ lease_recovery/knowledge_sync/vector_maintenance 以新构建执行，`build_loaded=ad213e8f`，automation_health=pass。
6. **宿主重连状态**：sessions 标记 stale 待重连：`zcode`（本会话）、`zcode-verify`、`trae`、`codex-wenjian3`。重连后即加载新代码（各宿主 MCP 连接不能代重连）。

## 任务 3：外部验证采办 — ⚠️ 部分完成（模型侧被外部配置变更阻断）

- **配置事实**：`~/.knowbase/config.json` 于 **11:34:37 被外部清空** automation/embedding/retrieval 三段（11:21 还有执行器回执 passed=True）。这是用户/并行会话的主动变更——未擅自恢复（执行器含 secret_ref 凭据引用，Agent 无从重建）。
- 因此：八关系模型 POC、检索评测真实样本、语义端到端测量**保持 external_pending**，阻塞原因由"无授权"更新为"模型端点配置已被清空，待用户回填"。
- **已落地（不依赖模型）**：
  - 设备密钥对 Ed25519（`~/.knowbase/team-keys/`，私钥 0600，key_id `key-fb9c763ddae9`）；
  - 信任名单模板 + 跨机登记流程文档（`trust-registry.template.json` + `ENROLLMENT.md`：登记/最小授权/轮换/吊销/启用交换步骤）；交换默认保持 off。
  - **真实宿主最小接线验证**：本 zcode 会话真实事件序列经 `observation.ingest` 入真实库 task_runs/native_events（4 事件、tool 配对 1/1、幂等重放零增量、identity_basis=local_segment 诚实标注）。Hook 管线自动接线（host_events→observation）仍 external_pending。

## 任务 4：真实积压处置 — ⚠️ 分类完成，执行受配置阻断

`worker retry-plan`（只读）真实分类：

| 类别 | 数量 | 明细 | 处置 |
|---|---|---|---|
| failed_permanent | 72 | 64 `material_budget_exceeded` + 8 `index_changed` | 不盲目重试：前者需材料拆分/分页策略（V2-18 容量项），后者需按 V2-05 重评估语义。保留 |
| needs_review | 4 | — | 人工出口，保留 |
| requeue_ready | 8 | awaiting_agent（等 Agent 领取提炼协议） | 正常等待态，非卡死，不动 |

- distillation 当前 `blocked: distillation_blocked_no_executor`——根因即 automation 配置被清空（非执行器故障：11:21 回执 passed=True）。
- **恢复条件（待用户）**：回填 `automation` 段（enabled/allowed_scopes/executors 含 secret_ref）→ worker 下一轮自动解锁；届时 8 个 awaiting 照常流转，72 failed 建议先做材料策略调整再选择性 `jobs retry`。

## 回滚方案（已演练/可行）

- **导入回滚**：DROP lifecycle/ 相关不影响服务；DB 侧 `DELETE FROM knowledge_revisions`（251 行）+ 删 `lifecycle/` 目录即可回导入前；整库恢复用备份 `v2-legacy-import-20261105-041955`（升级后新增账本会丢，按 V2-17 规程先封存增量）。
- **程序回滚**：按 AGENTS 用 Git 兼容源码重建 wheel 原地安装（本报告已示范构建+安装+worker 重启全流程）；旧程序不识别 v31+ 表但不读不写，安全。
- **配置**：未动（含 11:34 外部清空后的现状）。

## 遗留与建议

1. 请用户裁决 automation/embedding 配置清空是否有意；如需恢复 V2 验证，回填后我可继续 POC/评测。
2. 基线 7 个失败测试（test_hook_noise×6、test_production_fixes×1）疑似经 formal-CLI 子进程泄漏到真实环境——与本轮 cwd-加载机制同源，建议单独立项修复测试隔离。
3. 源码仓 41 个未提交文件 + 真实库本地提交 `bedd95a`（未推送）——提交/推送仍待你审查决定。
