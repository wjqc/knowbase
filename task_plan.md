# knowbase 检索与治理
1. 检查检索、导入、日志实现（完成）
2. 默认检索隔离、自动注入收紧、逐次命中记录、事务重建保留统计（完成）
3. init 显式导入、来源幂等、staging 编号修复、HTML 治理快照（完成）
4. 回归测试、README 与独立知识库增补同步（完成）

限制：codegraph 未初始化，使用源码；浏览器 URL 策略禁止 file 页面，视觉验收未完成。没有重启用户 MCP 服务，没有批量导入真实冷知识库。

## 单库原地升级与 V2 死代码清理（2026-09-18）

5. 重写升级方案，移除第二套数据库/迁移/双运行时假设（完成）
6. 将 doctor/alerts 改为检查现有 memory.db、Git 和检索状态（完成）
7. 将多格式解析器迁入正式 knowbase/parsers（完成）
8. 合并可复用检索算法，删除第二套 v2 运行时和无效测试（完成）
9. 完成导入、检索、MCP、Hook、治理、打包和死代码扫描（完成）
10. 补齐同库 embedding/source/sync 状态、TTL 拉取、scope_map 与 doctor 深检（完成）

## V2 多项目、多人员、多格式升级方案（2026-09-18）

5. 复核现有检索、导入、权限、Git 同步与测试边界（完成）
6. 设计 V2 目标架构、数据模型、混合检索和同步协议（完成）
7. 补齐接口兼容、迁移、部署、测试、灰度和回滚方案（完成）
8. 形成 `docs/knowbase-v2-upgrade-plan.md` 独立交付文档并静态校验（完成）

## 生产缺陷修复（2026-09-20）

11. 权限：封死 source=human 伪造；正式规则更新必须走可信人工 CLI（完成）
12. 写事务：INDEX/SQLite/Markdown 一致后再 commit；push 移出 RepoLock（完成）
13. 同步：fetch/merge/reindex 加 RepoLock；动态 remote/default branch；校验真实 URL（完成）
14. 检索：Hook 与 MCP 走同一路径；项目上下文强制 scope（完成）
15. 测试：修复标准 pytest/CI 收集入口（完成）
16. 全量回归、真实库静态检查、文档更新（完成）

## 经验卡引用边界（2026-09-21）

17. 盘点生成、更新、导入和人工修订入口的路径处理（完成）
18. 定义并实现“文档仅仓内相对引用、代码仅项目相对引用”的强校验（完成）
19. 移除共享卡片中的外部 import_path，保留本机幂等状态（完成）
20. 扫描并迁移真实经验库存量，补齐测试与 README（完成：18 个 import_path 已移除，明确的 ~/work 代码指针已改；另有 16 张旧卡需逐条提炼，未机械删除内容）
21. 运行针对性与全量回归，记录未验证边界（完成：pytest 102/102，脚本回归 e2e 35/35、import 12/12、hooks 15/15、governance/retrieval 通过）

## Errors Encountered

| Error | Attempt | Resolution |
|---|---:|---|
| `tests/test_import.py` 导入 0 条，原预期 3 条 | 1 | import 只执行引用边界校验，不套用新建卡片的完整必填小节 lint，保留“原文导入后提炼”语义 |

## 结构化 code_refs 与 bizrule-mining（2026-09-21）

32. 读取项目指令、knowbase 知识与当前实现（完成）
33. 先补 `tests/test_code_refs.py` 失败用例，覆盖 lint/save/search/read/update/旧卡兼容（完成）
34. 实现 store/server/index/hooks 的 structured `code_refs`（完成）
35. 补 README 与 ADR-0002，定义并全局保存 `bizrule-mining` 工作流（完成）
36. 运行定向、全量与脚本回归（完成：pytest 126/126，六组脚本回归通过）
37. 用 knowbase 自身执行 demo 挖掘，将真实 bizrule 卡落入 staging 供审核（完成：入口 save_impl，3 条候选全部落库 B-2026-0002/0003/0004，含结构化 code_refs 与 provenance 占位，反查验证命中；连同此前手动落的 B-2026-0001 共 4 张待人审）
38. 同步 `/Users/qc/knowledge` 知识文档，分别提交 knowledge 与 knowbase，不 push（完成：增补文档 `通用/05-智能助手/knowbase-业务规则代码定位code_refs-2026-09-21.md`；两仓本地提交，均未 push）

## 通用可复用知识入库方案（2026-09-21）

22. 复核当前类型、导入、检索与 source_state 边界（完成）
23. 设计 Source Artifact 与 Reusable Knowledge Card 双层模型（完成）
24. 给出生成、审核、检索、迁移、测试、灰度与回滚方案（完成）

## source/card 分层 P0 止血（2026-09-21）

25. 新建 sources.py：objects 内容寻址快照 + SRC manifest + sha 去重（完成）
26. store.py：card-v2 schema，八项小节 lint + 空洞表述拦截，存量卡沿用旧规则（完成）
27. index.py/server.py：reference 退出默认检索与速览，save/search 拒绝，read 带提示（完成）
28. __main__.py：import 只产 source（--type/--staging 废弃兼容），init 联动（完成）
29. hooks/session 提示与 MCP instructions 补八项结构引导（完成）
30. 新增 test_source_layer.py 15 项；重写 import/governance/bizrule/e2e/hooks/reference_boundaries/production_fixes 测试（完成：pytest 117/117）
31. ADR-0002、README 命令表与行为细则、真实库 reindex 验证（完成）

## Errors Encountered（P0）

| Error | Attempt | Resolution |
|---|---:|---|
| `index.search('%')` 意外命中新八项测试卡 | 1 | 非检索缺陷：卡正文含"10%"字面百分号被 instr 命中；改测试数据为"灰度小流量" |
| FTS5 虚拟表上 instr(lower(col),'%') 行为与普通表不一致的疑点 | 1 | 复现后确认根因是正文真含 '%' 字符，instr 字面匹配本身正确；无需改检索代码 |
| import 断言"staging 无 B 卡"失败 | 1 | staging 本就有前序步骤保存的提案卡；断言改为 import 前后 staging 数量不变 |

## 多人 Git 同步假冲突修复（2026-09-22）

39. 复核 auto_pull/auto_push/push/sync_before_read 与真实仓库状态（完成）
40. 先补多人并发回归：无内容冲突自动 rebase、真冲突安全 abort、push 抢先有限重试（完成）
41. 实现同步收敛并更新 README/同步设计说明（完成）
42. 运行定向、全量与真实临时远端并发验证（完成：定向 15/15、全量 130/130、compileall 与 diff-check 通过）
43. 同步独立知识文档并提交必要变更（完成：knowledge commit 92187b1；保留其他既有未跟踪内容）


## 执行中知识使用实施（2026-10-21，active goal）

目标：完整执行 docs/knowbase-inflight-memory-implementation-plan.md，保留 U1–U6 的真实验收，不以局部测试代替宿主/团队验收。
- E0 宿主能力探针、冻结基线与独立样本：in_progress。codegraph 未初始化，转直接源码；pytest 基线运行中。
- E1 原文保真、分页、片段覆盖与迁移：pending。
- E2 memory_context 执行中任务检索：pending。
- E3 技术经验/业务规则候选到真实卡片、恢复：pending。
- E4 正式 Hook/主动工作流接线：pending。
- E5 全量回归、升级备份、知识文档、团队试点：pending。
约束：不用独立模型/第二套 DB/队列；现有 memory.db 不删；不改旧迁移；staging 不自动生效；只有真实运行证据才能标 event_driven。

### 当前批次状态（2026-10-21 实施持续中）
- E0：宿主文档及 CLI 可执行入口已确认（ZCode 0.17.0）；真实下一步消费探针仍 external_pending。
- E1：capture 路径已实现保真/分页/租约/manifest/覆盖内核，旧 import 路径和自动 Hook 路径仍需切换；不标整批完成。
- E2：memory_context 已实现并有独立测试；新主题过预算继续调度、宿主消费/真实事件待完成。
- E3：共享 writer、候选到卡、规则 staging、提交/应用恢复已实现；需补并发/版本复查/正式 stdio 综合验收。
- E4/E5：尚未开始正式配置切换、团队试点；不得宣布目标完成。
下一步：先修复完整回归保留的显示契约，再审查 E3 应用恢复与 E1 import，接入 E4。

### 本地交付审计（2026-10-21）
- E0 PARTIAL：ZCode 0.17.0 真实工具事件→候选→后续回复消费通过（预置任务主题夹具）；headless build 的 MCP 被 No permission client configured 拒绝，未更改/绕过许可策略。
- E1 本地 PASS：完整材料、续页、持久观察、范围游标、压缩后已观察材料恢复、迁移/校验和与旧统计保留。
- E2 本地 PASS + 宿主递送局部 PASS：14 个正式 MCP 工具注册，memory_context 分阶段/错误查询、预算续做、缓存失效、scope 绑定；任意真实长方案规划未验收。
- E3 本地/正式 stdio PASS：候选→共享 writer→卡片/索引读回；业务规则 staging、无出处 review；租约/fencing/重放与重复复查；宿主实际自主提炼未验收。
- E4 PARTIAL：ZCode 用户级三工具事件、Claude 工具事件/PreCompact 均已备份注册；Trae 规则和 MCP instructions 已更新；真实 inflight.enabled 仍 false，等待重连及许可验收。
- E5 PARTIAL：387 passed, 1 skipped；stdio、双 Git 克隆、备份、真实 schema14、INDEX 派生视图退役验证；191 卡/668 usage/24 feedback 保留。内网 push 返回 Empty reply from server，未推送成功；三人真实任务/净 token/用时测量尚缺。
- 目标保持 active：已发异步问题请求普通 ZCode 会话配合重连验收，未获回复不视为授权/验收完成。无 live exec 遗留（最终 tests/probe 均已结束）。
