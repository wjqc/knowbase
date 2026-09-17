"""bizrule 类型测试：provenance 硬校验 / 提案制 / import 强制 staging。

运行：.venv/bin/python tests/test_bizrule.py
"""

import os, sys, tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="knowbase-bizrule-"))
os.environ["KNOWBASE_REPO_PATH"] = str(TMP / "repo")
os.environ["KNOWBASE_CONFIG"] = str(TMP / "nc.json")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from knowbase import __main__ as cli  # noqa: E402
from knowbase import config, store  # noqa: E402
from knowbase.server import save_impl, search_impl  # noqa: E402

PASS = 0
def check(name, cond, detail=""):
    global PASS
    if not cond: print(f"❌ {name}  {detail}"); sys.exit(1)
    PASS += 1
    print(f"✓ {name}")

cli.main(["init"])
os.environ["KNOWBASE_AGENT_NAME"] = "claude-code"
BODY = "## 规则\n欠费停机用户不可发起新业务受理。\n## 例外\nVIP 欠费 ≤50 元可透支。\n## 出处\nPRD-2026-031 §4.2。\n"

# 1. 无出处 → lint 拦截
r = save_impl("bizrule", "欠费停机不可受理", "## 规则\n欠费停机用户不可受理。\n")
check("无 provenance 拦截", "provenance" in r, r)

# 2. Agent 带出处保存 → 强制 staging
r = save_impl("bizrule", "欠费停机用户不可发起新业务受理", BODY,
              tags=["受理", "计费"], scope="cmi", provenance="PRD-2026-031 §4.2",
              domain="cmi-受理", rule_status="effective")
check("bizrule 落 staging", "staging" in r and "B-2026" in r, r)

# 3. 人直接写 → 直接入库
r = save_impl("bizrule", "VIP 透支受理额度规则", BODY,
              tags=["受理"], scope="cmi", provenance="2026-08-12 业务方邮件确认",
              source="human:文剑", domain="cmi-受理", rule_status="effective")
check("人写直入 bizrules/", r.startswith("已保存 B-2026") and "staging" not in r.splitlines()[0], r)
bid = r.split()[1]

# 4. 检索命中业务规则
r = search_impl("欠费停机 受理")
check("业务规则可检索", bid in r, r[:120])

# 5. import 强制 staging + 出处占位
SRC = TMP / "规则文档"; SRC.mkdir()
(SRC / "透支受理规则.md").write_text("# 透支受理规则\n\n金卡用户欠费 100 元内可透支受理。\n", encoding="utf-8")
import io, contextlib
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    cli.main(["import", str(SRC), "--type", "bizrule", "--scope", "cmi"])
stg = list((config.repo_path() / "staging").glob("B-*.md"))
check("import bizrule 强制 staging", (config.repo_path() / "staging" / "B-2026-0003.md").exists(), str([f.name for f in stg]))
meta, _, _ = store.load(config.repo_path(), "B-2026-0003")
check("出处占位待补", "待补出处" in meta.get("provenance", ""), str(meta.get("provenance")))

print(f"\n全部 {PASS} 项断言通过 ✅  {TMP}")
