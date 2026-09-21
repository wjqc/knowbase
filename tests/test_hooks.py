"""Hook 集成测试：自动检索注入 / 漏存阻断一次性 / 规则注入。

运行：.venv/bin/python tests/test_hooks.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="knowbase-hooks-"))
os.environ["KNOWBASE_REPO_PATH"] = str(TMP / "repo")
os.environ["KNOWBASE_CONFIG"] = str(TMP / "no-config.json")
os.environ["KNOWBASE_HOOK_STATE"] = str(TMP)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from knowbase import __main__ as cli  # noqa: E402
from knowbase import config, hooks  # noqa: E402
from knowbase.server import save_impl  # noqa: E402

PASS = 0


def check(name, cond, detail=""):
    global PASS
    if not cond:
        print(f"❌ {name}  {detail}")
        sys.exit(1)
    PASS += 1
    print(f"✓ {name}")


BODY = ("## 结论\nEasyConnect 7.6.7 在 macOS 26.1 上启动即 Rosetta 死锁。\n\n"
        "## 解决的问题\n避免把启动死锁误判为系统故障反复重装。\n\n"
        "## 适用条件\n- macOS 26.1 arm64\n- EasyConnect 7.6.7\n\n"
        "## 不适用条件\n- Linux 客户端\n\n## 可执行动作\n1. 升级客户端\n2. netstat 验证路由\n\n"
        "## 关键证据\nnetstat -rn | grep ^172 无路由表项。\n\n"
        "## 验证情况\n2026-09-21 macOS 实机复现修复确认。\n\n"
        "## 未知与待确认\nWindows 平台未验证。\n")

# 准备：初始化 + 存一条记忆
cli.main(["init"])
os.environ["KNOWBASE_AGENT_NAME"] = "claude-code"
config.CONFIG_PATH.write_text(json.dumps({"hooks": {"inject_min_confidence": "any"}}), encoding="utf-8")
save_impl("pitfall", "EasyConnect 7.6.7 在 macOS 上启动即死锁", BODY,
          tags=["network", "vpn"], source="agent:claude-code:sess_t1")
save_impl("pitfall", "proj07 场景部署异常排查", BODY,
          tags=["部署", "proj07"], scope="proj07", source="agent:claude-code:sess_t1")

# 1. 规则注入
text = hooks.session_start()
check("session-start 注入规则", "memory_search" in text and "memory_feedback" in text)

# 2. 自动检索：相关提示词命中
out = hooks.user_prompt("帮我看看内网 vpn 连不上的问题")
check("user-prompt 命中注入", "P-2026-0001" in out and "knowbase 自动检索" in out, out[:80])

# 3. 自动检索：无关/过短提示词静默
check("user-prompt 短输入静默", hooks.user_prompt("你好") == "")
check("user-prompt 无命中静默", hooks.user_prompt("今天天气真不错啊朋友们") == "")

# 4. 漏存阻断：有实质操作、无记忆操作 → 阻断一次
tr = TMP / "transcript.jsonl"
tr.write_text("\n".join([
    json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Edit", "input": {}}]}}),
    json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {}}]}}),
]), encoding="utf-8")
out1 = hooks.stop_event("sess-A", str(tr))
out2 = hooks.stop_event("sess-A", str(tr))
r1 = json.loads(out1)
check("stop 首次阻断", r1["decision"] == "block" and "memory_save" in r1["reason"])
check("stop 二次放行", out2 == "")

# 5. 已沉淀会话 → 不阻断
tr2 = TMP / "transcript2.jsonl"
tr2.write_text("\n".join([
    json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Edit", "input": {}}]}}),
    json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "memory_save", "input": {}}]}}),
]), encoding="utf-8")
check("stop 已沉淀放行", hooks.stop_event("sess-B", str(tr2)) == "")

# 5b. 读取过记忆但未回填 → 阻断提醒反馈
tr2b = TMP / "transcript2b.jsonl"
tr2b.write_text(json.dumps({"type": "assistant", "message": {"content": [
    {"type": "tool_use", "name": "memory_read", "input": {}}]}}), encoding="utf-8")
out_fb = hooks.stop_event("sess-D", str(tr2b))
rf = json.loads(out_fb)
check("stop 读后未回填阻断", rf["decision"] == "block" and "memory_feedback" in rf["reason"], out_fb[:120])

# 5c. 读后已回填 → 放行
tr2c = TMP / "transcript2c.jsonl"
tr2c.write_text("\n".join([
    json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "memory_read", "input": {}}]}}),
    json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "memory_feedback", "input": {}}]}}),
]), encoding="utf-8")
check("stop 已回填放行", hooks.stop_event("sess-E", str(tr2c)) == "")

# 6. 无实质操作 → 不阻断
tr3 = TMP / "transcript3.jsonl"
tr3.write_text(json.dumps({"type": "assistant", "message": {"content": [
    {"type": "text", "text": "只是聊天"}]}}), encoding="utf-8")
check("stop 无操作放行", hooks.stop_event("sess-C", str(tr3)) == "")

# 7. CLI 入口（管道模拟 stdin）
import subprocess
p = subprocess.run(
    [".venv/bin/python", "-m", "knowbase", "hook", "user-prompt"],
    input=json.dumps({"prompt": "EasyConnect 死锁"}),
    capture_output=True, text=True)
check("CLI hook 入口", "P-2026-0001" in p.stdout, p.stdout[:80] + p.stderr[:80])

# 7b. zcode 风格输出：严格 JSON additionalContext
import json as _json
out = subprocess.run(
    [".venv/bin/python", "-m", "knowbase", "hook", "user-prompt", "--style", "zcode"],
    input=_json.dumps({"prompt": "EasyConnect 死锁"}), capture_output=True, text=True).stdout
parsed = _json.loads(out)
check("zcode 风格 additionalContext", "additionalContext" in parsed and "P-2026-0001" in parsed["additionalContext"], out[:100])

# 7c. 项目感知：目录名推断 scope → 注入本项目优先且带标注
proj_dir = TMP / "proj07"; proj_dir.mkdir(exist_ok=True)
os.environ["CLAUDE_PROJECT_DIR"] = str(proj_dir)
out = hooks.user_prompt("部署 出问题了 怎么办")
check("scope 推断+标注", "scope=" in out and "proj07" in out, out[:150])
import re as _re
scopes_in = _re.findall(r"scope=([^）\)]+)", out)
check("注入无他项目混入", all(s == "proj07" for s in scopes_in), str(scopes_in))
os.environ.pop("CLAUDE_PROJECT_DIR", None)

# 8. auto_search 计入统计
conn = config.repo_path()
from knowbase import index  # noqa: E402
c = index.connect(conn)
s = index.stats(c)
c.close()
check("auto_search 计入漏斗", s["funnel"]["auto_search"] >= 2, str(s["funnel"]))

print(f"\n全部 {PASS} 项断言通过 ✅  {TMP}")
