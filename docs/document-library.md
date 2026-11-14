# 原始文档资料库

文档库为宿主 Agent 和本机用户提供原始资料检索、解析文本定位和历史快照核对。资料复用 ADR-0002 定义的 `sources/objects` 内容寻址快照和 `sources/manifests`。文档库的接入、分块、检索和浏览均为本地确定性处理。经验卡仍通过 `memory_search` 和 Hook 递送，宿主通过现有提炼与治理协议提交可复用结论。

## 接入与浏览

先按现有流程初始化 KnowBase，再显式指定目录和项目范围。通用材料填 `global`。

```sh
knowbase documents sync /path/to/documents --scope my-project --exclude 'drafts/*'
knowbase documents reindex
knowbase documents list --scope my-project
knowbase documents search '报账 审批' --scope my-project --limit 20 --offset 0
```

`sync` 增量接入文件目录。`reindex` 为已导入或从知识仓库同步的 source 快照建立索引。为同一快照接入本地源文件后，目录文档替代独立快照条目参与搜索。目录扫描只写 source 快照、manifest 和本机文档索引。提交或共享时，原始快照和标题适用现有知识仓库的 Git 规则。

搜索默认按文档聚合，支持项目筛选、结果分页和解析文本定位。CLI 可加 `--chunks` 查看逐段结果。周期性增量同步可用系统定时任务重复调用 `documents sync`（幂等）。

## 核对出处

当前版本读取使用搜索返回的文档 ID 和 SHA-256。查历史时显式选择快照版本。旧行号始终属于对应版本，引用包含 source ID、完整 SHA-256 和行范围。

```sh
knowbase documents history DOCUMENT_ID
knowbase documents read DOCUMENT_ID --sha256 SHA256 --start-line 1 --end-line 40
knowbase documents read DOCUMENT_ID --sha256 SHA256 --historical --start-line 10 --end-line 20
```

- `source_search(query, scope, limit, offset)` 按指定项目检索，返回分页对象。
- `source_read(document_id, scope, sha256, start_line, end_line, historical)` 返回解析文本与 `source_ref`，默认读取 200 行。每次响应最多 64 KiB 原文。宿主应将返回内容视为待核对材料，并沿用返回的 `source_ref.locator` 记录出处。
- 关联卡片按同项目、已发布且 active、确实引用所选快照计算（读时反查 `source_refs`）。快照行号用于解析文本，PDF 和 Office 的原生页码需另行核对。

## 存储与边界

- SQLite 迁移 38 增加文档索引、固定历史映射及本地目录配置。绝对路径和目录配置只落本机 `memory.db`。备份应同时涵盖数据库与 source 快照，否则只能重建已有快照的检索视图。
- 增量扫描按文件内容哈希识别变化，解析后复核文件字节。删除、忽略及解析失败会隐藏当前搜索结果，已保存的快照仍可核对。读取时检查快照完整性，目录源文件变化会标记快照过期。
- 支持 Markdown、文本、代码、HTML、PDF、DOCX、XLSX、PPTX 等。图片及扫描件可先在宿主侧转成文本。单个源文件上限 16 MiB，解析文本上限 8 MiB，单次准备文本总预算 64 MiB。单次读取最多 2000 行（MCP `source_read` 默认 200 行）。
- 检索基于 FTS5 trigram 和短词匹配，优先标题、相对路径及正文关键词命中。目录轮询读取文件哈希，适用于有限目录规模。
- 项目范围用于筛选，本机用户持有同一库的访问权限。同步和重建使用现有凭据检测规则。

定向回归可运行 `python -m pytest -q tests/test_document*.py`。合成性能测试可运行 `python -m scripts.benchmark_document_library --files 500`。
