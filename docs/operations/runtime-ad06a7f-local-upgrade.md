# ad06a7f 本机运行时升级

2026-10-24，经用户授权原地升级。源码来源为已推送main的`ad06a7fe06fc7dfe9a2cdcbe994b2edec24f3658`，不包含本记录随后产生的纯文档提交。

## 已完成

- 唯一运行路径`~/.knowbase/runtime/venv/bin/python`原地安装0.2.0rc8；新构建`ac0713e061bcaeaa`，101个Python/资源文件与已提交源码逐项一致，仅一个knowbase发行包，非editable。
- 安装前在线备份memory.db、team state、知识文件及8份配置/后台描述文件。私有目录`~/.knowbase/backups/runtime-ad06a7f-20261024-225553`；详细manifest、身份及验证文件权限0600，目录0700，不进Git。
- 真实库由迁移22升至28，旧迁移校验通过；204张知识卡正文逐文件一致，成员/安装身份不变；998条检索日志、28条反馈、5条使用记录、77个作业及spool、7条候选均保留。新增正常CLI计量单独增长，不回退覆盖历史。
- 固定包stdio端到端2 passed；隔离build/协议探针通过、17工具。三宿主现有18条Hook命令在临时库中通过并核对输出，Stop静默；安装态合成执行器探针通过，不是公司模型验收。
- 新LaunchAgent `com.knowbase.worker` 已加载、实际运行、租约/心跳可读；旧`com.knowbase.team-stats`入口移除。新team_sync任务已实际触发，不重登记成员，不清理ACK或统计历史。
- 租约恢复、归档、冲突复验及向量维护调度已运行；失败同步按error记账，未推进成功水位。提炼调度明确blocked，证据交换按原配置off而跳过。
- runtime active/manifest与依赖锁中的应用版本标识已更新。清理本次及前次遗留的临时wheel/构建目录和knowbase包缓存，不保留第二运行环境。

源码全量回归为912 passed、1 skipped；本次安装态验证独立记录，不以源码测试替代部署核验。

## 仍待处理

- 共享Git知识同步及team同步仍失败，新worker正在退避重试；升级未修改网络、VPN、认证或远端白名单。
- 22个历史提炼待办仍保留；P2没有真实执行器通过前不启用。公司Embedding仍未配置，基础检索L2正常，不调用远程模型。
- v2 evidence_role沿用off，不自动扩大证据上传范围；已有pending事件保留。
- 旧MCP服务连接已停止，未退出宿主应用或聊天。真实Agent会话需重连knowbase MCP才能加载新build；临时stdio验证不证明native会话已生效。
- doctor无fail项；同步/队列、缺OCR二进制、尚未接入的其他Agent等warn不伪装成全部健康。真实模型、成员试点与净Token收益仍待验。

## 恢复

数据与配置备份独立于运行包。优先在唯一固定路径安装兼容修复；迁移28后的库不得盲目交给不兼容旧代码，也不为回退向量或同步状态整库回滚丢掉新反馈。需要灾备恢复时以在线备份和原始材料/回执核对损失窗口，保留恢复后新增记录。
