# 03 · Embedding Provider 与模型版本治理设计（Phase 0 设计稿，未实现）

首期唯一 Provider：公司内网 OpenAI 兼容接口。**网关真实协议未验证**（base_url/模型清单/认证/是否返回不可变修订号——待用户提供，Phase 2 前实测），本设计按 OpenAI `/v1/embeddings` 语义假设，标注待验证点。

## 1. 适配器接口（新模块 embedding_provider.py）

```python
class EmbeddingProvider(Protocol):
    model_fingerprint: str          # 治理标识，见 §3
    def embed_documents(texts: list[str]) -> list[list[float]]   # 批量，卡片侧
    def embed_query(text: str) -> list[float]                    # 单条，查询侧
    def probe() -> ProbeResult        # 探针：延迟/身份/canary，见 §4
```

- 查询与卡片**必须同一模型同一预处理**。BGE 系查询需指令前缀而文档不需——若选用的内网模型属此类，预处理差异写入 model_fingerprint 的一部分，两侧由同一适配器封装，调用方不可绕过。
- 批量：单批 ≤64 条 / ≤256KB 文本，失败分片重试一次；限流 429 指数退避（1s/4s/16s），两轮后熔断降级（02 号§3）。
- 超时：单请求 2s、批 10s（**待实测内网延迟后定稿**）；认证失败不重试。
- 依赖：`urllib.request` 或 `httpx`（若 MCP 栈已带则复用）——**不新增 torch/onnx/numpy**（ADR 0003）。

## 2. 卡片侧语义文本与索引写入

- 复用现状 `vector_text = title + tags + body`（index.py:87）——卡片本身是"现象/根因/解决"结构化短文，per-card 向量（不分块，ADR 裁决）。写入前统计**分词截断比例**：若目标模型上下文下 >5% 卡片被截断，重议分块（评测集阶段一并输出该统计）。
- 写入路径：`upsert()`（index.py:88-97）改为调用 Provider 失败时**不阻塞卡片保存**——向量缺失标记 `pending_embed`，后台补嵌（复用 jobs 队列模式），保证"卡片先落、向量后补"不破坏 Git 同步。
- **增量**：写入前比对 content_hash——未变且 model_version 一致则跳过远程调用；git 同步路径（gitops.py:523 全量 rebuild）改为：markdown 解析全量、**向量计算仅对 content_hash 或 model_version 变化的行**。

## 3. 模型身份与索引版本（修复 01 号§6 核实的缺口）

- `model_version` 改存**模型指纹**：`{service_reported_model}:{dim}#{norm}`，norm=预处理版本号（适配器内嵌常量，预处理变更时递增）。网关只回别名（如 `Qwen3-Embedding-4B`）时照存——指纹是"我们以为在用谁"的记录，漂移检测交给 canary。
- 读取侧（`_dense_ids` index.py:273）：`WHERE model_version = :active_fp`——**当前激活指纹由状态机维护，查询向量由同一 Provider 产生**，杜绝混空间（现状：查询侧硬编码 `_fallback_vector` index.py:268，必须移除）。
- 索引兼容判定顺序：配置指纹 → 存量行指纹一致率 → 维度 → canary。任一不符 = `model_mismatch` → 降级 L2 + 冻结（**不自动删除旧索引**，人工 `knowbase reindex --embed` 或 init 重跑后切换）。
- 多版本共存：embedding_index 主键是 memory_id，切换模型即全量重嵌（172 张卡一次批量调用，分钟级）；不建多版本向量表，旧版本行被覆盖前先导出备份到 `~/.knowbase/backups/`。

## 4. Canary 漂移检测（修正版：非唯一依据）

采纳裁决修正：canary 是**漂移检测，不是绝对证明**；组合判定 = 模型身份信息 + 维度 + **多条固定 canary** + 索引版本管理。

- canary 集：5 条固定文本（纯中文短句 / 中英混合 / 错误码风格 / 卡片体例 / 否定句），构建索引时随卡片一起嵌入并预存向量于本地（`~/.knowbase/derived/canary.json`，不入 Git 库）。
- 每次探测（server 启动、熔断恢复、doctor）重嵌 canary：与预存向量逐条算余弦，**全部 ≥0.999** 判稳定（同一模型确定性推理应≈1.0）；任一条 <0.99 判漂移；区间内重探一次。
- 网关若提供不可变修订号/模型哈希，优先采用并记录（身份不符即漂移，canary 退居二线）。
- 漂移处置：降级 L2、冻结索引、doctor 提示"模型疑似更换，需重建向量"；**绝不混用不同指纹的向量、绝不静默删除索引**。

## 5. 旧字符哈希向量迁移与隔离

- 迁移：首次配置可用 Provider 后，后台批量重嵌（02 号§3 `index_incomplete` 期间保持 L2，覆盖率达标自动升 L3）；重嵌完成前旧 `char-ngram-v1` 行按 §3 读取过滤天然不参与 L3 打分。
- 退役：`_fallback_vector`（index.py:249-263）保留源码用于**复现旧基线评测**（A 组），运行路径不再调用；诊断命令可显式要求用它重建（排障用途）。
- 回滚：Provider 整体禁用（config `embedding.provider=null`）即回 L2/L1，embedding_index 数据保留不动。

## 6. 安全与边界

- api_key 仅环境变量（`KNOWBASE_EMBED_API_KEY`），不落 config/不进日志（rc6 凭据泄漏修复的延续：日志脱敏正则同步覆盖 embedding 错误响应体）。
- 出网边界：只允许 config 声明的 base_url（白名单校验沿用 git remote allowed_remote_prefixes 先例，init 时固化）。
- 隐私声明：查询文本与卡片文本将发送至内网该服务——写入接入手册（docs/share）与 init 确认步骤，未确认前不启用。
- 团队索引快照分发（远期，非首期前提）：同指纹下 content_hash 可校验快照有效性，机制预留不实现。

## 7. 待验证清单（Phase 2 前必须完成）

1. 内网网关：真实 endpoint、鉴权方式、模型清单接口、批量上限、限流策略、错误码语义。
2. 是否返回不可变修订号；别名→底座映射是否可查。
3. 实测 p50/p95 延迟（单条/64 条批量）→ 定稿超时与熔断参数。
4. 目标模型上下文长度 → 卡片截断比例统计（§2）。
5. 同模型确定性（同输入重复嵌入余弦是否 ≈1.0）→ canary 阈值定稿。
