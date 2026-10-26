# 2026-10-26 团队无人值守程序升级

- [x] 复核并保留已有rc9身份修复；程序源与知识源独立。
- [x] 实现独立更新器：轮询main、可信源/前进检查、隔离构建/数据影子验证、备份/空闲切换/恢复。
- [x] 稳定MCP连接代理及Hook入口；首次引导与定时安装。
- [x] 本地Git/双客户端/首次引导/失败/重启/忙碌反例；全量954 passed/1 skipped，末次定向34 passed。
- [ ] 提交/推送源码；本机旧直连MCP仍有3个进程，未强制结束；首次引导需旧连接退出。

旧版无更新器的机器需要一次性安装；不宣称其他成员已经升级或公司网络已验收。

---

# 2026-10-25 无内网依赖的能力补齐

目标：完成所有可独立实现/离线验证的缺口；不访问公司网关，不改VPN/认证，不冒充真实模型或Windows验收。

- [x] P2：执行器探针回执、受限文本执行、后台claim/分页/提交/读回、预算/续租/重试/恢复。
- [x] P3/P4：证据分包/身份与ACK校验、维护恢复、OS常驻调度、doctor真实状态。
- [x] 本地依赖：核对OCR能力，能安全修复则修复。
- [x] 隔离回归/反例：935 passed、1 skipped。
- [x] 功能提交f806d47已推送；在线备份后唯一runtime迁移29，安装态P2隔离链通过，生产提炼关闭。

基线00ff1c6（源码ad06a7f），生产构建ac0713e061bcaeaa，历史记录保留于下方。

---

# 当前任务：team-stats收口与本机升级（2026-10-24）

- [x] 复核现有代码/已部署版本/真实待发状态，保留既有身份。
- [ ] 完成Windows后台任务安装器与失败恢复，核对统计/检索组合契约。
- [ ] 定向、组合全套与实际打包版本隔离验证。
- [ ] 提交并push main；仅纳入相关源代码/安全文档/测试，不发布私有评测原文。
- [ ] 在线备份数据/配置，暂停旧后台，唯一runtime原地安装最新版本。
- [ ] 验证真实库一致性、统计远端读回ACK、后台任务及固定路径CLI/MCP/Hook。
- [ ] 核对旧MCP进程/实际会话生效边界，更新交付记录和项目知识。

范围：本机部署已获用户授权；其他成员设备与Windows原生实机验收不伪报完成。

以下为历史计划记录，以本节和本轮交付报告为当前状态。

# knowbase 检索与治理

## 全员统计设计（2026-10-22）
- [x] 确认运行条件：只有各成员电脑，无常驻服务器/CI。
- [x] 核对现有采集、导出、汇总缺口；选独立统计 Git 分支、每设备专属日摘要、各电脑本地汇总、指定单写 README。
- [x] 交付 docs/team-stats-git-plan.md 及明确示例的团队卡片预览。
- [ ] 后续实施：补齐全员采集/后台上报/团队汇总与 README 发布，并在三台电脑实际安装对账；本轮只交付方案。

### 2026-10-23 先检查再继续
- [x] 核对 rc6 当前实现与方案，确认自动上报仍未实现；工作区开始干净。
- [x] 用户确认本机 wenjian3，成员动态变化；方案改为动态有效名册与明确登记范围。
- [x] 修订 epoch 重叠、日窗口取数/口径隔离、命中率/Token 合计、双工作区发布隔离。
- [x] 补两份机器可校验的协议结构；修复预览新增/退出与范围切换后的覆盖标签。
- [ ] 后续产品实现和各成员设备接入仍未完成；协议结构测试不替代真实传输/去重验收。

## 2026-10-22 三宿主接入升级（当前任务）
- [x] 核对现有配置、固定包、Claude/Trae 官方 Hook 规范。
- [x] 补 Trae 独立适配与隔离测试；构建校验 rc5 固定包。
- [x] 备份原配置与 SQLite，增量更新三个宿主 MCP/Hook 及规则。
- [x] 三个实际 MCP 配置分别完成 stdio 初始化/16 工具核对；18 个 Hook 配置命令隔离通过；Trae 原生开关与规则已读回。旧会话进程保留，重连后加载。

## 2026-10-22 统计卡片与自动演进（当前任务）

- [x] 确认需求：浅色深绿统计卡片；减少明细；业务规则同样根据反馈自动晋升/淘汰。
- [x] 检查现有统计、反馈、待审激活链路和 Hermes 官方记忆/技能机制。
- [x] 交付可打开的统计卡片稿、指标口径与自动演进设计。
- [x] 实现只读统计快照、卡片页面、版本绑定自动演进和业务规则验证入口。
- [x] 运行必要回归、PC 浏览器检查，备份并更新用户指定 HTML。
- [x] 更新文档/项目知识；运行中的固定包未切换，真实宿主演进仍待升级后验证。
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


## 执行中知识使用实施（active goal，2026-10-22 更新）

后续代码开发路径：`/Users/qc/.codex/worktrees/inflight-contracts/knowbase`（codex/inflight-contracts）。生产固定包为32ad7f0/0.2.0rc1，主工作区产品代码暂冻结；C1–C4在隔离工作树实施。

目标完整保留 docs/knowbase-inflight-memory-implementation-plan.md 的 U1–U6、E0–E5；完工核对见 docs/inflight-completion-audit.md，不把预发布/局部绿色测试当全部完成。

- E0：ZCode真实注入/消费局部通过；永久32K输入+6个规则预标注包已交付，待人审；正常提炼、Claude/Trae未真实验收。
- E1：材料、分页、游标、旧Hook统一内核、恢复主链路有局部测试。
- E2：阶段检索/范围绑定/缓存/限频已实现；C1非阻塞刷新与C2采纳链尚待。
- E3：提炼候选到卡片、staging、恢复、任务覆盖主链路已实现；C3版本和任务反馈契约尚待。
- E4：ZCode原生页面显示6 Hook，服务已执行关闭/恢复刷新；准备固定发行环境，执行中检索/观察先试用，新增自动入队暂关。未修改审批权限或发送其他会话消息。
- E5：备份/迁移/双克隆有证据；内网Git已借助现有知识恢复并独立读回（cd332fe）；C4隐私最小化统计导出未实现，三人真实任务/净效果测量待验。

当前批次：收敛0.2.0rc1固定运行包并完成本机检索/观察试用，随后继续 C1→C2/C3→C4。源码后续开发不能直接改变已部署运行版本。新接口仍共用原库、原队列、宿主模型和既有写入服务。

本地最近全套401 passed/1 skipped；0.2.0rc1 wheel 已构建，43模块与提交32ad7f0逐字节核对；固定环境在源码工作目录下以-I启动的stdio smoke通过。异步协作问题尚无回复，不视为许可或验收通过；但C1–C4可独立实施，不能据此标整个目标blocked。

本机试用已配置：原库不变，ZCode/Claude/Trae 的MCP和Hook均指向 ~/.knowbase/runtime/releases/32ad7f0/venv/bin/python，以-I启动；enabled=true、capture_enabled=false。界面最终复核被Mac锁屏阻断，已请求用户解锁；不绕过登录/审批。后续开发转隔离工作树，继续C1–C4，不能把预览包当完整目标完成。

## 当前开发工作区（2026-10-22）

- 工作树：/Users/qc/.codex/worktrees/inflight-contracts/knowbase，分支codex/inflight-contracts。
- 开发解释器：本工作树/.venv/bin/python，editable导入已确认指向本工作树；生产MCP/Hook使用固定wheel解释器，不使用此环境。
- 测试须设置 KNOWBASE_CONFIG=/Users/qc/.knowbase/dev/inflight-contracts/config.json 及 KNOWBASE_REPO_PATH=/Users/qc/.knowbase/dev/inflight-contracts/memory。数据是私有本地快照，auto_pull/auto_push/auto_commit均关闭，不提交数据文件。
- 新工作树基线全套401 passed/1 skipped（49.29s）；代码图谱索引79文件/1546节点/2898边，后续查询projectPath必须传本工作树。
- 下一批C1：先通过codegraph核对context.execute、sync_before_read、跨进程同步租约调用链，补非阻塞刷新与last-known-good新鲜度；再C2/C3、C4。Mac锁屏不阻止这些后台代码工作，不能提前标整个目标blocked。

- C1已在隔离工作树实现并定向66项通过，整套411 passed/1 skipped；不代表固定运行包已升级或三人使用通过。下一步C2版本关联的采纳记录、C3任务结果反馈、C4只读统计导出。

- C2–C4已在隔离树落地：版本消费、任务结果反馈和只读metrics.v1。全套429 passed/1 skipped；新版仍未替换固定rc1。下一步独立复核/打包隔离验证，再按原门槛完成正常宿主及三人实际任务/净效果验收。

- rc2隔离安装/正式stdio2项/真实副本schema14→18已验证；最终全套430 passed/1 skipped。发行目录c88be4e为validation-only，真实业务库/客户端仍rc1；正常宿主、人工样本、三人真实试点与净效果对照尚缺，原目标保持未完成。


# 凭据脱敏修复（2026-10-22）

目标：修复 CLI 参数、嵌套 JSON 与 token 配置误伤；核查并清洗本地观察及材料，保持单一运行环境。

1. [complete] 跟踪写入/交付边界，以合成凭据复现缺陷。
2. [complete] 统一凭据规则、结构化递归与回归测试，澄清 candidate.duplicate_evaluation。
3. [complete] 提供有备份、完整性检查与安全报告的历史清洗工具；验证真实库。
4. [complete] 原地安装固定运行环境，验证 MCP/Hook，更新知识库及交接记录。
5. [complete] 2026-10-23 继续前独立检查：修复 Authorization/引号残留和占位符误报，回归后原地升级 rc7，核对历史库并收尾清单与知识文档。

约束：不输出真实凭据；不自行轮换外部服务凭据；不把源码修复当已加载的旧 MCP 连接。

错误记录：CodeGraph 默认根目录未找到项目；显式 projectPath 仍无索引，改用 rg 与源码追踪。

## 全员 Git 统计实施（2026-10-23，当前批次）

1. [complete] 动态名册、外部设备身份、日摘要校验、真实时间窗采集、宿主遥测迁移21。
2. [complete] 独立 bare 数据工作区、持久待发/远端读回ACK、有限抢推重试、离线保留与恢复隔离。
3. [complete] 团队同核聚合、动态覆盖、共享库存单版本计数、卡片成员摘要与README标记区域保护。
4. [complete] 完整638 passed/1 skipped，安装态MCP2项与正式team CLI临时Git贯通，浏览器通过；固定运行环境原地安装、本机登记/后台采集已接通，真实远端回执待用户恢复网络后验证。
5. [complete] 接入手册/知识文档已交付；其他成员首次安装/身份确认及三机实网验收明确为external_pending。

只用已确认本机wenjian3做真实接入；原有两位同事可预登记pending，不能冒充已接入。团队无人数上限常量；其他操作系统支持状态按实际实现说明。

## Hook降噪（2026-10-23）
1. [complete] 从真实会话的两次Stop强制续跑补反例：新回归首轮9项失败，确认覆盖旧行为。
2. [complete] Stop及legacy路径静默入队，移除前台提炼催办；自动采集的确定性噪音过滤、提示预算/去重、短启动规则。
3. [complete] 完整655 passed/1 skipped；固定安装态新边界17项+正式stdio2项，共19项；实际配置18Hook命令及新待办后的重复Stop均通过。
4. [complete] 数据配置备份后原地更新唯一runtime，72模块一致、原库正常；文档/知识库交付。现有会话的新初始化说明需重连或新会话，真实模型重放未执行。
