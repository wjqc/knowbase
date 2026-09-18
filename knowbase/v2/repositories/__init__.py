"""V2 仓储层（P1-A）。

注意：V2 数据库与 V1 index.db 完全独立（V2 计划 §11）：
- V1: knowbase/index.py → <repo>/.knowbase/index.db
- V2: knowbase/v2/repositories/sqlite_repo.py → <repo>/.knowbase/v2.db

这样 V2 字段/索引演进不会影响 V1 兼容入口；并支持回滚到 V1。
"""
from .schema import (
    V2_DB_FILENAME,
    SCHEMA_VERSION,
    apply_migrations,
    init_v2_db,
)
from .sqlite_repo import V2Repository

__all__ = [
    "V2_DB_FILENAME",
    "SCHEMA_VERSION",
    "apply_migrations",
    "init_v2_db",
    "V2Repository",
]
