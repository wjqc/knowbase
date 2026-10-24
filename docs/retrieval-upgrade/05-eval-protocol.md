# 05 · 真实查询评测集：抽样、脱敏、标注协议（Phase 0）

目标：60–100 条人工审核样本，覆盖任务书 D5 全部类别；向量实验可复现（模型指纹+向量缓存）。评测集是离线产物，存于本工作区，不进知识 Git 库。

## 1. 数据源与抽样

- 源：真实库 `~/knowbase/memory.db` usage_log（截至 2026-10-23：search 278 / auto_search 196 / inflight_search 153 条）。detail 为 JSON（record_search，index.py:520-527），含 query/scope/hits；解析失败按原样跳过。
- 去重：query 去空白转小写取前 64 字符为键，同键保留最新一条。
- 分层配额（目标 90 条，池不足时按比例缩减并记录）：

| 层 | 配额 | 判定 | 考察点 |
|---|---|---|---|
| golden 种子 | 12 | tests/test_retrieval.py 既有 must8+gap4 | 回归红线，预标注直接继承 |
| 长文负例 | 15 | len(query)>600 | 正确拒绝/无关注入率 |
| 零命中 | 20 | hits=[] 且 len≤600 | 不可回答 vs 漏检（标注区分） |
| 精确标识 | 10 | 含错误码/类名/版本号正则 | L1/L3 精确性、401 vs 403 型混淆 |
| 常规命中 | 25 | 有命中且非上述层 | 排序质量 P@3/MRR |
| 短模糊 | 8 | len<10 | 门控与 LIKE 回退 |

- 已生成候选：`eval/candidates.jsonl`（90 条，含分层标签与现场属性，**全部待人工标注**）。生成脚本只读打开生产库（mode=ro）。

## 2. 脱敏（先于一切标注与导出）

1. 正则掩码：`sk-[A-Za-z0-9]+`、`(?i)(token|password|secret|key|authorization)[=:]\s*\S+`、`Bearer \S+`、≥32 位十六进制/base64 串 → `[REDACTED]`；命中即记 `scrubbed=true`。
2. 查询正文截 500 字符入集（原始长度记 `query_len`）；完整原文不出本机。
3. 评测集含成员名（agent 字段）仅保留宿主类型（zcode/claude/trae），不保留个人标识。
4. 复用 rc6 凭据清洗（docs/credential-redaction-2026-10-22.md）的掩码正则做二次扫描。

## 3. 标注协议

- 标注字段（candidates.jsonl 内置空位）：`relevant_ids[]`（能回答该查询的卡片 id）、`unanswerable`（bool）、`confusable_note`（相似但不应命中的卡，如跨项目同名）、`labeler`、`date`、`notes`。
- 判定口径：**relevant = 该卡内容若在查询发生时被展示，能实质帮助完成任务**；仅有主题词重叠不算。unanswerable = 库内不应有任何卡命中（正确拒绝目标）。
- 双标：每条至少 1 人标注；golden 种子 + 长文负例 + 精确标识三层由 2 人独立标（负责人 + 一名成员），不一致仲裁后定稿；其余层 1 人标 + 抽查 20%。
- 标注者可查库（只读）；禁止为凑 relevant 改卡——发现的卡片缺陷记入 `notes`，走正常修订流程。
- 定稿：`eval/labeled.jsonl`（冻结，记 sha256）；后续加样本只追加新层不改动冻结部分。

## 4. 复现性（向量实验专用）

- 全部卡片向量 + 全部评测查询向量**一次性批量预嵌并缓存**（`eval/vector_cache/`，含 model_fingerprint、时间、维度）；五组实验（06 号）只读缓存，不在线调用服务。
- 缓存头部记录模型指纹（03 号§3）；指纹变化=缓存作废重建。
- 实验环境一次性 venv（既有惯例：源码测试用一次性 editable venv），不装生产运行时。

## 5. 维护

- 每月从新 usage_log 追加 10–15 条（同分层规则），保持与真实分布同步；卡片下线/合并导致 relevant_ids 失效时在冻结集外维护"有效性补丁表"，不改冻结数据。
- golden set（tests/test_retrieval.py）保持独立可执行回归红线地位，评测集是其超集扩展。
