#!/usr/bin/env python3
"""P5-C4: 周期评测 CLI 入口（解析器回归 / 版本对比）。

用法:
    # 跑默认 golden set，输出人类可读文本 + JSON 报告
    ./bin/run_periodic_eval.py run

    # 跑指定 golden set + parser version override
    ./bin/run_periodic_eval.py run \
        --golden-set knowbase-parsers-default \
        --parser-version markdown=2.1.0,code=1.5.3

    # 对比两份 JSON 报告（基线 vs 候选）
    ./bin/run_periodic_eval.py diff --baseline baseline.json --candidate candidate.json

    # 列出已知解析器与实现指纹
    ./bin/run_periodic_eval.py versions

设计要点：
- 子命令式（argparse subparsers），三个动词 run / diff / versions
- 退出码：
  - 0 一切正常 / 0 regression
  - 2 有 regression（CI gate 用）
  - 1 IO 错误 / 参数错误
- JSON 报告含 summary + per-case + 解析器版本快照，方便做历史趋势
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

# 把仓库根加进 sys.path，让 `bin/run_periodic_eval.py` 直接 ./ 执行时不依赖安装
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from knowbase.v2.evaluation.compare import (  # noqa: E402
    compare_reports,
    format_diff,
)
from knowbase.v2.evaluation.parser_runner import (  # noqa: E402
    format_parser_report,
    run_parser_golden_set,
)
from knowbase.v2.evaluation.version_registry import (  # noqa: E402
    compute_parser_versions,
)


# ---------- 辅助：序列化 ----------


def _parser_versions_to_dict(versions: dict) -> dict:
    """Convert ParserVersion map → JSON-friendly dict."""
    out = {}
    for name, pv in versions.items():
        out[name] = {
            "name": pv.name,
            "version": pv.version,
            "impl_sha256": pv.impl_sha256,
            "impl_source": pv.impl_source,
            "extensions": list(pv.extensions),
            "class_qualname": pv.class_qualname,
        }
    return out


def _case_results_to_dict(report) -> list[dict]:
    out = []
    for r in report.case_results:
        out.append({
            "case_id": r.case.case_id,
            "parser": r.case.parser_name,
            "extension": r.case.extension,
            "passed": r.passed,
            "failures": list(r.failures),
            "note": r.case.note,
        })
    return out


def _parse_version_overrides(raw: str | None) -> dict[str, str]:
    """Parse 'markdown=2.1.0,code=1.5.3' → {'markdown':'2.1.0','code':'1.5.3'}."""
    if not raw:
        return {}
    out: dict[str, str] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise SystemExit(f"invalid --parser-version entry: {part!r}")
        name, version = part.split("=", 1)
        out[name.strip()] = version.strip()
    return out


# ---------- 子命令 ----------


def cmd_run(args: argparse.Namespace) -> int:
    versions = compute_parser_versions(
        parser_versions=_parse_version_overrides(args.parser_version),
    )
    report = run_parser_golden_set()
    # 人类可读文本 → stdout
    print(format_parser_report(report))
    print()
    print(f"parser_versions: {len(versions)} entries")
    for name in sorted(versions):
        v = versions[name]
        short = (v.impl_sha256 or "?")[:12]
        print(f"  {name:10s} v{v.version:8s} sha={short:14s} ext={list(v.extensions)}")
    # JSON 报告 → 文件
    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "kind": "parser_eval",
            "report": report.summary(),
            "case_results": _case_results_to_dict(report),
            "parser_versions": _parser_versions_to_dict(versions),
        }
        out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                            encoding="utf-8")
        print(f"\nJSON 报告写入: {out_path}")
    # gate: 失败 → exit 2
    return 2 if report.failed else 0


def cmd_diff(args: argparse.Namespace) -> int:
    base_path = Path(args.baseline)
    cand_path = Path(args.candidate)
    if not base_path.exists():
        print(f"ERROR: baseline not found: {base_path}", file=sys.stderr)
        return 1
    if not cand_path.exists():
        print(f"ERROR: candidate not found: {cand_path}", file=sys.stderr)
        return 1
    base_data = json.loads(base_path.read_text(encoding="utf-8"))
    cand_data = json.loads(cand_path.read_text(encoding="utf-8"))
    # 重建 Report（仅用得到 summary + per-case 的最小子集）
    diff = _diff_from_json(base_data, cand_data)
    print(format_diff(diff))
    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(diff.summary(), indent=2, ensure_ascii=False),
                            encoding="utf-8")
    return 2 if diff.had_regression else 0


def _diff_from_json(base_data: dict, cand_data: dict):
    """Rebuild a RegressionDiff from JSON payloads written by `cmd_run`."""
    from knowbase.v2.evaluation.compare import (
        CaseDelta,
        RegressionDiff,
    )
    base_results = {r["case_id"]: r for r in base_data["case_results"]}
    cand_results = {r["case_id"]: r for r in cand_data["case_results"]}
    all_ids = sorted(set(base_results) | set(cand_results))
    deltas = []
    for cid in all_ids:
        b = base_results.get(cid)
        c = cand_results.get(cid)
        if b is None and c is not None:
            deltas.append(CaseDelta(
                case_id=cid, parser_name=c["parser"], status="added",
                baseline_passed=None, candidate_passed=c["passed"],
                baseline_failures=(), candidate_failures=tuple(c["failures"]),
            ))
        elif c is None and b is not None:
            deltas.append(CaseDelta(
                case_id=cid, parser_name=b["parser"], status="removed",
                baseline_passed=b["passed"], candidate_passed=None,
                baseline_failures=tuple(b["failures"]), candidate_failures=(),
            ))
        else:
            bpass, cpass = b["passed"], c["passed"]
            if bpass and not cpass:
                status = "failed"
            elif not bpass and cpass:
                status = "passed"
            else:
                status = "passed" if cpass else "failed"
            deltas.append(CaseDelta(
                case_id=cid, parser_name=b["parser"] or c["parser"], status=status,
                baseline_passed=bpass, candidate_passed=cpass,
                baseline_failures=tuple(b["failures"]),
                candidate_failures=tuple(c["failures"]),
            ))
    return RegressionDiff(
        baseline_name=base_data["report"]["golden_set"],
        candidate_name=cand_data["report"]["golden_set"],
        deltas=tuple(deltas),
        baseline_total=base_data["report"]["total"],
        candidate_total=cand_data["report"]["total"],
    )


def cmd_versions(_args: argparse.Namespace) -> int:
    versions = compute_parser_versions()
    payload = _parser_versions_to_dict(versions)
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


# ---------- argparse ----------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run_periodic_eval",
        description="P5-C: 周期评测 CLI（parser 回归 + 版本对比）",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="跑默认/指定 golden set，输出文本+JSON")
    p_run.add_argument("--golden-set", default=None,
                       help="golden set 名（暂未支持自定义加载，保留扩展位）")
    p_run.add_argument("--parser-version", default=None,
                       help="parser 版本覆盖，如 'markdown=2.1.0,code=1.5.3'")
    p_run.add_argument("--output", "-o", default=None,
                       help="JSON 报告输出路径")
    p_run.set_defaults(func=cmd_run)

    p_diff = sub.add_parser("diff", help="对比两份 JSON 报告")
    p_diff.add_argument("--baseline", required=True)
    p_diff.add_argument("--candidate", required=True)
    p_diff.add_argument("--output", "-o", default=None,
                        help="diff summary JSON 输出")
    p_diff.set_defaults(func=cmd_diff)

    p_ver = sub.add_parser("versions", help="列出已知解析器与实现指纹")
    p_ver.set_defaults(func=cmd_versions)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
