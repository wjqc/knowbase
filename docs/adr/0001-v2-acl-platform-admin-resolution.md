# ADR-0001：platform_admin 的 ACL 判定必须显式写规则，不给万能权限

- 状态：已归档（实现于 v2 Phase 3；v2 运行时已随 75bc74d 移除，本决策供未来访问控制重建时参考）
- 日期：2026-09-18
- 来源：原记忆 D-2026-0003（human:文剑），2026-09-20 迁入 ADR 后归档

## 背景

V2 计划 §7.1 定义 `platform_admin` 为"组织级运维，但默认不自动获得敏感正文访问权"。
实现 ACL `resolve()`（`v2/acl/policy.py`）时若不写 platform_admin 显式规则，会回落到普通
role 检查：platform_admin 没有组织角色，反而看不到 org_global，违反"运维"职责。

## 决策

`resolve()` 对 platform_admin 显式判定：

- `visibility == personal` → **deny**（用户私有经验默认不可读，需显式授权）
- `visibility in {public_template, project_shared, organization_global}` → **allow**（组织级运维语义）
- `visibility` 为 NULL / 枚举外脏数据 → fail-closed **deny**

## 取舍

- 不给 platform_admin "全部可见"的万能权限——违反 §7.1 红线，等于绕过 personal 隐私保护。
- 也不完全不写规则——运维读不到 org_global 体验差且不符职责。
- personal 默认 deny 是隐私下限：平台管理员不能借运维身份读用户私有内容。

## 验证

Phase 3 ACL 测试 208/208 通过（含 platform_admin 全 visibility 矩阵 + fail-closed 场景）。
