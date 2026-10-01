# Mini DeerFlow 搭建流程

## 第一部分 Agent 最小闭环

1. 开发环境
   Python、uv、Git、项目结构、测试环境

2. 安全工具
   AST 安全计算器

3. Agent 核心
   Message、Model、ToolCall、FinalAnswer、Agent 循环

4. CLI 入口
   交互输入、结果输出、执行轨迹

## 第二部分 工具与会话

5. 工具系统
   Tool、ToolRegistry、工具注册与统一执行

6. 会话状态
   ThreadState、消息快照与追加

7. 会话存储
   ThreadStore、InMemoryThreadStore

8. Agent 接入会话
   读取历史、暂存本轮消息、成功后统一提交

9. CLI 多会话管理
   创建、切换、列出、查看和删除会话

> 当前进度：完成到这里，共 88 个测试通过。

## 第三部分 数据持久化

10. 消息与会话序列化
    `to_dict`、`from_dict`、`schema_version`

11. SQLite 存储
    SQLiteThreadStore

12. 重启恢复
    CLI 重启后恢复会话和历史

13. 存储可靠性
    事务、损坏数据、版本迁移测试

## 第四部分 Agent 扩展能力

14. 长期记忆
    Memory、MemoryStore、提取、检索、去重

15. Skills
    SKILL.md、SkillRegistry、技能选择与注入

16. Sandbox
    文件隔离、命令执行、超时、路径安全、Docker

17. Subagent
    任务委派、并发、取消、结果回收、上下文隔离

18. 中间件
    日志、重试、限额、Trace、生命周期管理

## 第五部分应用与部署

19. FastAPI 服务端
    Thread API、Agent API、SSE、文件与产物接口

20. Web 前端
    会话列表、流式消息、工具卡片、执行轨迹

21. 真实模型
    小米模型适配器、配置和 API Key

22. MCP 扩展
    外部工具和服务接入

23. 部署与验收
    Docker Compose、集成测试、官方功能对照

**下一步：第 10 步，完成 Message 和 ThreadState 序列化。**
