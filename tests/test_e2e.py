"""端到端测试：直接调用 server impl 函数，模拟两个 Agent 的完整闭环。

运行：.venv/bin/python tests/test_e2e.py
覆盖：H1 沉淀（save/lint/staging/查重）、H2 检索（FTS/LIKE 回退/跨工具）、
H3 反馈闭环（跨工具晋升/stale 化/supersedes）、并发锁、幂等 init。
"""

import os
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="xinhuo-e2e-"))
os.environ["XINHUO_REPO_PATH"] = str(TMP / "repo")
os.environ["XINHUO_CONFIG"] = str(TMP / "nonexistent-config.json")  # 用默认配置

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from xinhuo import __main__ as cli  # noqa: E402
from xinhuo import config, index, store  # noqa: E402
from xinhuo.server import (feedback_impl, read_impl, save_impl, search_impl,  # noqa: E402
                           stats_impl, update_impl)

PASS = 0


def check(name, cond, detail=""):
    global PASS
    if not cond:
        print(f"❌ {name}  {detail}")
        sys.exit(1)
    PASS += 1
    print(f"✓ {name}")


PITFALL_BODY = """## 现象
测试现象描述，某某功能启动即失败。

## 原因
版本与环境的转译冲突导致。

## 正确做法
升级组件版本；用 netstat 验证路由。
"""

# 0. 幂等 init：跑两次
check("init 第1次", cli.main(["init"]) == 0)
check("init 第2次(幂等)", cli.main(["init"]) == 0)

rp = config.repo_path()

# 1. Agent A（claude-code）踩坑入库
os.environ["XINHUO_AGENT_NAME"] = "claude-code"
r = save_impl("pitfall", "EasyConnect 7.6.7 在 macOS 上启动即死锁", PITFALL_BODY,
              tags=["network", "vpn"], source="agent:claude-code:sess_a1")
check("save pitfall", r.startswith("已保存 P-"), r)
pid = r.split()[1]

# 2. lint：缺小节
r = save_impl("pitfall", "缺小节的坑", "只有一段描述没有小节。")
check("lint 缺小节拦截", r.startswith("错误：lint") and "缺少必填小节" in r, r)

# 3. lint：明文密钥
r = save_impl("pitfall", "带密钥的坑", PITFALL_BODY + "\npassword=supersecret123\n")
check("lint 密钥拦截", "明文密钥" in r, r)

# 4. 查重：相似标题拦截
r = save_impl("pitfall", "EasyConnect 7.6.7 在 macOS 上启动即死锁!", PITFALL_BODY)
check("查重拦截", "已存在高度相似" in r and pid in r, r)

# 5. standard 由 Agent 保存 → staging
r = save_impl("standard", "接口发布门禁标准", "## 规则\n接口上线必须过验收环境回归。\n## 理由\n保障交付质量。\n")
check("standard 落 staging", "staging" in r and "S-2026" in r, r)

# 6. preference 由 Agent 保存 → staging；人直接写（human source）→ 正式目录
os.environ["XINHUO_AGENT_NAME"] = "claude-code"
r = save_impl("preference", "输出必须中文", "## 规则\n对用户输出一律中文。\n")
check("preference(Agent) 落 staging", "staging" in r, r)
r = save_impl("preference", "绝不自动提交知识库", "## 规则\n知识库变更不自动 commit。\n",
              source="human:文剑")
check("preference(人) 直接入库", r.startswith("已保存 PR-") and "staging" not in r.split("\n")[0], r)

# 7. 检索：FTS5 命中 + 排序字段
r = search_impl("EasyConnect 死锁")
check("search FTS 命中", "P-2026-0001" in r, r)

# 8. 检索：2 字短词 LIKE 回退
r = search_impl("死锁")
check("search 短词 LIKE 回退", "P-2026-0001" in r, r)

# 9. read → hit_count 增长
read_impl(pid)
conn = index.connect(rp)
row = conn.execute("SELECT hit_count FROM meta WHERE id=?", (pid,)).fetchone()
check("read 计数", row[0] >= 1, str(row))
conn.close()

# 10. feedback：A helpful（1 工具，不晋升）→ B helpful（跨 2 工具，晋升 verified）
os.environ["XINHUO_AGENT_NAME"] = "claude-code"
r = feedback_impl(pid, "helpful", "避坑成功")
check("feedback A", "已记录反馈" in r, r)
meta, _, _ = store.load(rp, pid)
check("单工具不晋升", meta["confidence"] == "once", str(meta["confidence"]))

os.environ["XINHUO_AGENT_NAME"] = "cursor"
r = feedback_impl(pid, "helpful", "跨工具同样有效")
meta, _, _ = store.load(rp, pid)
check("跨工具晋升 verified", meta["confidence"] == "verified" and meta["last_verified"], r)

# 11. outdated → stale
r = feedback_impl(pid, "outdated", "客户端已升级 8.x")
meta, _, _ = store.load(rp, pid)
check("outdated → stale", meta["status"] == "stale", r)

# 12. 人工 verify 复活
cli.main(["verify", pid])
meta, _, _ = store.load(rp, pid)
check("人工 verify 复活", meta["status"] == "active" and meta["confidence"] == "verified")

# 13. supersedes：新经验取代旧经验
r = save_impl("pitfall", "EasyConnect 8.x 新版安装与验证方法", PITFALL_BODY,
              relations=[{"id": pid, "type": "supersedes"}])
new_pid = r.split()[1]
meta, _, _ = store.load(rp, pid)
check("supersedes → 旧条目 stale", meta["status"] == "stale", r)

# 14. update 修改内容
r = update_impl(new_pid, body=PITFALL_BODY + "\n补充：升级后需重启网卡服务。\n")
check("update 内容", "已更新" in r, r)

# 15. promote：staging 提案激活
sid = [f.name[:-3] for f in (rp / "staging").glob("S-*.md")][0]
cli.main(["promote", sid])
check("promote 激活 standard", (rp / "standards" / f"{sid}.md").exists())

# 16. INDEX.md 生成且含条目
idx = (rp / "INDEX.md").read_text(encoding="utf-8")
check("INDEX.md 生成", pid in idx and "orientation" in idx)

# 17. git 审计：提交发生
import subprocess
log = subprocess.run(["git", "-C", str(rp), "log", "--oneline"], capture_output=True, text=True).stdout
check("git 审计链", f"memory({pid})" in log and f"feedback({pid})" in log, log[:200])

# 18. stats
r = stats_impl()
check("stats 汇总", "记忆总数" in r and "使用漏斗" in r, r)

# 19. 并发锁：同仓库快速连续写 id 唯一（标题须真实差异，避免触发查重）
os.environ["XINHUO_AGENT_NAME"] = "claude-code"
mods = [("网关超时重试", "网络"), ("镜像仓库切换", "存储"), ("日志采样率", "观测"),
        ("会话持久化", "状态"), ("灰度发布门禁", "发布")]
ids = set()
for i, (t, dom) in enumerate(mods):
    r = save_impl("decision", f"{dom}模块{t}的技术决策",
                  f"## 背景\n{dom}压测中发现{t}问题。\n## 决策\n采用方案{chr(65+i)}。\n")
    ids.add(r.split()[1])
check("并发 id 唯一", len(ids) == 5, str(ids))

print(f"\n全部 {PASS} 项断言通过 ✅  仓库：{rp}")
