# Tasks: code-quality-optimization-14-items

实施按三批组织：**B 质量** -> **A 性能** -> **C 测试与回归**。每批内按优先级 P0 > P1 > P2 排序。

---

## Batch B: 代码质量 (5 tasks)

### B1: [P0] 拆分 `_main()` 为三个函数

- [x] 从 `_main()` (L911-L1647) 中提取 `_parse_args(argv) -> argparse.Namespace`，保持所有子命令和参数定义不变
- [x] 提取 `_load_config(args) -> dict`，处理 `--non-interactive` 等配置相关参数
- [x] 提取 `_dispatch(args, config) -> int`，将原 `if cmd == "init":` 等分支平移到 dispatch 中
- [x] `_main()` 瘦身为三行调用：`args = _parse_args(argv)`; `cfg = _load_config(args)`; `return _dispatch(args, cfg)`
- [x] 验证：`knowbase init --help` / `knowbase search --help` / `knowbase dashboard --help` 输出不变
- [x] 验证：`knowbase search "test"` 返回正常结果

**文件**: `knowbase/__main__.py`
**参考**: L911-1647

---

### B2: [P0] 细化 gitops.py 的关键路径异常处理

- [x] L97 `_run_git()` 内层 `except Exception` -> `except subprocess.TimeoutExpired`
- [x] L163 `commit_all()` `except Exception` -> `except (RuntimeError, OSError) as e`，保留注释说明（注：实际代码用 Popen 非 subprocess.run，git 错误包装为 RuntimeError 而非 CalledProcessError）
- [x] L184 `commit_paths()` `except Exception` -> `except (RuntimeError, OSError) as exc`（同上）
- [x] 其余 30 处 `except Exception` 各替换为具体异常类型：`_run` 调用点 -> `(RuntimeError, OSError)`；数据库操作 -> `sqlite3.Error`；文件 I/O -> `OSError`；回调包装 -> `RuntimeError`
- [x] 验证：模块导入和 knowbase list 正常；test_gitops_timeout.py 2/2 通过
- [ ] 验证：`knowbase push` 在无网络环境下给出清晰的网络错误信息（需实际 git remote 环境，当前无法模拟）

**文件**: `knowbase/gitops.py`
**参考**: L94-102, L163, L184, 全文件 grep `except Exception`

---

### B3: [P1] 细化 retrieval_state.py 的裸异常处理

- [x] L35 `except Exception:pass` -> `except (sqlite3.OperationalError, sqlite3.ProgrammingError)`，添加 `_log.debug()` 记录
- [x] L72 `except Exception:conn.rollback();raise` -> `except sqlite3.Error: conn.rollback(); raise`
- [x] 验证：模块导入正常、knowbase list 正常

**文件**: `knowbase/retrieval_state.py`
**参考**: L35, L72

---

### B4: [P1] 提取 __main__.py 的 try/except/return 骨架

- [x] 创建 `@subcommand_handler` 装饰器或 `run_subcommand()` 包装函数
- [x] 识别 48 个 try 块中的重复模式（try/except/print to stderr/return exit code）
- [x] 将模式一致的分支改为使用装饰器/包装函数
- [x] 对错误类型不同的分支（如需要特殊错误处理逻辑的），保留自定义处理不强行统一
- [x] 验证：`knowbase init --invalid-flag` 输出格式化的错误信息到 stderr
- [x] 验证：所有子命令的退出码行为不变

**文件**: `knowbase/__main__.py`
**参考**: 全文件 48 个 try 块

---

### B5: [P1] 优化 store.py find_similar() 的 SequenceMatcher 批处理

- [x] 在 `find_similar()` 中添加 `quick_ratio()` 预筛选步骤
- [x] 按 `quick_ratio()` 降序排序，仅对 `quick_ratio >= threshold * 0.8` 且排名前 `max(50, len(candidates) // 2)` 的候选计算完整 `ratio()`
- [x] 确保结果排序与现有行为一致（按完整 ratio 降序）
- [x] 添加阈值文档说明 pre-filter 逻辑
- [x] 验证：`find_similar(text, threshold=0.6)` 在 200 候选时返回结果与优化前一致

**文件**: `knowbase/store.py`
**参考**: L297-307

---

## Batch A: 性能优化 (6 tasks)

### A1: [P0] 优化 index.py search() 的 FTS snippet 批量查询

- [x] 将 L448-459 的逐条 `snippet()` 查询替换为批量查询：`SELECT id, snippet(mem_fts, 3, '「', '」', '…', 12) FROM mem_fts WHERE id IN (...)`
- [x] 构建 `{id: snippet}` 字典供后续组装结果使用
- [x] 验证：搜索结果中 snippet 内容与原实现完全一致（编写对比测试）
- [x] 验证：空结果时无 FTS 查询执行

**文件**: `knowbase/index.py`
**参考**: L448-459

---

### A2: [P0] 优化 index.py search() 的 meta 全表扫描

- [x] 将 L410-426 的 Python 侧后过滤转为 SQL WHERE 子句：
  ```sql
  SELECT * FROM meta WHERE status != 'archived' AND type != 'reference'
  ```
- [x] 将 `mtype`, `scope`, `tag` 过滤也加入 SQL WHERE（使用参数化查询）
- [x] `include_inactive` 条件：`AND (staging = 0 AND status = 'active')` 或通过 Python 参数控制
- [x] `entity_heads` 和 `source_current` 查询保持在 Python 侧（需要应用逻辑判断）
- [x] 验证：`search(conn, "query", mtype="card", scope="myproject")` 返回结果与原实现一致（对比测试）

**文件**: `knowbase/index.py`
**参考**: L410-426

---

### A3: [P0] 添加 config.py load_config() 缓存

- [x] 新增模块级 `_config_cache: dict | None = None` 和 `_config_mtime: float = 0`
- [x] 在 `load_config()` 开头检查 `os.path.getmtime()`，若未变则返回缓存
- [x] 首次调用或 mtime 变化时重新读取和解析
- [x] 文件不存在时返回默认配置，不设缓存
- [x] 验证：同一进程中多次调用 `load_config()` 只产生一次文件 I/O（可通过 mock 或计数验证）

**文件**: `knowbase/config.py`
**参考**: L107-130

---

### A4: [P0] 实现 index.py build_index() 增量重建

- [x] 在 `rebuild()` 中添加增量检测逻辑：维护 `entity_id -> content_revision` 映射
- [x] 首次运行全量重建；后续运行仅对 `content_revision` 变化的实体重建 FTS 索引
- [x] 新增/删除实体正确处理
- [x] 提供 `--full` flag 用于强制全量重建
- [x] 验证：修改 1 条 entity 后 `build_index()` 耗时显著低于全量重建
- [x] 验证：新增 entity 后索引正确包含新条目
- [x] 验证：删除 entity 后索引正确移除该条目

**文件**: `knowbase/index.py`
**参考**: `rebuild()` 和 `build_index()` 函数

---

### A5: [P1] 合并 index.py _match_ids() 的多词 FTS 查询

- [x] 将逐词循环查询改为单次 `OR` 连接的 FTS 查询
- [x] 添加 FTS 特殊字符转义（`_fts_sanitize()` 工具函数）
- [x] 对空/纯停用词查询保持现有行为（不抛异常）
- [x] 验证：`_match_ids(conn, ["memory", "search"])` 返回的词汇匹配 id 集合与原逐词查询后取并集+计数的结果一致
- [x] 验证：单词语义保持 O(1) 查询而非 O(N) 循环

**文件**: `knowbase/index.py`
**参考**: L247-260

### A6: [P2] compile segmenter.py 热路径正则

- [x] 在模块顶部声明 `_HEADING_RE = re.compile(r'^(#{1,6})\s+(.+)$')`
- [x] 在模块顶部声明 `_PARAGRAPH_SPLIT_RE = re.compile(r'\n\s*\n')`
- [x] 替换 L87: `re.match(r'^(#{1,6})\s+(.+)$', line)` -> `_HEADING_RE.match(line)`
- [x] 替换 L130: `re.split(r'\n\s*\n', text)` -> `_PARAGRAPH_SPLIT_RE.split(text)`
- [x] L203: 确认调用方传入的 `pattern` 已是 `re.Pattern` 对象；若是字符串，在调用前编译
- [x] 验证：markdown heading 解析（L87）输出与原逻辑一致
- [x] 验证：paragraph split（L130）输出与原逻辑一致
- [x] 验证：pattern-based extraction（L203）输出与原逻辑一致

**文件**: `knowbase/extraction/segmenter.py`
**参考**: L87, L130, L203

---

## Batch C: 测试、回归与收尾 (3 tasks)

### C1: [P2] 全量单元测试回归

- [x] 运行 `pytest` 全量单测，确认所有存量测试通过
- [x] 对 `_main()` 拆分后的三个新函数编写独立单元测试：
  - `test_parse_args_valid()` / `test_parse_args_invalid()`
  - `test_load_config_existing()` / `test_load_config_default()`
  - `test_dispatch_known_cmd()` / `test_dispatch_unknown_cmd()`
- [x] 对 `load_config()` 缓存编写测试：
  - `test_config_cache_hit()`
  - `test_config_cache_mtime_invalidated()`
- [x] 对 `_match_ids()` 批量查询编写对比测试：
  - `test_match_ids_single_word()`
  - `test_match_ids_multi_word()`
  - `test_match_ids_vs_old_implementation()`
- [x] 对 `search()` snippet 批量查询编写对比测试：
  - `test_search_snippets_batch_vs_individual()`

---

### C2: [P2] 冒烟验证

- [x] `knowbase init --scope test_opt` — 初始化成功（exit 0）
- [x] `knowbase search "memory"` — 返回结果包含 snippet（API 级验证：3 hits, snippet=yes）
- [x] `knowbase dashboard --output /tmp/test-dash.html` — 生成 HTML（exit 0，文件产出）
- [x] `knowbase reindex` — 增量重建正常（0 changed / 208 full）
- [x] MCP `memory_search` 工具调用 — server.search_impl 使用同一 index.search 路径，API 级已验证
- [ ] `knowbase push` (dry-run 或 whitebox 环境) — 无回归（需实际 git remote，当前环境不可用）

---

### C3: [P2] 收尾与文档

- [x] 确认 `pyproject.toml` version bump 是否需要（当前 0.2.0rc9，建议 0.2.0rc10）— 待用户决策
- [x] 确认所有排除项（P1-7, P2-15, P2-16）在提案中已说明核验结论 — 已确认
- [x] 更新变更日志（如有 CHANGELOG）— 项目无 CHANGELOG 文件，跳过
- [ ] Git commit 分批评交：B 质量 -> A 性能 -> C 测试（待用户触发）