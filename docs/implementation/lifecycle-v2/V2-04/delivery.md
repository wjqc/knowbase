# V2-04 版本资产、schema 与最小数据迁移内核 — 交付记录

- 基线：`425f3fe`；实施日期：2026-11-05。
- 状态：**实现完成 + 隔离验证通过**。真实库迁移在 V2-17 授权后执行；本轮只在临时库验证。

## 变更内容

| 文件 | 变更 |
|---|---|
| `knowbase/knowledge_lifecycle/__init__.py` | 新包（与单文件 `knowbase/lifecycle.py` 无导入冲突） |
| `knowbase/knowledge_lifecycle/contracts.py` | 冻结契约纯函数：`content-v1`（直接复用 `mutations._compute_revision`，防口径漂移）/`content-v2`（完整 SHA256，纳入 applicability/claims/importance，缺失字段以空结构参与不静默剔除）；`revision_identity` 按卡片标记派发（存量卡保持 v1 身份，历史反馈不失联）；`snapshot_hash`（快照原字节全文）；事件信封 `knowbase.lifecycle.events.v1`（canonical JSON 拒 NaN、稳定 event_id、envelope_hash 覆盖 issuer/subject/payload、build/validate、篡改检出）；任务身份（project_key/logical_task_key/origin_run_key/independence_group，未知显式 unknown）；变更 manifest 校验（八关系/R0-R3/base_heads/outputs/source_dispositions/claim_map，NO_CHANGE 无输出、NEW 无 base、redirect 必带目标）；applicability 校验（版本约束可解析，否则移 unknown_fields） |
| `knowbase/knowledge_lifecycle/revisions.py` | 不可变快照：`record_revision`（O_EXCL 写入、同身份幂等、文件被篡改报 SNAPSHOT_HASH_CONFLICT 不静默）；`load_revision` 独立读回（原字节+语义+hash 校验）；`legacy_import`（含 staging：v1 身份登记、快照保留磁盘原字节 CRLF/BOM、dry-run、畸形/缺 id/重复 id 单列不阻断、entity_heads drift 影子单列、幂等 manifest 只写一次、**不写 published_heads 不动 entity_heads**）；`shadow_heads_compare` |
| `knowbase/migrations.py` | v31：`knowledge_revisions`（PK(memory_id,revision_algo,content_revision)，snapshot_hash UNIQUE，写入后不 UPDATE）、`publication_batches`、`published_heads`、`task_runs`（UNIQUE(project_key,origin_run_key) 仅 identity_basis='native' 的部分索引）、`evidence_events`（subject/logical/revokes 三索引）——字段与设计 §9.2 一致 |
| `knowbase/store.py` | `parse_text` 入口统一 CRLF/CR→LF 归一（F-12 修复：`parse` 文本模式与 `source_current` bytes.decode 两路口径一致）；磁盘原字节不改写，快照审计另存原件 |

`store.iter_all`/`find_file` 只扫固定类型目录，V2 资产目录 `lifecycle/` 天然不进普通卡扫描（无重复入库风险，已验证）。

## 验证证据

`tests/test_v2_04_version_assets.py`（14 项全过）：
- v1 hash 与 `_compute_revision` 逐字节一致；v2 补 applicability/claims 必然换版本身份；64/16 位长度。
- canonical_json 拒 NaN、键序稳定；event_id 同输入稳定、改 payload 同 ID 检出 payload_hash_mismatch、未知事件类型拒绝、信封哈希覆盖 subject。
- 任务身份 unknown 显式；独立组确定性。
- manifest 校验矩阵（relation/risk/NO_CHANGE 带输出/NEW 带 base/redirect 无目标/claim mapping 非法/output disposition 非法）。
- 快照 write-once 幂等；篡改文件显式报错；读回 hash_ok。
- legacy 导入：2 卡首跑 2 快照、重跑 0 新增、dry-run 零写入、manifest 同路径幂等、drift 检测命中被改 head 的卡、published_heads 保持空（影子语义）。
- 坏卡（无 frontmatter）+ 重复 ID 单列，正常卡不被阻断。
- **CRLF 回归**：磁盘写 CRLF 后 `source_current` 保持 True（修复前恒 False）；CRLF/LF 同语义同 content_revision 不产生两版；快照保留 CRLF 原字节。

全套回归：1103 passed（除基线既有 7 个环境相关失败外，与本任务无关）。

## 未完成 / 外部待验证

- `meta`/`entity_heads` 扩展列（quality_state/publication_id 等）按消费任务分次迁移（V2-07/12 落地时加列），未提前做通用框架。
- 真实库（246 卡含 30 staging）迁移演练在 V2-17 受控副本执行。
