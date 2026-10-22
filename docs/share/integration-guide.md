# 薪火 knowbase 项目接入文档

> 部门内部分享版｜2026-10-22  
> 面向首次接入的研发、测试同事及团队维护者。按当前工作区 `0.2.0rc5` 编写；本文不表示该工作区全部改动已发布。安装前向维护者领取对应安装包和内网经验库地址。

了解项目用途可先读《[项目介绍](project-introduction.md)》。首次接入依次完成第 2～7 节；Hook、团队统计和升级恢复按需阅读。

## 1. 接入后要达到什么结果

第一次接入完成以下事项即可：

1. 本机能运行 knowbase，初始化配置和经验数据仓。
2. AI 宿主能发现并调用 MCP 工具。
3. 在隔离库保存一张示例卡，并用同一 scope 检索、读回。
4. 用真实返回的版本和回执记录一次使用与任务结果。
5. 加入团队库时，能确认已同步的知识在另一台机器上可见。

先完成 MCP 主动调用，再增加 Hook 自动化。完整链路跑通前，接入状态记为试用或辅助模式。

## 2. 前置条件和路径约定

需要 Python ≥ 3.10、Git，以及使用 MCP 的 AI 宿主。安装可用 `uv` 或 Python 自带的 `venv`。本文命令以 macOS / Linux 为主，Windows 见第 12 节。

先向维护者确认：安装包及版本、内网数据仓 URL 与分支、个人 Git 读写权限、项目 scope 名称。仓库地址、版本和本机绝对路径均按实际值填写。

| 名称 | 本文示例 | 用途 |
|---|---|---|
| 程序源码目录 | `~/work/knowbase` | 开发、构建和测试 |
| 固定运行环境 | `~/.knowbase/runtime/department-rc5` | 宿主启动 MCP / Hook 所用的 Python |
| 个人经验数据仓 | `~/knowbase` | Markdown 经验卡、source 与本机数据库 |
| 团队经验数据仓 | `~/knowbase-team` | 内网共享仓在本机的克隆 |
| 配置文件 | `~/.knowbase/config.json` | 所有宿主共同使用 |

程序源码和经验数据仓分开；固定运行环境只安装程序包，不是第二套业务库。

## 3. 安装并初始化

### 3.1 部门试用：安装维护者提供的固定包

以下文件名为示例，换成实际收到的 wheel。该目录用于首次安装；升级时新建版本目录，保留旧环境。

```bash
python3 --version
git --version
python3 -m venv "$HOME/.knowbase/runtime/department-rc5"
"$HOME/.knowbase/runtime/department-rc5/bin/python" -m pip install /实际下载目录/knowbase-0.2.0rc5-py3-none-any.whl
"$HOME/.knowbase/runtime/department-rc5/bin/python" -I -c 'import knowbase; print(knowbase.__version__)'
```

记录打印的实际版本。`-I` 防止宿主工作目录里的同名源码覆盖已安装的包。

若部门尚未提供安装包，维护者可在确认过的源码版本构建，不能把本地未发布功能当作已有发行包：

```bash
cd "$HOME/work/knowbase"
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/python -m pip wheel --no-deps --wheel-dir dist .
```

这里使用普通安装。源码改动后需重新安装或重新构建；正式接线统一指向固定运行环境。

### 3.2 初始化空库

先确认 Git 已配置提交者身份；有身份信息可跳过设置命令。

```bash
git var GIT_AUTHOR_IDENT
# 仅在尚未配置时，填写你自己的信息：
# git config --global user.name "你的姓名"
# git config --global user.email "你的公司邮箱"

"$HOME/.knowbase/runtime/department-rc5/bin/python" -I -m knowbase init
"$HOME/.knowbase/runtime/department-rc5/bin/python" -I -m knowbase doctor --json
```

`init` 会创建默认配置 `~/.knowbase/config.json` 和默认数据目录 `~/knowbase`，建立 Git、卡片目录和索引。已有配置不会被覆盖。若机器已接入其他版本，先按第 11 节备份和升级。

首次空库有 0 张卡是正常结果；不应要求一定搜到某个维护者专属编号。`doctor` 有告警时按具体检查项处理；未配置共享远端不等于 MCP 安装失败。

## 4. 选择个人使用或团队共享

### 4.1 个人使用

保留默认 `repo_path`，本机所有宿主使用同一份配置。默认自动 Git 提交开启、自动推送关闭。

### 4.2 加入团队共享库

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
"$HOME/.knowbase/runtime/department-rc5/bin/python" -I -m knowbase init
"$HOME/.knowbase/runtime/department-rc5/bin/python" -I -m knowbase stats
```

初次先保留 `auto_push=false`；确认内网远端、权限、分支及同步验收后，需要写后自动共享的成员再开启 `auto_push=true`。

自动推送关闭时，只在工作区干净、已确认没有冲突的情况下，人工执行：

```bash
git -C "$HOME/knowbase-team" status --short
git -C "$HOME/knowbase-team" pull --rebase
git -C "$HOME/knowbase-team" push
```

共享数据使用内网远端，不能把经验数据仓推到程序代码的公开远端。推送失败时保留本地提交，排除网络或内容冲突后再同步；以另一台机器拉取并检索到卡片作为共享成功的证据。

## 5. 在 AI 宿主注册 MCP

### 5.1 Claude Code

将 `knowbase` 条目合并到用户级 `~/.claude.json` 的 `mcpServers` 中，保留其他服务：

```json
{
  "mcpServers": {
    "knowbase": {
      "command": "/你的用户目录/.knowbase/runtime/department-rc5/bin/python",
      "args": ["-I", "-m", "knowbase", "serve"],
      "env": {"KNOWBASE_AGENT_NAME": "claude-code-你的姓名缩写"}
    }
  }
}
```

### 5.2 ZCode

将条目合并到用户级 `~/.zcode/cli/config.json` 的 `mcp.servers` 中：

```json
{
  "mcp": {
    "servers": {
      "knowbase": {
        "type": "stdio",
        "command": "/你的用户目录/.knowbase/runtime/department-rc5/bin/python",
        "args": ["-I", "-m", "knowbase", "serve"],
        "env": {"KNOWBASE_AGENT_NAME": "zcode-你的姓名缩写"}
      }
    }
  }
}
```

### 5.3 Trae CN

macOS 将与 Claude 示例相同结构的 `mcpServers.knowbase` 条目合并到：

```text
~/Library/Application Support/Trae CN/User/mcp.json
```

把来源标签改成 `trae-你的姓名缩写`。其他平台通过 Trae 的 MCP 设置入口编辑用户配置，并按第 6 节增加使用规则。

当前源码包含 Trae Hook 适配；本仓真实整链验收尚未完成，首次接入先使用 MCP 主动工作流，不照搬 Claude / ZCode Hook 格式。

### 5.4 其他 MCP 宿主

在宿主支持的本地 stdio MCP 配置中使用同一个绝对 Python 路径，参数为 `-I -m knowbase serve`，并设置独立来源标签。具体配置文件格式依宿主而定，不能直接照搬上面的 JSON 外层结构。

完成后按宿主提供的方式重新加载 MCP，必要时重启宿主或新建会话。`serve` 由客户端启动并通过 stdio 通信，不需要 HTTP 端口；在普通终端直接运行时等待输入是正常现象。

`KNOWBASE_AGENT_NAME` 用于来源归因，不认证人员身份。多宿主若需不同的测试配置，可在 `env` 中设置 `KNOWBASE_CONFIG`；测试完成后恢复，不要误连其他数据库。

## 6. 增加 Agent 使用规则

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

## 7. 首次验收：从空库跑通一次

### 7.1 先隔离测试数据

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
  "$HOME/.knowbase/runtime/department-rc5/bin/python" -I -m knowbase init
```

在宿主 `knowbase.env` 中临时添加 `KNOWBASE_CONFIG`，值为该 JSON 的绝对路径，再重连 MCP。切换前记录原配置；演示结束恢复原接线并重连。

### 7.2 发现工具

让 Agent 列出 knowbase 提供的工具，至少确认：

```text
memory_search / memory_read / memory_save / memory_context
memory_use / memory_feedback / memory_job_material / memory_metrics
```

当前源码共注册 16 个工具。若缺少 `memory_use` 或 `memory_metrics`，先核对实际 Python 路径与版本，再重连，不能用修改源码代替服务升级。

### 7.3 保存一张示例卡

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

### 7.4 检索、使用和反馈

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

**验收通过：** 发现工具 → 保存 → 默认检索命中 → 全文读回 → context 递送 → use 回执 → 任务结果反馈，全部走宿主 MCP，无错误响应。一次演示不满足默认自动晋升阈值，也不代表跨成员复用或收益验收。

## 8. 可选：增加 Hook 自动化

MCP 是读写接口，Hook 是宿主事件触发器。先完成第 7 节，再按宿主支持的格式增加 Hook，保持其他处理器不变。

### 8.1 Claude Code 基础三事件

将下面 `hooks` 的各事件合并到 `~/.claude/settings.json`。Python 路径必须与 MCP 同版本；这里用字符串内引号处理路径空格。

```json
{
  "hooks": {
    "SessionStart": [{"hooks": [{"type": "command", "command": "\"/你的用户目录/.knowbase/runtime/department-rc5/bin/python\" -I -m knowbase hook session-start --style claude", "timeout": 5}]}],
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "\"/你的用户目录/.knowbase/runtime/department-rc5/bin/python\" -I -m knowbase hook user-prompt --style claude", "timeout": 5}]}],
    "Stop": [{"hooks": [{"type": "command", "command": "\"/你的用户目录/.knowbase/runtime/department-rc5/bin/python\" -I -m knowbase hook stop --style claude", "timeout": 5}]}]
  }
}
```

### 8.2 ZCode 基础接线

在 `~/.zcode/cli/config.json` 的 `hooks.events` 合并事件，并设置 `hooks.enabled=true`。单个事件示例：

```json
{
  "hooks": {
    "enabled": true,
    "events": {
      "SessionStart": [{"hooks": [{"type": "process", "command": "/你的用户目录/.knowbase/runtime/department-rc5/bin/python", "args": ["-I", "-m", "knowbase", "hook", "session-start", "--style", "zcode"], "timeoutMs": 5000}]}]
    }
  }
}
```

按同样结构补充 `UserPromptSubmit → user-prompt`、`Stop → stop`。执行中工具事件参考 [ZCode 接线](../hosts/zcode.md)；本仓记录的 ZCode 版本不支持 `PreCompact`，不要添加该事件。

### 8.3 执行中检索与材料观察

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

## 9. 日常使用与治理

### 9.1 常用操作

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

### 9.2 自动演进的当前默认值

`config.evolution` 默认开启；当前源码要求版本绑定和独立任务证据。普通新卡默认 3 个已完成任务、至少 2 个会话的正向证据可晋升；业务规则候选还需要出处及允许自动激活的配置。标准和偏好继续人工治理。

业务规则候选通过 `memory_context.validation_candidates` 获取验证提示；用关联身份的 `memory_read` 取得 `read_use_id`，实际验证后连同版本和 `task_result` 反馈。它是验证候选，不能先当生效规则采纳。

如试点要求业务规则全部人工激活，可将 `evolution.auto_activate_bizrules` 设为 `false`；这不等于关闭普通经验的反馈治理。正文变更后旧验证不沿用，负面证据会影响默认检索资格。

## 10. 看板和团队统计

先用本机 `dashboard` 看汇总。团队统计默认不自动上传，`value.events_enabled` 默认关闭；需要试点采集时，由维护者统一约定开启时间、统计窗口和成员登记方式。

汇总流程为：源端 `value export` → 汇总端 `value import` → 源端 `value receipt` 核对回执 → 汇总端 `value snapshot` / `value report`。具体参数见 [价值事件协议](../value-events-protocol.md)。

导出成功不等于汇总端已接收；工具标签不等于人数。`memory_metrics` 提供本机最小化统计，缺失值保持未知。Claude 请求用量可采集，ZCode / Trae 对应入口尚不支持；没有对照数据就不填写净节省比例。

## 11. 升级、备份与恢复

升级前暂停本机 knowbase 写入，记录运行版本、配置和数据仓 Git revision。备份内容包括：配置、Markdown / source、数据库及 `.knowbase/local` 等本地材料。

数据库使用 SQLite backup API，不能只复制正在写入的 `memory.db` 主文件。下面是独立备份示例，替换路径后执行：

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

把新包安装到新版本目录，MCP / Hook 同时改指向新环境，显式执行初始化或升级检查并重连宿主，再在隔离库复验第 7 节。旧服务不会因为磁盘文件变更自动升级。

出问题时先关闭新机制、恢复原接线并保留当前数据。回退旧代码前核对数据库兼容性；若必须恢复备份，先保存升级后的新卡、回执和未完成材料，明确损失窗口。**不以删库重建作为常规恢复操作。**

## 12. Windows 补充

Windows 为实验性支持，本仓尚无 Windows 真机完整验收。PowerShell 示例：

```powershell
py -m venv "$env:USERPROFILE\.knowbase\runtime\department-rc5"
& "$env:USERPROFILE\.knowbase\runtime\department-rc5\Scripts\python.exe" -m pip install C:\下载目录\knowbase-0.2.0rc5-py3-none-any.whl
& "$env:USERPROFILE\.knowbase\runtime\department-rc5\Scripts\python.exe" -I -m knowbase init
```

MCP `command` 改为 `Scripts\\python.exe` 的绝对路径；JSON 反斜杠写成 `\\`。参数仍用数组。运行路径、用户配置入口和 Hook 支持按本机宿主确认。

## 13. 常见问题

| 现象 | 优先检查 |
|---|---|
| MCP 没有工具或启动失败 | Python 绝对路径、安装版本、JSON 外层位置；宿主是否重连 |
| 报 `No module named knowbase` | 该 Python 环境是否安装包；是否只在源码目录偶然能运行 |
| `serve` 在终端一直等待 | 它是 stdio 服务，应由 MCP 客户端启动 |
| 空库检索没有结果 | 先完成示例卡保存，或确认团队库已克隆并指向正确 repo_path |
| 当前项目无法解析 scope | 显式传 scope，或补充 scope_map；不要回退为全库查询 |
| 主动检索能找到卡，Hook 没注入 | 是否还是 once；Hook 默认只注入 verified；再核对事件实际触发 |
| 保存被 lint 拦截 | 八项正文是否具体；是否含明文凭据、本机路径、仓外文档引用 |
| source 已导入但搜不到经验 | source 默认不进知识检索，需要提炼并读回实际卡片 |
| 反馈成功但没有晋升 | 是否绑定版本、use_id、任务结果；是否同一任务重试或尚未达到阈值 |
| `CANDIDATE_NOT_DELIVERED` | use 是否指向实际返回该卡的 context 事件和内容版本 |
| `REVISION_CONFLICT` | 重新检索或读回当前版本，重新核对；不要替换哈希强行追认旧结果 |
| 自己能读到，同事搜不到 | 推送、拉取、scope、staging/stale 状态及对方运行版本分别检查 |
| 网络失败或 Git 冲突 | 看真实同步告警；本地保存可能已成功，先保留提交再处理 |
| 看板没有 Token 或节省数据 | 采集来源可能不支持或未开启；没有对照时保持未知 |

## 14. 接入交付清单

- [ ] 记录了固定安装包版本和 Python 路径。
- [ ] 程序目录、经验数据仓、配置位置清楚，已设置正确 scope。
- [ ] 至少一个宿主完成工具发现与第 7 节真实 MCP 验收。
- [ ] 配置变更合并保留了其他服务，演示结束恢复正式接线。
- [ ] 团队库完成另一台机器的拉取与检索读回。
- [ ] Hook 如已启用，另留真实触发、模型消费和成卡读回证据。
- [ ] 已明确谁审核标准 / 偏好，是否允许业务规则自动激活。
- [ ] 已准备数据库和文件备份，不把本机计数当全团队收益。

完整真实业务验收继续使用 [三宿主样本协议](../team-value-real-acceptance.md)。本文的接入演示验证工具链路，不替代三人跨机试点与提效评估。

编写校验记录：2026-10-22，配置示例的 10 个 JSON 块及本地文档链接检查通过；第 7 节示例卡与 context/use/feedback 参数在临时空库中经正式 stdio MCP 入口贯通，重复反馈返回幂等恢复。该校验使用当前工作区源码，未替代固定 wheel 安装、各宿主 UI 接线和跨机验收。
