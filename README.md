# knowbase（薪火）

跨 Agent 共享的经验记忆库 MCP 服务：踩坑 / 决策 / 流程 / 偏好 / 业务规则沉淀为一个本地 git 知识库，
供本机所有 AI Agent 检索复用。**一个人的坑，所有 Agent 的经验。**
原始材料（文档/代码/日志）以 source artifact 分层保真留存，不混入知识检索（ADR-0002）。

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

共享模式下写权限就是 git：保存即 commit，`git.auto_push` 开启后在释放仓库锁后推送（默认关；
远端白名单默认 `http://10.21.20.112/*`，误配公网会被拒）。冲突 = git merge，索引可随时重建。

## 可选：任务前自动检索 / 任务后漏存兜底（hooks）

Claude Code（`~/.claude/settings.json`）与 ZCode（`~/.zcode/cli/config.json`，**必须 `"enabled": true`**）：
照抄本仓库维护者的接线，三事件 `SessionStart` / `UserPromptSubmit` / `Stop` 都调
`<venv python> -m knowbase hook <事件> --style claude|zcode`。Trae 无 hook，贴 `docs/trae-rules.md` 即可。

## Agent 使用规则（Team 通用，六句话）

1. 任务涉及具体项目/系统/报错 → 先 `memory_search`（≥3 字技术词），命中先 `memory_read`；
2. 任务结束产生踩坑/决策/流程/约束 → `memory_save`（提示相似就改 `memory_update`）；新卡正文须含八项小节：**结论 / 解决的问题 / 适用条件 / 不适用条件 / 可执行动作 / 关键证据 / 验证情况 / 未知与待确认**（"视情况而定"不算适用条件、"参见某文档"不算证据、"测试通过"不算验证，lint 会拦）；
3. 按记忆行动后 → `memory_feedback` 回填 helpful / not_helpful / outdated / incorrect；
4. stale / once 的记忆采信前先验证；检索走 `memory_search`，不要整读 INDEX.md；
5. 标准/偏好类保存会自动进 staging 待人审，属正常治理，勿绕过；
6. body 里禁止明文密钥；经验卡禁止本机路径和仓外文档引用。文档结论必须正文自足或使用 knowbase 仓内相对路径；代码证据写成 `code:<项目>/<仓库相对路径>`（可带行号，lint 会拦违规写入）。**整篇文档等原始材料不入 memory_save**，走 `knowbase import` 导入为 source（见下节）。

### 结构化代码定位 `code_refs`

`memory_save` / `memory_update` 支持在所有卡类型上携带 `code_refs`（业务规则卡最常用）：

```yaml
code_refs:
  - repo: order-center
    path: svc/order/OrderServiceImpl.java
    symbol: OrderServiceImpl#changeCard
    lines: 120-180
    note: 实名校验入口
```

| 字段 | 要求 |
|---|---|
| `repo` | 必填，仓库/项目标识，只允许 `[A-Za-z0-9_.-]+` |
| `path` | 必填，仓库相对路径；禁止绝对路径、`..` 穿越和反斜杠 |
| `symbol` | 可选，函数名或 `类#方法` |
| `lines` | 可选，单行或闭区间，如 `120` / `120-180` |
| `note` | 可选，简短说明该位置的业务意义 |

索引会把每项拍平为 `code:<repo>/<path>[#symbol][:lines]` 追加到现有 FTS 与向量文本，因此可按类名、方法名或路径片段反查规则；不增数据库列。正文出现旧式 `code:` 定位而 frontmatter 未填 `code_refs` 时，lint 仅告警、不阻断存量卡。

## 原始材料入库（source）与知识提炼（2026-09-21 起）

原始材料与可复用知识已彻底分层（ADR-0002）：**source 负责保真，card 负责复用**。

```bash
# 把现存文档批量导入为 source artifact（支持 Markdown/TXT/PDF/Office/HTML/图片 OCR/代码/日志）
.venv/bin/python -m knowbase import <文档目录> --scope 项目名
```

- import 只写 `sources/`（objects 文本快照 + manifests 元数据），按内容 sha256 去重；**不生成知识卡、不进入默认检索**；
- `--type/--staging` 参数保留仅为兼容，一律忽略；
- 可复用结论从 source 中提炼后用 `memory_save` 按八项小节结构保存；
- 旧 `reference`（R 卡）已退出默认检索与速览，可 `memory_read` 单条查看，待逐条提炼迁移（P3）。

防污染开关（`~/.knowbase/config.json` → hooks）：

```json
"hooks": { "enabled": true, "inject_min_confidence": "verified" }
```

`"verified"` 时任务前自动注入只取已验证经验（once 的新经验仍可被主动 search 检索到），
默认 `"verified"`；已有配置显式 `"any"` 会保留兼容行为。MCP 主动检索不受置信度开关影响，但默认排除 staging/stale/reference。

## 命令速查（全部 15 个）

未把虚拟环境加入 PATH 时，将 `knowbase` 替换为 `.venv/bin/python -m knowbase`。

**治理（仅人工，Agent 无对应通道）**

| 命令 | 用途 |
|---|---|
| `knowbase verify <id>` | 人工确认有效（once→verified / stale 复活） |
| `knowbase promote <id>` | 激活 staging 提案（标准/偏好/业务规则生效） |
| `knowbase revise <id> --body-file <文件> [--title]` | 人工修订已生效的标准/偏好/业务规则 |
| `knowbase archive <id>` | 归档退役（检索与速览不再出现） |

**运维与观测**

| 命令 | 用途 |
|---|---|
| `knowbase init [--import-from <目录> --scope <名>]` | 初始化/补全（幂等），可顺带导入 source artifact |
| `knowbase import <目录> --scope <名>` | 批量导入为 source artifact（16 种格式，不产生知识卡、不进默认检索；`--type/--staging` 已废弃兼容保留） |
| `knowbase reindex` | 全量重建索引与 INDEX.md（markdown 唯一真相，坏了就重建） |
| `knowbase stats` | 数量分布 / 使用漏斗 / TOP |
| `knowbase list [type]` | 列出记忆（含 staging 标注） |
| `knowbase history [--limit N]` | 最近搜索命中记录（JSON） |
| `knowbase dashboard [--output <文件>] [--open]` | 生成 HTML 治理看板（静态快照，重跑即刷新） |
| `knowbase doctor [--json]` | 健康检查：索引一致性 / 同步 / Git / 解析器 |
| `knowbase alerts [--dry-run]` | 输出异常状态，供调度器通知 |

**服务与接入**

| 命令 | 用途 |
|---|---|
| `knowbase serve` | 启动 MCP 服务（stdio，供各 Agent 配置） |
| `knowbase hook <session-start\|user-prompt\|stop> [--style claude\|zcode]` | Agent 钩子入口（接线见上文 hooks 节） |

## 已知边界（诚实版）

- Windows 为实验性支持：跨进程锁在 Windows 走 msvcrt 字节范围锁（标准模式，但未在 Windows 真机回归），首次使用建议先跑 `.venv\Scripts\python tests\test_e2e.py` 验证；同义改写类查询命中弱（语义检索二期，本地向量方案已备）；
- Trae 无 hook，自动化程度低于 Claude Code / ZCode；
- 记忆库含内部系统经验，**只推内网 GitLab，永不推公网**。


## 导入、检索记录与治理看板（行为细则）

- `init --import-from` 要求明确 scope（通用资料填 global）；import 只生成 source artifact（sources/objects 快照 + manifests），不产生知识卡、不进默认检索。普通 init 仍为空库初始化，不扫描外部目录。
- source 快照按内容 sha256 去重（跨路径同内容只存一份）；同路径源文件变化产生新快照，不覆盖旧内容。源文件绝对路径仅留在本机 `memory.db.source_state` 做幂等，不写入共享内容。支持 Markdown/TXT/PDF/DOCX/XLSX/PPTX/HTML/图片 OCR/代码/日志。按项目分别导入，不把混合项目目录全部归为 global。
- 可复用知识用 `memory_save` 提炼（八项小节结构，lint 强制）；source → card 的自动提取与审核链路属 P1。
- 默认 MCP 检索仅 active 且非 staging，且排除 reference（原始材料卡）与 source；无项目上下文只查 global。有项目上下文时必须通过 `scope_map`、已存在的同名 scope 或显式参数确定项目，无法解析就拒绝检索，避免跨项目召回。自动 Hook 与 MCP 使用同一混合检索路径。
- 旧 `reference`（R 卡）只读不写不检索：`memory_save` 拒绝该类型，`memory_search` 不返回（显式 type=reference 也拒绝），`memory_read` 可单条查看并带"原始材料卡"提示；INDEX.md 速览不再列出，仅在头部计数标注。
- 新卡 `schema: card-v2` 强制八项小节与内容质量（空洞适用条件/纯引用证据/"测试通过"式验证/空小节均拦截）；存量卡不带标记沿用旧规则，`memory_update` 不受影响，待 P3 迁移。
- Hook 单词兜底要求命中标题/标签，或至少两个关键词命中正文；输出明确为候选，需检查适用项目、版本、证据和验证日期。词法分数不是正确率，verified 也不保证永远正确。
- 每次 MCP/Hook 搜索记录查询、scope、Agent、时间、返回 ID/顺序/分数/状态，包括零命中。旧记录缺少的命中明细无法追溯补齐。`stats.enabled=false` 关闭使用日志。
- 看板支持项目、标题搜索、有效反馈排行、疑似问题、staging 待审核、once 未验证及搜索明细。有效反馈数量不等于唯一任务数；负反馈/失效/矛盾属于复核线索，不自动判错。页面为静态快照，重新运行命令刷新。
- `init/reindex` 保留读取统计、usage_log、feedback_log。这些日志不是 Markdown 派生态；删除数据库或仅克隆 Markdown 仓库无法恢复，备份需包含 memory.db（SQLite 一致性备份）。看板包含本地查询和来源信息，不自动发布到网络。
- 记忆卡必须内容自足：禁止保存 `~/...`、`/Users/...`、`C:\Users\...` 等本机路径，也禁止指向 knowbase 仓库外的文档。文档若需独立维护，先纳入 knowbase，再用仓库相对路径引用；代码证据统一写成 `code:<项目>/<仓库相对路径>`，例如 `code:cmi-sales-assistant/agent-platform/app/service.py:42`。
