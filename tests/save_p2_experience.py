"""把 Phase 2 收尾产生的 3 条经验保存到 ~/knowbase(隔离环境变量,不污染 V2 测试 tmp)。

- 踩坑(P-2026-0008):rank_bm25 环境路径
- 决策(D-2026-0002):Phase 2 评测改用真实 30 条记忆
- 流程(W-2026-0003):Phase 2 V2 真实数据下的实测效果
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# 关键:隔离 KNOWBASE_REPO_PATH / KNOWBASE_CONFIG,让 server.save_impl 走默认 ~/knowbase
for k in ("KNOWBASE_REPO_PATH", "KNOWBASE_CONFIG", "KNOWBASE_V2_INGESTION",
          "KNOWBASE_V2_IDEMPOTENT_INGEST", "KNOWBASE_V2_PARSER_MARKDOWN",
          "KNOWBASE_V2_PARSER_TXT", "KNOWBASE_V2_PARSER_PDF", "KNOWBASE_V2_PARSER_DOCX"):
    os.environ.pop(k, None)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from knowbase import __main__ as cli  # noqa: E402
from knowbase import config, server  # noqa: E402

# 确保真实仓库 init 完毕(幂等)
print(f"repo_path = {config.repo_path()}")
cli.main(["init"])

P1 = (
    "pitfall",
    "rank_bm25 装在系统 Python 不在 .venv,跑 benchmark 必须用 python3",
    """## 现象
跑 `tests/v2_p2_benchmark.py` 时报 `ModuleNotFoundError: No module named 'rank_bm25'`;但 `tests/v2/test_p2_retrieval.py` 44/44 单测能过。

## 原因
`rank_bm25` 装在 `/Users/qc/Library/Python/3.14/lib/python/site-packages/`,只在系统 `python3` 可见;`.venv/lib/python3.14/site-packages/` 没有。`test_p2_retrieval.py` 此前用 `python3 -m pytest` 跑(系统 Python);`v2_p2_benchmark.py` 文档却写了 `.venv/bin/python tests/v2_p2_benchmark.py`,改成 `python3 tests/v2_p2_benchmark.py` 即可。

## 正确做法
跑 V2 retrieval / benchmark 一律用 `python3`(系统 Python,带 rank_bm25)。`.venv` 只用来跑 V1/V2 不依赖 rank_bm25 的部分。""",
    ["rank-bm25", "python", "venv"],
)


P2 = (
    "decision",
    "Phase 2 评测用 ~/knowbase 真实 30 条记忆(V1 iter_all 不含 staging,与真实 V1 检索行为一致)",
    """## 背景
V2 升级方案 Phase 2(混合检索)需要验证 V2 HybridSearch 在真实数据规模下仍优于 V1 FTS5+LIKE。原 benchmark 用 16 条 synthetic seed,与用户真实库无关,不能证明真实效果。

## 决策
1. `tests/v2_p2_benchmark.py` 改为拷贝 `~/knowbase` 的 7 个目录到 tmp 仓库(排除 `.git`、排除 `staging/`),`cli.main(["init"])` 重建索引。
2. V1 iter_all 默认不扫 staging,所以 staging 里的 2 条提案(PR-2026-0003 / S-2026-0001)不参与检索 —— 与真实 V1 行为一致,**不算偷分**。
3. golden set 14 case,基于真实 ID 锚定:`expected_ids=("P-2026-0001",)` 等,query 用真实标题中的核心 token。

## 取舍
不复制 staging 的好处:对照公平(两边都不扫 staging),golden set 用真实 ID 可读可审计,后续 Phase 6 灰度直接换库即可;坏处:golden set 与 V1 must 8/8 的旧 baseline 不直接可比,所以 P2-K 报告单独成段。""",
    ["v2-upgrade", "evaluation"],
)


P3 = (
    "workflow",
    "V2 真实数据评测:HybridSearch HR@5 50%→85.71%,semantic 0%→100%(Phase 2 收尾实测)",
    """## 步骤
1. `tests/v2_p2_benchmark.py` 跑 V1 vs V2 同 30 份 docs + 同 14 case golden set
2. 流程:拷贝 ~/knowbase → tmp 仓库 → init → load_v1_corpus → V1 search_impl + V2 HybridSearch 并行
3. 度量:HR@5 / MRR / 分类(exact/semantic/no_answer)/ P50/P95 延迟

## 产出
**V1 (FTS5 + LIKE fallback)**
- HR@5 = 50% (7/14) | MRR = 0.5
- exact 88% (7/8) — 唯一失败:"xlsx 验收 Numbers PDF PNG 日期小写" 命中 W-2026-0002(渲染流水线)而非 P-2026-0003(xlsx 渲染)
- semantic 0% (0/4) — 同义改写全失败(预期)

**V2 (Exact + BM25 + Dense + Metadata, RRF k=60, governance + reranker)**
- HR@5 = 85.71% (12/14) | MRR = 0.8571
- exact 100% (8/8) — BM25 把 P-2026-0003 拉回 Top 5
- semantic 100% (4/4) — bigram + dense 让 "VPN 客户端=EasyConnect" 这类改写命中
- latency P50 = 1.26ms(V1 = 3.13ms,更快)

## 结论
在不装公网依赖(无 sentence-transformers、无 HF 下载)的前提下,V2 HybridSearch(HashEncoder + rank_bm25 + RRF + governance + TokenOverlapReranker)在真实 30 条记忆上仍稳定优于 V1 FTS5+LIKE,达标 Phase 2 验收。""",
    ["v2-upgrade", "evaluation"],
)


for typ, title, body, tags in (P1, P2, P3):
    src = "human:文剑"
    out = server.save_impl(typ, title, body, tags=tags, source=src)
    print("=" * 60)
    print(f"[{typ}] {title}")
    print(out[:500])
    print("…\n")