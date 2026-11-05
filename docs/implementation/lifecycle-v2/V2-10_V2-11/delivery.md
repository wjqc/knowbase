# V2-10 自动采用关联 + V2-11 可信 L3 验证 — 交付记录

- 基线：`425f3fe`；实施日期：2026-11-05。状态：**实现完成 + 隔离验证通过**。

## 变更（`knowbase/knowledge_lifecycle/attribution.py`）

- `correlate(conn, repo, run_id)`：任务运行观测 → L0/L1/L2 证据事件
  （evidence_events 本地 ledger）：
  - L0=递送存在；L1=read/referenced 回执；
  - L2=卡动作签名（code: 引用 / code_refs / 正文带路径文件）与工具调用负载
    精确命中且**不歧义**（同 token 命中多卡 → ambiguous 不发奖）；通用命令
    （pytest/git status/ls…）进 `generic_rejected` 清单不作采用；任务 completed
    不反推任何卡的采用。
- `verify_execution(...)`（V2-11）：L3 只认**已登记验证器**（config
  `knowledge_lifecycle.validators`）+ 非空报告（artifact_hash + checks）；
  全 pass 才 pass、fail→negative L3、缺项→unknown；引用不存在 claim →
  unknown（防"跑过测试但没测该卡断言"）；Agent 自报（含
  `verification_source=independent`）无通道。事件带 checks 摘要去重键。
- `evidence_summary`：一卡一版本分层读回（trusted/untrusted L3 分开）。
- `registered_validators()`：登记制；未登记来源零 L3 资格。

## 验证证据（`tests/test_v2_10_11_attribution.py` 3 项全过）

- S4：三卡递送、read A/B、B 独有动作被真实工具命令命中 → A=L1、B=L2、C=L0；
  A/C 的 L2/L3=0（completed 不发奖）；git status 进拒收清单。
- 歧义：两卡同动作 → 都不发 L2。
- L3 硬门：未登记验证器拒绝；空报告 unknown；真报告 pass → trusted_L3=1；
  ghost claim → unknown；fail → negative（polarity_fail=1）；三条事件互不去重。

**external_pending**：≥60 独立人工标注 task-card 样本的 L2 precision ≥95% 验收、
真实宿主原生日志接线（V2-09 联动）、真实 pytest 报告适配器——需真实宿主与
材料授权；本轮以隔离 fixture 验证机制。
