# Hook 成卡 Path B 第一批实施报告

- 日期：2026-11-05（晚）
- 基线：`5d064ad`（远端 main，W1 接线第二批）
- 决策：用户 2026-11-05 19:45 拍板 B 先、A 后，B 第一批把 P0-1（Hook 时识别成卡）与 P0-2（Hook 时复用判断）一起做
- 任务书：`reports/knowbase-hook-path-a-b-decision-brief.md`「建议的第一批切分（B）」四步；背景与证据见 `reports/knowbase-hook-capture-audit.md`（含 §八 5d064ad 增量复核）
- 计划条目：`docs/plans/knowbase-lifecycle-v2-implementation.md` 新增 W1-9（已完成）
- 范围：只改 Knowbase 仓库内代码；本地-only，未 commit、未 push

## 做了什么

### 架构：复用提炼管线，不另起并行管线

检查点入队 → worker 短时处理 → 成卡。唯一新增的执行路径是 worker 的一个新任务
与一段提交前确定性规范化；领取、建批、分页、预算、模型调用、提交、结算全部复用
提炼的 `distillation._one`。

```
检查点（stop / before-compact / 6 工具事件 / 10 分钟）
  → capture_increment / capture_impl 入队 capture 任务，payload 置 hook_capture=True
  → worker hook_capture 任务（10 秒节律）取标记任务
  → distillation._one（Hook 提示词 + 提交前规范化）
  → materialize.submit（duplicate_evaluation 门）→ writer.create/update/link
  → 段结算 + 会话 settled 游标推进（与提炼同一机制，提炼不再重提同一段）
```

未带标记的历史任务与 legacy 路径任务不在 Hook 选择范围内，仍走 60 秒提炼；
提炼与 Hook 抢同一任务时 claim 原子 CAS 决定归属，先领者处理，后领者跳过，
失败/重试语义与提炼一致（transient 失败可由 `run_due` 的探针修复逻辑接回）。

### 改动文件

| 文件 | 改动 |
|---|---|
| `knowbase/automation/hook_capture.py`（新） | `HOOK_CAPTURE_INSTRUCTIONS`（Hook 提示词）；`normalize_hook_candidates`（提交前确定性规范化）；`pending_hook_jobs`（标记任务选择）；`run_hook_capture`（worker 入口） |
| `knowbase/automation/distillation.py` | `_one` 参数化：`instructions` / `key_prefix` / `input_marker` / `post_normalize` 四个关键字参数，默认即既有提炼行为；`request_id` 前缀由 `key_prefix` 派生（`auto:` / `hook:`） |
| `knowbase/automation/execution.py` | `execute_text` 接受 `instructions=None`，转发给 runner |
| `knowbase/automation/executor.py` | `CommandExecutorRunner.run_text` 接受 `instructions=None`，缺省用 `PROMPT_INSTRUCTIONS` |
| `knowbase/automation/text.py` | `IntranetTextRunner.run_text` 同上 |
| `knowbase/automation/worker.py` | `TASK_DEFS` 新增 `("hook_capture", 10, ...)`；`_h_hook_capture` handler；`_HANDLERS` 注册 |
| `knowbase/adapters/host_events.py` | `capture_increment` 入队 payload 置 `hook_capture=True` |
| `knowbase/server.py` | `capture_impl`（messages 路径）入队 payload 置 `hook_capture=True`（该路径直接 `jobs.enqueue`，不走 `capture_increment`） |
| `knowbase/extraction/materialize.py` | `_apply_candidate` 不再把 `None` 值的可选字段（tags/provenance/source_refs/code_refs/rule_context/relations）传入 writer（见下「附带修复」） |
| `tests/test_hook_capture_batch1.py`（新） | 9 项测试 |
| `docs/plans/knowbase-lifecycle-v2-implementation.md` | 新增 W1-9 条目与状态表行；总览 W1-1～W1-8 → W1-1～W1-9 |

### 关键设计决策（均有代码依据）

1. **为什么 Hook 路径能用 update/link_existing，而自动提炼执行器被禁。**
   禁止的原因是执行器跨进程无法可靠持有目标卡的 content/governance 双版本号
   （`executor.py:322-323`）。Hook 路径在同一进程内、提交前一刻现读版本号
   （`normalize_hook_candidates` 用 `store.load` + `_compute_revision` /
   `_compute_governance_revision` 回填），模型抄来的值只当不可信提示；
   `_apply_candidate` 在提交时还会再验一次 revision，不一致即 `REVISION_CONFLICT`。
   因此禁令的理由在 Hook 路径上不成立，update/link_existing 可用。
2. **verified 卡的 update 改走 review。** `writer.update` 会把 verified 卡改版降权回
   once（`writer.py:91-95` 注释）。自动路径不替 verified 卡做这个决定：目标卡
   confidence 为 verified 的 update 候选在规范化时改走 review（理由写入
   `duplicate_evaluation.rationale`，可审计）。`link_existing` 不改目标卡本身，
   可正常链接 verified 卡。
3. **标题查重改道（确定性防线）。** create 候选标题与现存卡相似度 ≥0.82（与写层
   `commit_adapter` 同一阈值）时：命中 active 非 reference 卡 → 改走
   `link_existing`（目标即命中卡，版本号现读）；命中 reference/非 active 卡 →
   改走 review。否则模型一次误判就会在写层撞 `DUPLICATE` 报错，任务打回重试循环。
4. **预算与提炼共用。** Hook 调用走同一 `_reserve` / `automation_budget`
   （日调用 20 次、日输入 512k 字符、单批 128k），超预算进 `retry_wait` 至次日零点
   （`_pause` 的 `daily_budget_exhausted` 语义），不静默丢弃；执行器响应缓存用
   `hook-capture-v1` 标记隔离，不与提炼缓存互串。
5. **无执行器时只 blocked，不探针。** `run_hook_capture` 无已验证执行器时返回
   blocked；探针与失败修复仍归提炼的 `run_due`，避免 Hook 任务重复探针烧预算。
6. **不新增配置门。** 复用既有两道门：`inflight.capture_enabled` 管采集、
   `automation.enabled` + 执行器探针管执行；默认配置下整条链路仍关闭，与现状一致。

### 附带修复：`_apply_candidate` 的 None 可选字段泄漏

调试 Hook 补充更新时发现：`_normalize_candidates` 会给缺失的 `relations` /
`code_refs` / `rule_context` 补 `None`，`_apply_candidate` 原样传入
`writer.update`，`adapter_update` 把 `meta["relations"]` 写成 `None`，随后
`_compute_revision` 在 `sorted(None)` 上抛 `TypeError`（被提交流程吞成 retryable，
任务卡在 `validating`）。这是提炼与 Hook 共用的缺口——模型不输出这些可选字段的
update 候选在提炼路径上同样会触发。修复：`_apply_candidate` 组装 `arguments`
时过滤 `None` 值（`None` = 未提供，不是清空）。`create` 路径共用同一组装逻辑，
同病同治。

## 测试

新测试 `tests/test_hook_capture_batch1.py`：**9 passed**（沙箱复跑）。

| 测试 | 覆盖 |
|---|---|
| `test_hook_capture_creates_card_without_distillation` | P0-1：检查点入队后 Hook 路径直接成卡，全程不调 `run_due`；卡 confidence 为 once |
| `test_hook_duplicate_links_existing_no_new_card` | P0-2 重复：link_existing 成功，不产生新卡；桩故意给错版本号，规范化现读修复后仍链接成功 |
| `test_hook_wrong_create_converted_by_title_guard` | P0-2 确定性防线：模型误判 create，标题相同，改走 link_existing，仍无新卡 |
| `test_hook_supplement_updates_existing_card` | P0-2 补充：update 增量更新旧卡，仍一张卡，confidence 保持 once |
| `test_hook_supplement_to_verified_card_goes_to_review` | verified 红线：补充 verified 卡改走 review，卡内容与 confidence 原样不动 |
| `test_hook_budget_exhausted_goes_retry_wait` | 超预算进 `retry_wait`（attempt 不扣、次日零点前可领）、不调模型 |
| `test_hook_no_executor_blocked` | 无已验证执行器时 blocked，任务保持 awaiting_agent |
| `test_hook_flag_selection_and_worker_wiring` | `capture_impl` 置标（`json_extract` 验得 1）；未带标记任务不被 Hook 处理；worker `TASK_DEFS` / `_HANDLERS` / `_h_hook_capture` 直调 |
| `test_hook_capture_leaves_quarantine_and_verified_untouched` | 成卡+链接后 `evidence_events` 0 行、`card_conflicts` 0 行、无 verified 卡、新卡无 `quality_state` |

模型调用一律用桩代替：桩从提示词的 `RELATED_KNOWLEDGE_DATA` 块里读相似卡快照，
与真实模型看到的输入一致；`duplicate_evaluation` 的快照 hash 与版本号由既有
`_normalize_candidates` 按建批快照回填（与提炼同一机制）。

## 全量回归

- **1184 passed / 5 failed / 2 skipped**（`TMPDIR=~/workspace/.tmp-pytest`，
  排除 `tests/test_script_regressions.py`——沙箱 `~/.knowbase/config.json`
  指向残留临时测试库，任何版本都失败，与代码无关）。
- 5 项失败全部在干净 `5d064ad` 基线 worktree 上逐项复现，确认为基线既有 /
  沙箱环境漂移，与第一批记录的集合一致（第二批交付记录称其在用户 Mac 本机
  单跑与全量均不复现）：
  - `test_inflight_refresh.py::test_context_async_refresh_consumes_peer_commit_and_invalidates_cache`
  - `test_inflight_stdio.py::test_registered_mcp_refresh_observes_peer_card`
  - `test_production_fixes.py::test_save_commits_index_and_leaves_clean_worktree`
  - `test_production_fixes.py::test_push_is_only_scheduled_after_repo_lock_is_released`
  - `test_concurrent_card_isolation.py::test_silent_auto_merge_is_detected_and_quarantined`
- **回归经过（已修复，写在明处）：** 首轮全量 6 failed，其中 1 项是我引入的——
  `test_p2_autonomous.py::test_provider_usage_is_recorded_once_and_not_a_bill`：
  `_one` 无条件给 `execute_text` 传 `instructions` 关键字参数，打破了既有桩的
  5 位置参数约定（`TypeError: unexpected keyword argument 'instructions'`）。
  修复为默认（提炼）路径保持原调用签名不变，只在 Hook 路径传参；重跑后该项
  通过，全量变为 1184 passed / 5 failed。**本批零回归。**

## 验收四条逐项

| 验收项 | 状态 |
|---|---|
| 会话结束时卡已在（不再等 worker 60 秒 + 提炼链路） | ✅ 沙箱验证：`run_hook_capture` 直接成卡，全程不调 `run_due`；worker 节律 10 秒，检查点后约 10 秒内处理。真实宿主会话的端到端时延待用户 Mac 实测（W3-3） |
| 重复内容不产生新卡 | ✅ 三层防线均有测试：模型判 link、标题查重改道、写层 DUPLICATE 兜底 |
| quarantine 与 verified 规则不动 | ✅ 无相关代码改动；测试验证无证据事件、无冲突行、无 verified 晋升、verified 目标改走 review |
| 全会话输入默认禁止 | ✅ 只送检查点增量片段（≤4000 字符/段，≤32 段/批）+ 相似卡摘要；提示词与代码均无全会话读取 |

## 已知边界与未决问题

1. **模型判断质量未验证。** 复用判断的第一层防线是模型（判重复/补充/全新）；
   本批只验证了“判断正确时执行正确”与“误判时的确定性兜底”，未验证真实模型
   的判断准确率——那是 W2-1（八关系 POC）的范畴，需真实脱敏样本盲测。
2. **Hook 更新 verified 卡之外的治理卡。** 非 verified 的受治理卡（standard /
   preference / bizrule）更新时走既有 `writer.update` 语义（修订即降权回 once，
   这是既有规则，不是本批新增）；staging 卡更新不受影响。
3. **检查点本身仍全量重读 transcript。** 本批未动 `parse_transcript_auto` 的
   全量重解析（审计 P3-11），只复用了检查点已有的增量游标与片段去重。
4. **与提炼抢任务时的提示词选择。** 带标记任务若被 60 秒提炼周期先领走，
   则按提炼提示词处理（create/review only），结果仍正确，只是复用出口变窄；
   10 秒节律使 Hook 先领走的概率占优，属可接受的竞态。
5. **真实宿主会话未实测。** 沙箱无真实宿主会话；Mac 侧安装 5d064ad + 本批后，
   需一次真实会话验证：stop 检查点 → 10 秒内卡出现、重复链接、预算行为（W3-3）。
6. **`_one` 的参数化与提炼共用。** Hook 与提炼现共用 `_one`（四处参数不同）；
   若后续两者分歧扩大，可再拆分为共享 `_run_batch`，本批不预先拆分。
