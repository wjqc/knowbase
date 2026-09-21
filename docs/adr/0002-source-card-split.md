# ADR-0002：原始材料（source）与可复用知识（card）彻底分层

- 状态：已接受（P0 已落地；P1–P3 见文末路线）
- 日期：2026-09-21
- 来源：多轮方案讨论定稿（"source 负责保真，card 负责复用；两者绝不能再混成一种东西"）

## 背景

`knowbase import` 曾把整篇文档直接产成 `reference`（R 卡）进入默认检索。这带来三个问题：

1. 未提炼的原始材料混入经验检索，`memory_search` 返回的是"某篇文档的全文快照"而不是"可判断、可执行的知识"；
2. 一篇架构文档塞成一张 R 卡，既不自足也不可判断适用性，Agent 无法决定"能不能用"；
3. `scope` 粗粒度隔离无法表达适用条件/排除条件，相关度分数被误当作适用性判断。

## 决策

### 双层存储

- **Source Artifact**（`sources/objects/<sha256>.md` + `sources/manifests/SRC-YYYY-NNNN.yaml`）：解析后的文本快照按内容哈希寻址，manifest 记录来源/格式/解析器/导入时间。绝对源路径只留在本机 `memory.db.source_state`。不生成知识卡、不进默认检索。
- **Reusable Knowledge Card**：一张卡一个可独立判断的结论。`reference` 退出可发布类型（`memory_save` 拒绝、`memory_search` 不返回），仅保留 `memory_read` 兼容读取。

### 八项小节强约束（card-v2 schema）

新卡 frontmatter 打 `schema: card-v2` 标记，正文必须含八项小节且内容可判断：结论 / 解决的问题 / 适用条件 / 不适用条件 / 可执行动作 / 关键证据 / 验证情况 / 未知与待确认。lint 同时拦截空洞表述（"视情况而定"不算适用条件、"参见某文档"不算证据、"测试通过"不算验证记录、小节只有标题不算通过）。存量卡不带标记，沿用旧 per-type 规则，不做迁移（P3 再迁）。

### 分期路线

- **P0（本次）**：检索/速览排除 reference；import 只产 source；save 拒绝 reference；八项结构强制；接口兼容保留。
- **P1**：knowledge_extract（source→candidate）、staging 审核（approve/merge/reject）、结构化 code_refs、source→card 派生关系。
- **P2**：applicability/exclusions schema、检索前适用性门（先判断能不能用，再算排第几）、版本漂移触发复验。
- **P3**：存量 18 张 R 卡转 source、分批提炼、人工审核后归档旧卡。

## 取舍

- 不把八项结构强加给存量卡：旧卡一改就要补全八项会阻断正常的 memory_update，P0 只约束新卡，迁移期可回滚。
- import 的 `--type/--staging` 参数保留但忽略：避免破坏既有脚本与命令行习惯，行为语义已废弃并在输出中明示。
- manifest 不记绝对路径：共享 Git 内容必须跨机器可理解；本机幂等状态由 source_state 承担。
- 同路径源文件变化 = 新 sha = 新快照（内容寻址语义），不再沿用旧"源文已变化请复核"的阻断：source 层没有覆盖问题。

## 验证

P0 落地回归：pytest 117/117（含 test_source_layer.py 15 项新测试：source 导入/去重/不进检索、reference 退出检索与速览、八项结构六类拒绝场景、存量卡 update 兼容）；脚本回归 e2e / import / hooks / governance / bizrule / retrieval（真实库）全部通过。
