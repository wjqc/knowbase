# knowbase（薪火）

跨 Agent 共享的经验记忆库 MCP 服务：踩坑 / 决策 / 流程 / 偏好 / 业务规则 / 项目参考文档沉淀为一个本地 git 知识库，
供本机所有 AI Agent 检索复用。**一个人的坑，所有 Agent 的经验。**

- 存储：markdown + frontmatter（唯一真相）+ git 审计 + SQLite FTS5 派生索引（可重建）
- 检索：在同一 `memory.db` 内执行词法 + dense 模糊双路召回、RRF 融合及原有 scope/可信度/新鲜度/反馈排序；不创建第二套数据库
- 治理：人员标准/偏好 Agent 只能提案（staging 人审）；confidence/status 由服务端状态机管理
- 完整设计：知识库 `通用/05-智能助手/Agent经验记忆库MCP服务-设计方案-v1.md`

## 环境要求

- macOS / Linux / Windows（Windows 为实验性支持，见下方说明）
- Python ≥ 3.10、git、[uv](https://docs.astral.sh/uv/)（没有 uv 就用 `python3 -m venv` + pip）

### Windows 安装（PowerShell）

```powershell
# 前置：Python ≥3.10 与 Git（winget install Python.Python.3.12 Git.Git），uv 官方脚本安装
uv venv
uv pip install -e .    # uv 自动识别 .venv，无需激活
.venv\Scripts\python -m knowbase init
```

MCP 注册时 command 用 `.venv\Scripts\python.exe` 的完整路径（JSON 里写 `\\`），
推荐 ZCode 用 `type: "process"`（参数数组，免转义）。钩子同样指向 Scripts 下的 python.exe。

## 五分钟接入

```bash
# 1. 获取代码
git clone <内网 GitLab 地址>/knowbase.git ~/work/knowbase && cd ~/work/knowbase
#    （内网 pip 慢就加镜像：-i https://pypi.tuna.tsinghua.edu.cn/simple）

# 2. 安装
uv venv && uv pip install -e .

# 3. 初始化（显式、幂等；创建 ~/.knowbase/config.json 与 ~/knowbase 记忆库）
.venv/bin/python -m knowbase init

# 4. 注册到你的 Agent（任选，command 指向本目录 venv 的 python）
```

**Claude Code**（`~/.claude.json` → mcpServers）：

```json
"knowbase": { "command": "<你本机路径>/knowbase/.venv/bin/python",
  "args": ["-m", "knowbase", "serve"],
  "env": { "KNOWBASE_AGENT_NAME": "claude-code-你的名字" } }
```

**ZCode**（`~/.zcode/cli/config.json` → mcp.servers）：同上，`"type": "stdio"`。

**Trae CN**（`~/Library/Application Support/Trae CN/User/mcp.json` → mcpServers）：同上；
并把 `docs/trae-rules.md` 内容贴进 Trae 用户规则（Trae 无 hook、不读 AGENTS.md）。

**KNOWBASE_AGENT_NAME 约定：`工具-姓名`**（如 `trae-lisi`）——审计溯源和经验晋升统计都靠它区分人。

```bash
# 5. 验证：重启 Agent 后问一句 "搜下 EasyConnect"，应命中 P-2026-0001
```

## 两种开局

| 模式 | 做法 | 适用 |
|---|---|---|
| **空库自己攒**（默认） | 什么都不做，init 建的是你自己的空库 | 个人使用 |
| **团队共享经验库**（推荐） | `git clone` 已有记忆库（如部门共享仓）到本机 → `~/.knowbase/config.json` 里 `repo_path` 指过去 → `knowbase reindex` | 想直接继承团队踩坑遗产 |

共享模式下写权限就是 git：保存即 commit，`git.auto_push` 开启后自动推（默认关；
远端白名单默认 `http://10.21.20.112/*`，误配公网会被拒）。冲突 = git merge，索引可随时重建。

## 可选：任务前自动检索 / 任务后漏存兜底（hooks）

Claude Code（`~/.claude/settings.json`）与 ZCode（`~/.zcode/cli/config.json`，**必须 `"enabled": true`**）：
照抄本仓库维护者的接线，三事件 `SessionStart` / `UserPromptSubmit` / `Stop` 都调
`<venv python> -m knowbase hook <事件> --style claude|zcode`。Trae 无 hook，贴 `docs/trae-rules.md` 即可。

## Agent 使用规则（Team 通用，六句话）

1. 任务涉及具体项目/系统/报错 → 先 `memory_search`（≥3 字技术词），命中先 `memory_read`；
2. 任务结束产生踩坑/决策/流程/约束 → `memory_save`（提示相似就改 `memory_update`）；
3. 按记忆行动后 → `memory_feedback` 回填 helpful / not_helpful / outdated / incorrect；
4. stale / once 的记忆采信前先验证；检索走 `memory_search`，不要整读 INDEX.md；
5. 标准/偏好类保存会自动进 staging 待人审，属正常治理，勿绕过；
6. body 里禁止明文密钥（lint 会拦）。

## 存量知识入库与防污染开关

```bash
# 把现存文档批量导入为 once 级记忆（支持 Markdown/TXT/PDF/Office/HTML/图片 OCR/代码/日志）
.venv/bin/python -m knowbase import <文档目录> --type pitfall --scope 项目名
.venv/bin/python -m knowbase import <文档目录> --type standard --staging   # 走人审路径
```

导入后建议让 AI 逐条 `memory_update` 提炼成标准结构（现象/原因/正确做法），`verify` 转正。

防污染开关（`~/.knowbase/config.json` → hooks）：

```json
"hooks": { "enabled": true, "inject_min_confidence": "verified" }
```

`"verified"` 时任务前自动注入只取已验证经验（once 的新经验仍可被主动 search 检索到），
默认 `"verified"`；已有配置显式 `"any"` 会保留兼容行为。MCP 主动检索不受置信度开关影响，但默认排除 staging/stale。

## 人工治理（CLI）

```bash
.venv/bin/python -m knowbase verify P-2026-0001   # 人工确认有效（once→verified / stale 复活）
.venv/bin/python -m knowbase promote S-2026-0001  # 激活 staging 里的标准/偏好提案
.venv/bin/python -m knowbase archive P-2026-0001  # 归档
.venv/bin/python -m knowbase stats                # 数量/漏斗/TOP
.venv/bin/python -m knowbase reindex              # 索引坏了就重建
```

## 已知边界（诚实版）

- Windows 为实验性支持：跨进程锁在 Windows 走 msvcrt 字节范围锁（标准模式，但未在 Windows 真机回归），首次使用建议先跑 `.venv\Scripts\python tests\test_e2e.py` 验证；同义改写类查询命中弱（语义检索二期，本地向量方案已备）；
- Trae 无 hook，自动化程度低于 Claude Code / ZCode；
- 记忆库含内部系统经验，**只推内网 GitLab，永不推公网**。


## 初始化导入、检索记录与治理看板

```bash
knowbase init --import-from /path/to/project-docs --scope 项目名 --type workflow
knowbase history --limit 100
knowbase dashboard --open
# 指定输出位置
knowbase dashboard --output /path/to/dashboard.html
```

未把虚拟环境加入 PATH 时，将 `knowbase` 替换为 `.venv/bin/python -m knowbase`。

- `init --import-from` 要求明确 scope（通用资料填 global），全部进入 staging；先 `promote ID` 人审激活，再 `verify ID` 确认有效。普通 init 仍为空库初始化，不扫描外部目录。
- 导入保留完整原文、源文件绝对路径和 SHA-256；按来源路径、scope、类型幂等。源内容变化时提示复核已有条目，不自动覆盖；当前只支持 Markdown，不自动提炼、分块或判断原文正确性。按项目分别导入，不把混合项目目录全部归为 global。
- 默认 MCP 检索仅 active 且非 staging；调查旧知识可显式传 `include_inactive=true`（仍排除 archived）。自动 hook 默认只提示 verified、active、非 staging、无 contradicts 关系的候选；项目目录名与 scope 精确对应，无目录只查 global，未知目录只查该目录名与 global。
- Hook 单词兜底要求命中标题/标签，或至少两个关键词命中正文；输出明确为候选，需检查适用项目、版本、证据和验证日期。词法分数不是正确率，verified 也不保证永远正确。
- 每次 MCP/Hook 搜索记录查询、scope、Agent、时间、返回 ID/顺序/分数/状态，包括零命中。旧记录缺少的命中明细无法追溯补齐。`stats.enabled=false` 关闭使用日志。
- 看板支持项目、标题搜索、有效反馈排行、疑似问题、staging 待审核、once 未验证及搜索明细。有效反馈数量不等于唯一任务数；负反馈/失效/矛盾属于复核线索，不自动判错。页面为静态快照，重新运行命令刷新。
- `init/reindex` 保留读取统计、usage_log、feedback_log。这些日志不是 Markdown 派生态；删除数据库或仅克隆 Markdown 仓库无法恢复，备份需包含 memory.db（SQLite 一致性备份）。看板包含本地查询和来源信息，不自动发布到网络。
