"""确定性本地 MCP 测试服务器（22 阶段，仅用于测试）。

不访问任何外部网络，不读取任何密钥，行为完全确定：

| 工具 | 行为 |
|---|---|
| `echo` | 原样返回文本 |
| `add` | 返回两整数之和 |
| `failing_tool` | 抛异常，用于验证错误传播 |
| `wait` | 睡指定秒数后返回，用于验证调用超时 |

用法：

    python tests/fixtures/mcp_test_server.py
"""

from __future__ import annotations

import time
import sys

from mcp.server.fastmcp import FastMCP


def echo(text: str) -> str:
    return text


def add(a: int, b: int) -> int:
    return a + b


def failing_tool(reason: str = "boom") -> str:
    raise ValueError(reason)


def wait(seconds: float = 1.0) -> str:
    time.sleep(seconds)
    return "done"


# 创建服务器并注册工具
mcp = FastMCP("mini-deerflow-test")
mcp.add_tool(echo)
mcp.add_tool(add)
mcp.add_tool(failing_tool)
mcp.add_tool(wait)

if __name__ == "__main__":
    mcp.run()
