"""bizrule 类型测试：provenance 硬校验 / 提案制 / import 只产 source。

运行：.venv/bin/python tests/test_bizrule.py
"""

import os, sys, tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="knowbase-bizrule-"))
os.environ["KNOWBASE_REPO_PATH"] = str(TMP / "repo")
os.environ["KNOWBASE_CONFIG"] = str(TMP / "nc.json")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from knowbase import __main__ as cli  # noqa: E402
from knowbase import config, sources, store  # noqa: E402
from knowbase.server import save_impl, search_impl  # noqa: E402

PASS = 0
def check(name, cond, detail=""):
    global PASS
    if not cond: print(f"❌ {name}  {detail}"); sys.exit(1)
    PASS += 1
    print(f"✓ {name}")

cli.main(["init"])
os.environ["KNOWBASE_AGENT_NAME"] = "claude-code"
BODY = ("## 结论\n欠费停机用户不可发起新业务受理，VIP 欠费 ≤50 元可透支。\n\n"
        "## 解决的问题\n统一受理口径，避免一线违规开通。\n\n"
        "## 适用条件\n- 欠费停机状态用户\n- 发起新业务受理\n\n## 不适用条件\n- VIP 欠费 ≤50 元透支额度内\n\n"
        "## 可执行动作\n1. 查询用户欠费状态\n2. 命中欠费停机即拒绝受理\n\n"
        "## 关键证据\nPRD-2026-031 §4.2 受理规则条目。\n\n"
        "## 验证情况\n2026-09-21 业务方按 PRD 条目确认生效。\n\n"
        "## 未知与待确认\n携号转网用户口径未定。\n")

# 1. 无出处 → lint 拦截
r = save_impl("bizrule", "欠费停机不可受理", BODY)
check("无 provenance 拦截", "provenance" in r, r)

# 2. Agent 带出处保存 → 强制 staging
r = save_impl("bizrule", "欠费停机用户不可发起新业务受理", BODY,
              tags=["受理", "计费"], scope="cmi", provenance="PRD-2026-031 §4.2",
              domain="cmi-受理", rule_status="effective")
check("bizrule 落 staging", "staging" in r and "B-2026" in r, r)

# 3. human source 入参不可绕过 staging；人工 CLI promote 后生效
r = save_impl("bizrule", "VIP 透支受理额度规则", BODY,
              tags=["受理"], scope="cmi", provenance="2026-08-12 业务方邮件确认",
              source="human:文剑", domain="cmi-受理", rule_status="effective")
check("human source 不可绕过 staging", "staging" in r, r)
bid = r.split()[1]
check("人工 promote bizrule", cli.main(["promote", bid]) == 0)

# 4. 检索命中业务规则
r = search_impl("欠费停机 受理", scope="cmi")
check("业务规则可检索", bid in r, r[:120])

# 5. import 原始规则文档 → 只产 source artifact（不产 staging 卡，规则提炼走 memory_save）
SRC = TMP / "规则文档"; SRC.mkdir()
(SRC / "透支受理规则.md").write_text("# 透支受理规则\n\n金卡用户欠费 100 元内可透支受理。\n", encoding="utf-8")
import io, contextlib
buf = io.StringIO()
staging_before = len(list((config.repo_path() / "staging").glob("B-*.md")))
with contextlib.redirect_stdout(buf):
    cli.main(["import", str(SRC), "--type", "bizrule", "--scope", "cmi"])
mfs = list(sources.iter_manifests(config.repo_path()))
staging_after = len(list((config.repo_path() / "staging").glob("B-*.md")))
check("import bizrule 只产 source", len(mfs) == 1 and staging_after == staging_before,
      f"manifests={[m['id'] for m in mfs]} staging {staging_before}->{staging_after}")
check("source 内容不进检索", "未命中" in search_impl("金卡用户欠费 100 元内", scope="cmi"))

print(f"\n全部 {PASS} 项断言通过 ✅  {TMP}")
