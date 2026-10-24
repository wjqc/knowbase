# 02 · 三级自适应检索状态机设计（Phase 0 设计稿，未实现）

目标：能力检测→等级选择→降级/恢复 全部为引擎核心行为；任何等级允许零结果；模型故障不得中断基础检索。

## 1. 等级定义与代码落点

| 等级 | 组成 | 现状基础（01 号报告§5） | 新增 |
|---|---|---|---|
| L1 | FTS5 trigram MATCH + 短词 LIKE + 排序因子 | `_match_ids`/`_like_ids`/排序链已存在 | 直通（即关闭扩展与 dense 后的 search） |
| L2 | L1 + 规则化同义扩展 | `_expanded_lexical_ids`（4 条硬编码） | 词典文件化、随包安装、可本地覆盖；扩展项仅召回不抬权（现状 ×0.7 保留）；**dense 通道不参与 L2** |
| L3 | L2 + 真实向量 dense + RRF | `_dense_ids`/`_rrf_scores` 已存在 | 向量来自 Provider（03 号）；读取按 model_version 过滤；阈值随模型校准 |

不变量：三级共用同一 `index.search` 入口与排序因子（scope/confidence/新鲜度/反馈）、同一 scope 治理过滤（index.py:354）；区别只在召回通道组合。**字符哈希向量在任何等级都不再参与打分**（退役为诊断数据，见 03 号 §5）。

## 2. 状态机

```
            ┌────────────────────────────────────────────┐
            │  能力探测（MCP server 进程内存态，hook 进程不探测） │
            └────────────────────────────────────────────┘
  probe: 配置存在? → 服务可达(短超时)? → 模型指纹匹配? → 索引覆盖率≥99%?
    全是 → L3        任一否 → L2（记录 degrade_reason）
  运行期故障（每次调用捕获）: 超时/连接错/限流/鉴权错/维度不符 → 熔断降 L2 + 记原因
  恢复探测: 半开熔断器按指数退避（10s→60s→5min 上限）重探；探针通过且指纹匹配 → 回 L3
```

状态（进程内存 + 落库双写，doctor 可读）：

```json
{"level": "L3", "since": "...", "degrade_reason": null,
 "probe": {"ok": true, "latency_ms": 42, "model_fp": "qwen3-emb-4b:2560@rev2"},
 "breaker": {"state": "closed", "failures": 0, "next_probe_at": null}}
```

- **熔断只存在于长驻 MCP server 进程**（内存态有效）。hook 一次性进程（`knowbase hook user-prompt`）按部署策略 `retrieval.auto_hook_semantic=false`（默认）**永远不调远程、永远 L2/L1**，因此无需熔断态——该策略是配置不是引擎硬编码，未来集中检索服务可翻转。
- 探测在 server 启动时**异步**执行，不阻塞 MCP 握手；探测完成前按 L2 服务。

## 3. 降级触发分类（写入 degrade_reason，进 usage_log）

| 类别 | 触发 | 行为 |
|---|---|---|
| `not_configured` | 未配置 Provider | L2，不探测不计数 |
| `probe_timeout` / `conn_error` | 探测或调用网络失败 | L2 + 熔断开启 |
| `auth_error` | 401/403 | L2 + **不自动重试**，提示 doctor 诊断 |
| `rate_limited` | 429 | L2 + 退避后单次重探 |
| `model_mismatch` | 指纹/维度/canary 异常（03 号§4） | L2 + **冻结索引不删除**，提示重建 |
| `index_incomplete` | 向量覆盖率<99% | L2 + 后台补嵌，完成自动回 L3 |

任何降级都**不是错误**：memory_search 正常返回 L2 结果并在响应中带 `retrieval_mode` 字段。

## 4. 零结果与相关性门控（全等级生效）

原则：降级成功≠必须返回知识；L3 相似度高≠强制注入。

- 门控作用于**最终融合排名后**：按模式分别校准的录取门槛（校准方法见 06 号§4；**RRF 分数不是相似度，禁止直接当阈值用**——现状 score≈1/(60+rank)×排序因子，量纲与语义均不适配）。
- 初始门槛（待评测校准后定稿）：L3 要求 dense 余弦 ≥ T3 或 lexical 首位命中；L2/L1 要求命中词数 ≥2 或首位 lexical 分 ≥ T2；长查询（>64 字符）自动追加"仅精确标识符可注入"收紧规则。
- hook 自动注入在门控之上再叠加：verified-only + 版本安全 + contradicts 排除（现状保留）+ **注入条数/字符预算**（复用 hook_notice_chars=900 先例，docs/hook-noise-reduction.md）。
- `unanswerable` 是合法返回：响应 `results: []` + `retrieval_mode` + 门控拒绝原因（`gate:below_threshold`），usage_log 记零结果事件（现状已记 hits:[]）。

## 5. 查询卫生（进入状态机之前，P0-02）

**自动 hook 路径**（无模型可用，纯规则）：
1. 跳过：长度 >512 字符、含代码块/Markdown 表格占比 >30%、明显粘贴文档特征（>3 个 URL 或 >5 级标题）→ 不检索不注入；
2. 提取：错误码 `([A-Z][A-Z0-9_]{3,})`、`HTTP \d{3}`、`E\d+`、反引号内标识符、首个疑问句/祈使句（≤64 字符）作为查询；
3. 保留技术标识原文不做同义替换。

**主动 MCP 路径**：优先复用 Agent 生成的 topics.queries（context.py:150-158 已有校验）；原始查询与扩展查询全部可追溯（coverage 机制 context.py:199 已缓存逐 query 完成态，扩展后新增查询自然重查）。

## 6. 配置（叠加在现有 config.json 之上，向后兼容）

```json
{"retrieval": {"mode": "auto", "auto_hook_semantic": false,
   "gate": {"l3_dense_min": null, "l2_lexical_min": null}},
 "embedding": {"provider": "openai_compatible", "base_url": null, "model": null,
   "api_key_env": "KNOWBASE_EMBED_API_KEY"},
 "index": {"vector_model": "...", "dense_threshold": ...}}
```

旧键 `index.vector_model`/`dense_threshold` 读取时合并进新结构（迁移在 Phase 2，双写一个版本期）。`api_key` 只从环境变量读，不落 config.json（沿用 rc6 凭据治理教训）。

## 7. 交付边界

引擎改动集中在：`index.search` 增加通道开关参数与门控、新模块 `retrieval_state.py`（探测/熔断/等级决策）、hooks.py 查询卫生。不改 MCP 工具签名（memory_search 加只读响应字段）、不改 scope 治理、不改 Markdown+Git 真相层。
