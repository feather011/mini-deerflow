"""本地 fixture MCP server（stdio）。

用 FastMCP 提供两个工具：echo（纯计算）和 bad_init（启动失败）。
通过环境变量 MCP_SERVER_MODE 选择：echo | bad_init。
"""

import os
import sys

mode = os.environ.get("MCP_SERVER_MODE", "echo")

if mode == "bad_init":
    # 模拟启动失败：退出码非零
    sys.stderr.write("fixture: bad_init mode, exiting with error\n")
    sys.exit(1)

from mcp.server.fastmcp import FastMCP

app = FastMCP("fixture-server")


@app.tool()
def echo(message: str) -> str:
    """纯计算 echo，无副作用。"""
    return f"echo: {message}"


if __name__ == "__main__":
    app.run()
