# Design: code-quality-optimization-14-items

## Context

knowbase 是一个纯 Python 后端项目，核心功能是通过 SQLite FTS5 + markdown 实现跨 Agent 经验记忆库的存储、检索与同步。项目已迭代到 0.2.0rc9，核心模块间的职责边界已基本稳定，但若干模块存在可维护性和性能短板。

本次设计聚焦于 10 项确认可落地的优化，不改变任何外部接口契约。

## Goals / Non-Goals

**Goals:**
- 提升 `__main__.py` 的可维护性（函数拆分、骨架提取）
- 细化异常处理，避免静默吞错
- 消除热点路径中的 N+1 查询和全表扫描
- 为高频读取的配置添加缓存
- 减少热路径正则编译开销

**Non-Goals:**
- 不新增功能，不改变 CLI 或 MCP 接口
- 不修改数据库 schema（无 DDL）
- 不引入新的第三方依赖
- 不修改 `pyproject.toml` 或 lockfile

## Decisions

### D1: `_main()` 拆分采用三函数模式

**Decision**: 拆为 `_parse_args(argv)`, `_load_config(args)`, `_dispatch(args, config)` 三个函数。

**Rationale**: 三个函数各司其职，职责清晰；`_parse_args` 可独立测试；`_dispatch` 可作为子命令路由的单一入口点。避免过度拆分（如每个子命令独立文件），项目规模尚不需要。

**Alternatives considered**:
- 每个子命令独立模块：过度工程化，当前 ~1671 行的 CLI 文件不需要此粒度
- 维持现状：736 行的 `_main()` 已经超出 Python 函数可维护长度阈值（通常建议 < 100 行）

### D2: 异常处理细化采用分级策略

**Decision**: 按风险分级处理：

| 级别 | 策略 | 适用文件 |
|------|------|----------|
| P0 | 每个 `except Exception` 替换为具体类型，保留通用 `except Exception` 仅在最外层兜底 | `gitops.py` L97, L163, L184 |
| P1 | 替换为类型化 catch，增加 warning 日志 | `retrieval_state.py` L35, L72; `gitops.py` 其余处 |
| 保留 | 已有 `_is_*_error()` 判断的异常处理保留现有逻辑，仅细化类型 | `gitops.py` push/sync 函数 |

**Rationale**: `gitops.py` 的 push 和 sync 函数已有较完善的错误分类（`_is_auth_error`, `_is_network_error`, `_is_push_race`），不应破坏现有重试和 fallback 逻辑。`retrieval_state.py` 的两处是明确的代码异味，需要最直接的修正。

### D3: 配置缓存使用 mtime 校验

**Decision**: `load_config()` 内部维护一个模块级 `_config_cache: tuple[int, dict]` 元组，key 为 `os.path.getmtime(config_path)`。

**Rationale**: 简单、无外部依赖、自动失效。同一进程内 config 极少变化，mtime 检查开销远低于 TOML 解析。

**Edge case**: 若 `mtime` 在同一秒内被 touch 两次（极罕见），缓存可能不失效。对于配置文件场景，这是可接受的 trade-off。

### D4: search() 性能优化分两步实施

**Decision**:
1. **meta 扫描优化**（P-P0-2 前半）：将 L410-426 的 Python 侧后过滤改为 SQL WHERE 子句，减少 Python 循环次数
2. **FTS snippet 批量查询**（P-P0-2 后半）：将 N+1 次 `snippet()` 查询合并为一次 `WHERE id IN (...)` 批量查询

**Rationale**: 两步独立可验证，互不依赖。SQL WHERE 直接减少从数据库返回的行数；批量 FTS 减少网络/磁盘往返。

### D5: `_match_ids()` 合并为单次 FTS 查询

**Decision**: 将逐词 FTS 查询改为 `OR` 连接的单个查询：

```python
# Before
for word in words:
    mids = conn.execute("SELECT id FROM mem_fts WHERE mem_fts MATCH ?", (word,))
    # aggregate hit counts

# After
query_str = " OR ".join(_fts_sanitize(w) for w in words)
mids = conn.execute("SELECT id, rank FROM mem_fts WHERE mem_fts MATCH ? ORDER BY rank", (query_str,))
```

**Rationale**: SQLite FTS5 原生支持 `OR` 操作符，单次查询的结果集与逐词查询取并集等价（对于 OR 语义）。减少 N-1 次数据库往返。

### D6: `find_similar()` 使用 quick_ratio() 预筛选

**Decision**: 使用 `SequenceMatcher(None, a, b).quick_ratio()` 做快速近似比较，仅对 `quick_ratio >= threshold * 0.8` 的候选计算完整 ratio。辅以排序：按 `quick_ratio` 降序，取前 `max(50, len(candidates) // 2)` 个。

**Rationale**: `quick_ratio()` 是 O(n) 的上界计算，比完整 `ratio()` 快 3-10x。对大量候选时效果显著，少量候选时 overhead 可忽略。

### D7: 正则编译提升为常量

**Decision**: 在 `segmenter.py` 模块顶部声明编译后的正则常量：

```python
_HEADING_RE = re.compile(r'^(#{1,6})\s+(.+)$')
_PARAGRAPH_SPLIT_RE = re.compile(r'\n\s*\n')
```

L203 的 `re.match(pattern, line)` 中 `pattern` 参数已是 `re.Pattern` 对象，检查调用方确保传入的是编译后的模式。

**Rationale**: 热路径中的 `re.match()` / `re.split()` 在 CPython 内部会缓存最近使用的模式，但该缓存有限（通常 < 100），且依赖实现细节。显式编译更可控。

### D8: 增量索引重建

**Decision**: 在 `rebuild()` 中维护一个 `_last_indexed_revision` 字典（存储 `entity_id -> content_revision`），仅在 `content_revision` 变化时重新索引该实体。

**Rationale**: 当前 `build_index()` 在主流程中调用频率不高（主要在 `init` 和显式重建时），但全量重建在大知识库场景下耗时显著。增量路径可显著减少 `mem_fts` 的写入 I/O。

**Trade-off**: 需要持久化 `_last_indexed_revision` 状态（可使用 SQLite 表或内存中的 dict，进程重启时全量重建一次）。

## Risks

| 风险 | 可能性 | 影响 | 缓解措施 |
|------|--------|------|----------|
| `_main()` 拆分引入 import 循环 | 低 | 高 | 拆分在同一文件内，不新增模块 |
| 异常细化后遗漏实际可能抛出的异常类型 | 中 | 中 | 保留最外层 `except Exception` 兜底；回归测试覆盖异常路径 |
| 配置缓存导致配置变更不生效 | 低 | 中 | mtime 校验；`mtime == 0` 时强制重读 |
| 批量 FTS 查询语义与逐条查询不一致 | 低 | 高 | 编写对比测试，验证批量与逐条结果完全一致 |
| `quick_ratio()` 预筛丢候选 | 低 | 中 | 阈值设为 `0.8 * threshold`，留出安全余量 |
| 增量索引遗漏更新 | 中 | 中 | `force=True` 参数保留全量重建路径；`build_index` 命令支持 `--full` flag |

## Architecture / Data Flow

本次变更不改变架构拓扑。所有优化在同一进程内的模块内部完成：

```
knowbase CLI entry
    │
    ├── _parse_args()          [NEW: split from _main]
    ├── _load_config()         [NEW: split from _main]
    ├── _dispatch()            [NEW: split from _main]
    │
    ├── config.load_config()   [MOD: add mtime cache]
    ├── index.search()         [MOD: batch FTS + SQL filter]
    ├── index._match_ids()     [MOD: single FTS query]
    ├── index.build_index()    [MOD: incremental rebuild]
    ├── store.find_similar()   [MOD: quick_ratio pre-filter]
    ├── gitops._run_git()      [MOD: refine exceptions]
    ├── retrieval_state.*      [MOD: refine exceptions]
    └── segmenter              [MOD: compile regex]
```

No new files are created. All changes are in-place modifications to existing modules.