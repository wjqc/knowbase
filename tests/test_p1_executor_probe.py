"""P1 执行器探针：静态探针、隔离探针、命令执行器与 CLI。"""

import json
import os
import sys

import pytest

from knowbase.automation.executor import (
    CommandExecutorRunner,
    diagnose,
    diagnosis_key,
    discover_executors,
    probe_all,
    probe_executor,
    select_executor,
)
from knowbase.automation.protocol import (
    DECISIVE_EXCEPTION,
    DECISIVE_RULE,
    DECOY_SENTENCE,
    INJECTION_MARKER,
    CapabilityReport,
    ExecutorFailure,
    PolicyViolation,
    build_isolation_material,
    run_isolation_probe,
)

SCOPE = "probe"


def bizrule_candidate(sids, evidence_refs, conclusion, key_evidence, title="已结算订单撤回规则"):
    return {
        "candidate_key": "probe-rule",
        "type": "bizrule",
        "title": title,
        "scope": SCOPE,
        "action": "create",
        "provenance": "隔离探针材料复核结论",
        "body": {
            "scope": SCOPE,
            "conclusion": conclusion,
            "problem": "避免结算后通过撤回改变财务相关的订单状态。",
            "applicable_when": "订单状态为已结算且用户请求撤回订单。",
            "not_applicable_when": "草稿订单使用单独的状态处理流程。",
            "actionable_steps": "读取订单状态，已结算时拒绝撤回并返回业务错误。",
            "key_evidence": key_evidence,
            "verification": "隔离探针材料复核确认了结算状态限制与例外。",
            "unknown_items": "跨系统退款与退单规则尚未确认。",
        },
        "rule_context": {
            "business_object": "订单",
            "trigger": "请求撤回",
            "required_or_forbidden": "已结算时禁止撤回",
            "exceptions": "仅财务红冲流程可处理",
            "applicable_version": "探针材料版本",
            "business_confirmation": "explicit_source",
            "implementation_status": "not_implemented",
        },
        "segment_ids": sids,
        "evidence_refs": evidence_refs,
    }


def page_all(ctx):
    first = ctx.first_page()
    pages = [first]
    cursor, has_more = first["next_cursor"], first["has_more"]
    while has_more:
        page = ctx.page(cursor)
        assert "error" not in page, page
        pages.append(page)
        cursor, has_more = page["next_cursor"], page["has_more"]
    return [m for p in pages for m in p["materials"]]


def uniq_segments(materials):
    """跨页切分的片段会在两页中各出现一次：按 segment_id 去重保序。"""
    seen = set()
    out = []
    for m in materials:
        if m["segment_id"] not in seen:
            seen.add(m["segment_id"])
            out.append(m)
    return out


def maybe_duplicate_evaluation(ctx, candidate):
    similar = ctx.claim.get("similar_cards") or []
    if similar:
        candidate["duplicate_evaluation"] = {
            "snapshot_hash": ctx.claim.get("similarity_snapshot"),
            "evaluated_cards": [{"id": c["id"], "content_revision": c["content_revision"]}
                                for c in similar],
            "rationale": "独立规则，与相似卡不重复",
        }


class GoodRunner:
    def run(self, request, ctx):
        materials = page_all(ctx)
        text = "".join(m.get("text", "") for m in materials)
        assert DECISIVE_RULE in text and DECISIVE_EXCEPTION in text
        unique = uniq_segments(materials)
        sids = [m["segment_id"] for m in unique]
        refs = [{"segment_id": m["segment_id"], "text_hash": m["text_hash"]}
                for m in unique]
        candidate = bizrule_candidate(sids, refs, DECISIVE_RULE, DECISIVE_EXCEPTION)
        maybe_duplicate_evaluation(ctx, candidate)
        result = ctx.submit([candidate],
                            [{"segment_id": sid, "decision": "distilled"} for sid in sids],
                            "probe-good-1")
        return {"submitted": True, "submit_result": result}


class TruncatedRunner:
    """只读第一页：必然拿到诱饵、错过决定性规则。"""

    def run(self, request, ctx):
        first = ctx.first_page()
        materials = first["materials"]
        assert DECOY_SENTENCE in "".join(m.get("text", "") for m in materials)
        # 只提交完整读过的片段：跨页切分的片段在第一页只是部分，
        # 尚未计入 delivered，提交它会被服务端校验挡回。
        complete = [m for m in materials if m.get("complete", True)]
        sids = [m["segment_id"] for m in complete]
        refs = [{"segment_id": m["segment_id"], "text_hash": m["text_hash"]}
                for m in complete]
        candidate = bizrule_candidate(sids, refs, DECOY_SENTENCE, "首段初步结论")
        maybe_duplicate_evaluation(ctx, candidate)
        ctx.submit([candidate],
                   [{"segment_id": sid, "decision": "distilled"} for sid in sids],
                   "probe-truncated-1")
        return {"submitted": True}


class InjectionRunner:
    """读完所有页却服从材料中的注入指令。"""

    def run(self, request, ctx):
        materials = page_all(ctx)
        unique = uniq_segments(materials)
        sids = [m["segment_id"] for m in unique]
        refs = [{"segment_id": m["segment_id"], "text_hash": m["text_hash"]}
                for m in unique]
        candidate = bizrule_candidate(sids, refs, DECISIVE_RULE, DECISIVE_EXCEPTION,
                                      title=INJECTION_MARKER)
        maybe_duplicate_evaluation(ctx, candidate)
        ctx.submit([candidate],
                   [{"segment_id": sid, "decision": "distilled"} for sid in sids],
                   "probe-injection-1")
        return {"submitted": True}


class PolicyRunner:
    def run(self, request, ctx):
        raise PolicyViolation("tried direct writer access")


class RateLimitRunner:
    def run(self, request, ctx):
        raise ExecutorFailure("rate_limited", "model rate limited")


class ProcessExitRunner:
    def run(self, request, ctx):
        raise ExecutorFailure("process_exited", "executor exited nonzero")


class MalformedRunner:
    def run(self, request, ctx):
        materials = page_all(ctx)
        sids = [m["segment_id"] for m in uniq_segments(materials)]
        ctx.submit([{"candidate_key": "bad"}],
                   [{"segment_id": sid, "decision": "distilled"} for sid in sids],
                   "probe-malformed-1")
        return {"submitted": True}


# ---------------------------------------------------------------------------
# 隔离探针：回调式
# ---------------------------------------------------------------------------

def test_good_runner_passes_full_isolation(memory_repo):
    report = run_isolation_probe(GoodRunner(), repo=memory_repo, scope=SCOPE)
    assert report["passed"] is True, report
    assert report["read_mode"] == "callback"
    assert report["checks"]["full_read"] is True
    assert report["checks"]["second_half_rule"] is True
    assert report["checks"]["exception"] is True
    assert report["checks"]["no_decoy"] is True
    assert report["checks"]["no_injection"] is True
    assert report["checks"]["structured_output"] is True
    assert report["checks"]["bounded_tools"] is True
    assert report["checks"]["read_back"] is True
    assert report["memory_id"]


def test_truncated_runner_fails_second_half_and_decoy(memory_repo):
    report = run_isolation_probe(TruncatedRunner(), repo=memory_repo, scope=SCOPE)
    assert report["passed"] is False
    assert report["checks"]["full_read"] is False
    assert report["checks"]["second_half_rule"] is False
    assert report["checks"]["exception"] is False
    assert report["checks"]["no_decoy"] is False


def test_injection_runner_fails_no_injection_only(memory_repo):
    report = run_isolation_probe(InjectionRunner(), repo=memory_repo, scope=SCOPE)
    assert report["passed"] is False
    assert report["checks"]["no_injection"] is False
    assert report["checks"]["second_half_rule"] is True


def test_policy_runner_fails_bounded_tools(memory_repo):
    report = run_isolation_probe(PolicyRunner(), repo=memory_repo, scope=SCOPE)
    assert report["passed"] is False
    assert report["checks"]["bounded_tools"] is False
    assert report["failure_reasons"] == ["policy_violation"]


def test_rate_limit_and_process_exit_are_categorized(memory_repo):
    rate = run_isolation_probe(RateLimitRunner(), repo=memory_repo, scope=SCOPE)
    assert rate["passed"] is False and rate["failure_reasons"] == ["rate_limited"]
    gone = run_isolation_probe(ProcessExitRunner(), repo=memory_repo, scope=SCOPE)
    assert gone["passed"] is False and gone["failure_reasons"] == ["process_exited"]


def test_malformed_candidate_fails_structured_output(memory_repo):
    report = run_isolation_probe(MalformedRunner(), repo=memory_repo, scope=SCOPE)
    assert report["passed"] is False
    assert report["checks"]["structured_output"] is False


# ---------------------------------------------------------------------------
# 隔离探针：提示词式（命令执行器）
# ---------------------------------------------------------------------------

FAKE_GOOD_SCRIPT = r"""
import json, sys
payload = json.load(sys.stdin)
text = payload["materials"]
segments = payload["segments"]
rule = ""
exc = ""
for line in text.splitlines():
    if "最终规则：" in line:
        rule = line.split("最终规则：", 1)[1].strip()
    if line.startswith("例外："):
        exc = line.strip()
assert rule and exc, "decisive rule not found in materials"
seen = set()
uniq = []
for s in segments:
    if s["segment_id"] not in seen:
        seen.add(s["segment_id"])
        uniq.append(s)
segments = uniq
sids = [s["segment_id"] for s in segments]
refs = [{"segment_id": s["segment_id"], "text_hash": s["text_hash"]} for s in segments]
candidate = {
    "candidate_key": "probe-rule", "type": "bizrule",
    "title": "已结算订单撤回规则", "scope": "probe", "action": "create",
    "provenance": "隔离探针材料复核结论",
    "body": {"scope": "probe", "conclusion": rule,
             "problem": "避免结算后通过撤回改变财务相关的订单状态。",
             "applicable_when": "订单状态为已结算且用户请求撤回订单。",
             "not_applicable_when": "草稿订单使用单独的状态处理流程。",
             "actionable_steps": "读取订单状态，已结算时拒绝撤回并返回业务错误。",
             "key_evidence": exc,
             "verification": "隔离探针材料复核确认了结算状态限制与例外。",
             "unknown_items": "跨系统退款与退单规则尚未确认。"},
    "rule_context": {"business_object": "订单", "trigger": "请求撤回",
                     "required_or_forbidden": "已结算时禁止撤回",
                     "exceptions": "仅财务红冲流程可处理",
                     "applicable_version": "探针材料版本",
                     "business_confirmation": "explicit_source",
                     "implementation_status": "not_implemented"},
    "segment_ids": sids, "evidence_refs": refs,
}
out = {"candidates": [candidate],
       "segment_results": [{"segment_id": sid, "decision": "distilled"} for sid in sids]}
print(json.dumps(out, ensure_ascii=False))
"""


def test_command_runner_passes_prompt_mode(memory_repo):
    runner = CommandExecutorRunner({
        "executor_id": "fake-good", "command": sys.executable,
        "noninteractive_argv": ["-c", FAKE_GOOD_SCRIPT],
        "timeout_seconds": 60,
    })
    report = run_isolation_probe(runner, repo=memory_repo, scope=SCOPE)
    assert report["passed"] is True, report
    assert report["read_mode"] == "prompt"
    assert report["checks"]["second_half_rule"] is True
    assert report["checks"]["no_decoy"] is True
    assert report["checks"]["no_injection"] is True


def test_command_runner_false_exits_process_exited(memory_repo):
    runner = CommandExecutorRunner({
        "executor_id": "fake-false", "command": sys.executable,
        "noninteractive_argv": ["-c", "raise SystemExit(1)"], "timeout_seconds": 10,
    })
    report = run_isolation_probe(runner, repo=memory_repo, scope=SCOPE)
    assert report["passed"] is False
    assert report["failure_reasons"] == ["process_exited"]


def test_command_runner_permission_and_rate_limit_mapped(memory_repo):
    perm = CommandExecutorRunner({
        "executor_id": "fake-perm", "command": sys.executable,
        "noninteractive_argv": ["-c",
                                "import sys; sys.stderr.write('permission denied'); sys.exit(126)"],
        "timeout_seconds": 10,
    })
    report = run_isolation_probe(perm, repo=memory_repo, scope=SCOPE)
    assert report["failure_reasons"] == ["permission_client_unavailable"], report
    limited = CommandExecutorRunner({
        "executor_id": "fake-limited", "command": sys.executable,
        "noninteractive_argv": ["-c",
                                "import sys; sys.stderr.write('rate limit exceeded'); sys.exit(1)"],
        "timeout_seconds": 10,
    })
    report = run_isolation_probe(limited, repo=memory_repo, scope=SCOPE)
    assert report["failure_reasons"] == ["rate_limited"], report


# ---------------------------------------------------------------------------
# 静态探针与选择
# ---------------------------------------------------------------------------

def test_missing_binary_is_unsupported(memory_repo):
    report = probe_executor({"kind": "command", "executor_id": "nope",
                             "command": "/nonexistent/xyz-123"})
    assert report.status == "unsupported"
    assert report.reason_code == "binary_missing"


def test_command_probe_auth_gate_and_no_secret_leak(monkeypatch):
    base = {"kind": "command", "executor_id": "py",
            "command": sys.executable, "version_arg": "--version",
            "noninteractive_argv": ["-c"]}
    blocked = probe_executor({**base, "auth_check": {"env_var": "P1_PROBE_SECRET"}})
    assert blocked.status == "blocked"
    assert blocked.reason_code == "auth_unavailable"
    monkeypatch.setenv("P1_PROBE_SECRET", "sentinel-value-123")
    authed = probe_executor({**base, "auth_check": {"env_var": "P1_PROBE_SECRET"}})
    assert authed.status == "unknown"
    assert authed.noninteractive_auth is True
    assert "sentinel-value-123" not in json.dumps(authed.to_dict())
    # 静态探针永远不直接产出 verified：verified 只来自隔离探针。
    assert authed.status != "verified"


def test_intranet_text_probe_never_verified_statically(monkeypatch):
    bad = probe_executor({"kind": "intranet_text", "executor_id": "intra"})
    assert bad.status == "unsupported" and bad.reason_code == "invalid_config"
    cfg = {"kind": "intranet_text", "executor_id": "intra",
           "endpoint": "https://intranet.example/v1", "model": "text-1",
           "secret_ref": "env:P1_PROBE_TEXT_KEY"}
    blocked = probe_executor(cfg)
    assert blocked.status == "blocked" and blocked.reason_code == "auth_unavailable"
    monkeypatch.setenv("P1_PROBE_TEXT_KEY", "sentinel-value-456")
    unknown = probe_executor(cfg)
    assert unknown.status == "unknown"
    assert unknown.reason_code == "protocol_unverified"
    assert "sentinel-value-456" not in json.dumps(unknown.to_dict())


def test_unknown_kind_is_unsupported():
    report = probe_executor({"kind": "telepathy", "executor_id": "x"})
    assert report.status == "unsupported"


def test_discover_finds_configured_cli_without_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "codex"
    script.write_text("#!/bin/sh\necho 'codex-cli 9.9.9'\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))
    found = discover_executors()
    assert [c["executor_id"] for c in found] == ["codex"]
    assert found[0]["noninteractive_argv"] == ["exec"]
    assert "codex-cli" not in json.dumps(found)
    reports = probe_all(found)
    # 该临时 codex 无授权文件也无环境变量：应为 blocked，而非误判可用。
    assert reports[0].status == "blocked"
    assert reports[0].reason_code == "auth_unavailable"


def test_select_executor_prefers_verified_and_upgrades_via_isolation():
    missing = {"kind": "command", "executor_id": "missing",
               "command": "/nonexistent/xyz-123"}
    candidate = {"kind": "command", "executor_id": "py",
                 "command": sys.executable, "version_arg": "--version",
                 "noninteractive_argv": ["-c"],
                 "auth_check": {"env_var": "P1_PROBE_SELECT_KEY"}}
    import os as _os
    _os.environ["P1_PROBE_SELECT_KEY"] = "present"
    try:
        chosen, isolation = select_executor([missing, candidate])
        assert chosen is not None and chosen.status == "unknown"
        assert isolation is None
        chosen, isolation = select_executor(
            [missing, candidate],
            run_isolation=lambda cfg, report, **kw: {"passed": True,
                                                     "usage_source": "observed"})
        assert chosen.status == "verified"
        assert chosen.usage_source == "observed"
        chosen, isolation = select_executor(
            [missing, candidate],
            run_isolation=lambda cfg, report, **kw: {"passed": False})
        assert chosen.status == "blocked"
        assert chosen.reason_code == "isolation_failed"
    finally:
        _os.environ.pop("P1_PROBE_SELECT_KEY", None)


def test_diagnose_key_and_report_validation():
    report = CapabilityReport(executor_id="e", version="1", headless=True,
                              noninteractive_auth=True, bounded_tools=True,
                              structured_output=True, mcp_v2=False,
                              status="unknown")
    key1 = diagnosis_key(report)
    report.status = "blocked"
    report.reason_code = "auth_unavailable"
    assert diagnosis_key(report) != key1
    assert "e" in diagnose(report)
    with pytest.raises(ValueError):
        CapabilityReport(executor_id="e", version=None, headless=True,
                         noninteractive_auth=False, bounded_tools=False,
                         structured_output=False, mcp_v2=False,
                         usage_source="telepathic")
    assert CapabilityReport.from_dict({**report.to_dict(), "secret": "x"}).executor_id == "e"


def test_material_invariants():
    material = build_isolation_material()
    assert len(material["text"].encode("utf-8")) >= 32 * 1024
    assert material["decisive_offset"] > material["total_chars"] // 2
    # 诱饵在第一页内（8000 字符），决定性规则不在第一页。
    assert material["text"].index(material["decoy"]) < 8000
    assert material["decisive_offset"] > 8000


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_cli_probe_reports_blocked_without_executors(tmp_path, monkeypatch, capsys):
    from knowbase.__main__ import _main

    monkeypatch.setenv("PATH", str(tmp_path))
    assert _main(["executor", "probe"]) == 2
    out = json.loads(capsys.readouterr().out)
    assert out["executors"] == []
    assert out["blocked"] is True
    assert out["selected"] is None
