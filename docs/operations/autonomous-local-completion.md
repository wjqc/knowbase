# 无公司网络依赖的能力补齐

2026-10-25。用户明确要求不验证公司内网，修复其余可独立实现的问题。以下区分建设完成与外部接入，不以测试替代真实模型、多人试点或Windows实机验收。

## 已实现的链路

`Hook/导入入队 → queued材料恢复 → 有授权且探针有效的执行器 → claim → 完整分页 → 文本推理 → 持久响应 → 候选校验/去重/CAS → writer → 卡片版本读回 → 后台同步`

- P2不再是恒定blocked占位。默认仍关闭；必须显式配置执行器、授权scope并通过隔离探针才可启用，不会将自动发现的公共服务作为默认依赖。
- 复用现有jobs、materials、materialize、writer和版本回执，不建立第二套知识权威。Markdown+Git及唯一固定runtime约束保持。
- 材料按批次和页完整读取；预算不足、清单缺失、输出未覆盖全部片段时不假完成。无价值片段允许no_value；业务规则成卡后仍按既有治理等待证据，不伪造helpful。
- 探针回执私有落盘，绑定配置与程序指纹；变更使旧回执失效。已授权且未改变的执行器，回执过期或暂时故障时可在有待办、预算允许时退避探针；恢复通过后续做暂时失败任务。空闲时不调用模型。
- claim/结果提交检查租约与fencing；长推理续租。模型响应先写本地运行账本，提交前崩溃可复用响应；已登记提交先恢复回执，不重复生成卡片。
- 默认每轮最多2批、单批120秒、单轮600秒、输入128000字符、输出64000字符，每日20次调用预留/512000输入字符额度。额度耗尽延期到下一UTC日，不消耗失败重试次数；未知或失败调用不退还调用预留，避免失控重试。参数可显式调整。

新增迁移29保存executor_runs与automation_budget；1–28的SQL不变。调用预留、真正启动、缓存重放分别记录；Provider响应中的用量与执行器自报用量分开，实际账单未知时仍为null。缓存不重复发射model.usage或重复计量；内网HTTP用量沿用最小价值事件渠道。

## 一次性接入

文本提炼与Embedding分开配置。内网文本适配器明确要求`openai_chat_completions`协议；真实公司网关协议仍需在可访问环境验证，不假设Embedding接口也支持文本生成。

私有JSON示例（地址与模型均为占位，禁止明文密钥）：

```json
{
  "kind": "intranet_text",
  "protocol": "openai_chat_completions",
  "executor_id": "company-text",
  "endpoint": "https://text.gateway.example.invalid/v1",
  "model": "company-approved-text",
  "allowed_hosts": ["text.gateway.example.invalid"],
  "secret_ref": "env:KNOWBASE_TEXT_API_KEY"
}
```

网关可访问且已有授权时，一条init命令完成接线、探针、限定范围启用与后台安装：

```bash
~/.knowbase/runtime/venv/bin/python -I -m knowbase init --non-interactive \
  --agents zcode,claude-code,trae \
  --executor-config /private/path/executor.json --verify-executor \
  --executor-scopes project-a --enable-automation --install-worker
```

也可分步`executor configure --config <file>`、`executor probe --isolation --id company-text`、`executor enable --id company-text --scopes project-a`；`executor disable`只停用，不丢弃材料或回执。MCP旧会话仍需重连。

推荐内网HTTP文本模式：只交换文本JSON，没有模型工具调用。命令执行器是对指定本地程序的显式信任，不是OS沙箱；后台默认不允许，启用须额外`--trust-command`（init为`--trust-executor-command`）。不能把该选项当作真实Agent工具隔离已经验证。真实宿主若无法限制工具，不能据此宣称满足受限执行生产验收。

TLS、host白名单、拒绝重定向、认证引用和有界传输复用已有适配内核；无公共服务回退。探针只在临时配置/知识库/工作目录执行，程序和模型数据不会通过探针写进真实知识库。

## 共享与维护修复

- v2按上限分包，使超过1000条的待发队列持续前进；导出使用配置团队标识，不固定为default。
- ACK要求远端原包、package_id、团队、发送安装身份、完整事件集合及逐事件哈希一致，不凭单条相同event_id就确认。
- v2拒绝嵌套payload旁路；不发送查询、知识正文或原始工具结果。
- 周期恢复现在包括写入intent、未结算候选回执，以及enqueue后崩溃留下的queued/preparing清单；单个坏材料不影响其他任务。
- 常驻worker与短周期team任务区分：macOS KeepAlive，Linux simple+Restart服务，Windows无限执行时限及失败重启。Windows仍需登录且缺实机验收；本地XML测试不替代实机。
- Windows MCP固定路径修正为Scripts/python.exe。未通过宿主能力验收的Hook支持不升级为已证实。
- 检索报告的分层采用统计改为正式delivery/use关联，不再拿search_id匹配使用事件自身event_id；同步error不会在sync_confirmed检查中显示pass。

## 历史身份恢复

有原始outbox或保留的v1原始事件包时，可离线重建一个全新的派生分析库：

```bash
~/.knowbase/runtime/venv/bin/python -I -m knowbase value rebuild-evidence \
  --output /private/path/recovered-analysis.db --team-id team-id \
  --package /private/path/original-peer-events.json
```

命令不会覆盖已有库、改写原outbox/ACK或切换当前聚合库；拒绝将分析库放进知识仓。仅有已被剥离身份的v2包不够，命令不会按时间/标签猜补。报告保留identity_still_missing，缺原始证据属于信息缺失，不是再跑试点就能恢复。

## 本机OCR

macOS缺Tesseract时，可使用系统Vision+Swift识别中英文图片，仍走原OcrParser/ParsedDocument契约。真实合成图片已识别出独立预期文本；无需安装ONNX/PyTorch或下载模型。其他平台继续沿用Tesseract与明确的缺依赖错误。

曾尝试Homebrew安装，但发现会升级16个系统依赖，已在安装前停止，未因此扩大系统依赖。Native OCR的可用性不代表对任意真实资料的识别质量已验收。

## 外部依赖仍未解决

公司网络/VPN/认证未验证、未修改；未向公司模型发探针。生产Embedding和文本执行器未凭空配置，P2默认不开启。真实执行器接入通过后才有条件运行生产自动沉淀；P5仍须真实成员/任务、独立相关性样本及同质量成本对照。Windows原生、真实文档OCR质量和缺原始证据的历史身份分别保留待验或不可恢复状态。

本轮最终源码回归：935 passed、1 skipped（123.40秒）；包含无真实模型、无公司网络的P2正式控制器及本地文本协议/证据/调度反例。
