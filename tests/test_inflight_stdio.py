"""经真实 stdio MCP 注册入口贯通，不调用服务端 impl 充当协议验收。"""

import asyncio
import json
import re
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from knowbase import config, store


def test_registered_mcp_pipeline_creates_and_consumes_knowledge(memory_repo):
    async def probe():
        parameters = StdioServerParameters(command=sys.executable, args=["-m", "knowbase", "serve"],
            cwd=str(Path(__file__).resolve().parents[1]), env={
                "KNOWBASE_CONFIG": str(config.CONFIG_PATH), "KNOWBASE_REPO_PATH": str(memory_repo),
                "KNOWBASE_AGENT_NAME": "stdio-probe"})
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                registered = {tool.name for tool in (await session.list_tools()).tools}
                assert {"memory_context", "memory_job_material", "memory_job_submit"} <= registered
                async def call(name, arguments):
                    result = await session.call_tool(name, arguments)
                    assert not result.isError, result
                    return result.content[0].text
                captured = await call("memory_capture", {"session_id": "stdio-s1", "scope": "orders",
                    "messages": [{"role": "user", "text": "受控样本：订单撤回应在服务层检查结算状态。"}]})
                job = re.search(r"J-[a-f0-9]+", captured).group()
                claim = json.loads(await call("memory_job_claim", {"job_id": job, "host_session_id": "stdio-s1", "protocol_version": 2}))
                material = claim["materials"][0]
                candidate = {"candidate_key": "stdio-order", "type": "pitfall", "title": "服务层订单结算检查的回归经验",
                    "scope": "orders", "action": "create", "segment_ids": [material["segment_id"]],
                    "evidence_refs": [{"segment_id": material["segment_id"], "text_hash": material["text_hash"]}],
                    "body": {"scope": "orders", "conclusion": "订单撤回先执行结算状态检查。",
                        "problem": "旧入口绕过前端检查时仍需保证服务层拒绝非法撤回。",
                        "applicable_when": "撤回入口收到已经结算的订单。", "not_applicable_when": "草稿订单有单独逻辑。",
                        "actionable_steps": "在写订单状态前读取结算状态，已结算时拒绝撤回。",
                        "key_evidence": "受控输入明确说明校验要放在服务层。",
                        "verification": "仅用隔离协议样本验证保存与检索链路，未验证真实订单业务。",
                        "unknown_items": "真实业务的审批和退单约束尚未确认。"}}
                submitted = json.loads(await call("memory_job_submit", {"job_id": job, "batch_id": claim["batch_id"],
                    "lease_token": claim["lease_token"], "fencing_token": claim["fencing_token"],
                    "request_id": "stdio-submit", "protocol_version": 2, "candidates": [candidate],
                    "segment_results": [{"segment_id": material["segment_id"], "decision": "distilled"}]}))
                assert submitted["state"] == "succeeded", submitted
                mid = submitted["results"][0]["memory_id"]
                context = json.loads(await call("memory_context", {"session_id": "stdio-s1", "event_id": "phase-2",
                    "scope": "orders", "stage": "subtask_start", "current_goal": "订单撤回服务层校验",
                    "topics": [{"topic_id": "stage-two", "queries": ["订单 撤回 结算"], "status": "active"}]}))
                assert mid in [card["id"] for card in context["candidates"]]
                assert "结算状态" in await call("memory_read", {"id": mid})
                assert store.load(memory_repo, mid)[0]["confidence"] == "once"
    asyncio.run(asyncio.wait_for(probe(), timeout=30))
