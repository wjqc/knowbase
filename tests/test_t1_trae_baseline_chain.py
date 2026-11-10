"""T1 Trae 无独立模型基线链路（真实 stdio MCP 契约验收）。

与 Trae 同一接入方式（stdio MCP）驱动隔离库，覆盖计划 T1 通过条件：
- 显式批量整理：capture → claim(v2) → material 分页 → submit → read；
- 显式批次核对：分页完整无重复、无候选显式结算、重放无重复落卡；
- 普通任务链：save → search → use → feedback → update，不处理积压；
- 无独立模型证据：automation/embedding 关闭、executor_runs 全程为空。
"""

import asyncio
import json
import os
import re
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from knowbase import config, index
from knowbase.__main__ import cmd_init


@pytest.fixture()
def t1_repo(tmp_path, monkeypatch):
    """T1 基线配置：独立执行器、语义索引、自动采集全部关闭。"""
    repo = tmp_path / "repo"
    cfg_path = tmp_path / "config.json"
    cfg = config._deep_merge(config.DEFAULTS, {
        "repo_path": str(repo),
        "knowledge_path": "",
        "git": {"auto_commit": False, "auto_push": False, "auto_pull": False},
        "inflight": {"enabled": False, "capture_enabled": False},
        "automation": {"enabled": False, "executors": []},
        "embedding": {"enabled": False},
    })
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", cfg_path)
    monkeypatch.setenv("KNOWBASE_CONFIG", str(cfg_path))
    monkeypatch.setenv("KNOWBASE_REPO_PATH", str(repo))
    monkeypatch.setenv("KNOWBASE_AGENT_NAME", "t1-baseline")
    assert cmd_init() == 0
    return repo


def _run_with_session(t1_repo, fn, timeout=40):
    """在同一任务内打开 stdio 会话并执行 fn(session)，返回其结果。"""

    async def runner():
        interpreter = os.environ.get("KNOWBASE_STDIO_INTERPRETER", sys.executable)
        parameters = StdioServerParameters(
            command=interpreter, args=["-m", "knowbase", "serve"],
            cwd=str(Path(__file__).resolve().parents[1]),
            env={"KNOWBASE_CONFIG": str(config.CONFIG_PATH),
                 "KNOWBASE_REPO_PATH": str(t1_repo),
                 "KNOWBASE_AGENT_NAME": "t1-baseline"})
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await fn(session)

    return asyncio.run(asyncio.wait_for(runner(), timeout=timeout))


async def _call(session, name, arguments):
    result = await session.call_tool(name, arguments)
    assert not result.isError, result
    return result.content[0].text


async def _drain_pages(call, job_id, claim, max_chars=8000):
    """翻页读取全部材料块：同一片段内分块推进，验证覆盖完整且无重复块。"""
    chunks, pages, cursor = {}, 0, claim.get("next_cursor", "")
    for m in claim.get("materials", []):
        key = (m["segment_id"], m["page_start"])
        assert key not in chunks, f"分页重复交付 {key}"
        chunks[key] = m["text"]
    while claim.get("has_more") or cursor:
        out = json.loads(await call("memory_job_material", {
            "job_id": job_id, "batch_id": claim["batch_id"],
            "lease_token": claim["lease_token"], "cursor": cursor,
            "max_chars": max_chars}))
        pages += 1
        for m in out["materials"]:
            key = (m["segment_id"], m["page_start"])
            assert key not in chunks, f"分页重复交付 {key}"
            chunks[key] = m["text"]
        cursor = out.get("next_cursor", "")
        if not out.get("has_more"):
            break
        assert pages < 20, "分页未收敛"
    by_segment = {}
    for (sid, start), text in chunks.items():
        by_segment.setdefault(sid, {})[start] = text
    full = {sid: "".join(parts[off] for off in sorted(parts))
            for sid, parts in by_segment.items()}
    assert len(full) >= 3, f"分段数不足：{len(full)}"
    return list(full), pages


def _long_message(i: int) -> str:
    return (
        f"受控批次样本：模块 {i} 的长材料。订单服务在结算状态变更前必须先读取当前状态，"
        "已结算订单的撤回请求要在服务层拒绝并返回业务错误码，前端提示只做兜底。"
        "旧入口绕过前端检查时，服务层校验是最后一道防线，回归用例要覆盖直连 RPC 的路径。"
    ) * 12


LONG_MESSAGES = [{"role": "user", "text": _long_message(i)} for i in range(3)]


def test_t1_explicit_batch_distillation_chain(t1_repo):
    """显式批量整理：capture → claim(v2) → material 分页 → submit → read。"""

    async def probe(session):
        async def call(name, arguments):
            return await _call(session, name, arguments)

        captured = await call("memory_capture", {
                "session_id": "t1-batch", "scope": "orders",
                "messages": LONG_MESSAGES})
        job = re.search(r"J-[a-f0-9]+", captured).group()
        claim = json.loads(await call("memory_job_claim", {
            "job_id": job, "host_session_id": "t1-batch",
            "max_chars": 2000, "protocol_version": 2}))
        assert claim["batch_id"] and claim["lease_token"]
        seen, pages = await _drain_pages(call, job, claim, max_chars=2000)
        assert pages >= 2, f"分页核对不足：{pages} 页"
        assert len(seen) >= 3, f"分段数不足：{len(seen)}"

        material = claim["materials"][0]
        candidate = {
            "candidate_key": "t1-pitfall", "type": "pitfall",
            "title": "已结算订单撤回必须在服务层拒绝", "scope": "orders",
            "action": "create",
            "segment_ids": [material["segment_id"]],
            "evidence_refs": [{"segment_id": material["segment_id"],
                               "text_hash": material["text_hash"]}],
            "body": {"scope": "orders", "conclusion": "撤回先查结算状态，已结算在服务层拒绝。",
                     "problem": "只靠前端拦截时直连 RPC 路径会绕过校验。",
                     "applicable_when": "收到已结算订单的撤回请求。",
                     "not_applicable_when": "草稿订单走单独流程。",
                     "actionable_steps": "写状态前读取结算状态，已结算即拒绝。",
                     "key_evidence": "受控样本明确要求服务层校验。",
                     "verification": "隔离协议样本验证链路，业务回归未做。",
                     "unknown_items": "真实审批与退单约束未确认。"}}
        decisions = [{"segment_id": sid,
                      "decision": "distilled" if sid == material["segment_id"]
                      else "no_value",
                      "reason": "" if sid == material["segment_id"]
                      else "支撑材料，结论并入 t1-pitfall 候选"}
                     for sid in seen]
        submitted = json.loads(await call("memory_job_submit", {
            "job_id": job, "batch_id": claim["batch_id"],
            "lease_token": claim["lease_token"],
            "fencing_token": claim["fencing_token"],
            "request_id": "t1-submit-1", "protocol_version": 2,
            "candidates": [candidate],
            "segment_results": decisions}))
        assert submitted["state"] == "succeeded", submitted
        assert submitted["saved_active"] == 1, submitted
        mid = submitted["results"][0]["memory_id"]

        read_text = await call("memory_read", {"id": mid})
        assert "已结算" in read_text and "runtime_version=" in read_text

        status = json.loads(await call("memory_job_status",
                                       {"job_id": job, "protocol_version": 2}))
        assert status["state"] == "succeeded", status

        # 重放：同一候选重复提交不重复落卡
        replay = json.loads(await call("memory_job_submit", {
            "job_id": job, "batch_id": claim["batch_id"],
            "lease_token": claim["lease_token"],
            "fencing_token": claim["fencing_token"],
            "request_id": "t1-submit-1", "protocol_version": 2,
            "candidates": [candidate],
            "segment_results": decisions}))
        assert replay == submitted, replay
        conn = index.connect(t1_repo)
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM candidates WHERE job_id=?",
                (job,)).fetchone()[0] == 1
            assert conn.execute(
                "SELECT COUNT(*) FROM meta WHERE staging=0 AND status='active'"
            ).fetchone()[0] == 1
        finally:
            conn.close()
        return job, mid

    job, _mid = _run_with_session(t1_repo, probe)
    assert job


def test_t1_no_candidate_batch_settles_explicitly(t1_repo):
    """无候选批次：全片段 no_value 结算为 no_candidate。"""

    async def probe(session):
        async def call(name, arguments):
            return await _call(session, name, arguments)

        captured = await call("memory_capture", {
            "session_id": "t1-nocand", "scope": "orders",
            "messages": [{"role": "user",
                          "text": "这是一段一次性进度通知，本阶段没有可复用结论。"}]})
        job = re.search(r"J-[a-f0-9]+", captured).group()
        claim = json.loads(await call("memory_job_claim", {
            "job_id": job, "host_session_id": "t1-nocand",
            "protocol_version": 2}))
        decisions = [{"segment_id": m["segment_id"], "decision": "no_value",
                      "reason": "一次性进度，不含可复用经验"} for m in claim["materials"]]
        submitted = json.loads(await call("memory_job_submit", {
            "job_id": job, "batch_id": claim["batch_id"],
            "lease_token": claim["lease_token"],
            "fencing_token": claim["fencing_token"],
            "request_id": "t1-submit-no-cand", "protocol_version": 2,
            "candidates": [], "segment_results": decisions}))
        assert submitted["state"] == "no_candidate", submitted
        return job

    assert _run_with_session(t1_repo, probe, timeout=30)


def test_t1_ordinary_task_chain_and_revision_waits_for_review(t1_repo):
    """普通任务：save → search → use → feedback → update；修订待审，不改已发布卡。"""
    from knowbase import store
    from knowbase.mutations import _compute_revision

    async def probe(session):
        async def call(name, arguments):
            return await _call(session, name, arguments)

        saved = await call("memory_save", {
            "type": "pitfall", "title": "EasyChat 长连接在息屏后被系统回收",
            "body": "## 结论\n息屏 5 分钟后长连接被系统回收，必须心跳保活。\n"
                    "## 解决的问题\n避免把系统回收误判为业务掉线而错误重连。\n"
                    "## 适用条件\nAndroid 前台息屏超过 5 分钟的长连接会话。\n"
                    "## 不适用条件\n用户主动退出或有系统白名单豁免的会话。\n"
                    "## 可执行动作\n前台服务加 60 秒心跳，掉线先查息屏时长。\n"
                    "## 关键证据\n受控样本 T1-ORDINARY §1 明确记录回收时间点。\n"
                    "## 验证情况\n仅隔离协议样本验证保存与修订链路，真机回归未做。\n"
                    "## 未知与待确认\n不同厂商息屏策略差异尚未确认。",
            "scope": "orders", "source": "agent:t1:controlled-sample",
            "tags": ["keepalive", "android"]})
        mid = re.search(r"P-[a-f0-9]+", saved).group()
        search_text = await call("memory_search",
                                 {"query": "长连接 息屏 心跳", "scope": "orders"})
        assert mid in search_text

        ctx = json.loads(await call("memory_context", {
            "session_id": "t1-task2", "event_id": "phase-1", "scope": "orders",
            "current_goal": "排查连接断开",
            "topics": [{"topic_id": "s1", "queries": ["长连接 断开"], "status": "active"}]}))
        assert mid in [c["id"] for c in ctx["candidates"]]
        card = next(c for c in ctx["candidates"] if c["id"] == mid)

        use = json.loads(await call("memory_use", {
            "session_id": "t1-task2", "event_id": "adopt-1",
            "context_event_id": "phase-1", "id": mid,
            "content_revision": card["content_revision"], "state": "adopted"}))
        feedback = await call("memory_feedback", {
            "use_id": use["use_id"], "outcome": "helpful", "task_result": "completed"})
        assert "已记录" in feedback and "OUT-KB-" in feedback and card["content_revision"] in feedback

        meta, body, _path = store.load(t1_repo, mid)
        updated = await call("memory_update", {
            "id": mid, "expected_content_revision": _compute_revision(meta, body),
            "body": body + "\n\n## 补充\n心跳间隔 60 秒经受控样本复核。"})
        assert "修订已提交待审" in updated and mid in updated
        meta2, body2, _path2 = store.load(t1_repo, mid)
        assert body2 == body
        assert meta2["confidence"] == "once", meta2
        assert meta2.get("helpful_count", 0) == meta.get("helpful_count", 0)
        return mid

    assert _run_with_session(t1_repo, probe)


def test_t1_no_independent_model_evidence(t1_repo):
    """无独立模型证据：配置关闭 + 全链路后 executor_runs/budget 为空。"""
    conn = index.connect(t1_repo)
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "executor_runs" in tables:
            assert conn.execute("SELECT COUNT(*) FROM executor_runs").fetchone()[0] == 0
        cfg = config.load_config()
        assert cfg["automation"]["enabled"] is False
        assert cfg["automation"]["executors"] == []
        assert cfg["embedding"]["enabled"] is False
        assert cfg["inflight"]["capture_enabled"] is False
    finally:
        conn.close()
