# TEST-01 Implementation Plan

> **设计文档**：`docs/test01-design.md`（实施按本文 Task 逐条推进）。

**Goal:** 使 harness 集成测试真实请求本地 mock，默认不受开发者凭据和 .env 影响。
**Architecture:** 测试专用 env_file 与回环网络保护；保留真实子进程。
**Tech Stack:** pytest, socket, subprocess, HTTPServer.
**Spec:** docs/test01-design.md

## Global Constraints
- 不修改生产代码，不调用真实 Provider，不 commit/push/merge；不跑全量。
- 不放宽已有断言；必要的失真夹具修复须记录。

## Review Focus
- 父进程和 Python 子进程的 DNS/连接都拒绝非回环。
- 真实开发者配置、代理、fake 模式残留不能替代 mock。
- mock 停止后不得回退公网。
- 停止与超时仍回收真实子进程。
- 所有手工子进程和服务器在 finally 中回收。

### Task 1: 隔离与集成验收（唯一交付任务）
Files: tests/conftest.py, tests/_offline_guard/sitecustomize.py,
tests/test_test_isolation.py, tests/test_harness_runner_integration.py, docs/PROGRESS.md.
- [x] 添加污染环境和子进程保护探针，先确认失败。
- [x] 接入默认保护，测试 env_file 与正确 URL，消除硬编码 D: 路径。
- [x] 用真实 harness 跑本地 mock，断言请求记录和文件内容；mock 不可用时明确失败。
- [x] 运行上述两测试文件及相关 HarnessRunner 环境测试；导出干净副本重复同一集合。
- [x] 一次集中审查；只修本阶段实质问题。记录 READY_FOR_REVIEW，不冒充独立通过。
- [x] 交付独立 patch；不提交、不推送。
