# 08 · 演进闭环接通：once 卡曝光与自动晋升（Phase 2 提案，待批准）

来源：2026-10-23 负责人裁决——知识卡管理必须走 memory_feedback/use 闭环自然演进（自动晋升与更新），不做人工批量 verify。

## 现状核实（file:line 证据）

1. **自动晋升/退役引擎已实现且为确定性规则**（无模型裁量）：
   - `evolution.py:28-42` decide：active 卡 helpful 且无失败/负反馈、独立任务数 ≥ max(2, min_helpful_tasks=3)、会话数 ≥ min_sessions=2 → promote；incorrect/outdated 或负反馈任务 ≥2 → retire；
   - `versioned_feedback.py:184-188` 落库：promote 时 confidence once→verified、写 last_verified、钉 verified_content_revision（版本绑定，内容更新后重新计票）；:193-201 另有跨工具标签的晋升路径；
   - 证据按 content_revision+task_key 去重（evolution.py:5-25），失败任务阻断晋升。
2. **闭环输入被 verified-only 注入政策掐断**（hooks.py inject_min_confidence=verified，hooks.py:84-85 语义在分支同）：once 卡不被自动注入 → 无曝光 → 无 memory_use 绑定 → 无 feedback → 永远 once。评测集实测 4 条正例（q048/q051/q052/q059）全部因此漏检——引擎裸检索排名 1/2/4/5 找得到。
3. 环整体偏细：5 周 668 次调用中 feedback 仅 47 次（usage_log 实测）。

## 提案：门控保护的 once 曝光（替代人工 verify）

- `inject_min_confidence` 新增档位 `"verified_plus_once_gated"`：
  - verified 卡照旧注入；
  - once 卡须通过硬门控（≥2 查询词命中，即门控 v0.2 规则）才可注入，**每次注入最多 1 张**，注入文本保留 `once` 标记（Agent 协议本就要求核对置信度）；
  - stale 卡维持排除。
- 曝光后完全走既有闭环：Agent memory_read → memory_use 绑定 → memory_feedback（helpful/not_helpful/outdated/incorrect）→ evolution 自动晋升/退役。有用卡 3 任务 2 会话后自动 verified；无用卡自动沉底。零人工干预。
- 依赖：门控 v0.2（CJK 分词对齐，修 q067 类漏网）先行——once 曝光面必须比 verified 更严，否则重新引入污染。q061/q073 型词典碰撞误伤由 v0.2 + 词典精调一并处理。
- 风险与回退：config 单开关回退 "verified"；曝光质量由 B+门控实测（负例正确拒绝 90%，CI 80–99%）与上线后 query_class/gate 遥测持续监控。

## "自动更新"的边界（明确写入产品规范）

- **自动**：状态演进（once→verified 晋升、退役、待复核标记）、版本钉定与重置、演进事件留痕。
- **不自动**：内容改写。outdated 反馈触发退役标记并指引修订，内容修订一律走 memory_update 的 expected_content_revision CAS——防伪造与防漂移的设计边界，不因自动化目标放松。

## 验收（若批准，随 Phase 2 一起）

- 4 张正例卡（P-2026-0045/P-b7e72b675530/P-262de4d654a3/D-d7a552d39910）在影子运行中应获得曝光与 use/feedback 机会；闭环周报能展示晋升/退役事件。
- once 曝光引入的新增误注入 ≤ 门控校准预算（负例正确拒绝率不低于 85%）。
