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
