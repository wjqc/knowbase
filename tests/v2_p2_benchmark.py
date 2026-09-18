"""P2 对照评测：V1 vs V2 在同一份真实 32 条记忆 + golden set 上的检索效果。

数据源：~/knowbase（用户真实记忆库，1 decision + 7 pitfalls + 2 active preferences
+ 18 references + 1 standard + 2 workflows = 32 条；其中 staging/ 提案 2 条
按 V1 store.iter_all 默认行为不参与检索；benchmark 也保持一致以保证对照公平）。

运行：.venv/bin/python tests/v2_p2_benchmark.py

度量：
- HitRate@5（任一期望 id 进 Top 5）
- MRR
- 分类（exact / semantic / no_answer）
- degraded / fallback 提示
- P50/P95 延迟

不依赖 numpy / sklearn / torch；纯 stdlib + V2 tokenize + hash-based dense embedding。
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

# 在 import knowbase 之前先把 KNOWBASE_REPO_PATH 切到 tmp 仓库，
# 避免 V1 的 _repo() 落到真实 ~/knowbase 上做写动作。
TMP = Path(tempfile.mkdtemp(prefix="knowbase-p2-bench-"))
os.environ["KNOWBASE_REPO_PATH"] = str(TMP / "repo")
os.environ["KNOWBASE_CONFIG"] = str(TMP / "nonexistent-config.json")

# knowbase 包所在的仓库根
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from knowbase import __main__ as cli  # noqa: E402
from knowbase import config, server, store  # noqa: E402
from knowbase.v2.evaluation import GoldenCase, GoldenSet, EvalReport  # noqa: E402
from knowbase.v2.retrieval import HybridSearch  # noqa: E402


REAL_REPO = Path(os.path.expanduser("~/knowbase"))


# ---------- 1. 准备：把 ~/knowbase 复制到 tmp 仓库（不污染真实数据）----------

def seed_real_repo() -> None:
    """把 ~/knowbase 的 7 个真实目录（不含 staging）拷到 tmp 仓库。

    V1 store.iter_all 默认不扫 staging/，所以 staging 里的 2 条提案
    （PR-2026-0003 / S-2026-0001）不参与本轮对照评测 —— 与真实 V1
    检索行为一致，不算偷分。
    """
    src = REAL_REPO
    dst = Path(os.environ["KNOWBASE_REPO_PATH"])
    assert src.exists(), f"真实仓库不存在: {src}"
    assert src != dst, "拒绝把 tmp 仓库指向真实仓库"

    # 排除 .git / .DS_Store 等杂物；只拷 7 个类型目录
    ignore = shutil.ignore_patterns(".git", ".DS_Store", "*.bak", "__pycache__")
    for sub in ("decisions", "pitfalls", "preferences", "references",
                "standards", "workflows", "staging"):
        s = src / sub
        if not s.exists():
            continue
        shutil.copytree(s, dst / sub, ignore=ignore, dirs_exist_ok=True)

    # init：会 git init + index.rebuild（重建 FTS5 索引 + INDEX.md）
    cli.main(["init"])


def load_v1_corpus() -> list[dict]:
    """加载仓库内所有 markdown 记忆为 docs（id/title/body/tags/kind/path）。"""
    rp = config.repo_path()
    docs: list[dict] = []
    for d in store.iter_all(rp):
        meta, body, _ = d
        docs.append({
            "doc_id": meta["id"],
            "title": meta.get("title", ""),
            "body": body,
            "tags": meta.get("tags", []),
            "kind": meta.get("type", ""),
            "status": meta.get("status", ""),
            "path": meta.get("path", ""),
        })
    return docs


# ---------- 2. Golden Set（基于真实 ID 与真实标题）----------

# 真实 ID ↔ 标题（从 ~/knowbase/INDEX.md 校对）
_REAL_IDS = {
    "P-2026-0001": "EasyConnect 7.6.7 在 macOS 26.1 上启动即 Rosetta 死锁",
    "P-2026-0002": "HAR 分析判断压缩只看 200 响应的 Content-Encoding",
    "P-2026-0003": "本机渲染 xlsx 验收：Numbers→PDF→PNG，日期小写",
    "P-2026-0004": "多仓库聚合项目禁止在根目录执行 git 命令",
    "P-2026-0005": "Docker Hub 直连超时：用 DaoCloud 镜像加速",
    "P-2026-0006": "ncoa 门户加载慢：后端冷查询 + 静态资源 no-cache",
    "P-2026-0007": "knowbase 升级后旧会话 search 命中但 read 未找到",
    "W-2026-0001": "CMI 日报生成流程：汇总四子仓库 git 提交",
    "W-2026-0002": "文档类交付物渲染验收流水线（xlsx/docx/pdf）",
    "PR-2026-0001": "未经点名的持久性设施先征求同意再部署",
    "PR-2026-0002": "登记表格类请求默认 Excel 且先在对话里给字段清单",
    "D-2026-0001": "业务知识图谱五项设计决策（分层/LPG/DSL/YAML/不做独立服务）",
    # references 是 cmi-sales-assistant 项目知识（R-2026-0001..0018）
    "R-2026-0001": "项目概述",
    "R-2026-0007": "Agent 核心引擎",
    "R-2026-0011": "记忆系统",
}


def build_golden_set() -> GoldenSet:
    """基于真实 ID 写 expected_ids；不依赖任何 synthetic seed。"""
    cases: list[GoldenCase] = [
        # ---- exact:用标题或正文里的具体名词检索 ----
        GoldenCase(
            query="EasyConnect 7.6.7 macOS 死锁",
            expected_ids=("P-2026-0001",),
            category="exact",
            note="EasyConnect + 7.6.7 + 死锁 三词全在标题/正文",
        ),
        GoldenCase(
            query="HAR Content-Encoding 304 误判",
            expected_ids=("P-2026-0002",),
            category="exact",
            note="HAR + Content-Encoding + 304 三词命中",
        ),
        GoldenCase(
            query="xlsx 验收 Numbers PDF PNG 日期小写",
            expected_ids=("P-2026-0003",),
            category="exact",
            note="Numbers→PDF→PNG 渲染链 + 日期小写规则",
        ),
        GoldenCase(
            query="多仓库聚合项目 git 命令 根目录 fatal",
            expected_ids=("P-2026-0004",),
            category="exact",
            note="fatal: not a git repository 报错原文命中",
        ),
        GoldenCase(
            query="Docker Hub DaoCloud 镜像加速 registry-mirrors",
            expected_ids=("P-2026-0005",),
            category="exact",
            note="DaoCloud + registry-mirrors 命中正文",
        ),
        GoldenCase(
            query="ncoa 门户加载慢 后端冷查询 静态资源 no-cache",
            expected_ids=("P-2026-0006",),
            category="exact",
            note="冷查询 + no-cache 命中正文",
        ),
        GoldenCase(
            query="持久性设施 容器 部署 同意",
            expected_ids=("PR-2026-0001",),
            category="exact",
            note="持久性设施 + 容器 + 同意 三词命中",
        ),
        GoldenCase(
            query="业务知识图谱 LPG DSL YAML 分层",
            expected_ids=("D-2026-0001",),
            category="exact",
            note="LPG + DSL + YAML 三词命中",
        ),

        # ---- semantic:V1 FTS5 难以命中的自然语言改写 ----
        GoldenCase(
            query="VPN 客户端启动就卡死怎么办",
            expected_ids=("P-2026-0001",),
            category="semantic",
            note="同义改写：VPN 客户端=EasyConnect,卡死=死锁",
        ),
        GoldenCase(
            query="怎么看请求有没有 gzip 压缩",
            expected_ids=("P-2026-0002",),
            category="semantic",
            note="同义改写：gzip 压缩=Content-Encoding,304 误判",
        ),
        GoldenCase(
            query="Excel 渲染不出来用什么工具看",
            expected_ids=("P-2026-0003",),
            category="semantic",
            note="同义改写：Excel=xlsx,渲染=验收",
        ),
        GoldenCase(
            query="项目下面有多个 git 仓库怎么提交",
            expected_ids=("P-2026-0004",),
            category="semantic",
            note="同义改写：多 git=聚合项目,怎么提交=执行 git",
        ),

        # ---- no_answer ----
        GoldenCase(
            query="量子纠缠态坍缩",
            expected_ids=(),
            category="no_answer",
            note="与 knowbase 业务无关",
        ),
        GoldenCase(
            query="zzznonexistentkeyword123",
            expected_ids=(),
            category="no_answer",
            note="人造无意义字符串",
        ),
    ]
    return GoldenSet(name="p2-real-32", version=2, cases=cases)


# ---------- 3. V1 / V2 评测 ----------

def evaluate_v1(gs: GoldenSet) -> tuple[EvalReport, dict]:
    report = EvalReport(golden_set_name=gs.name + "-v1")
    latencies = []
    for case in gs.cases:
        t0 = time.perf_counter()
        out = server.search_impl(case.query)
        latencies.append((time.perf_counter() - t0) * 1000)
        ids = _parse_v1_ids(out)
        rr = 0.0
        hit = False
        for rank, exp in enumerate(case.expected_ids, start=1):
            if exp and exp in ids[:5] and rank <= 5:
                rr = 1.0 / rank
                hit = True
                break
        report.total += 1
        if hit:
            report.hit += 1
            report.reciprocal_rank_sum += rr
        elif case.category == "no_answer" and not ids:
            report.zero_hit += 1
        else:
            report.zero_hit += 1
            if case.category != "no_answer":
                report.failures.append(_case_result(case, ids, hit, rr, "v1"))
        cat = report.by_category.setdefault(case.category, {"total": 0, "hit": 0, "rr_sum": 0.0})
        cat["total"] += 1
        if hit:
            cat["hit"] += 1
            cat["rr_sum"] += rr
    return report, {"p50_ms": _p(latencies, 0.5), "p95_ms": _p(latencies, 0.95)}


def evaluate_v2(gs: GoldenSet, docs: list[dict]) -> tuple[EvalReport, dict]:
    search = HybridSearch(docs, k=60)
    report = EvalReport(golden_set_name=gs.name + "-v2")
    latencies = []
    for case in gs.cases:
        t0 = time.perf_counter()
        result = search.search(case.query, top_k=5)
        latencies.append((time.perf_counter() - t0) * 1000)
        ids = [h.doc_id for h in result.hits]
        rr = 0.0
        hit = False
        for rank, exp in enumerate(case.expected_ids, start=1):
            if exp and exp in ids and rank <= 5:
                rr = 1.0 / rank
                hit = True
                break
        report.total += 1
        if hit:
            report.hit += 1
            report.reciprocal_rank_sum += rr
        elif case.category == "no_answer" and not ids:
            report.zero_hit += 1
        else:
            report.zero_hit += 1
            if case.category != "no_answer":
                report.failures.append(_case_result(case, ids, hit, rr, "v2"))
        cat = report.by_category.setdefault(case.category, {"total": 0, "hit": 0, "rr_sum": 0.0})
        cat["total"] += 1
        if hit:
            cat["hit"] += 1
            cat["rr_sum"] += rr
    return report, {"p50_ms": _p(latencies, 0.5), "p95_ms": _p(latencies, 0.95)}


# ---------- helpers ----------

_V1_ID = re.compile(r"\[(M-\d{6}|[A-Z]{1,3}-\d{4}-\d{4})\]")


def _parse_v1_ids(text: str) -> list[str]:
    if not text or text.startswith("未命中") or text.startswith("错误"):
        return []
    return _V1_ID.findall(text)


def _case_result(case: GoldenCase, predicted: list[str], hit: bool, rr: float, who: str):
    from dataclasses import dataclass
    @dataclass
    class _R:
        case: GoldenCase
        predicted: list[str]
        hit: bool
        reciprocal_rank: float
        note: str = ""
    return _R(case=case, predicted=predicted, hit=hit, reciprocal_rank=rr, note=who)


def _p(xs: list[float], q: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    i = max(0, min(len(s) - 1, int(len(s) * q)))
    return round(s[i], 2)


# ---------- 4. 报告 ----------

def fmt_report(name: str, r: EvalReport, lat: dict) -> str:
    s = r.summary()
    lines = [
        f"=== {name} ===",
        f"  total={s['total']} hit={s['hit']} (HR@5={s['hit_rate']})"
        f" zero_hit={s['zero_hit']} MRR={s['mrr']}",
        f"  latency P50={lat['p50_ms']}ms P95={lat['p95_ms']}ms",
        "  by_category:",
    ]
    for c, v in sorted(s["by_category"].items()):
        hr = v["hit"] / max(1, v["total"])
        lines.append(f"    {c:<10} hit={v['hit']}/{v['total']} (HR={hr:.0%})")
    if r.failures:
        lines.append(f"  failures({len(r.failures)}):")
        for f in r.failures[:8]:
            lines.append(f"    [{f.note}|{f.case.category}] q='{f.case.query}' predicted={f.predicted[:3]}")
    return "\n".join(lines)


def main():
    print(f"⏳ 准备 tmp 仓库（拷贝 ~/knowbase 真实 32 条记忆，不含 staging）…")
    print(f"   TMP={TMP}")
    seed_real_repo()
    docs = load_v1_corpus()
    print(f"  loaded {len(docs)} docs (V1 iter_all 不含 staging,期望 30)")
    print(f"  ID 样本: {sorted(d['doc_id'] for d in docs)[:5]} ... {sorted(d['doc_id'] for d in docs)[-3:]}")

    gs = build_golden_set()
    print(f"⏳ 评测中…（{len(gs)} cases, golden set={gs.name} v{gs.version}）")
    r_v1, lat_v1 = evaluate_v1(gs)
    r_v2, lat_v2 = evaluate_v2(gs, docs)

    print()
    print(fmt_report("V1 (FTS5 + LIKE fallback)", r_v1, lat_v1))
    print()
    print(fmt_report("V2 (Exact + BM25 + Dense + Metadata, RRF k=60, governance + reranker)", r_v2, lat_v2))

    # 增量表
    def delta(a, b):
        return round(b - a, 4)

    s1, s2 = r_v1.summary(), r_v2.summary()
    print()
    print("=== Δ (V2 - V1) ===")
    print(f"  HR@5      {s1['hit_rate']} → {s2['hit_rate']}  (Δ={delta(s1['hit_rate'], s2['hit_rate'])})")
    print(f"  MRR       {s1['mrr']} → {s2['mrr']}  (Δ={delta(s1['mrr'], s2['mrr'])})")
    print(f"  Latency P50  {lat_v1['p50_ms']} → {lat_v2['p50_ms']} ms")
    print(f"  Latency P95  {lat_v1['p95_ms']} → {lat_v2['p95_ms']} ms")

    # 分类对照
    for c in ("exact", "semantic", "no_answer"):
        a = s1["by_category"].get(c, {"hit": 0, "total": 0})
        b = s2["by_category"].get(c, {"hit": 0, "total": 0})
        ha = a["hit"] / max(1, a["total"]) if a["total"] else 0
        hb = b["hit"] / max(1, b["total"]) if b["total"] else 0
        print(f"  {c:<10}  V1={ha:.0%} ({a['hit']}/{a['total']})  V2={hb:.0%} ({b['hit']}/{b['total']})")


if __name__ == "__main__":
    main()