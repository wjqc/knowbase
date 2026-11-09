"""bizrule 统一生命周期测试：出处驱动路由 / staging 待审 / import 只产 source。

运行：.venv/bin/python tests/test_bizrule.py
统一规则（2026-11-09）：类型只作检索分类；项目内新增且有出处直接发布，
共享范围或来源不足进 staging 待审，无按类型的硬拒或强制分流。
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

# 1. 项目内 + 出处 → 直接发布（类型不再强制 staging）
r = save_impl("bizrule", "欠费停机用户不可发起新业务受理", BODY,
              tags=["受理", "计费"], scope="cmi", provenance="PRD-2026-031 §4.2",
              domain="cmi-受理", rule_status="effective")
check("bizrule 项目内+出处直接发布", "已保存 B-" in r and "staging（" not in r, r)
bid_active = r.split()[1]
meta = store.load(config.repo_path(), bid_active)[0]
check("发布为 active+once", meta["confidence"] == "once" and meta["status"] == "active", str(meta)[:200])

# 2. 无出处 → 来源不足进 staging 待审（不再 lint 硬拒）
r = save_impl("bizrule", "欠费停机不可受理兜底口径", BODY, tags=["受理"], scope="cmi")
check("无出处 bizrule 落 staging", "staging" in r, r)
bid_staged = r.split()[1]
check("待审不可检索", "未命中" in search_impl("兜底口径", scope="cmi"))

# 3. 共享范围（global）→ staging 待审；人工 promote 后生效
r = save_impl("bizrule", "VIP 透支受理额度规则", BODY,
              tags=["受理"], scope="global", provenance="2026-08-12 业务方邮件确认",
              source="human:文剑", domain="cmi-受理", rule_status="effective")
check("global bizrule 落 staging", "staging" in r, r)
bid_global = r.split()[1]
check("人工 promote staging 卡", cli.main(["promote", bid_global]) == 0)

# 4. 检索命中已发布业务规则
r = search_impl("欠费停机 受理", scope="cmi")
check("业务规则可检索", bid_active in r, r[:120])

# 5. import 原始规则文档 → 只产 source artifact（不产卡，规则提炼走 memory_save）
SRC = TMP / "规则文档"; SRC.mkdir()
(SRC / "透支受理规则.md").write_text("# 透支受理规则\n\n金卡用户欠费 100 元内可透支受理。\n", encoding="utf-8")
import io, contextlib
buf = io.StringIO()
staging_before = len(list((config.repo_path() / "staging").glob("*.md")))
with contextlib.redirect_stdout(buf):
    cli.main(["import", str(SRC), "--type", "bizrule", "--scope", "cmi"])
mfs = list(sources.iter_manifests(config.repo_path()))
staging_after = len(list((config.repo_path() / "staging").glob("*.md")))
check("import bizrule 只产 source", len(mfs) == 1 and staging_after == staging_before,
      f"manifests={[m['id'] for m in mfs]} staging {staging_before}->{staging_after}")
check("source 内容不进检索", "未命中" in search_impl("金卡用户欠费 100 元内", scope="cmi"))

print(f"\n全部 {PASS} 项断言通过 ✅  {TMP}")
