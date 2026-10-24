# ADR-0004：在原单主键 embedding_index 中隔离向量代际

日期：2026-10-24。状态：隔离实现及局部验证；生产待验。承接ADR-0003内网服务路线，细化索引存量兼容，替换设计稿中直接改复合主键的实施细节。

## 证据

旧index.upsert使用ON CONFLICT(memory_id)，旧dense以memory_id匹配meta中的合法卡ID；旧rebuild删除meta不存在的embedding行。直接改复合主键会使旧写入失效，直接在原卡ID上覆盖真实向量则可能让旧hash查询比较真向量。

## 决策

保留memory_id原主键，增加generation_id/entity_id/版本/维度/checksum列；真向量主键为`generation_id:card_id`。卡片ID校验不接受该前缀形式，因此旧dense不会读作该卡片的可比较向量。legacy行generation_id为空，真向量读者明确过滤相容generation、space_id、内容版本、维度和checksum。

同库embedding_generations保存清单、Canary与资格，retrieval_active短事务切指针，jobs承载增量构建。Markdown/Git仍为权威，查询/建索引前还检查来源内容与SQLite版本。迁移只追加22，不改已提交的既有迁移及运行日志表。未收口team迁移21不包含于本提交，编号22保留以兼容已有21的本机账本。

## 取舍与恢复

不引入第二向量库、运行版本或发布机制。旧rebuild可能删掉代际行，但新代码会检测缺失、回本地并补建；这是可用性退化，不是空间混用。安装包更新不表示旧MCP进程加载新能力，宿主仍需重连。

切代失败回滚指针事务；前代回切必须重新检查当前内容、模型、Canary和评测凭据。禁止为恢复向量而整体回滚数据库丢失反馈/任务事实。真实旧宿主组合与发布恢复演练尚未执行，不由本ADR的局部测试代替。


2026-10-24续作：用户已授权team-stats收口，迁移21随后与rc8纳入主干，22的既有定义不改。前述“21未进入独立检索提交”仅指d738581的发布边界，不表示rc8仍排除团队统计。
