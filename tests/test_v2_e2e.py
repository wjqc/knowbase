"""V2 端到端测试：模拟「真实文档 → V2 摄取 → 增量更新 → 冲突仲裁 → 审计回溯」全链路。

运行：.venv/bin/python tests/test_v2_e2e.py

覆盖：
- H1 多格式摄取（md / txt / pdf / docx）
- H2 幂等：相同内容不重复写
- H3 版本演进：内容变更触发 version CAS
- H4 解析失败：Tombstone / ERROR 状态可识别
- H5 状态机：DISCOVERED → ... → READY
- H6 审计：每步 Operation 可回溯
- H7 V1 兼容：V1 index.db 与 V2 v2.db 文件独立
- H8 unknown 后缀 → 友好错误
- H9 kill_switch：flag 关闭后服务拒收
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="knowbase-v2-e2e-"))
REPO = TMP / "repo"
os.environ["KNOWBASE_REPO_PATH"] = str(REPO)
os.environ["KNOWBASE_CONFIG"] = str(TMP / "nonexistent-config.json")
os.environ["KNOWBASE_V2_INGESTION"] = "true"
os.environ["KNOWBASE_V2_IDEMPOTENT_INGEST"] = "true"
os.environ["KNOWBASE_V2_PARSER_MARKDOWN"] = "true"
os.environ["KNOWBASE_V2_PARSER_TXT"] = "true"
os.environ["KNOWBASE_V2_PARSER_PDF"] = "true"
os.environ["KNOWBASE_V2_PARSER_DOCX"] = "true"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from knowbase.v2 import features as _features  # noqa: E402
from knowbase.v2.domain import (  # noqa: E402
    DocumentStatus, KnowledgeKind, OperationKind, SourceKind,
)
from knowbase.v2.features import FlagRegistry  # noqa: E402
from knowbase.v2.ingestion import IngestionService  # noqa: E402
from knowbase.v2.ingestion.parsers import register_builtin  # noqa: E402
from knowbase.v2.observability.errors import (  # noqa: E402
    FlagDisabledError, NotConfiguredError, ParseError,
)
from knowbase.v2.repositories import V2Repository  # noqa: E402

PASS = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS
    if not cond:
        print(f"❌ {name}  {detail}")
        sys.exit(1)
    PASS += 1
    print(f"✓ {name}")


# ---------- 0. flag 清零 + 内置解析器注册 ----------
_features._global = FlagRegistry()
register_builtin()


# ---------- 1. repo 隔离：V1 与 V2 数据库文件分离 ----------
# V1 走自己的 init（仅走一条命令，让 .knowbase/index.db 出现）
from knowbase import __main__ as cli  # noqa: E402
from knowbase import config, index  # noqa: E402
check("init 第1次", cli.main(["init"]) == 0)
check("init 第2次(幂等)", cli.main(["init"]) == 0)
rp = config.repo_path()
v1_db = rp / "memory.db"
v2_db = rp / ".knowbase" / "v2.db"
# V1 memory.db 在 init 时不一定落盘，强制 connect() 触发建库
index.connect(rp).close()
check("V1 memory.db 存在", v1_db.exists(), str(v1_db))

repo = V2Repository(rp)
svc = IngestionService(repo)
check("V2 v2.db 与 V1 独立", v2_db.exists() and v2_db != v1_db, f"v1={v1_db} v2={v2_db}")


# ---------- 2. H1 多格式摄取 ----------
md = rp / "doc.md"
md.write_text("# 标题\n\n第一段。\n\n第二段包含「EasyConnect 死锁」。\n",
              encoding="utf-8")
r_md = svc.ingest_file(md)
check("md 摄取", r_md.changed and r_md.parser == "markdown" and r_md.chunks >= 1,
      str(r_md))

txt = rp / "notes.txt"
txt.write_text("这是纯文本笔记。\n第二行。\n", encoding="utf-8")
r_txt = svc.ingest_file(txt)
check("txt 摄取", r_txt.changed and r_txt.parser == "txt", str(r_txt))

# PDF：pypdf 真实写一份多页
try:
    from pypdf import PdfWriter
    pdf = rp / "doc.pdf"
    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    w.add_blank_page(width=200, height=200)
    with open(pdf, "wb") as fp:
        w.write(fp)
    r_pdf = svc.ingest_file(pdf)
    check("pdf 摄取", r_pdf.changed and r_pdf.parser == "pdf"
          and r_pdf.version_id != "", str(r_pdf))
except ImportError:
    print("⚠ pypdf 未安装，跳过 pdf 摄取断言")


# DOCX：python-docx 真实写一份
try:
    from docx import Document as DocxDocument
    docx_path = rp / "doc.docx"
    d = DocxDocument()
    d.add_paragraph("第一段 DOCX 内容")
    d.add_paragraph("第二段包含「vpn 启动失败」")
    d.save(str(docx_path))
    r_docx = svc.ingest_file(docx_path)
    check("docx 摄取", r_docx.changed and r_docx.parser == "docx", str(r_docx))
except ImportError:
    print("⚠ python-docx 未安装，跳过 docx 摄取断言")


# ---------- 3. H2 幂等 ----------
r_idem = svc.ingest_file(md)
check("幂等：内容未变", r_idem.changed is False
      and r_idem.version_id == r_md.version_id, str(r_idem))


# ---------- 4. H3 版本演进 ----------
md.write_text("# 标题 v2\n\n第一段。\n\n第二段新增「Redis 雪崩」关键词。\n",
              encoding="utf-8")
r_v2 = svc.ingest_file(md)
check("版本演进：新版本号", r_v2.changed and r_v2.version_id != r_md.version_id, str(r_v2))
active = repo.active_version(r_v2.doc_id)
check("active 指向新版本", active is not None and active.id == r_v2.version_id, str(active))
all_vers = repo.list_versions(r_v2.doc_id)
check("旧版本 superseded", all_vers[1].superseded_by == r_v2.version_id, str(all_vers))


# ---------- 5. H4 解析失败 → ERROR 状态 ----------
# 注入一个总是抛 ParseError 的 .err 解析器,验证状态机 + 审计可回溯
from knowbase.v2.ingestion.parsers.base import DocumentParser, ParsedDocument  # noqa: E402
from knowbase.v2.observability.errors import ParseError as _PE  # noqa: E402


class _AlwaysFailParser(DocumentParser):
    name = "always_fail"

    def supports(self, path):
        return path.suffix.lower() == ".err"

    def parse(self, path):
        raise _PE(parser=self.name, reason="synthetic failure", path=str(path))


svc.parsers.register(_AlwaysFailParser())
bad = rp / "broken.err"
bad.write_bytes(b"trigger failure")
try:
    svc.ingest_file(bad)
    check("解析失败应抛 ParseError", False, "未抛异常")
except ParseError:
    all_docs = repo.list_documents()
    broken_doc = next((d for d in all_docs if d.path == str(bad.resolve())), None)
    check("解析失败落 ERROR", broken_doc is not None
          and broken_doc.status == DocumentStatus.ERROR, str(broken_doc))
    err_ops = repo.list_operations(broken_doc.id)
    check("解析失败审计可回溯", any(op.payload.get("error") == "synthetic failure"
                                   for op in err_ops), str([op.payload for op in err_ops]))


# ---------- 6. H5 状态机 ----------
got = repo.get_document(r_md.doc_id)
check("READY 状态最终一致", got is not None
      and got.status == DocumentStatus.READY
      and got.content_hash == active.content_hash, str(got))


# ---------- 7. H6 审计：每步 Operation 可回溯 ----------
ops = repo.list_operations(r_md.doc_id, limit=20)
ingest_ops = [op for op in ops if op.kind == OperationKind.INGEST]
check("INGEST 审计存在", len(ingest_ops) >= 2, str([op.kind for op in ops]))


# ---------- 8. H8 unknown 后缀 → NotConfiguredError ----------
weird = rp / "weird.xyz"
weird.write_text("???", encoding="utf-8")
try:
    svc.ingest_file(weird)
    check("unknown 后缀应抛 NotConfiguredError", False)
except NotConfiguredError as e:
    check("unknown 后缀友好提示", "no parser supports" in str(e), str(e))


# ---------- 9. Tombstone ----------
svc.tombstone(r_txt.doc_id, reason="上游删除")
got_t = repo.get_document(r_txt.doc_id)
check("tombstone 后 status=TOMBSTONED",
      got_t is not None and got_t.status == DocumentStatus.TOMBSTONED, str(got_t))
ops_t = repo.list_operations(r_txt.doc_id)
check("tombstone 写入审计", any(op.kind == OperationKind.TOMBSTONE
                                for op in ops_t), str([op.kind for op in ops_t]))


# ---------- 10. H7 V1 兼容：V1 6 工具仍可工作 ----------
from knowbase.server import search_impl, stats_impl  # noqa: E402
# V1 搜索（V2 摄取的内容 V1 看不到：数据库分离，这是预期）
v1_search = search_impl("EasyConnect")
check("V1 search 不受 V2 影响", v1_search.startswith("未命中") or "命中" in v1_search, v1_search[:60])
v1_stats = stats_impl()
check("V1 stats 仍工作", "记忆总数" in v1_stats, v1_stats[:60])


# ---------- 11. H9 kill_switch ----------
_features._global = FlagRegistry()
os.environ.pop("KNOWBASE_V2_INGESTION", None)
try:
    IngestionService(repo)
    check("kill_switch 后构造应抛 FlagDisabledError", False)
except FlagDisabledError as e:
    check("kill_switch 生效", e.flag == "v2_ingestion", str(e))


# ---------- 12. 多源类型 ----------
# Source registry 应包含至少 2 类：FILE 已有；再加一个 REMOTE 占位
from knowbase.v2.domain import Source  # noqa: E402
src_remote = Source.from_locator(SourceKind.REMOTE, "https://example.com/feed.json")
repo.upsert_source(src_remote)
got_src = repo.find_source(SourceKind.REMOTE, "https://example.com/feed.json")
check("REMOTE Source 可注册/查询", got_src is not None, str(got_src))


print(f"\n全部 {PASS} 项断言通过 ✅  仓库：{rp}")
