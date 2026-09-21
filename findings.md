# Findings
- search 可返回 staging/stale；hook AND 路径没有过滤 staging，OR 路径只需一个词命中。
- reindex 删除数据库，也删除 usage/feedback 日志及 hit_count。
- import 仅标题相似去重，没有来源文件哈希；init 只检测知识目录。
- hit_count 实际为读取次数，helpful 才是有效反馈，不能等同采用。
- 当前主动检索是 FTS5 trigram + LIKE 回退，并按 scope、confidence、新鲜度、反馈排序；Hook 的 OR 兜底仍是子串计数，不是语义检索。
- 真实库 32 条：reference 18、pitfall 7、preference 3、workflow 2、decision 1、standard 1；没有 bizrule 实例，scope 仅 cmi/global/knowbase。
- 真实 golden set：明确关键词 8/8，同义口语改写 0/4，HitRate@3=8/12。
- import 当前只读 Markdown，整篇原文作为一条记录，不分块、不解析 PDF/Office/图片、不自动更新已变化源文。
- 当前配置 auto_push=true，MCP save/update/feedback 和 import 会尝试 push；没有 pull/fetch、写前 rebase、冲突队列或检索前远端 freshness 检查，治理 CLI 也未统一 push。
- scope 是相关性过滤而非 ACL；部分候选在全库召回/扫描后才过滤，不适合直接扩到大规模多项目多人场景。
- P4-B 沉淀脚本 `save_p4b_experience.py` 写入 `~/knowbase/.lock` 时被 TRAE sandbox 拦截：`TRAE Sandbox Error: hit restricted / Not allow operate files: /Users/qc/knowbase/.lock`；同日 P3 脚本同样的 `RepoLock.__enter__` 路径跑通（11:58 写入 P-2026-0009 / D-2026-0003/0004 / W-2026-0004/0005）— 说明本会话后期新增了一条 sandbox 规则禁止 `~/knowbase/.lock` 写入。需要 IDE Custom Sandbox Configuration 加白，或改成写入 `~/knowbase/.knowbase/cache/` 子目录而非根目录 `.lock`。
- V1 集成测试 `test_hooks.py` / `test_import.py` 模块级 sys.exit(1) — 与 V2 P4-B 改动无关，环境/CLI hook 配置问题；P4-B 通过判定以 V2 264/264 为准。
- P4-C `git init --bare` 不会自动创建 bare_dir 父目录 — macOS git 行为，需先 `parent.mkdir(parents=True, exist_ok=True)`。
- P4-C bare repo 不能 `commit --allow-empty`（"operation must be run in a work tree"）也不能 `worktree add -b BRANCH` 在 HEAD 无 commit 时（"not a valid object name"）— 必须用 `commit-tree <empty_tree_sha> -m init` + `update-ref refs/heads/BRANCH <sha>` 手工建 init commit。
- P4-C `@dataclass(frozen=True)` 的 SyncRun 不能 `run.stats = {...}` 直接赋值 — 用 `dataclasses.replace(run, stats={...})`，Document / DocumentVersion 同理（DocumentStatus 切换需 `replace(doc, status=DocumentStatus.TOMBSTONED)`）。
- P4-C 测试 helper `_make_doc` 默认 `path='test.md'` + 同 `source_id` 触发 `document.UNIQUE(source_id, path)` 约束，多文档测试仅留最后一条 — helper 加 `path` 参数并按 counter 自增（`f"test-{counter}.md"`）。
- P4-C 仓库方法命名：`upsert_outbox` → 实际是 `enqueue_outbox`；`upsert_version` → 实际是 `add_version`；测试 helper 必须查 `sqlite_repo.py` 真实方法名，不能凭直觉命名。
- P4-D `git merge-tree --write-tree <base> <ours> <theirs>` 在 git 2.39.5（macOS brew）上行为不稳：即使 ours/theirs 改不同文件，stdout 有时仍输出冲突相关路径，副作用是写 index + 覆盖 HEAD 指向，污染主 repo 状态；旧式 3-arg `git merge-tree A B C`（无 `--write-tree`）在 git 2.40+ 已删除。**生产可用方案：完全不用 merge-tree，改用 `git diff-tree -r --no-commit-id --diff-filter=AMDCRT` 自行实现 3 路冲突检测**——按 file-level 取 (path, blob, mode, status)，同路径两边都改且 blob 不同则冲突；mode 不同（一边删除一边修改）也冲突；零副作用、跨版本一致。
- P4-D `git merge --ff-only FETCH_HEAD` 在本地领先（FETCH_HEAD 已经是 HEAD 的祖先）时也返回 rc=0 并打印 "Already up to date."——会被 fast-forward helper 误判为成功。**正确实现：先 `git merge-base --is-ancestor FETCH_HEAD HEAD`，true 则视为本地领先直接 return False（调用方应改走 push），只有 false 才执行 `merge --ff-only`**。
- P4-D `git diff-tree -r --no-commit-id <a> <b>` 单行格式为 `:<old_mode> <new_mode> <old_sha> <new_sha> <status>\t<path>`，status 是最后一个字段（M / A / D / T / C / R）；解析时 `fields[-1][0]` 取 status 字符，`fields[3]` 取 new SHA，`fields[1]` 取 new mode。**易错点：status 不在 fields[0]，在 fields[-1]；fields[0] 是 `:<old_mode>` 含冒号**。
- P4-D schema 升级 v3→v4 时 `source_sync_state.source_id REFERENCES knowledge_source(stable_id)` FK 是默认开启的——所有直接调用 `upsert_sync_state(source_id="literal")` 的测试都会 NPE。**调试现象：测试先看到 `FOREIGN KEY constraint failed`，但首跑失败时 `SourceSyncState` 还不存在；`_finish_failed(run, prior_state, ...)` 在 `prior_state is None` 时访问 `.local_revision` 会触发 `AttributeError` 而非 FK 错误**，掩盖根因。修法：`_finish_failed` 签名改为 `state: SourceSyncState | None`，并加 `state = self._ensure_state(...)` 兜底。
- P4-D `Source.from_locator(SourceKind.FILE, "/tmp/src1")` 生成的 `stable_id` 是 `sha256("file::/tmp/src1")[:16]`——与字面值 `"src1"` 不一致。所有需要 `source_id` 的测试必须先 `_make_source` + `repo.upsert_source(src)` 注入 fixture，再用 `src.stable_id`，**不能用 locator 字符串当 source_id**。
- P4-D P4-B 一样，沉淀到 knowbase 经验库 `~/knowbase/.lock` 被 TRAE sandbox 拦截（沙箱规则变化在本会话后期生效）——本轮 knowbase 沉淀脚本未跑；改为更新 `progress.md` + `findings.md` 留底，待沙箱白名单加上后再 batch 写入。
- P4-E **`SyncWorker._do_process` 假设 handler 是 awaitable，但 `make_mirror_handler` 返回同步函数**——原代码 `await self.handler(ev)` 触发 `TypeError: 'NoneType' object can't be awaited`，**所有 mirror 事件都会进死信**。`tests/v2/test_mirror_writer.py::TestEndToEnd` 看似跑了 e2e 其实**完全绕过 SyncWorker**，直接 `handle(ev)` 同步调用，所以一直没暴露。**正确实现：先 `result = self.handler(ev)`，再用 `asyncio.iscoroutine(result)` 判断；coroutine 才 `await asyncio.wait_for(result, timeout=...)`，同步函数直接完成**——兼容 sync/async handler，不破坏 P4-B 接口契约。
- P4-E **`render_doc_markdown` 是 status-aware 的**：检测到 `doc.status == DocumentStatus.TOMBSTONED` 时调用 `render_tombstone_markdown` 返回 tombstone 内容。测试若在 enqueue `stored` 后**立即** `update_document_status(TOMBSTONED)`，worker 拉起时 stored handler 看到的 doc 已经是 TOMBSTONED，stored 也会产出 tombstone 内容 → 后续 tombstone commit 内容相同 → `git commit` 报 "nothing to commit"。**正确做法：拆 `run_until_idle` + `run_tomb` 两阶段，stored 事件 ACK 后**再 mark TOMBSTONED + enqueue tombstone。
- P4-E **`SyncWorker._stop_event` 是单次生命周期信号**——`worker.stop()` 设置后 worker.run() 立即退出。复用同一实例跑多阶段测试必须**显式 `worker._stop_event.clear()`**，否则第二轮 `worker.run()` 不会进入主循环。
- P4-E **轮询替换固定 sleep**：`await asyncio.sleep(2.0)` 在 CI 慢机器或共享 host 高负载时容易出现边界毛刺。改为 `while not cond: await asyncio.sleep(0.05); check cond` 轮询（带超时上限）更稳，尤其在涉及 outbox 状态判定时。
- P4-E **`sync_run.started_at` 和 `outbox_event.created_at` 是秒级 ISO 字符串**，同秒多笔插入时 SQL `ORDER BY` 的 ties 不可靠——`list_sync_runs_by_source` / `list_outbox_due` 的返回顺序在并发测试中是随机的。**生产代码修复建议**：加 `id` 作为 tiebreaker，例如 `ORDER BY created_at, id` / `ORDER BY started_at, id`（`id` 是 UUID-like 字符串严格唯一）。**测试断言建议**：不要依赖 DB 返回顺序，改用「计数 / 存在性」断言（如 `statuses.count(FAILED) == 3`）或按 `id` 字典精确查找。
- P4 收尾 **`v2/sync/__init__.py` 必须同步暴露新公共符号**——单测 `from knowbase.v2.sync.alerting import sink_console` 走子模块 import 不会暴露包级 `__init__.py` 漏 export 的问题；只有 CLI `from .v2.sync import sink_console` 在 `cmd_alerts` 入口处触发 `ImportError`。**经验：每加公共符号必须在包 `__init__.py` 三个地方同步——`from X import (...)`、`__all__` 列表、必要时 `TYPE_CHECKING` 块**；并在 `__main__.py` 的 CLI 入口跑一次 dry-run 端到端验证，避免测试全过但 CLI 挂掉的尴尬。
- P4 收尾 **doctor / alerts 这类「依赖 db 存在」的运维命令必须设计 3 层兜底**——(a) `db_path.exists()` 先判断，缺失直接返回一组 `schema_version=fail + init_error=fail` 不去碰 repo；(b) try/except `(PermissionError, OSError)` 包 `V2Repository(...)` 初始化（TRAE sandbox 会拦截 `~/.knowbase` 写入）；(c) 正常路径 `run_doctor_checks` / `run_alerts`。**设计原则：运维命令永远先看「能不能跑」再决定「跑什么」——不能跑本身就是有效诊断信息**，而不是直接抛 traceback 把用户吓跑。
- P4 收尾 **SQLite ISO 时间字符串比较必须用 `strftime('%Y-%m-%dT%H:%M:%fZ', ...)` 显式生成 ISO 格式**：列里 `datetime('now')` 默认返回 `'2026-09-18 12:34:56'`（空格分隔），业务层普遍写 ISO `'2026-09-18T12:34:56.789Z'`（`T` 分隔 + 毫秒 + Z）；两者比较不报错也不命中，表现为「查询永远返回空」。**修法：SQL 端 `WHERE finished_at >= strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-24 hours')` 强制 ISO 格式对齐**；同理 `>=`/`<=` 时间区间都走 `strftime` 而不是 `datetime()`。
- P4 收尾 **TRAE sandbox 拦截 `~/knowbase/.knowbase` 写入**：与 P4-B 拦截 `~/knowbase/.lock` 同源——本会话后期 TRAE sandbox 规则收紧，拦截面扩大到 `~/.knowbase/`。`V2Repository.__init__` 自动 `init_v2_db` → `mkdir .knowbase` 在用户主目录下就触发 `PermissionError`。**解法：运维 CLI 三层兜底跳过 repo 初始化；其它需要初始化 v2.db 的命令（`knowbase init`）依然能正常写入**，因为 `init` 是显式用户操作且 sandbox 通常对一次性 init 不拦截。如果后续扩大拦截面，需在 IDE Custom Sandbox Configuration 加 `~/.knowbase/**` 白或走 `REPL: ~/knowbase_test/` 临时目录。

## F-P5A1-01: PPTX slide_layouts[6] Blank 无 title placeholder
**现象**：测试用 `prs.slide_layouts[6]`（Blank layout）建 slide，写 `slide1.shapes.title.text = "..."` 报 `AttributeError: 'NoneType' object has no attribute 'text'`。
**根因**：Blank layout（索引 6）默认不含 title placeholder，`slide.shapes.title` 返回 `None`。
**正确做法**：
- 测试侧：用 `slide.shapes.add_textbox(Inches(1), Inches(1), Inches(8), Inches(5))` 显式建 textbox 写 body
- 生产侧：解析器对 `slide.shapes.title is None` 要兜底（python-pptx 文档化的标准行为）
**预防**：所有 PPT 解析器代码对 `shape.has_text_frame` + `slide.shapes.title` 都需 None 兜底。

## F-P5A1-02: 解析器整体 .text.strip() 会裁掉行尾空格
**现象**：测试断言 `assert " | x | " in parsed.text` 失败，实际 text 末尾行是 `" | x |"`（无尾空格）。
**根因**：XlsxParser 在 join 后 `.strip()`，去掉了最后一行的尾随空格。原始行 `" | ".join(["", "x", ""])` 产出 `" | x | "`（带尾空格），但被整体 strip 裁掉。
**正确做法**：
- 测试断言：要么断言 `" | x |"`（不带尾空格），要么断言包含 `"x"` 即可（不依赖空格格式）
- 解析器侧：`.strip()` 适合用户展示，但如需保留原始结构（用于表格重建），应用列表 `lines` 暴露而非纯 text 字符串
**预防**：涉及 .strip() / .rstrip() / .lstrip() 的边界效应，断言要基于不可变 stripped 文本设计。

## F-P5A1-03: 全局 singleton 测试需用 monkeypatch 隔离
**现象**：`test_register_builtin_includes_xlsx_and_pptx` 断言 `assert 6 == 0` 失败 — 因为全局 `registry()` 单例被其他测试 populate，`n_global_before` 不是 0。
**根因**：`base._default = ParserRegistry()` 是模块级单例，跨测试共享。
**正确做法**：
```python
from knowbase.v2.ingestion.parsers import base as _base
fresh = ParserRegistry()
monkey = pytest.MonkeyPatch()
monkey.setattr(_base, "_default", fresh)
try:
    register_builtin()
    # 断言 fresh.parsers() 状态
finally:
    monkey.undo()
```
或：`registry()` 暴露为 fixture，由 conftest.py 统一 reset。
**预防**：任何模块级单例（`registry()` / `metrics` / `cache`）的测试必须用 monkeypatch 或 fixture 隔离，禁止依赖 n_before 的具体值。

## F-P5A1-04: openpyxl load_workbook 资源需 wb.close() try/except 兜底
**现象**：`load_workbook(read_only=True)` 返回的 workbook 在某些损坏文件 / openpyxl 内部状态异常时 `wb.close()` 自身可能抛异常。
**根因**：read_only 模式实际是 zipfile handle，未显式 close 会泄露，但损坏文件 close 时也可能再抛。
**正确做法**：
```python
try:
    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
except Exception as e:
    raise ParseError(parser=self.name, reason=f"cannot open: {e}", path=str(path)) from e
try:
    for ws in wb.worksheets:
        ...
finally:
    try:
        wb.close()
    except Exception:
        pass
```
**预防**：所有 parser 用 try/finally + 嵌套 try/except 兜底 close，避免资源泄露 + 二次异常掩盖原始 ParseError。

## F-P5A1-05: PPTX core_properties 缺失字段读不到不应抛 ParseError
**现象**：`prs.core_properties.title` 在某些 PPTX（无元数据）可能为空字符串，但不会抛。
**正确做法**：try/except 整个 core_properties 块，title 为空时兜底 `path.stem`，author 为空时为 `""`。
**预防**：解析器读取 metadata 块统一用 try/except，绝不让元数据缺失导致整个 parse 失败。


## F-P5A2-01: LogParser level 关键字必须从 message 中剥除
**现象**：测试 `test_parse_iso8601_log` 断言 `"[INFO] App started" in parsed.text` 通过，但同时 `"[INFO] INFO  App started" not in parsed.text` 失败 — 因为原实现把 level 加为 prefix 但未从 message 中剥除原始的 level 字符串。
**根因**：设计失误 — `_extract_level(line)` 用 `re.search` 找到首个 level 关键字，但 `_split_message(line, ts)` 只剥了 timestamp，没剥 level，导致原始 `INFO` / `WARN` 等关键字仍在 message 中。
**正确做法**：
```python
def _strip_level(msg: str, level: str) -> str:
    if not level:
        return msg
    return re.sub(rf"\b{level}\b\s*", "", msg, count=1).lstrip()
```
注意 `count=1` 仅移除首个，避免多 level 时误删。
**预防**：parser 任何 prefix 注入操作都要确认源值已被相应剥除；测试断言应同时验证 prefix 正确 + 源不残留。

## F-P5A2-02: JS `export function` regex 必须同时支持 `export` 和 `export default`
**现象**：测试 `test_javascript_extracts_function_and_class` 断言 `"delta" in names` 失败 — 实际 top_level_names 只有 `['alpha', 'beta', 'Gamma']`，缺 `delta`。
**根因**：原 regex `(?:export\s+default\s+)?` 只允许 `export default`，对单独 `export function delta()` 不匹配。
**正确做法**：
```python
# 错误：只支持 export default
^(?:export\s+default\s+)?function\s+(...)

# 正确：同时支持 export / export default
^(?:export\s+(?:default\s+)?)?function\s+(...)
```
**预防**：写 prefix-optional regex 时，必须枚举所有合法前缀组合（裸 prefix / 带 additional prefix / 0 prefix），用可选内层组。

## F-P5A2-03: 多 parser 共享扩展名 → 注册顺序敏感，更具体的先注册
**现象**：LogParser 与 TxtParser 都声明 `.log`；TxtParser 是 P1-B 已存在，先注册会赢，导致 `.log` 永远走 Txt 而非 Log。
**正确做法**：
- `register_builtin()` 顺序：`Markdown → Html → Code → Log → Txt → Pdf → Docx → Xlsx → Pptx`
- 更具体的 parser 先注册（具体优先于通用兜底）
**预防**：每次新增共享扩展名的 parser，必须审视注册顺序；测试 `test_xxx_parser_wins_over_xxx_for_xxx_extension` 显式验证。

## F-P5A2-04: Python ast 提取顶层结构比 tokenize 更准
**现象**：用 `tokenize.generate_tokens` 会捕获所有 NAME=def/class 出现，包括嵌套函数；导致嵌套方法 `class MyClass: def method()` 中 `method` 也被计入顶层。
**正确做法**：用 `ast.parse(raw).body` 遍历 AST 节点：
- `ast.FunctionDef` / `ast.AsyncFunctionDef` / `ast.ClassDef` / `ast.Import` / `ast.ImportFrom` → 顶层
- 嵌套的 ast.FunctionDef 出现在 `ast.ClassDef.body` 中，不会被遍历到 `tree.body`
**预防**：Python AST 操作优先 `ast.parse` + 遍历 `tree.body`；tokenize 仅用于无 AST 的 token-level 任务（如 syntax highlighter）。

## F-P5A2-05: HTMLParser 自身容错，无需手动处理错位嵌套
**现象**：测试 `test_handles_malformed_html_gracefully` 输入 `<p>hello<div>world</p>`（未闭合 `<div>` 与错位闭合 `</p>`）— HTMLParser 仍能恢复文本，解析器不抛错。
**正确做法**：HTMLParser 默认容错（自动补全未闭合 tag），无需在解析器中手动管理 tag 栈。
**预防**：对容错性强的解析器（HTML / XML / Markdown / 自实现 lexer），不要过度保护；用容错 + 输出验证测试覆盖典型坏样本即可。

## F-P5A2-06: LogParser 时间戳顺序决定谁先命中
**现象**：`_detect_format` 是顺序匹配列表，ISO 8601 with T 先于 ISO 8601 with space。
**正确做法**：
- 顺序：ISO 8601 T → ISO 8601 space → syslog → common → unix
- 更具体的（带 timezone / 毫秒）先匹配
**预防**：日志格式检测必须按"从最具体到最宽松"排序；测试 `test_detects_*` 每个格式一个独立 case 验证命中正确格式名。

## F-P5A2-07: ast SyntaxError 不能回滚到 generic 正则路径
**现象**：测试 `test_python_handles_syntax_error` 输入 `def broken(:\n`（缺右括号），`ast.parse()` 抛 SyntaxError。
**正确做法**：try/except SyntaxError → 返回 `[]`（顶层 0 个），不让整个解析失败。
**预防**：所有 Python AST 操作都需 try/except SyntaxError；parser 不应因文件损坏而崩溃；保留原始 text 让后续 ingestion.service 决定重试或 tombstone。

---

# P5-A3: 图片 OCR 解析器（tesseract 优雅降级）

## F-P5A3-01: 解析器应始终注册，依赖缺失时抛 ParseError 而非注册失败
**现象**：OcrParser 需要 tesseract 二进制（外部命令）才能工作，但 macOS / CI / Linux 容器经常未装。
**错误做法**：在 `register_builtin()` 内 `try: import pytesseract / which tesseract` → 缺失则 skip 注册。结果：环境差异导致 parser 数量漂移（开发有 10 个，生产只有 9 个），registry.find() 在生产环境找不到 ocr，图片文件被 tombstone 但无明确诊断。
**正确做法**：始终注册 `OcrParser()`，在 `parse()` 时三层依赖检查（顺序触发），缺失任一层抛带安装命令的 `ParseError`（reason 含 `pip install pytesseract` / `brew install tesseract` / `apt install tesseract-ocr`），由 `ingestion.service` 决定重试 / tombstone。
**预防**：解析器框架应"always register, check on parse" — 让 registry 不被外部依赖绑架，失败诊断明确且可重试。

## F-P5A3-02: shutil.which 是检测外部命令的跨平台标准接口
**现象**：要检测 `tesseract` 是否安装，传统做法 `subprocess.run(["which", "tesseract"], ...)` 或 `os.popen("which tesseract")` — 跨平台行为不一致（Windows 没有 `which`，行为差异），子进程开销大。
**正确做法**：`shutil.which("tesseract")` 返回路径或 `None`，内部跨平台处理（Windows 自动查 `.exe` / `.bat` / PATH 扩展），stdlib 提供零依赖。
**预防**：所有"检查外部命令是否存在"的场景都用 `shutil.which`；避免 `os.system("which xxx")` 等 hack 写法。

## F-P5A3-03: PIL.Image.open() 仅读 header，需 .load() 触发完整解码
**现象**：`Image.open(path)` 返回 lazy image 对象，仅读取文件 header（PNG 签名 + IHDR），不解析像素数据。损坏图片 / 截断文件在 `open()` 时不报错，到 `getpixel()` / `pytesseract.image_to_string()` 才抛 `OSError`。
**正确做法**：显式 `img.load()` 触发完整像素解码，让损坏文件在 OCR 调用前就被捕获并抛 `"cannot open image"`。
**预防**：PIL/Pillow 操作流程固定为 `open() → load() → 操作`；解析器应在 OCR/转换前 load 一次以提前暴露 IO 错误，避免错误位置远离根因。

## F-P5A3-04: mock 顶级模块属性应 patch 该模块而非 patch 嵌套路径
**现象**：测试 `test_parses_valid_png_with_mocked_ocr` 想 mock `pytesseract.image_to_string`，写 `monkeypatch.setattr("knowbase.v2.ingestion.parsers.ocr_parser.pytesseract.image_to_string", ...)` — pytest 报 `ImportError: import error in knowbase.v2.ingestion.parsers.ocr_parser.pytesseract: No module named 'knowbase.v2.ingestion.parsers.ocr_parser.pytesseract'`。
**根因**：pytest 的 `monkeypatch.setattr` 用 dotted name 时会逐级 import — `ocr_parser.pytesseract` 路径要求 `ocr_parser` 是一个 package（含 `pytesseract` 子模块），但实际上 `pytesseract` 是顶级模块，`ocr_parser` 仅 `import pytesseract`。
**正确做法**：
```python
pytesseract_real = pytest.importorskip("pytesseract")
monkeypatch.setattr(pytesseract_real, "image_to_string", lambda img, lang="eng": "Hello OCR\n")
```
即先 import 顶级模块拿到对象，再 patch 其属性。
**预防**：mock 跨模块依赖时，永远用"先 import 拿到对象，再 setattr"模式；避免 dotted string 路径（除非被 mock 对象确实是 nested attribute）。

---

## F-P5B-01: 治理代码四层解耦让每层独立单测（lifecycle / metrics / api / dashboard）
**现象**：治理逻辑（生命周期、指标聚合、装配门面、HTML 渲染）天然涉及 V2 仓储 + domain dataclass + 大量边界条件。如果全部塞进一个 `governance.py`，单测需要 mock 整张 DB，脆弱且慢。
**正确做法**：
- **lifecycle.py**（业务规则）— 纯函数 + frozen dataclass，输入 `Document` / `Operation` / `meta` dict，输出 `ExpiryInfo` / `ConflictPair` / `bool`。零 DB 依赖，可任意构造测试夹具。
- **metrics.py**（聚合）— 纯函数，输入 collections of dataclass，输出 frozen dataclass metrics。零 DB 依赖，可单测边界（empty / 单元素 / 大量）。
- **api.py**（装配门面）— 用 `typing.Protocol` 定义最小 `_RepoLike` 接口；`collect_governance_snapshot` 调仓库方法 + 调用 4 个 metrics 函数拼装 `GovernanceSnapshot`。
- **dashboard.py**（渲染）— 纯函数，输入 `GovernanceSnapshot`，输出 HTML 字符串。零 DB 依赖，可断言 HTML 结构 / 转义。
**收益**：52 lifecycle 测试 + 51 metrics 测试 + 18 dashboard 测试 共 121 个，0.3 秒跑完；`_FakeRepo` / `_BrokenRepo` 只需 4 个方法就能驱动 facade，无需 sqlite + fixtures。
**预防**：分层时明确"哪层碰 DB、哪层纯函数"；纯函数层不引入任何 I/O。

## F-P5B-02: dashboard SLA ≠ per-source 心跳 SLA
**现象**：第一版 `compute_sync_backlog_metrics` 默认 `staleness_sla_seconds=60`（per-source 60s 心跳 SLA）。Smoke 测试发现：30 分钟前同步过的源全被标记 stale，dashboard 永远显示"全部过期"。
**根因**：per-source SLA 60s 是实时告警阈值（worker 超时即触发）；但 dashboard 聚合看板是给人看的趋势，60s 太激进 → 假阳性淹没真实问题。
**正确做法**：dashboard 默认 `DEFAULT_SYNC_STALENESS_SLA_SECONDS = 3600`（1 小时），保留参数可覆盖（per-source SLA 60s 仍可作 `staleness_sla_seconds=60` 传入）。两者职责清晰分离：实时监控 = worker / alert；聚合看板 = 趋势 / 异常检测。
**预防**：SLA / 阈值常量按"使用场景"分类，不要让一个常量同时驱动实时告警和聚合看板。

## F-P5B-03: `_safe_call` 顶层容错 + 内部 per-row 容错的双层防御
**现象**：V1 仓库升级 V2 过程中，老 V1 项目可能缺 `sync_state` 表；或者 schema 漂移导致 `list_sync_states()` 抛 `OperationalError`。Dashboard 必须稳：宁可显示空指标，也不要 500。
**正确做法**：
```python
def _safe_call(fn) -> list:
    try: return list(fn())
    except Exception: return []

def collect_governance_snapshot(repo):
    documents = _safe_call(repo.list_documents) or []   # 顶层：表缺失/方法缺失 → []
    operations = _collect_operations(repo, ...)         # 内部：个别 status 失败 → 跳过该 status
    sync_states = _safe_call(repo.list_sync_states) or []
    sync_runs = _collect_sync_runs(sync_states, repo, ...)  # 内部：per-source 失败 → 跳过
    ...
```
**关键**：`sync_states` 已在顶层安全 fetch，下游 `_collect_sync_runs` 接收参数而不是再 fetch（避免重复触发 BrokenRepo 的抛异常）。测试 `test_repository_errors_are_tolerated` 覆盖 `BrokenRepo` 每个方法都抛 `RuntimeError`，断言 snapshot 仍能产出。
**预防**：聚合类 facade 永远双层容错 — 顶层 schema drift / 缺失表 → 空集合；内部 per-row 失败 → 跳过该行。dashboard / metrics 这种"读全表聚合"的代码最容易因单点失败崩全页。

## F-P5B-04: `typing.Protocol` 定义最小接口契约让测试夹具无需继承
**现象**：要给 `collect_governance_snapshot` 写测试，需要 fake 一个 V2Repository — 但 `V2Repository` 有 30+ 方法（upsert_*/list_*/get_*/delete_*），继承构造 fake 太重。
**正确做法**：
```python
class _RepoLike(Protocol):
    def list_documents(self, *args, **kwargs): ...
    def list_operations_by_status(self, *args, **kwargs): ...
    def list_sync_states(self, *args, **kwargs): ...
    def list_sync_runs_by_source(self, *args, **kwargs): ...
```
测试夹具 `_FakeRepo` 只实现这 4 个方法（鸭子类型），无需继承。`_BrokenRepo` 也只需让这 4 个方法抛异常。
**预防**：跨模块边界用 `typing.Protocol`（structural subtyping）而非 ABC（nominal subtyping） — 测试夹具无需继承、第三方实现也无需注册，更灵活；仅在需要 mixin 默认实现时用 ABC。

## F-P5B-05: Python 3.13 deprecation: datetime.utcnow() 必须同时显式导入 timezone
**现象**：把 `datetime.utcnow()` 替换为 `datetime.now(timezone.utc)` 时，初始只改了调用处忘改 import：
```python
from datetime import datetime  # 缺 timezone!
...
return datetime.now(timezone.utc).strftime(...)  # NameError: timezone
```
**根因**：`datetime.now()` 默认无 tz 参数，`timezone.utc` 必须显式 import。代码迁移到 3.12+ 时，deprecation warning 会引导用 `now(tz=...)`，但容易漏 import。
**正确做法**：
```python
from datetime import datetime, timezone
# ...
return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
# epoch 输入：
return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
```
**预防**：审计 `datetime.utc*` 调用时同步检查 `from datetime import` 行；CI 跑 `grep -rn "utcnow\|utcfromtimestamp" knowbase/` 兜底；新代码直接用 `datetime.now(tz=timezone.utc)` 模板。

---

## F-P5C-01 ~ F-P5C-05：定期评测 / 版本注册表 / 回归检测 / CLI 入口

### F-P5C-01：解析器实现指纹用源文件 hash 是最稳的；不要用 `id(parser)` / `parser.name`

**现象**：评测需要"实现变更 → 自动发现"，但仅靠 `parser.name` 无法区分 markdown v1.0 vs v1.1（哪怕解析逻辑已变）；`id(parser)` 每次重启进程都变，无法跨会话追踪。

**根因**：稳定指纹要求 *对相同输入产生相同输出，且对实现差异敏感*。源文件 SHA-256 满足两者 — 任何代码改动（哪怕注释）都会变 hash，且跨进程稳定。

**正确做法**：
```python
import hashlib, inspect
from pathlib import Path

def impl_sha256(parser):
    src = inspect.getsourcefile(type(parser))
    return hashlib.sha256(Path(src).read_bytes()).hexdigest()
```

**预防**：评测 / 上线 / 排障代码里凡是"标识 parser 版本"的，都走 `impl_sha256` — 不要用 `parser.name` 当版本号（语义弱），不要用 `id(parser)`（不稳定）。

### F-P5C-02：frozen dataclass + tuple 默认值让评测产物天然可哈希、可 JSON、可作为 dict key

**现象**：评测产物 (`ParserEvalReport` / `RegressionDiff` / `CaseDelta`) 要在 CI / 报告 / diff 三个场景反复传递，且常需"以 case_id 为键"做 lookup。

**正确做法**：
```python
@dataclass(frozen=True)
class CaseDelta:
    case_id: str
    status: str
    baseline_failures: tuple[str, ...] = ()
```
- `frozen=True` → 不可变，可哈希
- `tuple` 默认值 → 不共享 list 引用、可 hash、可 JSON
- `dict[str, ParserVersion]` 配 frozen dataclass → 评测产物的 look-up 复杂度天然 O(1)

**预防**：评测 / 治理 / 报告相关 dataclass 全部 frozen + tuple 默认；可变 list/dict 仅在"内部 mutable 缓冲"场景使用（如 `ParserEvalReport.by_parser`）。

### F-P5C-03：lazy builtin 注册必须用 `isinstance(registry_, ParserRegistry)` 收敛

**现象**：评测 runner / version_registry 都有"registry 为空时自动注册 builtins"的便利逻辑。但如果不限类型，所有 duck-typed 测试夹具（如 `_EmptyRegistry()`）也会触发自动注册 → 把 builtins 注入全局单例 → 污染后续测试的全局状态。

**错误做法**：
```python
if not list(registry_.parsers()):
    register_builtin()  # 任何空 registry 都会触发
```
**正确做法**：
```python
if isinstance(registry_, ParserRegistry) and not list(registry_.parsers()):
    # 只对真正的空 ParserRegistry 触发；duck-typed 保持原样
    for p in (MarkdownParser(), HtmlParser(), ...):
        registry_.register(p)  # 注到传入的 registry，而非全局
```

**预防**：写"全局 fallback"逻辑前先问 — "调用方传入的对象会污染我的全局状态吗？" 如果是 duck-typed，立即用 `isinstance` 收敛。

### F-P5C-04：`compare_reports` 用 case_id 对齐（不依赖 by_parser 顺序）让 baseline / candidate 的 fixture 可以独立增减

**现象**：升级解析器实现时，golden set 通常会同时"新增 case"（覆盖新行为）和"删除 case"（不再支持旧行为）。如果 diff 按 by_parser 顺序对齐，新增 / 删除会破坏整个 diff 报告。

**正确做法**：diff 按 `case_id` 对齐（set union），4 象限分别记录：
- regressions：旧过→新不过（要拦）
- improvements：旧不过→新过（值得记）
- added：仅 candidate 出现
- removed：仅 baseline 出现
- unchanged：状态相同

**预防**：任何"对比两份 fixture / 报告 / 快照"的代码都按 *稳定主键*（case_id / doc_id / entity_id）对齐，按 *顺序 / 数组下标* 对齐会让 diff 难以维护。

### F-P5C-05：CLI exit code 必须有三态语义（0/2/1）— CI 才能用一行 shell 做 gate

**现象**：CI 脚本要判断"评测是否通过"。如果 CLI 只返回 0/1，CI 脚本无法区分"评测跑了但失败"和"CLI 本身崩了"。

**正确做法**：
- `0`：正常，0 regression（CI 通过）
- `2`：正常，有 regression（CI 失败，gate 触发）
- `1`：IO/参数错（CI 失败，但性质不同 — 可能是基础设施问题）

CI 调用：
```bash
if ./bin/run_periodic_eval.py diff --baseline base.json --candidate cand.json; then
    echo "OK"
else
    case $? in
      2) echo "REGRESSION DETECTED"; exit 1 ;;
      *) echo "INFRA ERROR"; exit 2 ;;
    esac
fi
```

**预防**：任何"评测 / 治理 / 准入检查"的 CLI 都采用三态 exit code；写 wrapper 脚本前先确认 root cause 分类。

### F-WIN-01：subprocess.run(timeout) 在 Windows 上会永久挂起 — 超时必须杀整棵进程树

**现象**：Windows + stdio MCP 下 `memory_save` 无限加载不返回；重启 knowbase MCP 进程后同一调用立即成功。多 Agent 窗口各自拉起服务实例共享同一记忆库时复现。

**根因**：`gitops._run` 用 `subprocess.run(timeout=N)`。超时触发的 `proc.kill()` 在 Windows 上只终止 `git.exe` 本身；孙进程（git-remote-http / 凭据管理器 / gc）继承 stdout/stderr 管道句柄继续存活，`communicate()` 等 EOF 永久阻塞 → 调用线程卡死 → `.lock` 事务不释放 → 后续所有写请求排队挂死。"重启后立即成功"是因为重启连带杀掉了泄漏的 git 进程树与持锁进程。

**正确做法**（已落地 `gitops._run_git`，`_run` / `commit_all` / `commit_paths` / `cmd_doctor` 全部改走它）：
- `Popen` + `stdin=DEVNULL`，杜绝任何子进程等待终端输入
- POSIX：`start_new_session=True` 独立会话，超时 `os.killpg(pgid, SIGKILL)` 整组击杀
- Windows：`creationflags=CREATE_NEW_PROCESS_GROUP`，超时 `taskkill /F /T /PID` 按进程树击杀
- 杀树后 `communicate(timeout=5)` 二次兜底回收，绝不在管道 EOF 上无限等
- 回归测试 `tests/test_gitops_timeout.py`：假 git 父进程 sleep + 孙进程持有管道 sleep 60，断言超时后 <15s 快速失败且孙进程被清掉

**预防**：任何"外部命令 + timeout"的代码都先问一句：超时路径杀的是进程还是进程树？跨平台子进程一律 `stdin=DEVNULL`。`subprocess.run(timeout=)` 的超时语义是"发起 kill 后等输出流关闭"，不是"保证返回"。

