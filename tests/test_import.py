"""import 命令（source artifact）+ 注入置信度开关 测试。

运行：.venv/bin/python tests/test_import.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="knowbase-import-"))
os.environ["KNOWBASE_REPO_PATH"] = str(TMP / "repo")
CFG = TMP / "config.json"
os.environ["KNOWBASE_CONFIG"] = str(CFG)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from knowbase import __main__ as cli  # noqa: E402
from knowbase import config, hooks, sources, store  # noqa: E402
from knowbase.server import save_impl, search_impl  # noqa: E402

PASS = 0


def check(name, cond, detail=""):
    global PASS
    if not cond:
        print(f"❌ {name}  {detail}")
        sys.exit(1)
    PASS += 1
    print(f"✓ {name}")


def card(conclusion, problem, applies, excludes, action, evidence,
         verified="2026-09-21 生产环境实测确认。", unknown="长期方案未评估。"):
    return (f"## 结论\n{conclusion}\n\n## 解决的问题\n{problem}\n\n## 适用条件\n{applies}\n\n"
            f"## 不适用条件\n{excludes}\n\n## 可执行动作\n{action}\n\n## 关键证据\n{evidence}\n\n"
            f"## 验证情况\n{verified}\n\n## 未知与待确认\n{unknown}\n")


# 准备：初始化 + 存量文档目录
cli.main(["init"])
os.environ["KNOWBASE_AGENT_NAME"] = "claude-code"
os.environ["CLAUDE_PROJECT_DIR"] = "/tmp/projx"
CFG.write_text(json.dumps({"hooks": {"inject_min_confidence": "any"}}), encoding="utf-8")
SRC = TMP / "存量文档"
SRC.mkdir()
(SRC / "数据库连接池耗尽排查.md").write_text(
    "# 数据库连接池耗尽排查\n\n高峰期连接池打满，应用挂起。\n临时方案：重启应用并扩容 maxPool。\n",
    encoding="utf-8")
(SRC / "老仓库推送超时.md").write_text(
    "# 老仓库推送超时\n\n大文件历史导致 push 超时，用 shallow clone 或 lfs 迁移。\n",
    encoding="utf-8")
(SRC / "登录验证码不刷新.md").write_text(
    "验证码缓存键带死用户名，改用组合键。\n", encoding="utf-8")  # 无 # 标题 → 用文件名

# 1. 批量导入 → 只产 source artifact
rc = cli.main(["import", str(SRC), "--type", "pitfall", "--scope", "projx"])
check("import 退出码 0", rc == 0)
mfs = list(sources.iter_manifests(config.repo_path()))
check("导入 3 个 source", len(mfs) == 3, str(len(mfs)))
check("不产生知识卡", len(list(store.iter_all(config.repo_path(), True))) == 0)
check("scope 正确", all(m["scope"] == "projx" for m in mfs))
check("无标题文件用文件名", any("登录验证码不刷新" in m["title"] for m in mfs))
check("快照保真", any("maxPool" in (config.repo_path() / m["object"]).read_text(encoding="utf-8") for m in mfs))

# 2. source 不进入默认检索与自动注入
out = search_impl("数据库连接池 耗尽 排查", scope="projx")
check("source 不进默认检索", "未命中" in out, out[:80])

# 3. 重复导入：内容寻址去重全跳过
import io, contextlib
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc2 = cli.main(["import", str(SRC), "--type", "pitfall", "--scope", "projx"])
check("重复导入全跳过", rc2 == 0 and "导入 0 个 source artifact" in buf.getvalue()
      and "跳过 3" in buf.getvalue(), buf.getvalue()[-80:])

# 4. 提炼为知识卡（八项结构）后可检索、可注入；置信度开关照旧生效
POOL_CARD = card(
    "高峰期连接池打满致应用挂起，重启并扩容 maxPool 可恢复。",
    "避免把连接池耗尽误判为应用缺陷而反复重启无效。",
    "- 高峰流量集中\n- 应用挂起且无异常堆栈", "- 连接泄漏（须修代码）",
    "1. 重启应用恢复\n2. 扩容 maxPool\n3. 复盘池参数",
    "监控显示活跃连接数等于 maxPool 且等待队列增长。")
os.environ["KNOWBASE_AGENT_NAME"] = "claude-code"
r = save_impl("pitfall", "数据库连接池耗尽排查", POOL_CARD,
              tags=["db", "pool"], scope="projx", source="agent:claude-code:sess_i1")
pid = r.split()[1]
out_any = hooks.user_prompt("数据库连接池 耗尽 排查")
check("any 放行 once 注入", pid in out_any, out_any[:80])
CFG.write_text(json.dumps({"hooks": {"enabled": True, "inject_min_confidence": "verified"}}),
               encoding="utf-8")
out_strict = hooks.user_prompt("数据库连接池 耗尽 排查")
check("verified-only 拦截 once", out_strict == "", out_strict[:60])
cli.main(["verify", pid])
out_ok = hooks.user_prompt("数据库连接池 耗尽 排查")
check("verify 后恢复注入", pid in out_ok, out_ok[:80])

# 5. --staging 路径同样只产 source（--type/--staging 已废弃）
SRC2 = TMP / "待审文档"
SRC2.mkdir()
(SRC2 / "未定稿的部署注意事项.md").write_text("# 未定稿的部署注意事项\n\n内容待评审。\n", encoding="utf-8")
buf2 = io.StringIO()
with contextlib.redirect_stdout(buf2):
    cli.main(["import", str(SRC2), "--type", "standard", "--scope", "projx", "--staging"])
check("staging 导入同样只产 source",
      len(list(sources.iter_manifests(config.repo_path()))) == 4
      and not list((config.repo_path() / "staging").glob("*.md")))

# 6. MCP 主动检索不受开关影响（verified-only 只收紧自动注入口）
out_mcp = search_impl("数据库连接池", scope="projx")
check("MCP 检索不受开关影响", pid in out_mcp, out_mcp[:80])

print(f"\n全部 {PASS} 项断言通过 ✅  {TMP}")
