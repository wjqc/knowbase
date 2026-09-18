"""knowbase V2 包：按 docs/knowbase-v2-upgrade-plan.md §11 目标架构拆分。

与 V1 模块并存：
- V1（knowbase/store.py、index.py、server.py、…）继续承担 6 个 MCP 工具与 CLI 的兼容入口
- V2 通过 feature flag（knowbase/v2/features.py）逐步启用，失败可一键回退

公开模块（按依赖顺序）：
    features        Feature Flag 注入框架（P0-A）
    observability   基线指标采集（P0-D）与结构化日志
    contracts       MCP/API 入参与返回结构契约（P0-C）
    evaluation      真实评测集 Golden Set 装载与运行（P0-B）
    domain          统一领域模型：Document/Version/Chunk/KnowledgeRecord/Operation/…
    policies        生命周期、权威级别、状态机
    ingestion       多格式摄取、parser 注册表、分块、SHA-256 幂等、tombstone
    retrieval       查询处理、四路召回、RRF 融合
    repositories    仓储接口与 SQLite 实现
"""
from __future__ import annotations

__all__ = ["__version__"]
__version__ = "2.0.0-dev"