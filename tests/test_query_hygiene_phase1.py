"""Phase 1 检索卫生测试：查询门控/蒸馏、词典文件化、门控骨架、遥测字段。

运行：.venv/bin/python tests/test_query_hygiene_phase1.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="knowbase-p1-"))
os.environ["KNOWBASE_REPO_PATH"] = str(TMP / "repo")
os.environ["KNOWBASE_CONFIG"] = str(TMP / "config.json")
os.environ["KNOWBASE_HOOK_STATE"] = str(TMP)
os.environ.pop("KNOWBASE_SYNONYMS", None)
os.environ.pop("CLAUDE_PROJECT_DIR", None)
os.environ.pop("ZCODE_PROJECT_DIR", None)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from knowbase import __main__ as cli  # noqa: E402
from knowbase import config, hooks, index, query_hygiene  # noqa: E402
from knowbase.server import save_impl  # noqa: E402

# 绑定路径守卫：pytest 同进程按字母序先导入其他测试模块时，knowbase.config 可能已绑定
# 真实 ~/.knowbase/config.json（本模块顶部设 env 为时已晚）。此处不在 TMP 即中止，
# 防止任何写操作触达生产配置（2026-10-23 事故根因）。
if not str(config.CONFIG_PATH).startswith(str(TMP)):
    raise SystemExit(f"拒绝运行：CONFIG_PATH 未隔离（{config.CONFIG_PATH}），以独立进程运行本测试")

PASS = 0


def check(name, cond, detail=""):
    global PASS
    if not cond:
        print(f"❌ {name}  {detail}")
        sys.exit(1)
    PASS += 1
    print(f"✓ {name}")


cli.main(["init"])
os.environ["KNOWBASE_AGENT_NAME"] = "phase1-test"
config.CONFIG_PATH.write_text(json.dumps({"hooks": {"inject_min_confidence": "any"}}), encoding="utf-8")
BODY = ("## 结论\n测试卡片结论行，含 docker pull 超时关键词。\n\n"
        "## 解决的问题\n测试。\n\n## 适用条件\n- 测试环境\n\n## 不适用条件\n- 无\n\n"
        "## 可执行动作\n1. 测试\n\n## 关键证据\n测试。\n\n"
        "## 验证情况\n测试。\n\n## 未知与待确认\n无\n")
save_impl("pitfall", "Docker pull 超时镜像加速", BODY, tags=["docker"],
          source="agent:phase1:s1")

# ---------- 1. 查询卫生：跳过规则 ----------

hy = query_hygiene.prepare("x")
check("过短跳过", hy["action"] == "skip" and hy["reason"] == "too_short")

hy = query_hygiene.prepare("一句话" * 400)
check("超长跳过", hy["action"] == "skip" and hy["reason"] == "too_long")

hy = query_hygiene.prepare("看下这几篇 https://a.com/x https://b.com/y https://c.com/z https://d.com/w 的内容")
check("URL 过多跳过", hy["action"] == "skip" and hy["reason"].startswith("doc_paste"))

md = "\n".join(f"# 标题{i}\n正文内容" for i in range(7))
hy = query_hygiene.prepare(md)
check("标题过多跳过", hy["action"] == "skip" and hy["reason"].startswith("doc_paste"))

code = "\n".join(["def f():", "    return 1", "    pass", "    pass", "    pass"])
hy = query_hygiene.prepare(code)
check("代码占比跳过", hy["action"] == "skip" and hy["reason"].startswith("doc_paste"))

# ---------- 2. 查询卫生：蒸馏与标识符保留 ----------

hy = query_hygiene.prepare("帮我看看内网 vpn 连不上的问题")
check("短句原样使用", hy["action"] == "search" and hy["query_class"] == "raw"
      and hy["query"] == "帮我看看内网 vpn 连不上的问题")

long_q = ("我们组有个老问题一直没查清楚，Agent 调用知识库工具时总是提示参数不合法，"
          "之前定位过一次说是 topics 的结构问题。怎么解决？如果还有别的方案也一并说说。")
hy = query_hygiene.prepare(long_q)
check("长句蒸馏保留主干信息", hy["action"] == "search" and hy["query_class"] == "distilled"
      and "参数不合法" in hy["query"] and len(hy["query"]) <= 64, hy["query"])

err_q = ("背景说明比较长，此处省略两百字的上下文描述与排查过程记录，最后发现报错信息是 "
         "INVALID_TOPICS，另一个疑似相关的类是 TopicsValidator。请帮忙看看。")
hy = query_hygiene.prepare(err_q)
check("错误码与类名保留", hy["action"] == "search"
      and "INVALID_TOPICS" in hy["query"] and "TopicsValidator" in hy["query"], hy["query"])

hy = query_hygiene.prepare("好" * 200)
check("无信号跳过", hy["action"] == "skip" and hy["reason"] == "no_signal")

# ---------- 3. 词典文件化 ----------

aliases = index._load_aliases()
check("随包词典含基础条目", aliases.get("连不上") == ("连接失败", "启动失败", "vpn")
      and aliases.get("问一下") == ("征求同意", "确认"), str(aliases))

local = TMP / "synonyms.json"
local.write_text(json.dumps({"连不上": ["网络不通"], "新增短语": ["扩展词"]}, ensure_ascii=False), encoding="utf-8")
os.environ["KNOWBASE_SYNONYMS"] = str(local)
aliases = index._load_aliases()
check("本地覆盖整组替换并追加", aliases.get("连不上") == ("网络不通",)
      and aliases.get("新增短语") == ("扩展词",), str(aliases))
os.environ.pop("KNOWBASE_SYNONYMS")

local.write_text("{broken json", encoding="utf-8")
os.environ["KNOWBASE_SYNONYMS"] = str(local)
aliases = index._load_aliases()
check("本地文件损坏回退随包词典", aliases.get("连不上") == ("连接失败", "启动失败", "vpn"))
os.environ.pop("KNOWBASE_SYNONYMS")

# ---------- 4. 门控骨架：默认关闭不改变结果，开启后可过滤 ----------

rp = config.repo_path()
conn = index.connect(rp)
before = index.search(conn, "Docker pull 超时", limit=5)
check("门控默认关闭结果不变", len(before) >= 1 and before[0]["id"].startswith("P-"))

gate_cfg = {"retrieval": {"gate": {"enabled": True, "min_fused": 99999.0}}}
config.CONFIG_PATH.write_text(json.dumps(gate_cfg), encoding="utf-8")
after = index.search(conn, "Docker pull 超时", limit=5)
check("门控开启词法命中保留", len(after) == len([r for r in before if "lexical" in r["channels"]])
      and all("lexical" in r["channels"] for r in after))
config.CONFIG_PATH.write_text("{}", encoding="utf-8")
conn.close()

# ---------- 5. hook 集成：跳过留痕、蒸馏检索、遥测字段 ----------

config.CONFIG_PATH.write_text(json.dumps({"hooks": {"inject_min_confidence": "any"}}), encoding="utf-8")

paste = ("请评估以下方案。\n\n" + ("架构描述与论证段落，持续输出若干文字。" * 60))
out = hooks.user_prompt(paste)
conn = index.connect(rp)
detail = json.loads(conn.execute(
    "SELECT detail FROM usage_log WHERE tool='auto_search' ORDER BY rowid DESC LIMIT 1"
).fetchone()[0])
conn.close()
check("粘贴文档不注入", out == "")
check("跳过事件留痕含原因与原始长度", detail["query_class"].startswith("skipped:")
      and detail["raw_len"] == len(paste) and detail["hits"] == [], str(detail.get("query_class")))

out = hooks.user_prompt("帮我看看 docker pull 超时怎么办")
check("短句原样命中注入", "ocker" in out, out[:80])
long_hit = ("这周排查任务比较多，先记录一下背景：线上环境最近在拉镜像的时候经常失败，"
            "运维同事反馈过好几次了，帮我看看 docker pull 超时怎么办，需要尽快给个结论。")
out = hooks.user_prompt(long_hit)
check("蒸馏后正常命中注入", "ocker" in out, out[:80])
conn = index.connect(rp)
detail = json.loads(conn.execute(
    "SELECT detail FROM usage_log WHERE tool='auto_search' ORDER BY rowid DESC LIMIT 1"
).fetchone()[0])
conn.close()
check("检索事件含 query_class", detail["query_class"] == "distilled"
      and detail["raw_len"] == len(long_hit) and detail["query"] != long_hit,
      str(detail.get("query_class")))

# 卫生开关关闭时回退旧行为（原样查询）
cfg = {"hooks": {"inject_min_confidence": "any", "query_hygiene": False}}
config.CONFIG_PATH.write_text(json.dumps(cfg), encoding="utf-8")
out = hooks.user_prompt("帮我看看内网 vpn 连不上的问题")
conn = index.connect(rp)
detail = json.loads(conn.execute(
    "SELECT detail FROM usage_log WHERE tool='auto_search' ORDER BY rowid DESC LIMIT 1"
).fetchone()[0])
conn.close()
check("卫生开关关闭回退原样查询", detail["query_class"] == "raw"
      and detail["query"] == "帮我看看内网 vpn 连不上的问题")
config.CONFIG_PATH.write_text("{}", encoding="utf-8")

print(f"\nPhase 1 检索卫生：{PASS} 项全部通过")
