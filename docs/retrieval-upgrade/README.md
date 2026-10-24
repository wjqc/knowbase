# knowbase 自适应混合语义检索升级 · Phase 0 设计交付 + Phase 1 实施（2026-10-23）

依据：2026-10-23 正式设计任务书（"knowbase 自适应混合语义检索升级——正式设计任务书"）。
Phase 0 性质：设计稿与可复核证据，未实施任何生产变更；仅对生产库做过只读探测。
**Phase 1 状态（2026-10-23）**：代码完成于分支 `phase1-retrieval-hygiene`（worktree `/Users/qc/work/knowbase-phase1`，提交 2317c0d，基于 9e33bb2），四项可测门槛全过（[experiments/results-phase1.md](experiments/results-phase1.md)）；待人工标注补 P@3 对照后裁决转生产。主工作区未提交改动与生产运行时未触碰。

## 文档索引

| 文件 | 内容 | 对应任务书 |
|---|---|---|
| 0003-adr-retrieval-tiering-intranet-embedding.md | 架构转向 ADR（内网服务取代本地 ONNX；批准后移入 docs/adr/） | 交付物 1 |
| 01-code-audit.md | 前置代码调查（9 项，全部 file:line 证据） | 前置调查 |
| 02-retrieval-state-machine.md | L1/L2/L3 状态机、降级分类、零结果门控、查询卫生、配置 | D2/D4 |
| 03-embedding-provider-governance.md | Provider 适配器、模型指纹、canary 组合判定、增量索引、迁移回滚 | D3 |
| 04-init-doctor-spec.md | init 向导（宿主发现/备份/三态区分）、doctor 扩展、遥测字段 | D1/D6 |
| 05-eval-protocol.md | 评测集抽样/脱敏/标注协议 | D5 前半 |
| 06-experiment-plan.md | 五组对照实验、指标、验收门槛（建议值待批）、门控校准 | D5 后半 |
| 07-rollout-plan.md | Phase 0–5 计划、风险、回滚、复用清单 | 交付物 7 |
| eval/candidates.jsonl | 86 条真实查询候选（已脱敏，待人工标注） | D5 证据 |
| eval/README.md | 标注指引 | D5 证据 |
| experiments/run_ab.py + ab-results.json + results-phase1.md | Phase 1 A/B 实验脚本、明细与结果 | Phase 1 证据 |

## 尚未验证（不得宣布已具备）

1. 内网 Embedding 网关真实协议（base_url/认证/模型清单/限流/修订号）——**需用户提供**，03 号§7 清单；
2. Codex 宿主 MCP/Hook 配置方式（docs/hosts 无文档）；
3. rc7 待发版与生产 rc6 的 A 组基线差异冻结；
4. 全部验收门槛数值为建议值，须经实验与负责人批准后定稿。

## 批准路径

负责人审阅本目录 → 批准 ADR 与门槛 → Phase 1 启动（查询卫生 + L2 + 门控，B 组实验）。

## Phase 2 状态（2026-10-23 下午）

代码完成于分支 `phase2-retrieval-l3`（worktree，提交 cce7aeb）：Provider 适配器（含 canary）、状态机（熔断/降级/恢复）、指纹隔离 dense、增量重嵌、`reindex --embed`、门控 v0.2（注入路径+同义词集并集）、once 曝光档、doctor 检索状态。FakeProvider 测试 22 项；全套 603 passed+1 skipped 零失败；golden 12/12。**未上生产**（主树仍为 Phase 1 状态）。

上线剩余步骤（依赖内网网关信息）：① config 配 embedding.base_url/model + semantic.enabled → ② `knowbase reindex --embed`（批量真向量+canary 基线）→ ③ serve 探测过 → L3 生效，doctor 看 retrieval_state → ④ C/D/E 对照实验（预嵌缓存）→ ⑤ 发布裁决（gate v0.2 默认开、once 曝光档由负责人定）。
