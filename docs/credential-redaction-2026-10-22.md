# 观察与提炼材料的凭据脱敏修复

日期：2026-10-22；版本：0.2.0rc6。凭据原文不得写入问题单、测试、日志或本报告。

## 原因与修复

旧 Hook 的确在写入 `context_events.observation_json` 前脱敏，但没有识别 argv 中相邻的旗标和值，字符串正则也无法一致处理内嵌 JSON。`context.execute` 的直接调用还可绕过 Hook。旧队列在领取、分页时不再检查脱敏完整性。

- `credentials.py` 统一规则；识别 shell/JSON argv 中 API key、secret、token、密码旗标及 `-s`，递归解析 JSON 字符串，保留并清洗 preview；常见密钥前缀作兜底。
- `token_limit`、`token_budget`、`max_tokens` 等数值配置保留；仍会拦截纯数字的真正 `token`/`password` 值。
- Hook 和 `context.execute` 都在写前清洗。队列入库再检查用户内容，不把协议租约当用户凭据。
- 旧 spool 在校验 checksum 后检查凭据；未清洗时返回 `MATERIAL_REDACTION_REQUIRED`，不交付旧页。检查在切片前拼接同一事件的片段，覆盖跨片段密钥。
- 入卡拒绝提示只包含 `[REDACTED:credential]`，不回显命中的秘密。

## 历史清洗

只读盘点（不输出命中原文，不记 CLI 遥测）：

```sh
~/.knowbase/runtime/venv/bin/python -I -m knowbase privacy-scrub
```

备份后执行，目录须为仓库外尚不存在的路径：

```sh
~/.knowbase/runtime/venv/bin/python -I -m knowbase privacy-scrub --apply \
  --backup-dir ~/.knowbase/backups/credential-scrub-YYYYMMDD-HHMMSS
```

持有仓库锁和 SQLite 写锁，在线备份数据库及 knowbase 配置；目录权限 0700、文件 0600。清洗观察、材料、相似卡查询和候选/提交载荷中的凭据。保留片段 ID、数量及已完成处理记录，更新材料 checksum、输入版本和片段哈希，撤销受影响的旧批次/租约；未完成作业需重新领取。部分正在物化的提交须先恢复，工具会拒绝直接修改该作业。

历史已提交候选的证据哈希保留原值，表示修复前的证据版本，不把过去审核伪装为针对新输入的审核；原数据只能从私有备份核查。清洗后检查数据库完整性，执行 VACUUM 和 WAL 截断；`compacted` / `wal_checkpoint_busy` 分别报告物理清理是否完成及活跃读者影响。

本机清洗结果：53 条观察、6 份材料、1 份候选载荷、1 份提交载荷；复扫本工具覆盖的字段无待修改项，integrity_check=ok，VACUUM 完成，WAL 截断无阻塞。备份位于 `~/.knowbase/backups/credential-rc6-20261022-183843/`。

备份含原始明文，旧备份和宿主自身的会话/日志也可能含明文；本工具不改写其他产品的记录或 Git 历史，不能撤回已经发送给模型的内容，也不能代替供应商侧撤销和轮换密钥。未知形态的无标记密钥仍可能漏检。

## 宿主提交协议澄清

`duplicate_evaluation` 在每个候选内部，不能放在 `memory_job_submit` 顶层：

```json
{
  "candidates": [{
    "candidate_key": "example",
    "duplicate_evaluation": {
      "snapshot_hash": "领取时的 similarity_snapshot",
      "evaluated_cards": [{"id": "相似卡ID", "content_revision": "阅读时的版本"}],
      "rationale": "逐卡判断相同、补充、独立或矛盾的理由"
    }
  }]
}
```

这仅展示字段位置；完整提交还需候选正文、证据及批次/租约参数。服务端提炼指引与 `NEEDS_REASSESSMENT` 提示已明确该位置。旧 MCP 连接须重连才能加载新指引。

## 验证与边界

源码全量回归：566 passed、1 skipped（OCR 环境依赖）。新增 70 项合成用例覆盖 CLI、五层 JSON 编码、token 配置、直接写入、跨片段密钥、旧分页拦截、私有备份、哈希修复与租约失效；收尾定向 80 项通过，安装态 stdio 端到端 2 项通过；三个实际 MCP 配置均完成初始化与脱敏交付，18 个配置 Hook 命令通过；安装态 62 个模块与源码逐字节一致。真实供应商密钥轮换、历史宿主记录处置与重连后的真实任务验收仍需单独完成。

任务 `J-98fc081c5e83` 的数据库记录为 52 段，11 distilled / 41 no_value；用户转述中的“其余 62 段”与此不符。产出的经验卡与任务聚合状态需分开看，不能用 no_candidate 聚合状态否定已保存的卡片。
