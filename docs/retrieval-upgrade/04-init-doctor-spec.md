# 04 · knowbase init / doctor 一站式接入规格（Phase 0 设计稿，未实现）

定位：`init` 是唯一首次接入入口（重复运行=补全/修改配置，不破坏既有）；`doctor` 是唯一诊断入口。两者让非维护者成员独立安装、长期使用。

## 1. init 交互流（在 cmd_init 现有内容库引导之上增量，__main__.py:72）

```
knowbase init
 1. 现有内容库引导（不变：配置幂等/目录/远端白名单/可选导入）
 2. 宿主发现：探测已装 Agent（§2），列出 checkbox 多选
 3. 逐宿主写入：MCP 配置 + Hook 注册（docs/hosts/*.md 的落点），每写一个先备份
 4. Embedding（可选，默认跳过）：
    [1] 使用公司内网模型服务（输入 base_url + 模型名 + 认证方式）
    [2] 暂不配置，使用本地同义改写检索（默认）
 5. 若选[1]：连通性验证 → canary 探针 → 后台批量建索引 → 完成后自动升级 L3
 6. 汇总报告：配置完成 / 连接验证 / 会话生效 三态分列
```

硬性要求（对应任务书 D1）：

- **幂等**：已存在的宿主配置中 knowbase 条目原样跳过或按 `--force` 更新；绝不触碰 knowbase 之外的条目；配置文件写入前整体备份到 `~/.knowbase/backups/hosts/<时间戳>/`。
- **失败回滚**：单宿主写入失败恢复备份并继续其他宿主，报告明确到文件路径。
- **无管理员权限**：全部落用户目录（docs/hosts 落点均如此）；TRAE sandbox 拦截场景按三层兜底（探测→降级为打印手动配置步骤→退出码 2 区分）。
- **三态区分**（禁止"填了 URL 就宣称已启用语义检索"）：`configured`（写盘成功）→ `verified`（探测+canary+索引覆盖率达标）→ `active`（MCP 会话实际加载）。结束时提示：**旧会话需重连/新会话才生效**（AGENTS.md 既有教训：不能只改文件就宣称会话已加载）。
- Embedding 验证失败（错地址/不可达）：提示具体原因，允许改或跳过；跳过=纯 L2 完成 init，**不阻断**。

## 2. 宿主发现与差异矩阵

| 宿主 | 发现方式 | MCP 落点 | Hook 落点 | 事件集 | 备注 |
|---|---|---|---|---|---|
| ZCode | `~/.zcode/cli/` 存在 | config.json mcp.servers | 同文件 hooks.events | 6 | --style zcode |
| Claude Code | `~/.claude.json` 存在 | mcpServers | ~/.claude/settings.json | 7（含 PreCompact） | --style claude |
| Trae CN | `~/Library/Application Support/Trae CN/` | User/mcp.json | ~/.trae-cn/hooks.json | 5（失败从 PostToolUse 识别） | --style trae；sandbox 拦截风险 |
| Codex | **待调查** | 未文档化 | 未文档化 | — | 首期可只做 MCP 不做 Hook，标待验证 |

- 所有命令固定 `~/.knowbase/runtime/venv/bin/python`（AGENTS.md 单版本政策）。
- 发现≠自动写入：逐宿主让用户确认（"检测到 X，是否接入？Y/n"）；`--yes` 跳过确认用于脚本化。

## 3. doctor 扩展（现有检查保留，__main__.py:640-670）

新增检查项（每项 pass/warn/fail + 修复建议一句化）：

| 检查 | 判定 | 数据来源 |
|---|---|---|
| 检索等级 | 当前 L 几 + degrade_reason + 持续时长 | 状态机落库快照（02 号§2） |
| Embedding 服务 | 可达性/延迟/认证/限流四态；未配置=info 非 fail | Provider.probe() |
| 模型与索引兼容 | 指纹一致率、维度、canary 余弦 | 03 号§3/§4 |
| 向量覆盖率 | 当前指纹行数/meta 总数（替换现有 emb==meta 检查语义） | embedding_index WHERE model_version=fp |
| Agent 配置有效性 | 各已接入宿主配置中 knowbase 条目存在、解释器路径有效、hook 命令可 dry-run | docs/hosts 落点重读 |
| 降级与模式分布 | 近 7 天 L3/L2/L1 占比、降级原因 top、无关注入率 | usage_log mode 字段（02 号§3） |
| Token 估算 | 模型调用 token（API usage 返回值）、注入 token 估算——**标注为估算，不得伪装成账单或有效帮助次数** | 新遥测字段 |

输出分两档：默认人类可读摘要；`--json` 供调度器（alerts 命令沿用）。

## 4. 遥测字段扩展（复用现状，只加不改）

- `record_search`（index.py:520-527）payload 追加：`mode`（L3/L2/L1）、`degrade_reason`（可空）、`gate_rejected`（门控拒绝数）、`query_class`（hook_distilled/raw/topics，查询卫生落地后）。旧事件无这些字段按 null 读——遥测只读契约不破坏。
- value export/snapshot（__main__.py:869-902）聚合新增：L3 使用率、降级率及原因分布、每模式采纳率（feedback 关联）、模型调用 token、注入 token 估算。周报模板（value report）加"检索质量与成本"一节。

## 5. 验收标准（Phase 3 出口）

1. 新成员无模型地址：`knowbase init` 一条命令后 ZCode/Claude 至少一宿主可检索（会话重启后验证 MCP 工具可达 + hook 注入一次正常/正常零注入）。
2. 有模型地址：init 后自动到 L3（含后台建索引与自动升级），拔掉服务地址后检索不失败、doctor 显示降级原因，恢复后 5 分钟内自动回 L3。
3. 重复 init 不破坏：已有宿主配置的其他条目逐字节不变（快照对比测试）。
4. 全程无需管理员权限；TRAE sandbox 下至少完成"打印手动步骤"降级路径。
