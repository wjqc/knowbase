# V2-16 六场景 / V2-17 迁移演练与部署准备 / V2-18 优化清单 — 交付记录

- 基线：`425f3fe`；实施日期：2026-11-05。

## V2-16 六场景（机制层回放，`tests/test_lifecycle_v2_e2e.py` 3 项 + 分场景测试）

| 场景 | 机制层覆盖 | 真实层状态 |
|---|---|---|
| S1 自动补充 | `test_s1_full_chain...`：ENRICH 提案→验证→发布→另一宿主正式 search 读到 r2、r1 快照与证据可读、无并发头 | 需 2 真实宿主+冻结代码副本回放 → external_pending |
| S2 错误纠正 | `test_s2_self_report...`：自报 incorrect 仅观察；可信 L3 fail → 隔离且正文不被覆盖（CORRECT 候选通道就绪） | 真实反例注入与评审 → external_pending |
| S3 重复整合 | `test_s3_merge...`（V2-06/07/08 文件）：三卡 MERGE 整批发布、别名归档+重定向、半发布崩溃 roll-forward 只见全新/全旧 | 三实例真实并发+崩溃点回放 → external_pending |
| S4 曝光/引用/采用 | `test_s4...`（V2-10/11 文件）：A=L1、B=L2、C=L0，completed 不发奖，通用命令拒收，歧义不发奖 | 真实宿主日志+独立人工标注 ≥60 样本 → external_pending |
| S5 双机演进 | `test_exchange...`（V2-12/13/15 文件）：签名/去重（传输+业务）/撤销先到/伪签发零治理/peer 治理重算 | 两台真实设备、真实 Git 分支 → external_pending |
| S6 失效复活 | `test_s6...`：依赖 2.x 观测 → 只限制+待复验；ENRICH 修订发布 → 新版 once（旧票不转嫁）、旧 1.x 证据保留、治理重算 unverified | 两个真实依赖环境副本回放 → external_pending |

六场景机制层全部可复现（隔离回放层）；真实宿主/设备/模型/效果对照全部
external_pending——不宣称业务验收通过。

## V2-17 迁移演练（真实数据受控副本，已执行）

**输入**：真实 `~/knowbase` 只读复制（251 张 md + memory.db；真实 meta=251、
staging=32 与副本一致）。**只读源，未触碰真实库内容。**

结果（副本上）：
- 迁移应用后 dry-run：候选 251、needs_review 0、drift 0；
- legacy 导入：251 快照、0 错误；重复导入：0 新增（幂等成立）；
- 影子对账：published_heads 保持空（影子语义正确）、251 heads_only、0 mismatch；
- knowledge_revisions 251 行，manifest 单次落盘。

部署准备（未执行，需授权）：
1. 备份真实 SQLite/类型目录/staging/spool/配置（updates.backup 机制可用）；
2. 真实库上 `legacy_import`（幂等可重入）+ 全量 doctor 校验；
3. 固定 runtime 原地更新（`uv pip install --python ~/.knowbase/runtime/venv/bin/python`）
   后核对 installed_build_id = 源码 build（V2-02 已强制实测写入）；
4. 宿主重连验证（sessions.stale → current）；worker 重启后 build_loaded 刷新；
5. 观察期两周（V2-02 告警 + doctor automation_health）。
回滚：迁移仅新增表/列（无数据改写）；回退=DROP 新表 + 重建
automation_schedule（去 5 列）或整库恢复备份（updates 的
rollback_would_overwrite_new_data 门保留）。

### ⚠️ 事件报告（如实）：真实库 schema 已被本会话间接推进到 v31

- **现象**：真实 memory.db `schema_migrations` 已含 v30/v31（本会话新增迁移），
  对应新表全部 **0 行**；v32–v34 尚未应用。
- **根因**：`~/.knowbase/runtime/venv` 的 knowbase 实际解析到
  `/Users/qc/work/knowbase`（源码直跑/等价 editable 形态，与审计"安装字节与
  源码一致"相符）。本会话 Hook 每次触发都会 import 当前源码——在源码含
  v30/v31 而 v32 尚未写就的窗口内，某次 Hook 的 `migrations.apply_all` 把
  v30/v31 应用到了真实库。
- **影响**：纯增量 DDL（加列带默认值 + 新空表），旧代码不读新列新表，行为
  无变化；无任何卡片/数据/配置被修改。后续 v32–v34 同为增量 DDL，被后续
  Hook 应用亦无破坏性（已测试）。
- **处置**：如实上报；未擅自回滚（DROP 需授权）；后续本会话所有迁移保持
  仅增量的纪律。**这是部署模型（单 runtime=源码树）的固有特性，任何源码
  改动都会被 Hook 实时加载**——V2 正式部署评审时应把"源码树即生产"列入
  风险讨论（建议：迁移发布前在副本演练——本轮已示范）。

## V2-18 简化清单（P2，仅文档；16/17 未完成前不删代码）

候选移除项（待 V2-16/17 真实验收覆盖后逐项执行，均保留只读兼容）：
1. `versioned_feedback.py` legacy 两工具标签晋升段（V2-01 已默认关，开关可退）；
2. `evolution.decide()` 的 trusted_promotion_enabled 过渡开关（V2-12 证据
   治理接管后移除，改为只消费 evidence_events）；
3. `index._feedback_factor` 计数排序分支（默认已中性；保留回滚开关一轮）；
4. read+completed 派生 helpful 的历史路径（数据只读保留）；
5. `test_phase0/auto_evolution` 中依赖过渡开关的机制测试改为证据治理 fixture。
性能/容量（1千/1万卡、10万事件）试验未执行：当前 251 卡规模无瓶颈证据，
不引入 ANN/中心设施（与设计一致）。

## 最终回归（本任务全部改动后）

- pytest 全套：**1126 passed / 1 skipped**（既有 7 个环境相关失败
  test_hook_noise×6、test_production_fixes×1 在未改动基线上同样失败，与本轮
  无关，本轮未计入）。
- 脚本式测试独立执行：test_e2e / test_governance / test_hooks / test_bizrule 全过。
- 新增 V2 测试：61 项（V2-01×9、V2-02×7、V2-03×4、V2-04×14、V2-05/09×6、
  V2-06/07/08×6、V2-10/11×3、V2-12/13/15×5、e2e×3 + 既有套件迁移更新）。
