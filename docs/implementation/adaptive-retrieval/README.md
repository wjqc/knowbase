# 自适应混合检索：剩余工程实施交付

日期：2026-10-24。工作区：`/Users/qc/.codex/worktrees/knowbase-local-retrieval/knowbase`；分支：`codex/knowbase-local-retrieval`。

**代码已在隔离工作区实现并接线；本轮未安装生产、未修改真实库或宿主配置。主干提交状态见文末发布边界。** 真实公司模型的增益、真实宿主会话和团队净收益仍按原验收条件判定。没有以合成模型测试替代业务质量验收。

## 复用 ZCode 成果

已核对 `phase2-retrieval-l3` 的 `ed76fb2` / `cce7aeb`：继承其 OpenAI 兼容适配、Query 前缀配置、Canary、RRF 与增量重嵌思路，保留历史 Qwen3-Embedding-4B（2560维）实验和缓存。源码差异与复用边界见 [zcode-reuse.md](zcode-reuse.md)。

历史记录显示真实向量实验尚无检索增益证据，当前运行配置也未启用 embedding；因此没有重复索取网关资料或重复调用实网模型。本轮只对本机合成 HTTP 网关测试。历史别名指纹缺少新契约所需完整身份/预处理清单，不与新向量空间混用，不伪报新版实网验收。

## 已实现与正式调用

| 用户结果 | 代码与接线 | 验证范围 |
|---|---|---|
| 无模型完整可用 | [index.py](../../../knowbase/index.py)、[retrieval_policy.py](../../../knowbase/retrieval_policy.py) 继续L1/L2；只有主动MCP显式来源才尝试L3 | 无配置、配置异常、零结果和既有回归 |
| 内网协议适配 | [embedding_provider.py](../../../knowbase/embedding_provider.py)：单条/批量、模型发现、严格index/数量/有限数/维度检查、归一化、错误分类、Retry-After | 合成HTTP、倒序响应、重复/缺失index、NaN/零向量/不同维度、401/403/404/429/500 |
| 安全认证与有界调用 | 明确host白名单，无公共默认/重定向/环境代理回退；TLS默认验证；env或0600私有文件引用；不记录密钥/服务原始错误body；全调用截止时间+有界后台transport槽 | 慢响应返回本地；错误不回显；不安全文件/symlink/FIFO拒绝；文件凭据轮换后重探 |
| 真模型身份 | root/租户/model/部署声明/维度/Query与Doc前缀/分块策略/归一化共同绑定；12条Canary，首次三次测量数值噪声 | 同维漂移、身份变化；Canary只作为检测，不声称证明别名不可变 |
| 索引增量与切换 | [semantic_index.py](../../../knowbase/semantic_index.py) 使用原embedding_index与jobs：影子代际、内容与版本指纹、完整分块、未变内容复用、CAS回写、100%当前覆盖才激活、事务切指针、保留前代与可恢复任务 | 部分构建/腐坏/迟到更新、旧路径清理、切指针失败回滚、配置切换拒绝旧代 |
| 故障降级与恢复升级 | [retrieval_state.py](../../../knowbase/retrieval_state.py)：SQLite共享熔断、单半开探针租约、指数退避与抖动、限流尊重Retry-After、认证低频重试；重启不清状态；server生命周期30秒维护tick | 跨连接状态、并发仅一个探针、429与401恢复、就绪索引在评测凭据到达后自动激活 |
| 自动Hook不触网 | Hook/inflight来源不能进入dense；MCP服务维护线程只由serve启动，不由Hook启动 | 正式Hook与自动context的HTTP调用计数不增长 |
| 查询与来源一致 | Markdown原文与entity_heads/治理状态读回校验；远程调用期间SQLite版本变化，重新走本地；context递送再核对检索版本；覆盖缓存绑定模型、代际、凭据、策略版本和恢复窗口 | 不重放旧topic、文件未重建/畸形文件、并发更新返回新标题；旧结果不冒充新版本 |
| init一站式流程 | [integration.py](../../../knowbase/integration.py)、[model_setup.py](../../../knowbase/model_setup.py)、CLI：六宿主发现/配置、模型配置/发现/合成连接测试、备份/幂等/CAS/回滚；GUI可安全输入到本机私有凭据文件；原配置其他字段保留 | 原生JSON、TOML、JSONC；注释保留；受控CLI；密钥不进JSON/输出/journal |
| 区分配置、协议和会话 | [build_identity.py](../../../knowbase/build_identity.py) 内容build标记，`--verify-mcp`在临时空库验证固定runtime的stdio及build | 来源stdio探针通过；真实宿主仍须重连/信任，旧运行包会明确报build不匹配 |
| doctor与报告 | [doctor.py](../../../knowbase/doctor.py)、[retrieval_metrics.py](../../../knowbase/retrieval_metrics.py)：只读默认诊断、实际最近等级、模型/索引状态与覆盖、近期原因、采纳回执与完成后helpful关联、估计Token和实际费用分开 | 正式CLI只读、库字节/日志对账、无对照时净节省为null；绑定实际结果的采纳链补测 |
| 五组可复现工具 | [retrieval_evaluation.py](../../../knowbase/retrieval_evaluation.py)：批准输入的模型缓存、身份与checksum、A旧源独立进程/B词法/C真实dense/D混合/E门控、Recall@5/P@3/MRR@10/拒绝/无关率、按split与配对区间 | 合成60条夹具证明五组执行；缺人工qrels或缓存身份返回NOT_RUN；真实质量不据此PASS |

## 存储兼容性决策

新增迁移22，已提交迁移1–20及command_calls位置布局保持；未收口迁移21留在本地工作区，22保留编号以兼容已有21账本的本机库。本次未对真实库执行迁移。既有库从v8升级与日志保留有回归测试。

采用 `embedding_index.memory_id = generation_id + ':' + card_id`，新增generation_id/entity_id/版本/维度/checksum列。沿用原单主键，避免复合主键破坏旧upsert。旧dense仅接受合法卡ID，因而无法把代际键与字符哈希查询比较；旧rebuild可能删除真向量，此时新代码检测缺失并回本地、再构建。详细决策见 [ADR-0004](../../adr/0004-compatible-vector-generations.md)。

切换/回滚只改派生向量与指针，不恢复整库旧备份，不删除usage/feedback/uses/jobs。active、前代和最新ready的完成向量保留有界；在建任务保留续作信息。维护线程复用同库jobs，不新增常驻服务、向量库、ONNX、PyTorch或另一个运行环境。

## 模型质量门禁

`enabled=true`只代表允许内网能力。L3激活还要求：部署声明与模型身份相容、Canary通过、当前内容向量完整、冻结评测报告的模型/代码/覆盖scope匹配、人工审核条数和质量/拒绝/回归/延迟门禁通过。输入报告属于本机配置操作者的信任边界；程序验证结构及绑定关系，不靠一个布尔开关或HTTP 200推定业务质量，也不认证JSON中填写的审核人真实身份。

未通过时可生成ready影子索引，主动检索仍走L1/L2；既有合格组合恢复后自动升级，新增/漂移的模型不会复用旧批准。Canary无法排除所有路由漂移，仍依赖公司固定部署/变更契约。历史45条探索标注及匿名/隐含负例不自动变成新的完整人工金标准。

## 使用入口（合入并安装后）

```bash
knowbase init
knowbase init --agents claude-code,zcode,trae,codex,cursor,opencode --non-interactive --plan
knowbase init --non-interactive --agents claude-code --embedding-config company-embedding.json --probe-embedding --verify-mcp
knowbase doctor --json
knowbase doctor --json --probe-embedding
knowbase reindex --embed --deadline-seconds 60
knowbase embedding-work --deadline-seconds 60
knowbase embedding-activate GENERATION_ID
knowbase retrieval-report
```

上述新参数尚未安装到当前固定runtime。源码测试使用现有开发解释器和临时HOME，不通过`pip install`建立另一运行版本。可选配置示例：

```json
{
  "enabled": true,
  "api_root": "https://embedding.company.invalid/v1",
  "model": "company-approved-model",
  "allowed_hosts": ["embedding.company.invalid"],
  "allowed_scopes": ["global", "orders"],
  "auth_kind": "bearer",
  "api_key_env": "COMPANY_EMBED_API_KEY",
  "deployment_id": "company-confirmed-deployment",
  "query_prefix": "",
  "document_prefix": "",
  "auto_index": true
}
```

使用ZCode已实测的Qwen配置时，将其Query指令前缀显式写入query_prefix；不要把旧缓存的`#v2`字符串当作完整版本身份。无密钥时可跳过模型；GUI宿主不能继承shell变量时，交互向导可创建私有凭据文件，配置只保存file引用，不将密钥写入MCP env或Git。非交互配置不接受api_key等明文秘密字段。

`retrieval-cache --dataset FILE --output FILE`显式缓存已批准可发送的评测输入；`retrieval-evaluate --dataset FILE --cache FILE --baseline-source DIR --dense-min N --gate-dense-min N --output FILE`只做离线重放。输出区间当前为按query配对bootstrap，正式发布还应按任务簇重采样；离线计算延迟不当作在线p95。真实账单、费用和净Token收益无来源/对照时保持null。

## 宿主差异与边界

Claude/ZCode/Trae安装各自支持的生命周期/工具事件；ZCode不添加PreCompact，Trae不添加不支持的独立失败事件。Codex使用TOML MCP与独立hooks.json，保留宿主Hook信任流程；Cursor/OpenCode目前接主动MCP，不臆造兼容Claude的自动Hook。

原生格式依据已核对：[Codex MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)、[Codex Hooks](https://learn.chatgpt.com/docs/hooks)、[Cursor MCP](https://cursor.com/docs/mcp)、[OpenCode MCP](https://opencode.ai/docs/mcp-servers/)。本机应用版本仅作只读元数据发现，不等于原生事件触发验收。Trae原生开关/重复导入Claude、Codex信任、GUI凭据访问与真实会话加载仍须在宿主验证。

## 验证与尚未验证

最终全量 **724 passed、2 skipped**（88.49秒）；冻结SQLite的must **8/8**、gap **4/4**。跳过项为外部OCR依赖及隔离HOME下的实库golden，后者另以冻结副本补跑。以上不证明真实模型质量或团队收益。

详细数字与执行范围见 [validation.json](validation.json)、[发布验证](publish-validation.json)。补丁相对于本任务最初开工快照，只包含本任务改动，不包含父工作区未收口team-stats等变更。本次提交已排除未收口team CLI、迁移21、统计适配和私有评测原文；迁移22保留编号以兼容已存在迁移21的本机库。

- 已实现、已接线、局部验证：本页矩阵中的工程功能，合成HTTP、正式CLI/Hook/MCP stdio、迁移与恢复反例。
- 外部待验：新版真实内网模型/稳定部署契约、真实标注集五组终判、实际在线p95、宿主新会话/成员接入、任务质量与净Token收益。
- 历史实网证据：保留ZCode记录及其阴性结论，不作为本轮新版Provider/索引/策略已验收。
- 生产状态：未安装、未合入main、未push；未修改真实配置、知识数据或team-stats实现。


## main 发布边界

本次按用户要求提交并推送独立检索/接入功能及必要的Hook降噪依赖。上文隔离验证记录保留为历史证据；正式提交版本另在干净源码目录验证，结果见 publish-validation.json。生产runtime、模型开关、宿主配置与真实知识库不随Git推送自动升级。

独立提交范围回归：**689 passed、2 skipped**（77.81秒）。原724项为包含开工未提交依赖的历史隔离验证，不能替代本次干净提交验证；本次提交结果见 publish-validation.json。


## rc8组合发布

2026-10-24按用户授权收口team-stats，与检索升级组成rc8；迁移21现纳入源码，与22保持原定义。该独立检索提交的689项和历史隔离724项保留为对应范围证据，最新组合测试与本机安装结果见[rc8发布记录](../../operations/rc8-local-rollout.md)。
