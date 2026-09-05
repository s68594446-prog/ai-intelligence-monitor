# 文档导航与适用边界

本目录同时包含冻结规范、阶段说明和当前运维指南。历史验收结果不代表
今天的云端、配额或收件箱状态；排障时必须记录运行 ID、提交和时间。

## 先读哪些文档

- [项目入口](../README.md)：安装、手工入口及 Phase 3C 定时任务。
- [架构边界](../ARCHITECTURE.md)：部门路由、结构化交接、预算和发送边界。
- [Phase 3C 运维手册](PHASE_3C_RUNBOOK.md)：失败分类、受控补发与证据要求。

## 冻结规范

- [PRODUCT_CONTRACT](PRODUCT_CONTRACT.md)：产品职责与禁止事项。
- [HARNESS_STANDARD](HARNESS_STANDARD.md)：Phase 0–2 持久状态与审计边界。
- [SOURCE_STANDARD](SOURCE_STANDARD.md)：来源、抓取和证据要求。
- [SIGNAL_STANDARD](SIGNAL_STANDARD.md)：冻结 Signal 规则。

Phase 3C 文档不能授权绕过以上规范，也不能把日报部门状态写回冻结核心。

## 分阶段说明

- [Phase 0](PHASE_0.md)、[Phase 1](PHASE_1.md)、[Phase 2](PHASE_2.md)：核心与受控手工研究。
- [Phase 3A](PHASE_3A.md)：既有分析简报的独立邮件链路，不是每日日报。
- [Phase 3B](PHASE_3B.md)：受控发现，搜索摘要不构成 Evidence。
- [独立 A 股链路](DAILY_AI_A_SHARE.md)：包含 2026-07-22 的本地环境阻塞记录，
  不等于当前 Phase 3C China Desk 或整个项目暂停。
