# eval · 评测集工作目录

## 标注流程（2026-10-23 起，选定的 Phase 1 收尾路径）

1. **填表**：打开 [labeling-sheet.xlsx](labeling-sheet.xlsx)——"待标注"表 74 行，每行附当前检索 top5 作机器建议列（仅供参考）；判定口径见表内"标注指引"页。**优先填前三行高亮的 q045/q057/q066**（Phase 1 实验待仲裁项）。
2. **双标**：golden/长文负例/错误码三层需两人各填一份（labeler 列署名），分歧由负责人仲裁。
3. **复核**：标注完跑一键脚本，输出 A/B 两臂 P@3、正确拒绝率与 Phase 1 终判：
   `/Users/qc/work/knowbase-phase1/.venv-p1/bin/python eval/compute_p3.py <填好的xlsx>`
4. 通过后裁决 Phase 1 转生产（经负责人批准后原地升级运行时并重连 MCP）。

## candidates.jsonl（86 条，2026-10-23 生成，sha256 前 16 位 e5217f8240120381）

- 74 条来自真实 usage_log（去重后池 462 条按层等距抽样），12 条 golden 种子（tests/test_retrieval.py 预标注）。
- 分层：normal 25 / zero_hit 20 / long_neg 15 / errcode 9 / short 5 / golden_must 8 / golden_gap 4。
- 已脱敏 8 条（token/密钥/长哈希→[REDACTED]）；查询正文截 500 字符，原文不出本机。
- 生成方式：只读打开生产库（mode=ro），脚本见 05 号文档§1；**除 golden 外全部待人工标注**。

## 标注方法（详见 ../05-eval-protocol.md §3）

对每条填 `labels` 字段：
- `relevant_ids`：能实质帮助完成该查询的卡片 id（仅主题词重叠不算）；
- `unanswerable`：true=库内不应有任何命中（正确拒绝目标）；
- `confusable_note`：记录"相似但不应命中"的卡（如跨项目同名、401 vs 403）；
- zero_hit 层重点：区分"不可回答"与"应命中但漏检"（漏检请填 relevant_ids——这是 B/C/D 组最珍贵的正样本）；
- long_neg 层默认预期 unanswerable=true，若有真日志粘贴应命中的反例请特别标注。

双标要求：golden + long_neg + errcode 三层 2 人独立标注，分歧仲裁；其余 1 人 + 20% 抽查。

## 冻结

标注完成后输出 `labeled.jsonl` 并记录 sha256；此后只追加不改冻结部分（05 号§5）。
向量缓存（Phase 2 建）：`vector_cache/`，含模型指纹头，指纹变即作废。
