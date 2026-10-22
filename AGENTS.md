# knowbase 项目约束

继承 `~/AGENTS.md` 的环境、知识库与工具规则。

## 运行与升级

- 用户明确要求：永远只保留一个 knowbase 运行版本。
- 所有宿主 MCP 和 Hook 固定使用 `~/.knowbase/runtime/venv/bin/python`，更新原地安装。
- 不创建 releases/<版本>、不维护并行运行包，不保留旧 wheel/构建产物。
- 安装前备份数据与配置；备份与运行包分离。源码 Git 历史正常保留。
- 更新后验证固定路径启动、MCP 工具与 Hook；旧连接需重连，不能只改文件就宣称会话已加载。
