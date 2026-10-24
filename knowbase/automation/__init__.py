"""无人值守自动化包：执行器探针（P1）与后台 supervisor（P3）。

P1：执行器探针与协议契约；探针只校验标记存在性，从不读取或回显密钥值。
P3：worker supervisor 与证据交换；提炼任务位在 worker 中恒为 blocked，
直到至少一个真实执行器通过 P1 隔离探针后才可启用。
P2 的 supervisor/dispatch 完整调度循环不在本包本批范围内。
本包不输出凭据、租约密钥或认证信息。
"""
