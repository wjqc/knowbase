# knowbase 生命周期 V2 实施报告（第一批）

- 日期：2026-11-05
- 基线：远端 main `7e6f8cc`（0.2.0rc9），本地克隆 `~/workspace/knowbase-fix/knowbase-v2`
- 范围：任务书 `docs/plans/knowbase-lifecycle-v2-implementation.md` 的 **W0 收口（W0-1～W0-4）+ W1-6 + W1-1 + W1-8**
- 状态：**仅本地改动，未提交、未推送**。部署到用户 Mac 运行时是用户侧步骤（按仓库 AGENTS.md：备份数据与配置 → 原地安装到固定路径 → 验证固定路径启动、MCP 工具与 Hook → 旧连接重连）。

## 实施结果总览

| 任务 | 内容 | 测试 |
|---|---|---|
| W0-1 | retry_plan 文档与代码不一致修复 | 7 passed（既有测试扩展，新增 4 个种子场景） |
| W0-2 | evidence.py 与 logical_task_key 不一致修复 | 6 passed（新增 1 项治理独立性测试） |
| W0-3 | 工具数等过期表述修正 | 0 新增（文档修正，随套件回归） |
| W0-4 | 核查 redirect_ids 是否被检索跟随 | 结论：不跟随，确认为缺口 → 转 W1-8 |
| W1-6 | lifecycle propose CLI + staging 卡自动提案 | 5 passed（新增） |
| W1-1 | observation.ingest 接线（宿主原生事件落库） | 4 passed（新增） |
| W1-8 | redirect_ids 检索跟随 | 3 passed（新增） |
| 回归 | 全量套件（除 test_script_regressions） | 见 §7 |

## W0-1：retry_plan（worker.py）

**问题**：`retry_plan` 的文档描述与代码行为不一致，且错误分类口径不完整。

**改动**（`knowbase/automation/worker.py`）：
- 新增 `CONFIG_BLOCKED_ERRORS = frozenset({"material_manifest_required"})`：目前已知的唯一被配置阻塞的写入器是 `distillation.py:135`；`blocked_config` 仅当 error 属于该集合时才成立，否则归入 `unknown_state`。
- `lease_stuck` 要求 state 为 `claimed` **且** 租约在未来；`requeue_ready` 要求 state 属于重试态、租约已释放、**且** job_spool 行存在（新增 spool JOIN 检查）。
- 重写 docstring，写明全部 6 类（含 failed_permanent 可经 `jobs.retry` / `run_due` 的 TRANSIENT_ERRORS 自动恢复）。

**测试发现**：原测试 INSERT 漏了 `error` 列（error 从未入库——旧代码能通过正是因为它从不查 error）。测试扩展 4 个新种子（J-nospool / J-blocked-other / J-stuck-retrywait / J-claimed-expired → unknown_state）+ spool 行插入。文件：`tests/test_v2_02_runtime_health.py`。

## W0-2：evidence / governance 一致性

**决策：以文档修正为事实，不改行为。** 核查确认代码行为本身是对的，错的是文档与一处边界口径。

**改动**：
- `knowbase/knowledge_lifecycle/evidence.py`：`import_package` docstring 改为真实语义——未验证事件**不入库，仅在返回统计中计数并记 event_id 与原因**；代码在 `stats["details"]` 中追加 `{"event_id", "result": "external_unverified", "reason"}`（仅返回值，不写库）。
- `knowbase/knowledge_lifecycle/trust.py`：模块 docstring 纠正。
- `docs/implementation/lifecycle-v2/V2-12_V2-15/delivery.md`：措辞纠正。
- `knowbase/knowledge_lifecycle/governance.py` 的 `decide()`：新增 `_counts_toward_independence()`——`logical_task_key` 或 independence_group 以 `unknown:` 开头的**正向**事件不计入 pass_tasks / pass_groups；**负向事件仍触发隔离**（信任来自验证器与签名，不来自任务身份）。模块 docstring 同步更新。

**测试**：新增 `test_governance_unknown_task_keys_do_not_count`（2 真实 + 2 unknown → validated/tasks==2；第 3 个真实 → verified/tasks==3）；exchange 测试扩展（新伪造包 → unverified、details 有记录、对端库无行）。文件：`tests/test_v2_12_13_15_governance_exchange.py`。

## W0-3：过期表述修正

- `knowbase/server.py:1-5` docstring「6 个工具」→ 分组描述 +「2026-11-05 计 17 个」（已核实：`server.py:910-927` 共 17 处 `mcp.tool` 注册）。
- `docs/implementation/lifecycle-v2/V2-12_V2-15/delivery.md`：`recompute_all` 表述 →「可调用的重算函数（worker/导入后重算入口，待 W1-4 接线；当前无任何调用方）」。
- `docs/share/integration-guide.md:314` 与 `docs/share/project-introduction.md:141`：16 → 17 个工具。
- `integration-guide.md:623` 是 2026-10-22 的 dated 验证记录，**刻意不改写**。

## W0-4：redirect_ids 跟随核查（结论：不跟随）

**核查方法**：全仓库检索 redirect_ids 的写与读。

- 写：仅 `publication.py` 的 `_materialize`（published_heads.redirect_ids_json + 旧卡 frontmatter）。
- 读：仅 `contracts.py:270`（manifest 校验）。
- `index.py`、`extraction/recall.py`、`knowledge_lifecycle/recall.py`、`consumption.py`、`delivery.py`、`server.py` **零读取**。
- `evolution_recall` 的邻接只跟 frontmatter `relations[].id`，不跟 redirect_ids；`_materialize` 不为重定向补 relations 条目。

**结论**：MERGE/SPLIT 后旧卡指向新卡的重定向在任何检索路径上都不生效，旧内容只能靠正文命中。缺口确认，按任务书转 W1-8。

## W1-6：lifecycle propose CLI + staging 卡自动提案

**新增**（`knowbase/knowledge_lifecycle/changes.py`）：
- `propose_new_card(conn, repo, memory_id, *, proposer="")`：读卡 → `_compute_revision` → `admission.classify_risk(type, "NEW")` 风险 → base_heads=[]、claim_map=[]、source_dispositions=[]；确定性 change_id `CHG-NEW-<memory_id>-<revision[:12]>`——同内容重提幂等，内容变则新 ID。
- `propose_admitted(conn, repo, *, proposer="", publish=False, limit=50)`：扫描 `admission.pending_admissions`，仅 staging——stale 的跳过并记原因 `stale_needs_revalidation_path`；复用最新通过的 admission 报告的 authority_ref；propose → `evaluate_admission(change_id)` → 若 publish：`validate_change` → `publication.publish`；返回 {proposed, already, skipped, published, failed}。

**CLI**（`knowbase/__main__.py`）：`lifecycle propose <memory_id> [--proposer]`、`lifecycle propose-admitted [--publish] [--proposer] [--limit]`。

**两个安全修复**（`knowbase/knowledge_lifecycle/publication.py`，提案→发布窗口的竞态）：
1. `publish()` 的 a2 步：仅对 relation==NEW，重验 staging 源文件的快照 hash 是否仍与冻结输出一致，否则置 needs_rebase 并返回 `{"error": "SOURCE_MOVED"}`。NEW 没有 base heads，没有这一步时，提案与发布之间编辑 staging 卡会静默丢失新编辑。
2. `_materialize()`：向正式目录写出 active 输出后，删除同 ID 的 staging 文件。否则 `iter_all` 会把卡产出两次、pending_admissions 永久列出它（与 versioned_feedback._finish 的 move 语义一致）。

**权威门仍成立**：standard / global-preference 若无 authority_ref → admission 失败，change 停在 `proposed`，绝不静默发布。

**测试**：`tests/test_w1_06_propose_admitted.py`，5 passed。

## W1-1：observation.ingest 接线

**新增模块** `knowbase/knowledge_lifecycle/observe.py`：
- `project_key_for_repo()`：取 config 的 git remote URL → `contracts.project_key_from_repo`，否则 `repo:unknown`。
- `observe_hook_tool_event()`：tool-before → tool_call，tool-after/tool-failure → tool_result；其他事件跳过；native_key 为 `hook:<event>:<call_id>`，无 call_id 时用摘要回退。
- `observe_mcp_call()`：kind=tool_call，单边事件；native_key 为 `mcp:<tool>:<event_id>` 或 `mcp:<tool>:digest:<hash>`；无 session 时跳过，**绝不伪造会话身份**。

**设计决策（任务 W1-1(c)）：不新增 `observation.batch` MCP 工具。** 零额外努力原则——在实现内部自动记录，宿主不需要多调一个工具。

**接线点**：
- `knowbase/adapters/host_events.py` 的 `handle()`：插在 scope 解析连接关闭之后、scope 未解析分支之前（scope 未解析的工具事件也记录）；best-effort try/except，绝不破坏 hook；self_calls（memory_*）此前已返回，不与 MCP 路径重复记录。
- `knowbase/server.py`：helper `_observe_mcp_call(rp, session_id, tool_name, event_id="", payload=None, native_task_key="")`（新建连接，吞异常）；在 `read_impl`（bump_hit 之后）、`context_impl`（context.execute 之后；payload 为 stage/scope/task_id/topics 的 topic_id+queries 截断；native_task_key=task_id）、`use_impl`（consumption.record 之后）、`search_impl`（新增可选参数 `session_id: str = ""`，向后兼容；payload 为 query[:512]+scope+命中 id 列表；memory_search 工具描述已更新）中调用。
- `feedback_impl` **刻意不接线**（无 session_id 参数；feedback_events 账本本身已带任务身份）。

**验证**：hook tool-before+tool-after 对 → 2 条 native_events 行，配对（gap_flags==[]），重放幂等；scope 未解析的工具事件仍记录；无 session 的 MCP search 不记录；同一会话的 hook 与 MCP 事件落入**同一个 task_run**（两者 native_task_key 均为空 → 同一 run_id）。

**测试**：`tests/test_w1_01_observation_wiring.py`，4 passed。

## W1-8：redirect_ids 检索跟随

**新增**（`knowbase/index.py`）：
- `redirect_targets(conn, memory_id)`：优先读 published_heads.redirect_ids_json，frontmatter 回退（经 store.parse）。
- `follow_redirects(conn, candidate_ids, *, scope=None, include_archived=False, max_depth=3)`：BFS 跟链；`processed`（已展开源）与 `emitted`（已输出目标）分离，另加路径（path）防环——目标仅因同为原始候选而已被展开时**不跳过**（这正是旧卡与新卡同时命中时需要标注的情形）。
- `_redirect_extra_results(conn, results, lex_all, *, scope=None, mtype=None, max_extra=10)`：源 = 结果 id + 预过滤词法集中的 archived id（上限 20）；目标须为 active、非 staging、mtype 匹配；已在结果中的目标补 "redirect" 通道 + redirected_from 而不重复；新增项得分 = 源得分 × 0.9，snippet 经 mem_fts。

**接线**：
- `index.search()`：预过滤前捕获 `lex_all = dict(lex)`；`if not fused: return []` 改为返回重定向 extras（MERGE 情形：旧内容只在 archived 卡里、无直接命中）；hook automatic_match 过滤与截断之后，extras 追加在截断结果之后（不挤占直接命中）；diagnostics 增加 `redirect_followed` 计数与 "redirect" 通道。
- `knowledge_lifecycle/recall.py` 的 `evolution_recall`：relations 邻接循环后，对前 10 候选跟重定向 → 新候选得分 0.25、通道 "redirect"、`redirected_from` 字段；snapshot 增加 `redirect_added` 计数。`wide_recall` **刻意不接线**（去重语义，见缺口文档）。

**测试中发现并修复的两个实现 bug**：
1. BFS 发现时即把目标加入 seen，导致深度 2 展开被跳过（改为 processed/emitted 分离）。
2. 路径防环初版用 `seen` 统管：旧卡 d 与新卡 e/f 同时命中时，e/f 先作为源被处理进 seen，d 的重定向目标 e/f 被误跳过，`redirect_added` 为 0。改为 path 携带链路祖先判断，根候选不再被误判为环。

**测试**：`tests/test_w1_08_redirect_follow.py`，3 passed。
- 链/环/scope + frontmatter 回退；
- MERGE 三卡：b/c archived 且 redirect_ids=[a]，搜 b 的专属词（不在合并正文中）→ 经 redirect 通道返回 a，redirected_from==b，旧卡不复活；
- SPLIT：d（redirect 处置）→ e/f，evolution_recall 含 e/f 且通道含 redirect、redirect_added ≥ 2。

**附带回归修复**：`tests/test_local_retrieval_upgrade.py` 钉死的 memory_search schema 因 W1-1 的可选 `session_id` 参数多了一项——这是与 read/context/use 同款的既定模式，测试期望已同步更新（该测试在干净基线 worktree 上通过、在改动树上失败，确认为本次改动引入的 schema 变化，非偶发）。

## 全量回归

- 基线（7e6f8cc，V2 交付时）：1126 passed / 1 skipped；另有 7 项已知基线失败（test_hook_noise ×6、test_production_fixes ×1，疑似测试隔离泄漏——失败集合随运行环境/顺序漂移，见下）。
- 本批最终全量（第三轮，修复后）：**1159 passed / 2 skipped / 5 failed**，耗时 9:51（`pytest tests/ -q --ignore=tests/test_script_regressions.py`）。
- 本次新增测试：W1-6 5 项、W1-1 4 项、W1-8 3 项、W0-2 1 项，W0-1 扩展既有测试（+4 种子场景）。
- 5 项失败**全部在干净基线 worktree（HEAD 7e6f8cc，未含本批任何改动）上单跑复现**，确认为基线既有失败，非本批引入：
  - `test_concurrent_card_isolation.py::test_silent_auto_merge_is_detected_and_quarantined`
  - `test_inflight_refresh.py::test_context_async_refresh_consumes_peer_commit_and_invalidates_cache`
  - `test_inflight_stdio.py::test_registered_mcp_refresh_observes_peer_card`
  - `test_production_fixes.py::test_save_commits_index_and_leaves_clean_worktree`
  - `test_production_fixes.py::test_push_is_only_scheduled_after_repo_lock_is_released`
- 回归过程插曲：
  1. 首次全量跑中途因 `/tmp`（512M tmpfs）被 pytest 临时目录占满而崩塌（71 failed / 166 errors，均为 `OSError: [Errno 28] No space left on device`，非代码问题）；清理后改用 `TMPDIR=~/workspace/.tmp-pytest` 重跑。
  2. 第二轮全量 6 failed：其中 1 项（`test_search_snippets_batch_vs_individual`）为本批真回归——`redirect_targets` 无条件查询 `published_heads`，而该测试的最小库无此表（迁移 v30 才建）；修复为缺表时走 frontmatter 兜底（`index.py` 的 try/except OperationalError），单跑验证通过。另 1 项（concurrent_card_isolation）在基线 worktree 上同样失败，定性为基线既有。
  3. 第三轮全量（修复后）5 failed，全部为上述基线既有失败。**本批零回归。**
- 未跑项：`tests/test_script_regressions.py` 全程排除——沙箱 `~/.knowbase/config.json` 的 repo_path 指向残留临时测试库（无 cards 表），该文件在任何版本上都会失败，与检索代码无关（见 AGENTS.md）。

## 改动清单

16 个修改文件 + 1 个新模块 + 3 个新测试文件（另有上一轮的三份 V2 文档仍为未跟踪状态，不属本批）：

| 文件 | 改动 |
|---|---|
| knowbase/automation/worker.py | W0-1：retry_plan 分类口径 + docstring |
| knowbase/knowledge_lifecycle/evidence.py | W0-2：import_package docstring 纠正 + details 记录 |
| knowbase/knowledge_lifecycle/governance.py | W0-2：unknown: 任务键不计入独立性（正向） |
| knowbase/knowledge_lifecycle/trust.py | W0-2：模块 docstring 纠正 |
| knowbase/server.py | W0-3：工具数 docstring；W1-1：MCP 观测接线 + search_impl session_id |
| docs/implementation/lifecycle-v2/V2-12_V2-15/delivery.md | W0-2/W0-3 措辞纠正 |
| docs/share/integration-guide.md、project-introduction.md | W0-3：16 → 17 工具 |
| knowbase/knowledge_lifecycle/changes.py | W1-6：propose_new_card / propose_admitted |
| knowbase/__main__.py | W1-6：lifecycle propose / propose-admitted CLI |
| knowbase/knowledge_lifecycle/publication.py | W1-6：SOURCE_MOVED 重验 + staging 清理 |
| knowbase/knowledge_lifecycle/observe.py | W1-1：新模块 |
| knowbase/adapters/host_events.py | W1-1：hook 工具事件观测 |
| knowbase/index.py | W1-8：redirect_targets / follow_redirects / search 接线 |
| knowbase/knowledge_lifecycle/recall.py | W1-8：evolution_recall 重定向 |
| tests/test_v2_02_runtime_health.py | W0-1 测试扩展 |
| tests/test_v2_12_13_15_governance_exchange.py | W0-2 测试扩展 |
| tests/test_w1_06_propose_admitted.py | W1-6 新测试 |
| tests/test_w1_01_observation_wiring.py | W1-1 新测试 |
| tests/test_w1_08_redirect_follow.py | W1-8 新测试 |
| tests/test_local_retrieval_upgrade.py | W1-1 附带：schema 期望同步 session_id |

## 未做事项（留待后续批次）

- W0-5（真实库 72/4/8 积压只读复核）、W2-1（内网模型八关系 POC）、W4-5（V2-17 生产迁移）：**需用户授权，未执行**。
- 其余 W1 任务（W1-2 correlate、W1-3 verify_execution、W1-4 治理重算触发、W1-5 证据包 worker/CLI、W1-7 admission→publish 调度）与 W2–W4：见能力缺口文档。
- 本批所有改动**未提交、未推送**；是否合入 main 由用户决定。合入后仍需按仓库 AGENTS.md 在 Mac 运行时原地安装并验证。
