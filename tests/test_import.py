"""import 命令 + 注入置信度开关 测试。

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
from knowbase import config, hooks, store  # noqa: E402

PASS = 0


def check(name, cond, detail=""):
    global PASS
    if not cond:
        print(f"❌ {name}  {detail}")
        sys.exit(1)
    PASS += 1
    print(f"✓ {name}")


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
    "# 老仓库推送超时\n\n大文件历史导致 push 慢，用 shallow clone 或 lfs 迁移。\n",
    encoding="utf-8")
(SRC / "登录验证码不刷新.md").write_text(
    "验证码缓存键带死用户名，改用组合键。\n", encoding="utf-8")  # 无 # 标题 → 用文件名

# 1. 批量导入
rc = cli.main(["import", str(SRC), "--type", "pitfall", "--scope", "projx"])
check("import 退出码 0", rc == 0)
metas = []
for f in sorted((config.repo_path() / "pitfalls").glob("*.md")):
    meta, body, _ = store.load(config.repo_path(), f.stem)
    metas.append(meta)
check("导入 3 条", len(metas) == 3, str(len(metas)))
check("once 级 + 来源标注", all(m["confidence"] == "once" and m["source"].startswith("human:import") for m in metas))
check("scope 正确", all(m["scope"] == "projx" for m in metas))
check("无标题文件用文件名", any("登录验证码不刷新" in m["title"] for m in metas))

# 2. 导入内容可被检索
out = hooks.user_prompt("数据库连接池 耗尽 排查")
check("导入内容可检索注入", "连接池" in out, out[:80])

# 3. 重复导入：查重全跳过
import io, contextlib
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc2 = cli.main(["import", str(SRC), "--type", "pitfall", "--scope", "projx"])
check("重复导入全跳过", rc2 == 0 and "导入 0，跳过 3" in buf.getvalue(), buf.getvalue()[-60:])

# 4. 注入置信度开关：默认 any（once 可注入）→ 收紧 verified（once 被挡）→ verify 后恢复
out_any = hooks.user_prompt("数据库连接池 耗尽 排查")
check("显式 any 放行 once", out_any != "")
CFG.write_text(json.dumps({"hooks": {"enabled": True, "inject_min_confidence": "verified"}}),
               encoding="utf-8")
out_strict = hooks.user_prompt("数据库连接池 耗尽 排查")
check("verified-only 拦截 once", out_strict == "", out_strict[:60])
pid = metas[0]["id"]
cli.main(["verify", pid])
out_ok = hooks.user_prompt("数据库连接池 耗尽 排查")
check("verify 后恢复注入", pid in out_ok, out_ok[:80])

# 5. --staging 路径
SRC2 = TMP / "待审文档"
SRC2.mkdir()
(SRC2 / "未定稿的部署注意事项.md").write_text("# 未定稿的部署注意事项\n\n内容待评审。\n", encoding="utf-8")
buf2 = io.StringIO()
with contextlib.redirect_stdout(buf2):
    cli.main(["import", str(SRC2), "--type", "standard", "--scope", "projx", "--staging"])
stg = list((config.repo_path() / "staging").glob("S-*.md"))
check("staging 导入路径", len(stg) == 1, str([f.name for f in stg]))

# 6. MCP 主动检索不受开关影响（verified-only 只收紧自动注入口）
from knowbase.server import search_impl
out_mcp = search_impl("数据库连接池")
check("MCP 检索不受开关影响", "连接池" in out_mcp, out_mcp[:80])

print(f"\n全部 {PASS} 项断言通过 ✅  {TMP}")
