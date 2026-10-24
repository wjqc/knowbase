"""B 组实验：A（现状：原文查询）vs B（查询卫生后）在真实库副本上的对比。

只读生产库备份；不写任何真实数据。运行：
  /Users/qc/work/knowbase-phase1/.venv-p1/bin/python experiments/run_ab.py
"""

import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, "/Users/qc/work/knowbase-phase1")

EVAL_DIR = Path("/tmp/kb-phase1-eval")
EVAL_DIR.mkdir(exist_ok=True)
DB = EVAL_DIR / "memory.db"
if not DB.exists():
    subprocess.run(["sqlite3", str(Path.home() / "knowbase" / "memory.db"),
                    f".backup {DB}"], check=True)

os.environ["KNOWBASE_REPO_PATH"] = str(EVAL_DIR)  # connect 指向副本；config 仍读真实配置（只读）

from knowbase import index, query_hygiene  # noqa: E402

CANDIDATES = [json.loads(ln) for ln in
              Path("/Users/qc/work/knowbase/docs/retrieval-upgrade/eval/candidates.jsonl").read_text(
                  encoding="utf-8").splitlines() if ln.strip()]


def hook_like_hits(conn, query, scope, verified_only=True):
    """复现 hook 注入口径：search + verified 过滤 + top3（省略 contradicts/版本过滤，两者只减不增）。"""
    hits = index.search(conn, query, scope=scope, limit=100)
    if verified_only:
        hits = [h for h in hits if h.get("confidence") == "verified"]
    return hits[:3]


def pct(lat, p):
    lat = sorted(lat)
    return lat[min(len(lat) - 1, int(len(lat) * p))]


conn = index.connect(EVAL_DIR)
report = {"per_stratum": {}, "golden": {}, "latency": {}, "notes": []}
rows = []

for c in CANDIDATES:
    raw = c["query"]
    raw_len = c.get("query_len") or len(raw)
    scope = c.get("scope") if c["source"] == "usage_log" else None
    # A：原文（副本里存的是脱敏后 ≤500 字符；长度判定用真实 raw_len 还原生产行为）
    t0 = time.perf_counter()
    a_hits = hook_like_hits(conn, raw, scope)
    a_ms = (time.perf_counter() - t0) * 1000
    # B：查询卫生（对超长原文按真实长度判定跳过）
    probe = query_hygiene.prepare(raw) if raw_len <= 512 else {"action": "skip", "reason": "too_long"}
    t0 = time.perf_counter()
    if probe["action"] == "skip":
        b_hits, b_query, b_cls = [], "", f"skipped:{probe['reason']}"
    else:
        b_hits = hook_like_hits(conn, probe["query"], scope)
        b_query, b_cls = probe["query"], probe["query_class"]
    b_ms = (time.perf_counter() - t0) * 1000
    rows.append({"id": c["id"], "stratum": c["stratum"], "a_n": len(a_hits),
                 "b_n": len(b_hits), "a_ids": [h["id"] for h in a_hits],
                 "b_ids": [h["id"] for h in b_hits], "b_class": b_cls,
                 "a_ms": a_ms, "b_ms": b_ms, "expect": (c["labels"].get("relevant_ids") or [None])[0]
                 if isinstance(c["labels"].get("relevant_ids"), list) and c["labels"].get("relevant_ids")
                 else c["labels"].get("relevant_ids")})
conn.close()

# 分层统计
for s in sorted({r["stratum"] for r in rows}):
    sub = [r for r in rows if r["stratum"] == s]
    report["per_stratum"][s] = {
        "n": len(sub),
        "a_inject": sum(1 for r in sub if r["a_n"] > 0),
        "b_inject": sum(1 for r in sub if r["b_n"] > 0),
        "a_total_injected": sum(r["a_n"] for r in sub),
        "b_total_injected": sum(r["b_n"] for r in sub),
        "b_skip_classes": {k: sum(1 for r in sub if r["b_class"] == k)
                           for k in {r["b_class"] for r in sub if r["b_class"].startswith("skipped")}},
    }

# golden 口径（期望卡出现在 top3）
for kind in ("golden_must", "golden_gap"):
    sub = [r for r in rows if r["stratum"] == kind and r["expect"]]
    report["golden"][kind] = {
        "a_pass": sum(1 for r in sub if r["expect"] in r["a_ids"]),
        "b_pass": sum(1 for r in sub if r["expect"] in r["b_ids"]), "n": len(sub)}

all_ms_a = [r["a_ms"] for r in rows]
all_ms_b = [r["b_ms"] for r in rows]
report["latency"] = {"a_p50_ms": round(pct(all_ms_a, .5), 1), "a_p95_ms": round(pct(all_ms_a, .95), 1),
                     "b_p50_ms": round(pct(all_ms_b, .5), 1), "b_p95_ms": round(pct(all_ms_b, .95), 1)}
report["notes"] = [
    "A 组查询为脱敏后 ≤500 字符副本（长度>512 的按真实 raw_len 判定 B 跳过）",
    "注入口径：search+verified 过滤+top3；省略 contradicts/版本过滤（只减不增，对注入率对比无偏）",
]

print(json.dumps(report, ensure_ascii=False, indent=2))
Path("/Users/qc/work/knowbase/docs/retrieval-upgrade/experiments/ab-results.json").write_text(
    json.dumps({"report": report, "rows": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
