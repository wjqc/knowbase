# ADR-0002：ACL pre-filter 在 HybridSearch 构造期生效，channel 物理不可见无权 doc

- 状态：已归档（实现于 v2 Phase 3；v2 运行时已随 75bc74d 移除，检索通道思路部分沿用 `knowbase/index.py` 原地混合检索，本决策供未来访问控制重建时参考）
- 日期：2026-09-18
- 来源：原记忆 D-2026-0004（human:文剑），2026-09-20 迁入 ADR 后归档

## 背景

V2 计划 §7.2 要求"ACL 必须在召回前硬过滤，channel 拿不到无权 doc"。若只在 `search()`
阶段过滤：慢，且 channel 内部索引仍然看得到无权 doc——任何一个 channel 忘了过滤就是泄露。

## 决策

`HybridSearch.__init__`（`v2/retrieval/hybrid_search.py`）立即按 ACL 过滤：

1. 接收 `principal` 或 `acl_predicate`（后者优先）两个等价入口。
2. 构造期执行 `allowed = [d for d in docs if predicate(d)]`，记录 `denied_ids`。
3. **默认 channels 用 allowed docs 构造**——无权 doc 物理上不存在于任何 channel 索引。
4. 自定义 channels 由调用方自行用 allowed docs 构造（fail-closed 责任转移并文档化）。
5. `principal=None` 完全跳过 ACL，行为向后兼容（V1/无 ACL 场景零回归）。

`HybridResult` 暴露 `principal_id / acl_enabled / acl_denied / acl_denied_ids`，
`acl_denied_ids` 供零泄露率审计。

## 取舍

- 不在 `search()` 过滤（慢 + 通道内部仍可见）。
- 不让 channel 各自接收全量 docs + principal（逻辑分散，易漏一个 channel）。
- 集中在构造期一次过滤 + 默认通道自动继承，是"集中、零回归"的平衡点。

## 验证

Phase 3：HybridSearch 主路 ACL 单测 20/20，3 入口越权 E2E 零泄露率，
`principal=None` 场景旧测试 118/118 零改动通过。
