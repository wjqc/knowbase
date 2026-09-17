# knowbase（薪火）

跨 Agent 共享的经验记忆库 MCP 服务：踩坑 / 决策 / 流程 / 偏好沉淀为一个本地 git 知识库，
供本机所有 AI Agent 检索复用。**一个人的坑，所有 Agent 的经验。**

- 存储：markdown + frontmatter（唯一真相）+ git 审计 + SQLite FTS5 派生索引（可重建）
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
