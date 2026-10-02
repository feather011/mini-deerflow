"""MCP 领域异常的公共基类（Phase 22）。

**只放公共基类**：各具体异常定义在它们所属的模块里——

- `MCPConfigError` → `mcp_config.py`（配置解析属于配置模块，避免反向依赖）
- `MCPStartupError` → `mcp_manager.py`（启动失败由管理器判定）

调用方若要一网打尽地捕获 MCP 相关错误，用这里的 `MCPError`：
上面两个都继承它。

**凭据与环境安全**：任何 MCP 异常的消息只允许包含服务器名、工具名、
超时秒数与异常类型；绝不携带子进程环境变量值、父进程环境或 API Key。
"""

from __future__ import annotations


class MCPError(Exception):
    """MCP 相关错误的基类。"""
