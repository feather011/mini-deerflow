"""确定性本地 MCP 测试服务器（22 阶段，仅用于测试）。

不访问任何外部网络，不读取任何密钥，行为完全确定：

| 工具 | 行为 |
|---|---|
| `echo` | 原样返回文本 |
| `add` | 返回两整数之和 |
| `failing_tool` | 抛异常，用于验证错误传播 |
| `wait` | 睡指定秒数后返回，用于验证调用超时 |

用法：

    uv run python tests/fixtures/mcp_test_server.py
"""

from __future__ import annotations

import time

from mcp.server.mcpserver import MCPServer

server = MCPServer(name="mini-deerflow-test")


@server.tool(name="echo", description="原样返回输入文本")
def echo(text: str) -> str:
    return text


@server.tool(name="add", description="返回 a + b")
def add(a: int, b: int) -> int:
    return a + b


@server.tool(name="failing_tool", description="总是失败，用于测试错误传播")
def failing_tool(reason: str = "boom") -> str:
    raise ValueError(reason)


@server.tool(name="wait", description="睡眠指定秒数后返回 done，用于测试超时")
def wait(seconds: float = 1.0) -> str:
    time.sleep(seconds)
    return "done"


if __name__ == "__main__":
    server.run(transport="stdio")
