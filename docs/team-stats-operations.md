# 团队统计接入与运维

适用条件：只有成员各自电脑，沿用现有内部共享 Git；没有统计服务器或新增模型服务。团队名单按登记发现，不限制三人。统计不等于所有未接入同事已被覆盖。

## 首次接入

先将当前源码原地安装到唯一运行环境。已有环境不另建版本目录，安装前在线备份 memory.db 和配置；升级失败从源码 Git 恢复代码后原地安装，不保留旧运行包。

macOS / Linux（在当前 knowbase 源码目录执行；Windows将解释器替换为固定的 `Scripts/python.exe`）：

```bash
uv pip install --python ~/.knowbase/runtime/venv/bin/python --offline --no-deps --reinstall .
~/.knowbase/runtime/venv/bin/python -I -m knowbase team enroll --member-id YOUR_STABLE_ID --label YOUR_NAME
~/.knowbase/runtime/venv/bin/python -I -m knowbase team sync
~/.knowbase/runtime/venv/bin/python -I -m knowbase team install-worker
```

`YOUR_STABLE_ID` 使用本人的稳定 ID，例如 zhaozk11、yizhuo；显示名可随后改，稳定 ID 不变。同一个人另一台电脑使用同一 member-id，各电脑会自动生成不同安装 ID。预登记 pending 的成员首次自确认后自动转 active。不要复制别人的 `~/.knowbase/team/`，也不要用复制数据库代替新设备登记。

`--offline --no-deps` 适用于已安装 knowbase 及其依赖的电脑；首次装机先按原 knowbase 安装说明安装依赖。路径、MCP/Hook 接入方式仍见 [宿主接入](hosts/latest-local.md)，不需要改成第二套 MCP。后台任务在 macOS 使用 LaunchAgent，在 Linux 使用 systemd user timer；Windows 已提供当前用户 Task Scheduler 安装器，固定使用 `~/.knowbase/runtime/venv/Scripts/python.exe`，不要求保存账户密码；仅登录后运行并恢复补报。Windows原生实机验收仍待目标电脑执行，不能把macOS上的XML/控制流程测试当作Windows已部署。

首次登记仅自动纳入登记后的记录。明确确认本库历史属于本人时，登记命令加 `--include-history --source-timezone Asia/Shanghai`，回补当前保留的历史；旧日覆盖为部分，首次新调用日志之前的主调用数为未知。不要把复制来的历史日志再次归给另一人。

本机 wenjian3 为指定 README 发布者，首次登记使用 `--publisher`。其他成员不要加此参数。ZCode / Claude Code / Trae 在本机共享同一个队列和后台任务，无须每个宿主分别安装采集器。MCP 旧连接重连后能记新宿主归属；未重连进程仍兼容原调用表，归属显示 unknown。

## 日常工作

- 登录后执行一次，每15分钟后台采集、补报和拉取；无统计变化不提交，当前日最多每6小时更新一次空闲心跳。
- Hook 不等 Git 网络。统计失败保留 pending；不会改变知识保存或已有知识 Git 同步结果。
- 默认看板 `~/knowbase-dashboard.html`，有团队快照后默认展示团队卡片，支持切换本机；已打开页面每60秒重新载入磁盘快照。
- README 的团队表格由指定发布机更新。它离线时 README 保留时间戳，其他人的上报和本地汇总照常运行。

```bash
~/.knowbase/runtime/venv/bin/python -I -m knowbase team status
~/.knowbase/runtime/venv/bin/python -I -m knowbase team collect  # 只采集，不联网
~/.knowbase/runtime/venv/bin/python -I -m knowbase team sync     # 立即采集/同步/重建页面
```

配置位于 `~/.knowbase/config.json` 的 `team`：`enabled`、`data_dir`（默认 `~/.knowbase/team`）、`interval_seconds`（默认900，安装器下限900）、`dashboard_output`。修改定时间隔后重跑 `install-worker`。`git.allowed_remote_prefixes` 默认仅允许内部服务器；不自动向公共 Git 上传。

## 成员变化和角色转交

预登记可以增加未安装成员的分母；只有其本人在自己的电脑登记，才会成为 active。不能把预登记当作已收到数据。

```bash
# 指定发布者预登记
~/.knowbase/runtime/venv/bin/python -I -m knowbase team member --member-id NEW_ID --label NEW_NAME --status pending
# 本人改显示名，稳定 ID 保持不变
~/.knowbase/runtime/venv/bin/python -I -m knowbase team member --member-id MY_ID --label NEW_NAME --status active
# 指定发布者确认退出，历史日桶和使用量仍保留
~/.knowbase/runtime/venv/bin/python -I -m knowbase team member --member-id LEAVING_ID --label NAME --status retired
```

退出记录包含 retired_at。当前覆盖按当前名册算，历史整日窗口按当时有效区间计算。首版不支持同一个 retired ID 静默复活为第二段任期；需先迁移成员有效区间历史协议，不能覆盖旧任期后假称历史覆盖完整。

README 转交：原发布机先执行 `team publisher` 关闭角色，再在接手机执行 `team publisher --enable`，最后同步。首版无自动选主；仓库写权限及明确角色转交是受信任团队模式的前提，不是中心身份认证。

## 数据目录与恢复

`~/.knowbase/team/` 是本机统计数据，不含第二套 knowbase 运行包：

- `state.db`：独立安装身份/epoch、待发送快照/哈希/ACK、已接收摘要与恢复证据。
- `stats/data.git`：仅统计分支的 bare 数据工作区。
- `readme/data.git`：默认知识分支的独立 bare 数据工作区，供库存读取和指定发布者更新 README。
- `snapshot.json`：脱敏团队投影；备份、工作日志保留在本机。

统计分支为 `knowbase-stats`，路径为 `members/<member_id>.json` 与 `devices/<installation_id>/<db_epoch>/<北京时间日期>.json`。每台电脑只写自己的设备日桶，成员登记有单调序列。抢推时最多重试3次，重新取远端树后只重放拥有的文件，不 force push，不对现有知识工作区 checkout/rebase/reset。

后台成功仅在重新读取远端路径、摘要哈希一致后记 ACK。发出后崩溃，下一次仍会读回确认。接收方拒绝倒退序列、同序列不同内容、错误路径/团队、私密字段、畸形计数和超限文件。

库被恢复或来源不符时停止推进采集；先核实恢复的是本设备库，再显式执行：

```bash
~/.knowbase/runtime/venv/bin/python -I -m knowbase team recover --confirm-local-database
~/.knowbase/runtime/venv/bin/python -I -m knowbase team sync
```

恢复前再次在线备份，不删除旧日桶。重叠 epoch 不相加：已有接收端保留此前确认的 epoch 观测下界；新接收端没有可确认基线时保留未知，标记恢复待核对。首版不自动“补齐”缺失历史。

README 仅替换 `knowbase:team-stats:start/end` 标记区域，区域有内容哈希。人工改写区域、标记不完整或抢推重试耗尽时跳过并记录错误；区域外人工内容保留。不要手改自动区域，需要改统计口径时修改生成器。

停止后台：`team remove-worker`；完全暂停自动统计并保留数据：`team disable`。手工 `team sync` 仍需 enabled=true。macOS 任务标签为 `com.knowbase.team-stats`，stdout/stderr 在本机统计目录。

## 数字怎么读

调用是 `command_calls` 实际尝试，含 Hook、重试和失败；历史 `usage_log` 单列。命中率是所有已知命中数/已知检索数，不平均成员百分比。知识库存从共享默认分支固定 revision 数一次，不把各设备副本相加；本机未同步卡不算已共享。

Token 仅汇总可用请求日志，优先本机 Claude transcript 请求，避免再加其镜像 model.usage。缺失字段保留 null，缓存单列；ZCode/Trae 无可用合法来源时明确未采集。Claude 来源包含本机其他 Claude 任务，不能全归因于 knowbase。无同模型/同任务配对对照时，净节省仍为待测。

有效反馈、采纳、晋升与失效是日回执/事件次数，不能冒充跨日独立受益任务。当前日未结束，已收到 X/N 只覆盖已登记成员；缺成员/恢复待核对/过期均要结合时间水位看。

## 验证边界

源码定向及完整回归、临时 Git 的多设备并发/离线补报、浏览器卡片/缩放、固定运行包正式 stdio、本机真实远端上传分别记录。其他成员电脑必须各自执行一次接入；没有它们的真实日志，不能宣称完成所有人的统计或节省量测量。

## rc8收口（2026-10-24）

team-stats与自适应检索组合发布；Windows安装器已补，macOS/systemd安装失败恢复原描述文件。安装操作备份配置，未知Task Scheduler同名任务不覆盖；任务本机单实例、15分钟默认频率。Windows规范参考[Task Scheduler Schema](https://learn.microsoft.com/en-us/windows/win32/taskschd/task-scheduler-schema)及[schtasks](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/schtasks-create)。

本机升级、统计远端读回和宿主生效证据见本轮交付记录；多成员真实设备仍逐机验收。退出后第二段任期保持明确拒绝，属于v1协议边界，不静默复活或覆盖历史。真实净Token收益继续待对照，不把调用总量当节省。

### 设备绑定升级与恢复

设备指纹现采用 macOS IOPlatformUUID、Windows MachineGuid 或 Linux machine-id 的本地哈希，不随 DHCP 主机名或网卡变化，也不上传原始标识。旧版指纹不自动接受，以免复制目录后复用同一发送者。

确认这仍是原本机数据库、安装身份匹配后，执行 `knowbase team rebind-device --confirm-local-device`。命令先私有备份统计数据库，再更新设备绑定；不改变成员、installation_id、统计 epoch、待发送文件或 ACK。若数据库身份不同则拒绝。复制到另一台电脑应正常 enroll 新设备，不能用 rebind 冒充原设备。重装系统或克隆 machine-id 仍需单独核查；本协议不提供中心设备认证。

Trae CN 的 MCP 服务位于根对象 `mcpServers`，接入向导在该对象内增量合并，不创建根层同名影子服务。
