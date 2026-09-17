"""薪火（xinhuo）：跨 Agent 共享的经验记忆库 MCP 服务。

不变量：
1. Markdown 是唯一真相，Git 是唯一审计，SQLite/INDEX.md 是可重建的派生态；
2. 所有改变仓库状态的操作必须持有 repo 级跨进程互斥锁；
3. Agent 提交 evidence，服务端决定 state（confidence/status 只能由状态机变更）。
"""

__version__ = "0.1.0"
