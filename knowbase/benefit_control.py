"""Paired with/without-library control runs (Phase 3-B, threshold 4).

The script schedules the same task set twice — once with the library
injected, once without — and accounts paired token/duration numbers.
It never invents numbers: the executor is caller-supplied (an agent
runner), every arm's figures are validated finite and non-negative,
and net_token_savings is always the recomputed difference of the two
arms, never None and never a placeholder. A report whose digest does
not verify, or whose pairs do not recompute, fails acceptance.
"""

import hashlib
import json
import math
import os
from pathlib import Path

SCHEMA = "knowbase.benefit-control.v1"


def _canonical(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(obj) -> str:
    return hashlib.sha256(_canonical(obj).encode("utf-8")).hexdigest()


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value) and value >= 0


def load_tasks(path) -> list[dict]:
    """Load a task set: a JSON list of {task_id, query}, or a derived
    evaluation dataset (JSONL rows with id/query, as written by
    execution_eval.write_dataset). Duplicate or empty task ids rejected."""
    p = Path(path).expanduser()
    raw = p.read_text(encoding="utf-8")
    tasks: list[dict] = []
    parsed_json = None
    if p.suffix != ".jsonl":
        try:
            parsed_json = json.loads(raw)
        except ValueError:
            parsed_json = None
    if parsed_json is not None:
        items = parsed_json["tasks"] if isinstance(parsed_json, dict) else parsed_json
        for item in items:
            tasks.append({"task_id": item["task_id"], "query": item["query"]})
    else:
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            tasks.append({"task_id": row["id"], "query": row["query"]})
    seen = set()
    for task in tasks:
        if not isinstance(task["task_id"], str) or not task["task_id"]:
            raise ValueError("task_id must be a non-empty string")
        if not isinstance(task["query"], str) or not task["query"]:
            raise ValueError("query must be a non-empty string")
        if task["task_id"] in seen:
            raise ValueError(f"duplicate task_id: {task['task_id']}")
        seen.add(task["task_id"])
    if not tasks:
        raise ValueError("task set is empty")
    return tasks


def _validate_arm(arm, task_id: str, arm_name: str) -> dict:
    if not isinstance(arm, dict):
        raise ValueError(f"{task_id}/{arm_name}: executor must return a dict")
    out = {}
    for key in ("input_tokens", "output_tokens", "seconds"):
        value = arm.get(key)
        if not _is_number(value):
            raise ValueError(f"{task_id}/{arm_name}: {key} must be a finite number >= 0")
        out[key] = value
    return out


def run_control(tasks: list[dict], executor) -> dict:
    """Run both arms per task and account the paired difference.

    executor(task, use_library: bool) -> {"input_tokens", "output_tokens",
    "seconds"}. Totals count input+output tokens as reported by the
    executor; net_token_savings = without_total - with_total (negative
    is honest: the library cost more than it saved on that set).
    """
    pairs = []
    with_tokens = without_tokens = 0
    with_seconds = without_seconds = 0.0
    for task in tasks:
        with_arm = _validate_arm(executor(task, True), task["task_id"], "with_library")
        without_arm = _validate_arm(executor(task, False), task["task_id"], "without_library")
        wt = with_arm["input_tokens"] + with_arm["output_tokens"]
        nt = without_arm["input_tokens"] + without_arm["output_tokens"]
        with_tokens += wt
        without_tokens += nt
        with_seconds += with_arm["seconds"]
        without_seconds += without_arm["seconds"]
        pairs.append({"task_id": task["task_id"], "query": task["query"],
                      "with_library": with_arm, "without_library": without_arm,
                      "with_tokens": wt, "without_tokens": nt,
                      "net_tokens": nt - wt,
                      "net_seconds": without_arm["seconds"] - with_arm["seconds"]})
    totals = {"task_count": len(pairs),
              "with_tokens": with_tokens, "without_tokens": without_tokens,
              "net_token_savings": without_tokens - with_tokens,
              "with_seconds": with_seconds, "without_seconds": without_seconds,
              "net_seconds": without_seconds - with_seconds}
    report = {"schema": SCHEMA, "totals": totals, "pairs": pairs}
    report["digest"] = _digest({"pairs": pairs, "totals": totals})
    return report


def write_report(report: dict, path) -> Path:
    """Write a control report (0600). The digest is recomputed by readers."""
    out = Path(path).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        os.chmod(out, 0o600)
        f.write(json.dumps(report, ensure_ascii=False, indent=2))
    return out


def load_report(path) -> dict:
    """Load a control report and verify its digest; mismatch raises."""
    raw = Path(path).expanduser().read_bytes()
    try:
        report = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("BENEFIT_REPORT_MALFORMED") from exc
    if not isinstance(report, dict) or report.get("schema") != SCHEMA:
        raise ValueError("BENEFIT_REPORT_UNSUPPORTED_SCHEMA")
    if report.get("digest") != _digest({"pairs": report.get("pairs"),
                                        "totals": report.get("totals")}):
        raise ValueError("BENEFIT_REPORT_DIGEST_MISMATCH")
    return report
