"""解析 pytest 摘要，并执行已在本机配置中登记的验证计划。

解析目标：pytest -ra / -rf 的 short test summary 行，形如
    FAILED tests/test_x.py::test_y - AssertionError: ...
    PASSED tests/test_x.py::test_z
    SKIPPED tests/test_x.py::test_w: reason
    ERROR tests/test_x.py::test_setup
无 summary 行（如纯点号输出）→ checks 为空，verify_execution 按空报告
返回 EMPTY_REPORT（unknown），不猜。

普通解析可以省略 claim_map，只有预登记的完整声明覆盖能进入治理。
"""
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from xml.etree import ElementTree

from .. import config, materials
from . import contracts

# short test summary info 行：状态词 + nodeid（+ 可选 " - reason" / ": reason"）。
# nodeid 不含空格；尾缀分隔用 [ \t]（\s 会吞换行把下一行并进本行匹配）；
# SKIPPED 的 ": reason" 形态以结尾冒号剥离。
_SUMMARY_LINE = re.compile(
    r"^(FAILED|ERROR|PASSED|SKIPPED|XFAIL|XPASS)[ \t]+(\S+)(?:[ \t].*)?$", re.M)
# 末行汇总："N failed, M passed in ..."。
_COUNTS_LINE = re.compile(
    r"^=+(?: short test summary info =+)?[ \t]*(.*?)[ \t]*=+[ \t]*$", re.M)
_PASSED_COUNT = re.compile(r"(\d+)\s+passed")
_FAILED_COUNT = re.compile(r"(\d+)\s+(?:failed|error)")

# 执行戳（统一生命周期：报告必须绑定执行时的卡片版本与任务身份）。
# 由 lifecycle verify --run 在真实执行时写入报告首部；投喂侧核对该戳与
# validator_executions 记录，旧报告改版后不再被绑定到新版本。
_STAMP_LINE = re.compile(r"^knowbase-execution-v1:[ \t]*(\{.*\})[ \t]*$", re.M)
_CHECKS_PREFIX = "\nknowbase-checks-v1:"

_OBSERVED = {"PASSED": "pass", "FAILED": "fail", "ERROR": "error",
             "SKIPPED": "skip", "XFAIL": "skip", "XPASS": "unknown"}


def parse_pytest_text(text: str, *, claim_map: dict | None = None) -> dict:
    """真实 pytest 文本报告 → verify_execution 的 report dict。

    artifact_hash 覆盖报告全文（含执行戳，审计原件指纹）；执行戳解析进
    execution_stamp（无戳为空 dict，verify_execution 按统一生命周期拒绝）。
    """
    text = str(text or "")
    stamp = {}
    m = _STAMP_LINE.search(text)
    if m:
        try:
            parsed = json.loads(m.group(1))
            if isinstance(parsed, dict):
                stamp = parsed
        except ValueError:
            stamp = {}
    checks = []
    for status, nodeid in _SUMMARY_LINE.findall(text):
        check = {"check_id": nodeid[:256].rstrip(":"),
                 "observed_result": _OBSERVED.get(status, "unknown")}
        claim = ((claim_map or {}).get(nodeid) or "").strip()
        if claim:
            check["claim_id"] = claim
        checks.append(check)
    passed = failed = 0
    for line in _COUNTS_LINE.findall(text):
        m = _PASSED_COUNT.search(line)
        if m:
            passed = max(passed, int(m.group(1)))
        m = _FAILED_COUNT.search(line)
        if m:
            failed = max(failed, int(m.group(1)))
    return {
        "artifact_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "expected_hash": "",
        "execution_stamp": stamp,
        "checks": checks,
        "counts": {"passed": passed, "failed": failed,
                   "summary_lines": len(checks)},
        "parser": "pytest-text-v1",
    }


def stamp_report_text(text: str, *, memory_id: str, content_revision: str,
                      run_id: str = "", validator_id: str = "",
                      executed_at: str = "") -> str:
    """在报告首部写入执行戳（lifecycle verify --run 专用，执行时调用）。"""
    stamp = json.dumps({
        "memory_id": memory_id, "content_revision": content_revision,
        "run_id": run_id, "validator_id": validator_id,
        "executed_at": executed_at,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"knowbase-execution-v1: {stamp}\n{text}"


def parse_execution_report(text: str, *, claim_map=None) -> dict:
    report = parse_pytest_text(text, claim_map=claim_map)
    _, marker, encoded = text.rpartition(_CHECKS_PREFIX)
    if marker:
        try:
            structured = json.loads(encoded)
            report["checks"] = structured["checks"]
            report["junit_sha256"] = structured["junit_sha256"]
        except (KeyError, ValueError, TypeError):
            report["checks"] = []
    return report


def binding_for(validator_id, meta, body, revision):
    """Only private, version-bound configuration can authorize a verifier run."""
    raw = config.load_config()["knowledge_lifecycle"].get("validators", [])
    matches = [binding for validator in raw if validator.get("validator_id") == validator_id
               and validator.get("kind") == "pytest"
               for binding in validator.get("bindings", [])
               if binding.get("memory_id") == meta["id"] and binding.get("content_revision") == revision]
    if len(matches) != 1:
        return None
    binding = matches[0]
    required_claims = {c["claim_id"] for c in meta.get("claims", []) if c.get("claim_id")} or {"C-whole"}
    argv = binding.get("argv") or []
    claim_map = binding.get("claim_map") or {}
    files = binding.get("verifier_files") or {}
    inputs = binding.get("input_paths") or []
    group = binding.get("independence_group") or ""
    cwd = Path(binding.get("cwd") or ".").expanduser()
    if (not argv or not Path(argv[0]).is_absolute() or argv[1:4] != ["-I", "-m", "pytest"] or not cwd.is_absolute()
            or not files or not inputs or not group or set(claim_map.values()) != required_claims
            or (binding.get("auto") and not (binding.get("workspace") and binding.get("project_key")))
            or any(node.split("::", 1)[0] not in files for node in claim_map)):
        return None
    fingerprint = contracts.content_v2_hash(meta, body)
    if (meta.get("claims") or meta.get("applicability") or binding.get("contract_fingerprint")) and (
            binding.get("contract_fingerprint") != fingerprint):
        return None
    return {**binding, "cwd": cwd, "argv": argv, "claim_map": claim_map,
            "binding_hash": materials.digest(["pytest-junit-v1", validator_id, binding, fingerprint]),
            "contract_fingerprint": fingerprint}


def _file_hashes(cwd, paths):
    hashes = {}
    for relative in paths:
        path = (cwd / relative).resolve()
        if not path.is_relative_to(cwd) or not path.is_file():
            raise ValueError("unavailable verifier input")
        hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def _junit_checks(raw, claim_map):
    checks = []
    for case in ElementTree.fromstring(raw).iter("testcase"):
        file, name = case.get("file", ""), case.get("name", "")
        module = file.removesuffix(".py").replace("/", ".")
        cls = case.get("classname", "").removeprefix(module).strip(".")
        node = "::".join([file, *cls.split("."), name]) if cls else f"{file}::{name}"
        if node not in claim_map:
            continue
        failure = case.find("failure")
        if failure is not None:
            outcome = "fail" if re.match(r"(?:assert\b|AssertionError\b|Failed:)", failure.get("message", "")) else "unknown"
        elif case.find("error") is not None or case.find("skipped") is not None:
            outcome = "unknown"
        else:
            outcome = "pass"
        checks.append({"check_id": node, "claim_id": claim_map[node], "observed_result": outcome})
    return checks


def run_registered(binding, *, memory_id, revision, run_id, validator_id, executed_at, timeout):
    cwd = binding["cwd"].resolve()
    try:
        files = _file_hashes(cwd, binding["verifier_files"])
        inputs = _file_hashes(cwd, binding["input_paths"])
        if files != binding["verifier_files"]:
            return {"error": "VERIFIER_SOURCE_CHANGED"}
        env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME"}
               and not k.startswith("PYTEST_")}
        env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        with tempfile.TemporaryDirectory(prefix="knowbase-validator-") as temp:
            result_path = Path(temp) / "pytest.xml"
            proc = subprocess.run([*binding["argv"], "-o", "junit_family=xunit1", f"--junitxml={result_path}"],
                                  cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
            junit = result_path.read_bytes() if result_path.is_file() else b""
        if files != _file_hashes(cwd, files) or inputs != _file_hashes(cwd, inputs):
            return {"error": "VALIDATION_INPUT_CHANGED"}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {"error": "VALIDATOR_EXECUTION_UNAVAILABLE"}
    try:
        checks = _junit_checks(junit, binding["claim_map"]) if junit else []
    except ElementTree.ParseError:
        checks = []
    structured = {"checks": checks, "junit_sha256": hashlib.sha256(junit).hexdigest()}
    raw = stamp_report_text(proc.stdout + "\n" + proc.stderr + _CHECKS_PREFIX + contracts.canonical_json(structured), memory_id=memory_id,
                            content_revision=revision, run_id=run_id, validator_id=validator_id,
                            executed_at=executed_at)
    report = parse_execution_report(raw, claim_map=binding["claim_map"])
    expected = set(binding["claim_map"])
    verdict = "unknown"
    if proc.returncode == 1 and any(c["observed_result"] == "fail" for c in checks):
        verdict = "fail"
    elif (proc.returncode == 0 and len(checks) == len(expected)
          and {c["check_id"] for c in checks} == expected and all(c["observed_result"] == "pass" for c in checks)):
        verdict = "pass"
    source_key = materials.digest(sorted(inputs.values()))
    return {"raw": raw, "report": report, "verdict": verdict, "exit_code": proc.returncode,
            "source_key": source_key}
