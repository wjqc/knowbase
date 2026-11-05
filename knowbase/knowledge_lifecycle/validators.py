"""首个注册验证器适配器（W1-3）：pytest 文本输出 → validation report。

信任语义：registered-adapter 的信任来自「登记（config）+ 本地显式调用」。
解析器只做确定性文本解析，不执行任何命令、不访问网络；报告文件由调用方
（CLI / CI）用真实 pytest 运行产物提供。

解析目标：pytest -ra / -rf 的 short test summary 行，形如
    FAILED tests/test_x.py::test_y - AssertionError: ...
    PASSED tests/test_x.py::test_z
    SKIPPED tests/test_x.py::test_w: reason
    ERROR tests/test_x.py::test_setup
无 summary 行（如纯点号输出）→ checks 为空，verify_execution 按空报告
返回 EMPTY_REPORT（unknown），不猜。

claim 映射可选：claim_map 为 {nodeid: claim_id}；缺省卡级验证（claim_id
留空，claim 校验退化为卡存在性）。
"""
import hashlib
import re

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

_OBSERVED = {"PASSED": "pass", "FAILED": "fail", "ERROR": "fail",
             "SKIPPED": "skip", "XFAIL": "skip", "XPASS": "pass"}


def parse_pytest_text(text: str, *, claim_map: dict | None = None) -> dict:
    """真实 pytest 文本报告 → verify_execution 的 report dict。

    artifact_hash 覆盖报告全文（审计原件指纹）；expected_hash 留空
    （基线比对属调用方流程，不在解析器内伪造）。
    """
    text = str(text or "")
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
        "checks": checks,
        "counts": {"passed": passed, "failed": failed,
                   "summary_lines": len(checks)},
        "parser": "pytest-text-v1",
    }
