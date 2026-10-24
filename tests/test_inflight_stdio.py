"""经真实 stdio MCP 注册入口贯通，不调用服务端 impl 充当协议验收。"""

import asyncio
import json
import os
import re
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from knowbase import config, store


def test_registered_mcp_pipeline_creates_and_consumes_knowledge(memory_repo):
    async def probe():
        interpreter = os.environ.get("KNOWBASE_STDIO_INTERPRETER", sys.executable)
        server_args = ["-I", "-m", "knowbase", "serve"] if "KNOWBASE_STDIO_INTERPRETER" in os.environ else ["-m", "knowbase", "serve"]
        parameters = StdioServerParameters(command=interpreter, args=server_args,
            cwd=str(Path(__file__).resolve().parents[1]), env={
                "KNOWBASE_CONFIG": str(config.CONFIG_PATH), "KNOWBASE_REPO_PATH": str(memory_repo),
                "KNOWBASE_AGENT_NAME": "stdio-probe"})
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                assert "普通提炼待办不阻断主任务" in initialized.instructions
                assert "入队后继续memory_job_claim" not in initialized.instructions
                registered = {tool.name for tool in (await session.list_tools()).tools}
                assert {"memory_context", "memory_job_material", "memory_job_submit", "memory_use", "memory_metrics"} <= registered
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
                card=next(card for card in context["candidates"] if card["id"]==mid)
                use=json.loads(await call("memory_use",{"session_id":"stdio-s1","event_id":"adopt-phase-2",
                    "context_event_id":"phase-2","id":mid,"content_revision":card["content_revision"],"state":"adopted"}))
                assert use["feedback_recorded"] is False
                feedback=await call("memory_feedback",{"id":mid,"outcome":"helpful",
                    "expected_content_revision":card["content_revision"],"use_id":use["use_id"],
                    "task_outcome_id":"controlled-protocol-verification","request_id":"stdio-feedback"})
                assert "已记录反馈" in feedback
                # 仅为隔离协议样本，不冒充真实业务质量或独立人工验收。
                assert store.load(memory_repo,mid)[0]["helpful_count"]==1
                assert store.load(memory_repo, mid)[0]["confidence"] == "once"
                rule_body="\n\n".join("## "+heading+"\n"+value for heading,value in [
                    ("结论","受控PRD要求已结算订单拒绝撤回。"),("解决的问题","验证规则提案读回不会误显示为已生效。"),
                    ("适用条件","隔离合成订单样本处于已结算状态。"),("不适用条件","真实业务未在本次协议测试中确认。"),
                    ("可执行动作","先核对结算状态，拒绝已结算订单的撤回请求。"),("关键证据","合成协议夹具STDIO-SYNTHETIC-ORDER第1条。"),
                    ("验证情况","仅验证提案保存和读取协议，不证明真实订单代码已实现。"),("未知与待确认","真实业务确认和人工激活尚未完成。")])
                saved=await call("memory_save",{"type":"bizrule","title":"合成PRD结算后的订单状态变更约束", "body":rule_body,
                    "scope":"orders","provenance":"STDIO-SYNTHETIC-ORDER §1",
                    "rule_context":{"business_object":"订单","trigger":"撤回请求","required_or_forbidden":"已结算时拒绝撤回",
                        "exceptions":"草稿由独立规则处理","applicable_version":"合成协议夹具v1",
                        "business_confirmation":"explicit_source","implementation_status":"not_implemented"}})
                rule_id=re.search(r"B-[a-f0-9]+",saved).group()
                rule_read=await call("memory_read",{"id":rule_id,"session_id":"stdio-s1","event_id":"read-staged-rule",
                    "context_event_id":"phase-2"})
                assert "staging=true" in rule_read and "未激活" in rule_read and "runtime_version=" in rule_read
                assert store.load(memory_repo,rule_id)[2].parent.name=="staging"
                metrics=json.loads(await call("memory_metrics",{"source_label":"stdio-device"}))
                assert metrics["schema"]=="knowbase.metrics.v1"
                calls={r["command"]:r["count"] for r in metrics["counts"]["command_calls"]}
                assert calls["memory_context"]==1 and calls["memory_feedback"]==1
                assert "memory_metrics" not in calls
    asyncio.run(asyncio.wait_for(probe(), timeout=30))


def test_registered_mcp_refresh_observes_peer_card(memory_repo,tmp_path):
    from test_inflight_refresh import pair,publish
    peer=pair(memory_repo,tmp_path)
    mid=publish(peer)
    async def probe():
        interpreter=os.environ.get("KNOWBASE_STDIO_INTERPRETER",sys.executable)
        args=["-I","-m","knowbase","serve"] if "KNOWBASE_STDIO_INTERPRETER" in os.environ else ["-m","knowbase","serve"]
        params=StdioServerParameters(command=interpreter,args=args,
            cwd=str(Path(__file__).resolve().parents[1]),env={"KNOWBASE_CONFIG":str(config.CONFIG_PATH),
                "KNOWBASE_REPO_PATH":str(memory_repo),"KNOWBASE_AGENT_NAME":"stdio-refresh"})
        async with stdio_client(params) as (read,write):
            async with ClientSession(read,write) as session:
                await session.initialize()
                loop=asyncio.get_running_loop();deadline=loop.time()+15
                attempt=0
                delivered=False
                while loop.time()<deadline:
                    started=loop.time()
                    result=await session.call_tool("memory_context",{"session_id":"peer-refresh",
                        "event_id":f"stage-{attempt}","scope":"orders","current_goal":"SSE 断线"})
                    context=json.loads(result.content[0].text)
                    if attempt==0:
                        assert loop.time()-started<2,"检索不应等待网络刷新"
                    delivered = delivered or mid in [card["id"] for card in context.get("candidates",[])]
                    if delivered and context["freshness"]["stale"] is False:
                        full=await session.call_tool("memory_read",{"id":mid})
                        assert "消息序号" in full.content[0].text
                        return
                    attempt+=1
                    await asyncio.sleep(0.1)
                raise AssertionError("后台刷新未把同事卡送达正式MCP检索")
    asyncio.run(asyncio.wait_for(probe(),timeout=25))
