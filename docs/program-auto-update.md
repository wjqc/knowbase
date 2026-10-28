# 团队电脑无人值守程序升级（rc9）

## 用户得到的行为

成员电脑首次接入后，独立于知识同步，每15分钟检查一次明确配置的程序 Git 分支。发现代码变化后，在临时目录构建与验证，等待本机调用空闲，备份数据与配置，然后在唯一运行环境原地安装。失败则退避；已开始安装的失败执行回滚，意外中断由下次独立更新任务恢复。整个流程不需要模型服务。

必须区分三条通道：

| 通道 | 内容 | 是否更新本机 Python 程序 |
|---|---|---|
| 程序源码仓库的指定分支 | `knowbase/`、`pyproject.toml` | 是，首次明确信任该源以后 |
| 团队知识仓库 main | Markdown 知识卡、README | 否 |
| 知识仓库 knowbase-stats | 设备统计与证据包 | 否 |

不能从知识仓库的 main 提交推断成员运行版本。统计 enroll 也不等于程序升级器 enroll。发布者只需测试后提交、推送**程序源码仓库**；不需要在同事电脑逐次执行安装。

## 老电脑首次接入一次

前提：成员自己的 Python≥3.10、Git、uv 已安装，能够以自己现有凭证读取程序源。程序源必须由管理员提供，不能填写知识卡仓库。下面命令在可信的新版程序源码目录执行；把 `<PROGRAM_GIT_URL>` 换为团队实际可访问的源码地址。

```bash
python scripts/bootstrap_auto_update.py --source <PROGRAM_GIT_URL> --branch main --agents zcode,trae
```

首次需要关闭旧的直连 knowbase MCP，再运行引导；完成后重连或新建 Agent 会话一次，让它启动稳定 MCP 入口。引导拒绝强杀成员正在进行的调用。macOS/Linux 可用 `python3`；Windows 用 `py -3`。`--agents` 支持已有接入器的 `claude-code,zcode,trae,codex,cursor,opencode`。省略时迁移已配置的宿主，不替成员创建全部宿主配置。

原本没有加入统计通道的成员，可以同时追加自己的、已确认的身份：

```bash
python scripts/bootstrap_auto_update.py --source <PROGRAM_GIT_URL> --agents zcode --member-id <自己的成员ID> --label <自己的姓名>
```

不猜测身份、不替其他成员确认 enroll，不重新生成已存在的设备/统计身份。显式 `--publisher` 才配置发布角色。现有 config、Markdown、SQLite 和统计发送积压保留。默认要求 HTTPS/SSH；旧内网 HTTP 源须显式 `--allow-insecure-source`，不降低 Git 的 TLS 校验。

已有**完整、发布态的 rc9 固定运行包**也可使用：

```bash
~/.knowbase/runtime/venv/bin/python -I -m knowbase init --non-interactive --auto-update --update-source <PROGRAM_GIT_URL> --install-worker
```

若安装态能与源上当前构建逐文件匹配，第一次定时检查建立来源记录；若安装态更旧、被手改或不一致，返回 `bootstrap_provenance_required`，使用上面的首次引导，不凭版本号猜测来源。

## 后续无人值守升级

- 唯一路径：macOS/Linux `~/.knowbase/runtime/venv/bin/python`；Windows 同目录 `venv/Scripts/python.exe`。
- 独立任务：macOS `com.knowbase.updater`，Linux 用户级 `knowbase-updater.timer/service`，Windows 用户级任务 `knowbase-updater`。不依赖可替换应用包成功导入才启动。
- 默认周期900秒，可配置300–86400秒。断网/认证失败/忙碌均保留当前包，指数退避到最多6小时；恢复后再次检查。关机期间不更新，登录后再运行；不承诺推送后立刻在线。
- MCP 配置使用同一个固定 Python 启动 `runtime/mcp_bridge.py`。代理保留宿主 stdio 连接，等待旧调用结束，在安装完成后重新启动新版后端并内部初始化；不自动重放断线中的写操作。
- Hook 使用固定 Python + `runtime/entry.py`。维护期不阻塞用户任务，不远程检索；只保存会话/显式 transcript 引用等元数据。后续 worker 按已有 scope 与会话绑定补采；缺 transcript 则记录真实 `capture_gap`，不伪造完整覆盖。
- 已加载的代理进程继续使用原有协议1实现。破坏该稳定协议需要显式迁移。Agent 缓存的启动规则/说明文本可能到**新会话**才刷新；后端新版工具运行和宿主新规则生效分别验收。

## 安全边界与失败行为

1. 代码来自首次授权的源与分支，沿已安装提交快进；改源须重新接入，强推/历史重写不会自动安装。TLS与Git凭证由系统现有配置提供，settings不保存密钥或带密码的URL。
2. Git只写独立裸缓存，不 pull/reset 成员开发工作区，不把知识仓库当可执行程序。仅Markdown变化的源码提交不重装。
3. 临时 wheel 校验 RECORD/内容指纹；模块版本与包元数据必须一致。协议门禁、Python兼容性、依赖集合、MCP旧工具参数契约、影子SQLite迁移、doctor与知识/台账校验全部先过关。
4. 后续更新不下载/替换第三方依赖，构建使用首次接入缓存。依赖变化或构建依赖缓存缺失会延后，明确报 `dependency_change_requires_bootstrap` 或构建失败；这类发布需要再次引导。不会无人值守引入新的本地模型/框架。
5. 两阶段维护屏障：先拒接新工作、排空旧调用，再停止后端。旧直连MCP、其他配置库、活跃提炼租约/作业和CLI活动未排空会延后。不要绕过稳定入口并发手工写库。
6. 在线SQLite备份和配置/知识快照置于私有 `~/.knowbase/backups/auto-update-*`。不保留旧wheel、构建目录或第二运行包；回滚从Git历史临时重建旧包。Git源码缓存不承担第二套运行环境。
7. 安装日志持久记录 `installing/migrating/committed/rolled_back`。进程被终止后，下次先恢复，再考虑联网。发现备份后新增知识或台账时，拒绝覆盖新数据并保留维护屏障，状态为 `rollback_would_overwrite_new_data`，需要管理员检查备份。首次bootstrap遭遇进程/断电中断仍需重跑引导，不能等同于已接入后的定时恢复。
8. 统计Git提交复用成员自己的显式 `team.commit_name/email`，否则取知识仓库/本机Git身份，最后才是合成身份。企业邮箱门禁下需确保真实Git身份，升级不替成员配置凭证、VPN或代签身份。

## 运维命令

```bash
~/.knowbase/runtime/venv/bin/python -I -m knowbase update status
~/.knowbase/runtime/venv/bin/python -I -m knowbase update check
~/.knowbase/runtime/venv/bin/python -I -m knowbase update run
~/.knowbase/runtime/venv/bin/python -I -m knowbase doctor
~/.knowbase/runtime/venv/bin/python -I -m knowbase update disable
```

`check`仅检查源，不装包；`run`跳过退避但不跳过验证/空闲门禁；`disable`停更新任务，不删除知识、成员身份或稳定MCP入口。升级恢复未完成时不能disable。`doctor`只读本机缓存，不为检查而访问公司网络。真实构建看 active/manifest、bridge的 `current_build` 以及实际工具回包，不只看配置文件或版本字符串。

## 代码落点与验收

| 文件 | 责任 |
|---|---|
| `knowbase/updates/engine.py` | 独立Git/构建/验证/备份/安装/日志恢复；复制为runtime/updater.py |
| `knowbase/updates/bridge.py` | MCP稳定连接、排空与后端切换 |
| `knowbase/updates/entry.py`、`recovery.py` | Hook维护入口、显式transcript补采/缺口 |
| `knowbase/updates/control.py` | 首次配置事务与独立OS任务 |
| `integration.py`、`__main__.py` | init/命令接入、宿主配置迁移 |
| `scripts/bootstrap_auto_update.py` | 老电脑一次接入，必要的首次依赖准备 |

本地两台隔离成员环境使用真实Git、wheel、原地安装、真实MCP SDK连接，验证正常更新、中断恢复和安装后失败回滚。覆盖旧调用排空、同连接进入新版、配置调度失败回滚、跨库阻断、新证据不覆盖和Hook缺口。Windows任务XML与Linux服务配置属于本地代码/描述符验证，实际Windows/Linux调度及各宿主UI连接行为仍待对应实机验证。公司源网络、其他成员首次接入、统计积压补报与跨成员收益不能由这些测试代替。

## Windows 后台黑窗口修复（2026-10-28）

旧任务直接启动 `python.exe`，登录/周期触发时可能出现空白控制台。新版任务改由**同一venv**的 `pythonw.exe` 启动，再以 `CREATE_NO_WINDOW` 调用固定的 `python.exe`；stdin关闭、stdout/stderr进入各自任务日志，子进程退出码返回任务调度器。Git、执行器、MCP代理后端及更新器子进程也使用无控制台标志。MCP宿主仍使用固定 `python.exe` 和原有stdio连接，不要直接把MCP解释器改为pythonw。

窗口抑制依据：[Microsoft CREATE_NO_WINDOW](https://learn.microsoft.com/en-us/windows/win32/procthread/process-creation-flags)。不是给任务打“隐藏”勾选框，也不绕过企业认证或安全提示。

同批修正：Windows安装依赖包含tzdata，Trae配置由APPDATA解析为 `%APPDATA%\Trae CN\User\mcp.json`，不再写macOS的Library目录。已经手工修正的真实Trae配置会按原接入事务保留和检查，不自动删除旧错误目录下的文件。

旧Windows电脑需要用新版源码重新运行一次引导，以安装tzdata并**重新注册已有任务**（仅更新Python文件不能改掉已注册的任务Action）。在程序源码目录执行：

```powershell
git pull --ff-only
py -3 scripts/bootstrap_auto_update.py --source https://github.com/wjqc/knowbase.git --branch main --agents trae
```

若使用公司镜像，替换为实际已授权的程序源。不要填知识仓库。依赖集合变更会按既有策略阻止无人值守安装；此次不能仅等待旧定时更新器。若旧MCP仍是直连入口，引导会要求先退出/重连一次。

接入后检查任务 `knowbase-worker` 和 `knowbase-updater` 的操作应指向 `~\.knowbase\runtime\venv\Scripts\pythonw.exe`；后台日志在配置的 `team.data_dir`（默认 `~\.knowbase\team`）下 `knowbase-worker.log`、`knowbase-updater.log`。必要的旧 `knowbase-team-stats` 任务也需用新安装器重新注册；统一worker安装器会清理旧统计调度。没有pythonw时安装器明确失败，不降回会弹窗的启动方式。

本地验证覆盖XML操作、参数/日志/退出码、旧新任务身份识别、Windows专用进程标志及Trae路径。当前开发环境为macOS；实际Windows桌面“不再弹窗”与任务运行结果仍需该电脑验证。
