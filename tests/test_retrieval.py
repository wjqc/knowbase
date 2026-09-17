"""检索质量 golden set 评测（打真实库 ~/knowbase）。

must 类 = 回归红线，任何一条失败即退出码 1（改排序公式/换分词/上向量必须跑）；
gap 类 = 已知词法层盲区（同义改写），只报告不判失败——L2 Agent 改写 / L3 向量检索的目标，
        二期验收标准：gap 类 HitRate 从基线提升且 must 类不回退。

运行：.venv/bin/python tests/test_retrieval.py
基线（2026-09-17，v1.3）：must 8/8，gap 0/4，HitRate@3 = 8/12
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from knowbase import config, index  # noqa: E402

CASES = [
    # (query, 期望首命中 id（None=应为空）, 类别)
    ("EasyConnect 死锁", "P-2026-0001", "must"),
    ("vpn", "P-2026-0001", "must"),
    ("Docker 镜像加速", "P-2026-0005", "must"),
    ("docker pull 超时", "P-2026-0005", "must"),
    ("根目录 git", "P-2026-0004", "must"),
    ("门户加载慢", "P-2026-0006", "must"),
    ("HAR", "P-2026-0002", "must"),
    ("数据库", None, "must"),
    # —— 已知盲区：同义改写/口语化（词法层无重叠）——
    ("内网连不上怎么办", "P-2026-0001", "gap"),
    ("系统很卡", "P-2026-0006", "gap"),
    ("日报怎么写", "W-2026-0001", "gap"),
    ("部署要问一下吗", "PR-2026-0001", "gap"),
]


def main() -> int:
    rp = config.repo_path()
    if not rp.exists():
        print("错误：真实记忆库未初始化（knowbase init）")
        return 1
    conn = index.connect(rp)
    must_fail, gap_hit, rows = [], 0, []
    for q, expect, kind in CASES:
        hits = index.search(conn, q, limit=3)
        ids = [h["id"] for h in hits]
        if kind == "must":
            ok = (ids == [] if expect is None else expect in ids)
            if not ok:
                must_fail.append((q, expect, ids))
            rows.append((q, expect, kind, ok, ids))
        else:
            hit = expect in ids
            gap_hit += hit
            rows.append((q, expect, kind, hit, ids))
    conn.close()

    print(f"{'query':<18}{'期望':<14}{'类别':<6}结果")
    print("-" * 72)
    for q, expect, kind, ok, ids in rows:
        mark = "✓" if ok else ("○" if kind == "gap" else "✗")
        print(f"{q:<18}{str(expect):<14}{kind:<6}{mark} top3={ids}")

    total = len(CASES)
    must_total = sum(1 for _, _, k, _, _ in rows if k == "must")
    print("-" * 72)
    print(f"must 回归红线：{must_total - len(must_fail)}/{must_total}"
          + ("  ❌ 存在回退！" if must_fail else "  ✅"))
    print(f"gap 同义盲区命中：{gap_hit}/{total - must_total}（L2 改写/L3 向量的提升目标）")
    print(f"HitRate@3 = {sum(1 for _, _, k, ok, _ in rows if k == 'must' and ok) + gap_hit}/{total}")
    return 1 if must_fail else 0


if __name__ == "__main__":
    sys.exit(main())
