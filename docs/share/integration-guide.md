# 薪火 knowbase 软件接入使用手册

> 部门内部交付版｜2026-10-22｜手册版本 v2.0（对应软件版本 `0.2.0rc6`）  
> 面向首次接入的研发、测试同事及团队维护者。按运行版本 `0.2.0rc6`、固定运行环境 `~/.knowbase/runtime/venv` 编写。安装前向维护者领取对应安装包和内网经验库地址。

了解项目用途可先读《[项目介绍](project-introduction.md)》。首次接入依次完成第 2～5 节；Hook、团队统计和升级恢复按需阅读。

## 1. 文档说明

### 1.1 编写目的与适用范围

本文说明如何在成员本机安装 knowbase、接入 AI 宿主并完成首次验收，以及上线后的日常操作、升级与排障。适用于部门试点期间的成员接入；正式推广后以维护者发布的最新手册版本为准。

### 1.2 读者对象

- **接入成员（主要读者）：** 使用 Claude Code / ZCode / Trae 等 AI 宿主的研发、测试同事。
- **团队维护者：** additionally 负责构建安装包、管理数据仓与审核提案，升级与备份章节与其相关。

### 1.3 术语与缩略语

| 术语 | 含义 |
|---|---|
| 经验卡 | 可复用结论的知识单元，正文含八项小节，Markdown + frontmatter 保存 |
| source | 原始材料层（文档 / 代码 / 日志）保真留存，默认不进知识检索 |
| MCP | AI 工具调用外部能力的统一接口协议；本服务经 stdio 启动，无 HTTP 端口 |
| 宿主 | 运行 AI Agent 的工具，如 Claude Code、ZCode、Trae CN |
| Hook | 宿主事件触发器（会话开始、提交提示等），用于自动提醒与注入 |
| scope | 项目范围标识；检索默认限定当前项目与 `global` 通用经验 |
| staging | 待人工审核区；标准 / 偏好提案先入此处 |
| 固定运行环境 | `~/.knowbase/runtime/venv`，本机唯一运行版本，原地升级 |

### 1.4 适用版本

| 项 | 值 |
|---|---|
| 软件版本 | knowbase `0.2.0rc6`（wheel：`knowbase-0.2.0rc6-py3-none-any.whl`） |
| 手册版本 | v2.0，2026-10-22 按大纲重组并统一单版本运行口径 |
| 运行环境 | `~/.knowbase/runtime/venv`（单版本，升级原地安装） |

## 2. 接入前准备

### 2.1 接入流程总览

```text
领取材料 → 环境自检 → 安装并初始化 → 宿主注册 MCP → 配置使用规则
        → 隔离库首次验收（第 5 节）→ 切换正式库 / 加入团队共享（第 6 节）
        → 日常使用与治理 →（可选）Hook 自动化 →（维护者）升级与备份
```

先完成 MCP 主动调用并验收，再考虑 Hook 自动化；完整链路跑通前，接入状态记为试用或辅助模式。

### 2.2 接入申请

接入前向维护者确认并领取：

1. 固定安装包（wheel）及版本号；
2. 内网经验数据仓 URL 与团队分支；
3. 个人 Git 读写权限（数据仓推送 / 拉取）；
4. 项目 scope 名称（由团队统一命名）；
5. 远端白名单前缀（写入配置时使用）。

仓库地址、版本和本机绝对路径均按实际值填写，本文示例一律为占位。

### 2.3 环境要求与路径约定

- Python ≥ 3.10、Git，以及支持 MCP 的 AI 宿主；安装可用 `uv` 或 Python 自带 `venv`。
- macOS / Linux 为主，Windows 为实验性支持（见 8.4）。
- 网络须能访问内网 Git 远端；服务无 HTTP 端口要求。

| 名称 | 本文示例 | 用途 |
|---|---|---|
| 程序源码目录 | `~/work/knowbase` | 开发、构建和测试（仅维护者需要） |
| 固定运行环境 | `~/.knowbase/runtime/venv` | 宿主启动 MCP / Hook 所用的 Python，本机唯一 |
| 个人经验数据仓 | `~/knowbase` | Markdown 经验卡、source 与本机数据库 |
| 团队经验数据仓 | `~/knowbase-team` | 内网共享仓在本机的克隆 |
| 配置文件 | `~/.knowbase/config.json` | 所有宿主共同使用 |

程序源码和经验数据仓分开；固定运行环境只安装程序包，不是第二套业务库。**本机永远只保留一个运行版本**：升级原地安装到同一 venv，不创建版本目录、不保留旧 wheel（见 6.4）。

## 3. 接入方式说明

### 3.1 支持的接入模式总览

| 模式 | 形态 | 是否必需 |
|---|---|---|
| MCP 主动调用 | 宿主经 stdio 启动本地服务，Agent 按需调用 16 个 `memory_*` 工具 | 必需，接入的基础 |
| Hook 自动化 | 宿主事件（会话开始 / 提交提示 / 停止）自动提醒、检索、注入 | 可选，验收 MCP 后再加 |
| CLI 直接使用 | 终端执行 `python -I -m knowbase <命令>`，用于初始化、诊断、看板与治理 | 运维与维护场景 |

### 3.2 各模式适用场景与选型建议

- 所有成员：先 MCP。任何支持本地 stdio MCP 的宿主都能接，不绑定模型。
- 希望减少“忘查”的成员：MCP 验收通过后再加 Hook；Hook 默认只注入 `verified` 知识，主动检索仍由 Agent 判断。
- 维护者：日常用 CLI 做诊断、导入、看板、提案审核与升级。

## 4. 接入操作步骤

### 4.1 账号开通与权限配置

1. **Git 身份：** 确认 Git 已配置提交者身份（有则跳过设置命令）：

```bash
git var GIT_AUTHOR_IDENT
# 仅在尚未配置时，填写你自己的信息：
# git config --global user.name "你的姓名"
# git config --global user.email "你的公司邮箱"
```

2. **数据仓权限：** 由维护者在内网 Git 开通读写；远端白名单只允许批准的内网地址前缀。
3. **来源标签：** 每个宿主设置独立 `KNOWBASE_AGENT_NAME`（如 `trae-lisi`），只作工具来源归因，不认证人员身份。

### 4.2 安装运行环境并初始化

#### 4.2.1 部门试用：安装维护者提供的固定包

以下文件名为示例，换成实际收到的 wheel。`~/.knowbase/runtime/venv` 是本机唯一运行环境，升级原地安装（见 6.4），不建版本目录。

```bash
python3 --version
git --version
python3 -m venv "$HOME/.knowbase/runtime/venv"
"$HOME/.knowbase/runtime/venv/bin/python" -m pip install /实际下载目录/knowbase-0.2.0rc6-py3-none-any.whl
"$HOME/.knowbase/runtime/venv/bin/python" -I -c 'import knowbase; print(knowbase.__version__)'
```

记录打印的实际版本。`-I` 防止宿主工作目录里的同名源码覆盖已安装的包。

若部门尚未提供安装包，维护者可在确认过的源码版本构建，不能把本地未发布功能当作已有发行包：

```bash
cd "$HOME/work/knowbase"
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/python -m pip wheel --no-deps --wheel-dir dist .
```

这里使用普通安装（源码构建专用 `.venv`，与运行环境分离）。源码改动后需重新安装或重新构建；正式接线统一指向固定运行环境。

#### 4.2.2 初始化空库

```bash
"$HOME/.knowbase/runtime/venv/bin/python" -I -m knowbase init
"$HOME/.knowbase/runtime/venv/bin/python" -I -m knowbase doctor --json
```

`init` 会创建默认配置 `~/.knowbase/config.json` 和默认数据目录 `~/knowbase`，建立 Git、卡片目录和索引；已有配置不会被覆盖。若机器已接入其他版本，先按 6.4 备份再原地升级。

首次空库有 0 张卡是正常结果；不应要求一定搜到某个维护者专属编号。`doctor` 有告警时按具体检查项处理；未配置共享远端不等于 MCP 安装失败。

### 4.3 在 AI 宿主注册 MCP

#### 4.3.1 Claude Code

将 `knowbase` 条目合并到用户级 `~/.claude.json` 的 `mcpServers` 中，保留其他服务：

```json
{
  "mcpServers": {
    "knowbase": {
      "command": "/你的用户目录/.knowbase/runtime/venv/bin/python",
      "args": ["-I", "-m", "knowbase", "serve"],
      "env": {"KNOWBASE_AGENT_NAME": "claude-code-你的姓名缩写"}
    }
  }
}
```

#### 4.3.2 ZCode

将条目合并到用户级 `~/.zcode/cli/config.json` 的 `mcp.servers` 中：

```json
{
  "mcp": {
    "servers": {
      "knowbase": {
        "type": "stdio",
        "command": "/你的用户目录/.knowbase/runtime/venv/bin/python",
        "args": ["-I", "-m", "knowbase", "serve"],
        "env": {"KNOWBASE_AGENT_NAME": "zcode-你的姓名缩写"}
      }
    }
  }
}
```

#### 4.3.3 Trae CN

macOS 将与 Claude 示例相同结构的 `mcpServers.knowbase` 条目合并到：

```text
~/Library/Application Support/Trae CN/User/mcp.json
```

把来源标签改成 `trae-你的姓名缩写`。其他平台通过 Trae 的 MCP 设置入口编辑用户配置，并按 4.4 增加使用规则。

当前源码包含 Trae Hook 适配；本仓真实整链验收尚未完成，首次接入先使用 MCP 主动工作流，不照搬 Claude / ZCode Hook 格式。

#### 4.3.4 其他 MCP 宿主

在宿主支持的本地 stdio MCP 配置中使用同一个绝对 Python 路径，参数为 `-I -m knowbase serve`，并设置独立来源标签。具体配置文件格式依宿主而定，不能直接照搬上面的 JSON 外层结构。

完成后按宿主提供的方式重新加载 MCP，必要时重启宿主或新建会话。`serve` 由客户端启动并通过 stdio 通信，不需要 HTTP 端口；在普通终端直接运行时等待输入是正常现象。

多宿主若需不同的测试配置，可在 `env` 中设置 `KNOWBASE_CONFIG`；测试完成后恢复，不要误连其他数据库。

### 4.4 配置 Agent 使用规则（接口使用规范）

把下面内容放到宿主实际会加载的用户规则中；Claude Code 可放 `CLAUDE.md`，其他宿主使用其相应规则入口。不要假设每个工具都会读取 `AGENTS.md`。

```text
使用 knowbase 时：
1. 涉及项目、系统、报错或环境配置，先 memory_search；显式传当前项目 scope，命中后 memory_read，检查适用条件。
2. 长任务拆解后、进入新子任务、遇到新错误及重要决策前，用 memory_context 按当前主题补查。
3. 实际参考或采纳候选时用 memory_use；沿用检索的 session_id、host_profile、context_event_id 和内容版本，使用事件另给 event_id。
4. 验证任务结果后再 memory_feedback，携带 expected_content_revision、use_id、稳定 task_outcome_id 和 task_result。检索、阅读不能冒充有效反馈；重试不换结果 ID。
5. 只沉淀可复用结论；先查重，有同类经验优先更新。新卡包含八项小节：结论、解决的问题、适用条件、不适用条件、可执行动作、关键证据、验证情况、未知与待确认。
6. 业务规则须有 provenance，代码推断与业务确认分开。待审候选只用于验证；不自行把它当生效规则。业务规则达标后是否自动激活由服务配置决定，标准和偏好仍走人工治理。
7. 不写明文凭据或个人机器路径；代码证据用 code_refs 的仓库相对路径。整篇原始材料走 source 导入，不直接存成经验卡。
8. 长材料走 capture → claim(protocol_version=2) → material 分页 → submit → 读回，核对真正保存的 memory_id；queued 不等于成卡。
```

`once` 表示尚未充分验证；`verified` 也要核对版本与验证日期。`stale` 和待审卡不应作为默认执行依据。

### 4.5 可选：Hook 自动化

MCP 是读写接口，Hook 是宿主事件触发器。先完成第 5 节验收，再按宿主支持的格式增加 Hook，保持其他处理器不变。

#### 4.5.1 Claude Code 基础三事件

将下面 `hooks` 的各事件合并到 `~/.claude/settings.json`。Python 路径必须与 MCP 同版本；这里用字符串内引号处理路径空格。

```json
{
  "hooks": {
    "SessionStart": [{"hooks": [{"type": "command", "command": "\"/你的用户目录/.knowbase/runtime/venv/bin/python\" -I -m knowbase hook session-start --style claude", "timeout": 5}]}],
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "\"/你的用户目录/.knowbase/runtime/venv/bin/python\" -I -m knowbase hook user-prompt --style claude", "timeout": 5}]}],
    "Stop": [{"hooks": [{"type": "command", "command": "\"/你的用户目录/.knowbase/runtime/venv/bin/python\" -I -m knowbase hook stop --style claude", "timeout": 5}]}]
  }
}
```

#### 4.5.2 ZCode 基础接线

在 `~/.zcode/cli/config.json` 的 `hooks.events` 合并事件，并设置 `hooks.enabled=true`。单个事件示例：

```json
{
  "hooks": {
    "enabled": true,
    "events": {
      "SessionStart": [{"hooks": [{"type": "process", "command": "/你的用户目录/.knowbase/runtime/venv/bin/python", "args": ["-I", "-m", "knowbase", "hook", "session-start", "--style", "zcode"], "timeoutMs": 5000}]}]
    }
  }
}
```

按同样结构补充 `UserPromptSubmit → user-prompt`、`Stop → stop`。执行中工具事件参考 [ZCode 接线](../hosts/zcode.md)；本仓记录的 ZCode 版本不支持 `PreCompact`，不要添加该事件。

#### 4.5.3 执行中检索与材料观察

完成隔离验证后，可合并配置：

```json
{
  "hooks": {"enabled": true, "inject_min_confidence": "verified"},
  "inflight": {"enabled": true, "capture_enabled": false, "mode": "assisted"}
}
```

基础三事件和执行中工具事件分别配置。`inflight.enabled` 默认关闭；上面的设置开启对应执行中路径，但暂不自动入队，Agent 显式阶段提炼仍可用。Hook 自动注入默认只取 verified；主动 MCP 检索可找到 active 的 once 卡。

进一步接线参考 [Claude](../hosts/claude.md)、[ZCode](../hosts/zcode.md)、[Trae](../hosts/trae.md)。这些文件包含特定版本的静态记录，必须核对当前宿主事件支持情况；尤其不要把 Claude 的失败事件和 ZCode 的外层协议混用。

真实验收至少要有一次输入、两个工具阶段，并证明后续行动实际使用了注入内容。配置已保存、手工给处理器喂 JSON、Hook 退出成功，都不能单独证明模型已消费。

## 5. 联调与验收：从空库跑通一次

### 5.1 验收环境：先隔离测试数据

建议现场演示和接入验收使用专用配置及数据目录，避免给团队正式库加入演示卡。

新建 `~/.knowbase/onboarding.json`，内容如下，路径换成你本机的绝对路径：

```json
{
  "repo_path": "/你的用户目录/knowbase-onboarding",
  "git": {"auto_push": false, "auto_pull": false},
  "inflight": {"enabled": false},
  "value": {"events_enabled": false}
}
```

然后初始化：

```bash
KNOWBASE_CONFIG="$HOME/.knowbase/onboarding.json" \
  "$HOME/.knowbase/runtime/venv/bin/python" -I -m knowbase init
```

在宿主 `knowbase.env` 中临时添加 `KNOWBASE_CONFIG`，值为该 JSON 的绝对路径，再重连 MCP。切换前记录原配置；演示结束恢复原接线并重连。

### 5.2 验收步骤

#### 5.2.1 发现工具

让 Agent 列出 knowbase 提供的工具，至少确认：

```text
memory_search / memory_read / memory_save / memory_context
memory_use / memory_feedback / memory_job_material / memory_metrics
```

当前版本共注册 16 个工具（完整清单见 8.1）。若缺少 `memory_use` 或 `memory_metrics`，先核对实际 Python 路径与版本，再重连，不能用修改源码代替服务升级。

#### 5.2.2 保存一张示例卡

以下是给 Agent 的操作要求；MCP 调用由 Agent 发起，不是在终端执行函数：

```text
请先 memory_search(query="接入验收程序数据仓分离", scope="knowbase-onboarding")。
若没有相同示例，用 memory_save 保存：
type=pitfall
title=接入验收：程序目录与经验数据仓应分开
scope=knowbase-onboarding
tags=[接入验收, 仓库分离]
body 使用下面的八项正文，不增加虚构验证。
保存后返回实际卡片 ID，并用 memory_read 读回。
```

示例正文：

```markdown
## 结论
程序安装目录和经验数据仓使用两个独立目录，所有接入宿主共用同一数据配置。
## 解决的问题
首次接入时容易把程序代码目录误配为经验数据仓，导致知识文件和开发文件混放。
## 适用条件
首次安装 knowbase，并为本机多个 MCP 宿主配置共同的数据仓路径。
## 不适用条件
已经接入的正式库升级，需要保留原 repo_path 并执行备份，不能直接切换为空库。
## 可执行动作
初始化后核对配置中的 repo_path，并确认经验卡目录与程序包目录分开；重连宿主再查询。
## 关键证据
init 根据 repo_path 创建数据目录；MCP 服务按同一配置读取经验文件。
## 验证情况
本卡是接入协议演示材料；保存和读回用于验证接线，不证明真实业务任务提效。
## 未知与待确认
跨机器同步和当前宿主的自动 Hook 尚需另行验收。
```

预期结果：保存返回实际 ID，全文读回包含八项正文与 `content_revision`。新卡初始通常为 `once`，不要把它改成“已验证”来绕过检查。

#### 5.2.3 检索、使用和反馈

继续让 Agent 通过 `memory_search` 用同一 scope 查到该卡。再执行下面的阶段检索，以获得可绑定任务的递送事件：

```json
{
  "session_id": "onboarding-session-1",
  "event_id": "onboarding-context-1",
  "scope": "knowbase-onboarding",
  "stage": "subtask_start",
  "task_id": "onboarding-task-1",
  "task_revision": "1",
  "host_profile": "onboarding-manual",
  "current_goal": "核对本机程序和经验数据仓分离",
  "topics": [{"topic_id": "setup", "queries": ["接入验收程序数据仓分离"]}]
}
```

该 JSON 是 `memory_context` 的参数。确认 `candidates` 真正返回示例卡；使用其中的实际 ID 和 `content_revision` 调用 `memory_use`：

```json
{
  "session_id": "onboarding-session-1",
  "event_id": "onboarding-use-1",
  "context_event_id": "onboarding-context-1",
  "id": "<刚返回的卡片ID>",
  "content_revision": "<候选中的实际内容版本>",
  "state": "referenced",
  "host_profile": "onboarding-manual"
}
```

记录返回的 `use_id`。实际核对配置、程序路径和数据目录后，若示例确实帮助完成了本次检查，再调用 `memory_feedback`：

```json
{
  "id": "<实际卡片ID>",
  "outcome": "helpful",
  "context": "已核对当前安装路径和 repo_path，接入检查完成；仅代表本次演示结果。",
  "expected_content_revision": "<本次使用的实际内容版本>",
  "use_id": "<memory_use返回的实际use_id>",
  "task_outcome_id": "onboarding-result-1",
  "task_result": "completed",
  "request_id": "onboarding-feedback-1"
}
```

占位值必须替换为真实响应；未完成检查时不报 `helpful/completed`。同一请求重试保留所有 ID；新的任务运行使用新的 session、任务及结果 ID。

### 5.3 验收通过标准

**发现工具 → 保存 → 默认检索命中 → 全文读回 → context 递送 → use 回执 → 任务结果反馈，全部走宿主 MCP，无错误响应。** 一次演示不满足默认自动晋升阈值，也不代表跨成员复用或收益验收。

## 6. 上线与日常使用

### 6.1 切换正式库与加入团队共享

#### 6.1.1 个人使用

保留默认 `repo_path`，本机所有宿主使用同一份配置。默认自动 Git 提交开启、自动推送关闭。

#### 6.1.2 加入团队共享库

先克隆批准的内网数据仓；以下占位地址和分支须替换。

```bash
git clone --branch <团队分支> <批准的内网经验数据仓URL> "$HOME/knowbase-team"
```

将以下字段**合并**进 `~/.knowbase/config.json`，保留其他现有配置。JSON 中写真实绝对路径，不写 `$HOME`。

```json
{
  "repo_path": "/你的用户目录/knowbase-team",
  "scope_map": {
    "/你的项目绝对路径/order-center": "order-center"
  },
  "git": {
    "auto_commit": true,
    "auto_push": false,
    "auto_pull": true,
    "remote": {
      "name": "origin",
      "url": "<批准的内网经验数据仓URL>",
      "branch": "<团队分支>"
    },
    "allowed_remote_prefixes": ["<批准的内网Git地址前缀，包含结尾斜杠>"]
  }
}
```

示例 scope `order-center` 仅代表一个项目标识；由团队统一命名。绝对路径因人而异，scope 应一致。查询该项目时可检索其经验和 `global` 通用经验，其他项目被过滤。

切换完成后初始化本地索引，再重连各宿主：

```bash
"$HOME/.knowbase/runtime/venv/bin/python" -I -m knowbase init
"$HOME/.knowbase/runtime/venv/bin/python" -I -m knowbase stats
```

初次先保留 `auto_push=false`；确认内网远端、权限、分支及同步验收后，需要写后自动共享的成员再开启 `auto_push=true`。注意：宿主内嵌 MCP（无独立服务进程）的形态下 `auto_push` 可能不生效，推送不前进时先手动 `git push` 兜底，并向维护者反馈形态。

自动推送关闭时，只在工作区干净、已确认没有冲突的情况下，人工执行：

```bash
git -C "$HOME/knowbase-team" status --short
git -C "$HOME/knowbase-team" pull --rebase
git -C "$HOME/knowbase-team" push
```

共享数据使用内网远端，不能把经验数据仓推到程序代码的公开远端。推送失败时保留本地提交，排除网络或内容冲突后再同步；以另一台机器拉取并检索到卡片作为共享成功的证据。

### 6.2 日常操作与治理

下表命令均在固定 Python 后加 `-I -m knowbase` 执行。

| 命令 / 工具 | 用途 |
|---|---|
| `stats` / `memory_stats` | 看数量和使用情况 |
| `doctor --json` | 检查索引、Git 和运行状态 |
| `reindex` | 重建派生索引，保留本地运行日志 |
| `dashboard --output /本机目录/knowbase-dashboard.html --open` | 生成本机离线汇总看板 |
| `import /材料目录 --scope order-center` | 原始材料导入 source，默认不生成经验卡 |
| `import /材料目录 --scope order-center --mode extract` | 导入并进入宿主提炼队列，仍需领取、提交、读回 |
| `jobs list` | 查看提炼队列 |
| `memory_update` | 补充普通经验；保留并核对版本 |
| `promote <ID>` | 人工审核后激活待审提案 |
| `verify <ID>` | 人工确认当前版本有效 |
| `revise <ID> --body-file /正文文件` | 人工修订已生效的治理规则 |
| `archive <ID>` | 人工归档不再使用的卡片 |

PDF / Office / HTML / 代码 / 日志等可作为导入材料；图片 OCR 另需系统安装 tesseract。某格式解析成功只代表材料已读取，不代表业务结论已验证。

自动演进的当前默认值：`config.evolution` 默认开启，要求版本绑定和独立任务证据。普通新卡默认 3 个已完成任务、至少 2 个会话的正向证据可晋升；业务规则候选还需要出处及允许自动激活的配置。标准和偏好继续人工治理。

业务规则候选通过 `memory_context.validation_candidates` 获取验证提示；用关联身份的 `memory_read` 取得 `read_use_id`，实际验证后连同版本和 `task_result` 反馈。它是验证候选，不能先当生效规则采纳。

如试点要求业务规则全部人工激活，可将 `evolution.auto_activate_bizrules` 设为 `false`；这不等于关闭普通经验的反馈治理。正文变更后旧验证不沿用，负面证据会影响默认检索资格。

### 6.3 看板和团队统计

先用本机 `dashboard` 看汇总。团队统计默认不自动上传，`value.events_enabled` 默认关闭；需要试点采集时，由维护者统一约定开启时间、统计窗口和成员登记方式。

汇总流程为：源端 `value export` → 汇总端 `value import` → 源端 `value receipt` 核对回执 → 汇总端 `value snapshot` / `value report`。具体参数见 [价值事件协议](../value-events-protocol.md)。

导出成功不等于汇总端已接收；工具标签不等于人数。`memory_metrics` 提供本机最小化统计，缺失值保持未知。Claude 请求用量可采集，ZCode / Trae 对应入口尚不支持；没有对照数据就不填写净节省比例。

### 6.4 升级、备份与恢复（单版本原地升级）

**本机永远只保留一个运行版本。** 升级流程：

1. 暂停本机 knowbase 写入，记录运行版本、配置和数据仓 Git revision。
2. 备份：配置、Markdown / source、数据库及 `.knowbase/local` 等本地材料；备份与运行包分离存放。
3. 数据库使用 SQLite backup API，不能只复制正在写入的 `memory.db` 主文件：

```python
from pathlib import Path
import sqlite3

repo = Path("/你的用户目录/knowbase-team")
backup_dir = Path("/你的备份目录/升级前快照")
backup_dir.mkdir(parents=True, exist_ok=False)
source = sqlite3.connect((repo / "memory.db").as_uri() + "?mode=ro", uri=True)
target = sqlite3.connect(backup_dir / "memory.db")
try:
    source.backup(target)
finally:
    target.close()
    source.close()
```

这段代码仅备份数据库；其余文件和未提交知识改动须另行保留。

4. 新包**原地安装**到同一运行环境，MCP / Hook 路径不变，不新建版本目录、不留旧 wheel：

```bash
"$HOME/.knowbase/runtime/venv/bin/python" -m pip install --upgrade /实际下载目录/knowbase-<新版本>-py3-none-any.whl
"$HOME/.knowbase/runtime/venv/bin/python" -I -c 'import knowbase; print(knowbase.__version__)'
```

5. 显式执行初始化或升级检查并重连宿主，再在隔离库复验第 5 节。旧服务不会因为磁盘文件变更自动升级；已连接的宿主必须重连。

出问题时先关闭新机制、恢复原接线并保留当前数据。回退旧代码前核对数据库兼容性；若必须恢复备份，先保存升级后的新卡、回执和未完成材料，明确损失窗口。**不以删库重建作为常规恢复操作。**

### 6.5 上线检查清单

- [ ] 记录了固定安装包版本和 Python 路径。
- [ ] 程序目录、经验数据仓、配置位置清楚，已设置正确 scope。
- [ ] 至少一个宿主完成工具发现与第 5 节真实 MCP 验收。
- [ ] 配置变更合并保留了其他服务，演示结束恢复正式接线。
- [ ] 团队库完成另一台机器的拉取与检索读回。
- [ ] Hook 如已启用，另留真实触发、模型消费和成卡读回证据。
- [ ] 已明确谁审核标准 / 偏好，是否允许业务规则自动激活。
- [ ] 已准备数据库和文件备份，不把本机计数当全团队收益。

完整真实业务验收继续使用 [三宿主样本协议](../team-value-real-acceptance.md)。本文的接入演示验证工具链路，不替代三人跨机试点与提效评估。

## 7. 常见问题（FAQ）

| 现象 | 优先检查 |
|---|---|
| MCP 没有工具或启动失败 | Python 绝对路径、安装版本、JSON 外层位置；宿主是否重连 |
| 报 `No module named knowbase` | 该 Python 环境是否安装包；是否只在源码目录偶然能运行 |
| `serve` 在终端一直等待 | 它是 stdio 服务，应由 MCP 客户端启动 |
| 空库检索没有结果 | 先完成示例卡保存，或确认团队库已克隆并指向正确 repo_path |
| 当前项目无法解析 scope | 显式传 scope，或补充 scope_map；不要回退为全库查询 |
| 主动检索能找到卡，Hook 没注入 | 是否还是 once；Hook 默认只注入 verified；再核对事件实际触发 |
| 开了 auto_push 远端不前进 | 宿主是否内嵌 MCP（无独立服务进程）；先手动 `git push` 兜底，并向维护者反馈形态 |
| 保存被 lint 拦截 | 八项正文是否具体；是否含明文凭据、本机路径、仓外文档引用 |
| source 已导入但搜不到经验 | source 默认不进知识检索，需要提炼并读回实际卡片 |
| 反馈成功但没有晋升 | 是否绑定版本、use_id、任务结果；是否同一任务重试或尚未达到阈值 |
| `CANDIDATE_NOT_DELIVERED` | use 是否指向实际返回该卡的 context 事件和内容版本 |
| `REVISION_CONFLICT` | 重新检索或读回当前版本，重新核对；不要替换哈希强行追认旧结果 |
| 自己能读到，同事搜不到 | 推送、拉取、scope、staging/stale 状态及对方运行版本分别检查 |
| 网络失败或 Git 冲突 | 看真实同步告警；本地保存可能已成功，先保留提交再处理 |
| 看板没有 Token 或节省数据 | 采集来源可能不支持或未开启；没有对照时保持未知 |

## 8. 附录

### 8.1 MCP 工具清单（16 个）

| 工具 | 用途 |
|---|---|
| `memory_search` | 检索经验卡（词法 + 模糊双路、RRF 融合） |
| `memory_read` | 按卡 ID 读全文 |
| `memory_context` | 执行中按主题分阶段检索并记录上下文事件 |
| `memory_save` | 保存经验卡 |
| `memory_update` | 修改已有卡内容 / 标签 / 关系 |
| `memory_use` | 记录候选实际使用状态（referenced / adopted / irrelevant / unverified） |
| `memory_feedback` | 回填任务结果（helpful / not_helpful / outdated / incorrect），绑定版本与回执 |
| `memory_capture` | 阶段完成时入队提炼任务 |
| `memory_import` | 导入文档目录为 source 材料 |
| `memory_job_claim` | 领取提炼批次（protocol_version=2） |
| `memory_job_material` | 分页读取已领取批次材料 |
| `memory_job_renew` | 续租当前批次 |
| `memory_job_status` | 查询提炼任务状态 |
| `memory_job_submit` | 提交提炼结果候选 |
| `memory_stats` | 记忆库统计 |
| `memory_metrics` | 本机隐私最小化统计汇总 |

### 8.2 常见返回码

| 返回码 | 含义与处理 |
|---|---|
| `CANDIDATE_NOT_DELIVERED` | `memory_use` 未指向实际递送该卡的 context 事件；核对 `context_event_id` 与内容版本 |
| `REVISION_CONFLICT` | 内容版本已变化；重新检索读回当前版本后再操作，不要替换哈希追认旧结果 |
| `NEEDS_REASSESSMENT` | 提炼候选未通过查重复核；`duplicate_evaluation` 须写在每个 candidate 对象内部，不是 `memory_job_submit` 顶层参数 |

### 8.3 Windows 补充

Windows 为实验性支持，本仓尚无 Windows 真机完整验收。PowerShell 示例：

```powershell
py -m venv "$env:USERPROFILE\.knowbase\runtime\venv"
& "$env:USERPROFILE\.knowbase\runtime\venv\Scripts\python.exe" -m pip install C:\下载目录\knowbase-0.2.0rc6-py3-none-any.whl
& "$env:USERPROFILE\.knowbase\runtime\venv\Scripts\python.exe" -I -m knowbase init
```

MCP `command` 改为 `Scripts\python.exe` 的绝对路径；JSON 反斜杠写成 `\\`。参数仍用数组。运行路径、用户配置入口和 Hook 支持按本机宿主确认。

### 8.4 相关文档与技术支持

- [项目介绍](project-introduction.md)：背景、功能、价值与当前状态。
- [宿主接线记录](../hosts/claude.md)、[ZCode](../hosts/zcode.md)、[Trae](../hosts/trae.md)：各宿主事件与配置细节。
- [价值事件协议](../value-events-protocol.md)、[消费与统计协议](../inflight-consumption-metrics.md)。
- 技术支持：部门 knowbase 维护者（安装包、数据仓权限、scope 命名、升级发布均由维护者统一出口）。

---

编写校验记录：2026-10-22 v2.0 按交付大纲重组章节，版本口径统一为 `0.2.0rc6` 与单版本运行环境 `~/.knowbase/runtime/venv`（替换旧版 `department-rc5` 多版本目录表述）。本次已核对：正文 JSON 配置块语法合法；引用的仓库内相对链接目标存在；16 个工具清单与 `server.py` 注册一致；本机运行时版本实测 `0.2.0rc6`。第 5 节验收链路沿用 2026-10-22 早前版本在隔离空库经正式 stdio MCP 入口的贯通记录，本次重组未重跑该链路；固定 wheel 安装、各宿主 UI 接线与跨机验收仍以实际执行为准。


## 程序自动升级

团队知识同步不会更新本机程序。rc9支持首次接入后的独立定时程序升级、稳定MCP连接切换与失败恢复；老电脑需要一次引导，见[团队无人值守升级](../program-auto-update.md)。
