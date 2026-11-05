# V2-05 演进检索 + V2-09 宿主观测 — 交付记录

- 基线：`425f3fe`；实施日期：2026-11-05。状态：**实现完成 + 隔离验证通过**。

## V2-05 变更（`knowbase/knowledge_lifecycle/recall.py` 新增）

- `evolution_recall(conn, query, scope, include_archived=False)`：
  - 域 = 本 scope + global；**staging/stale 参与**（宽召回基础域只排 archived），archived 仅治理目的显式开启；跨 scope 召回为 0（不拿 B 项目卡做"全库查重"）。
  - 召回路：wide_recall（FTS/CJK/标题/code_refs）+ 错误码精确路（HTTP 3xx-5xx/ECONN*/ERR_*/0x…）+ 关系邻接路（top-10 命中卡 frontmatter relations 指向的同域卡补入——替代/矛盾对象不被词法漏掉）。
  - 每路 50、融合上限 30、截断数入快照诊断；候选带 content_revision 版本绑定。
  - 冻结快照 `knowbase.evolution-recall.v1`：purpose=evolution、query hash、候选 heads digest + **域 heads digest**（双 digest：候选被改或域内新卡出现都判漂移）、降级标记（当前 lexical_only——演进语义代际未建）。
- `snapshot_changed`：提交候选前的 NEEDS_REASSESSMENT 检测基础。

**external_pending**：真实内网模型八关系 POC（≥80 对、每关系 ≥10 盲测）未运行（无模型授权）；演进 Embedding 代际（purpose=evolution）待 POC 后建，当前诚实降级 lexical_only。

## V2-09 变更

| 文件 | 变更 |
|---|---|
| `migrations.py` | v32：`native_events`（PK(host,session,native_key)，payload_hash、artifact_hash、tool_call_id 索引） |
| `adapters/normalizer.py` | 新 `normalize_events()`：复合 content 的**每个 tool_use/tool_result 独立成事件、各自保留 tool_call_id**（旧逻辑把同消息多调用压成一个、id 被最后一个覆盖——配对失真根因）；`normalize_event` 保留为兼容包装；三条 parse 路径全部切换 |
| `knowledge_lifecycle/observation.py` | `ingest()`：原生事件幂等落账（同 key 同 hash 零增量、同 key 异 hash 记冲突不覆盖）、tool_call/result 按 tool_call_id 配对（未配对进 gap_flags 不猜）、task_runs 维护（identity_basis=native/local_segment，logical/origin 键来自 contracts，unknown 显式）、artifact_set_hash、completion 只记终态不推知识有效；`run_summary()` 只读读回 |

## 验证证据（`tests/test_v2_05_09_recall_observation.py` 6 项全过）

- stale 卡被演进检索召回（任务检索会过滤）、越 scope 0；错误码精确路 + 邻接路（relations 补入词法未命中对象）；候选全部版本绑定；快照未漂移 False / 新增卡后 True。
- 原生事件：4 事件首插 4、重放零增量同 run、同 key 改 payload 冲突计数且原 payload_hash 未被覆盖、tc-2 无 result 进 gap、native 任务键 → identity_basis=native + 跨机可比 logical key；无任务键 → local_segment + unknown 显式；同会话两任务不串（不同 run_id/logical key）。
- normalizer：复合双 tool_use 各自 id/name 保留；tool_result 复合块逐项保留；兼容包装不破坏单事件路径。
- 回归：`test_hooks.py` 脚本直跑 OK、`test_script_regressions` 6 项、capture/inflight normalizer 消费方 22 项全过。

**external_pending（V2-09 §10/11）**：Codex/Claude/Zcode/Trae/OpenCode 各一次真实原生任务的事件接线实测、≥3 宿主 tool/result 配对准确率 100% 样本集——需要真实宿主会话授权；当前实现以隔离 fixture（复合块/嵌套/未知事件）验证机制。
