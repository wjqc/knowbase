"""knowbase 知识卡片全生命周期 V2（2026-11 设计，任务书 V2-01~18）。

新模块统一放本包，避免与既有单文件 ``knowbase/lifecycle.py`` 争用导入名。
知识资产目录 ``<knowledge-repo>/lifecycle/``（revisions/publications/imports）
是 Git 内的数据目录，与本 Python 包无关。

分层：
- contracts：冻结的身份/事件/manifest 契约与纯函数（无 I/O）。
- revisions：不可变内容版本快照、legacy 导入与影子对账（V2-04）。
- 其余模块按任务书逐个落地（changes/publication/admission/...）。
"""
