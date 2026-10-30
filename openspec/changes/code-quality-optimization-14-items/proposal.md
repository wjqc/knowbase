# Proposal: code-quality-optimization-14-items

## Why

knowbase 项目已经历多轮迭代，核心模块（`__main__.py`、`index.py`、`gitops.py`、`config.py`、`store.py`、`segmenter.py`、`retrieval_state.py`）累积了若干代码质量问题，影响可维护性和运行时性能。本次变更集中解决 10 项确认可落地的问题（原列 14 项，其中 4 项在源码中经核验不存在或已被修复，详见下文排除说明）。

## What Changes

按维度分为两大组：

### 质量维度（5 项）

| 编号 | 文件 | 问题 | 等级 |
|------|------|------|------|
| Q-P0-1 | `knowbase/__main__.py` | `_main()` 函数体 ~736 行，超出可维护阈值，需拆分为 `_parse_args()` / `_load_config()` / `_dispatch()` | P0 |
| Q-P0-5 | `knowbase/gitops.py` | 33 处 `except Exception` 捕获过于宽泛，其中 L97/L163/L184 等关键路径无区分，掩盖真实错误类型 | P0 |
| Q-P1-6 | `knowbase/retrieval_state.py` | L35 `except Exception:pass` 和 L72 `except Exception:conn.rollback();raise` 裸异常未细化 | P1 |
| Q-P1-8 | `knowbase/__main__.py` | 多个子命令分发链路重复相同的 try/except/return 骨架，可提取为通用装饰器或上下文管理器 | P1 |
| Q-P1-10 | `knowbase/store.py` | `find_similar()` L297-307 逐条计算 `SequenceMatcher.ratio()`，无批处理或阈值提前剪枝 | P1 |

### 性能维度（5 项）

| 编号 | 文件 | 问题 | 等级 |
|------|------|------|------|
| P-P0-2 | `knowbase/index.py` | `search()` 中 L448-459 对每条结果分别执行 `snippet()` FTS 查询（N+1 问题），且 L410-426 全表扫描 `meta` | P0 |
| P-P0-3 | `knowbase/config.py` | `load_config()` L107-130 每次调用都重新读取并解析配置文件，无缓存 | P0 |
| P-P0-4 | `knowbase/index.py` | `build_index()` / `rebuild()` 通过全量迭代 `iter_all()` 重建索引，缺少增量更新路径 | P0 |
| P-P1-9 | `knowbase/index.py` | `_match_ids()` L247-260 多词查询逐词查 FTS 再统计命中数，应合并为单次查询 | P1 |
| P-P2-14 | `knowbase/extraction/segmenter.py` | L87 `re.match()`, L130 `re.split()`, L203 `re.match()` 热路径正则每次编译，应提升为模块级常量 | P2 |

### 排除说明

以下原列 4 项经核验在源码中不存在或已被修复：

| 编号 | 原描述 | 核验结论 |
|------|--------|----------|
| P1-7 | `PACKAGE_NAME`+正则分散定义在 `config.py` 与调用处 | 全项目搜索 `PACKAGE_NAME` / `_IDENTIFIER_RE` 均无命中，该项已不存在 |
| P2-15 | `quarantine.py` 中 `push` 分两次推送应合并 | `quarantine.py` 无任何 push 操作；`gitops.py:push()` 中 push 已为单次调用 |
| P2-16 | `quarantine.py` 中 `except` 块内只有注释无实际处理 | `quarantine.py` 无此类模式；全项目含注释的 except 块均有必要的 fallback 逻辑 |
| — | （原列表共列 13 项，但标题称 14 项，差额 1 项无法对应） | 按实际可落地的 10 项执行 |

## Capabilities

### New Capabilities
无新增用户功能。所有变更为内部重构和性能优化，对外 API 契约不变。

### Modified Capabilities
无接口签名变更。以下行为保持兼容：
- `knowbase search` CLI 输出格式不变
- MCP `memory_search` / `memory_context` 返回结构不变
- 配置文件格式不变
- Git 同步协议不变

## Impact

- **风险等级**：低。全部为内部重构，不涉及跨中心接口、DDL、数据归属变更或异步调度。
- **受影响规范**：无外部 spec 受影响。
- **受影响代码**：`knowbase/__main__.py`, `knowbase/index.py`, `knowbase/gitops.py`, `knowbase/config.py`, `knowbase/store.py`, `knowbase/extraction/segmenter.py`, `knowbase/retrieval_state.py`
- **回归范围**：全量单元测试 + `knowbase search` / `knowbase init` / `knowbase push` 冒烟验证
- **迁移**：无需数据迁移