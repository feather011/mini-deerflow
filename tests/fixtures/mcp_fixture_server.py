"""本地 fixture MCP server（stdio）— TOOL-03 R4。

支持 mcp 1.x (FastMCP) 和 mcp 2.x (MCPServer) 两种 API。
通过环境变量 MCP_SERVER_MODE 选择：echo | bad_init。
"""

import os
import sys

mode = os.environ.get("MCP_SERVER_MODE", "echo")

if mode == "bad_init":
    sys.stderr.write("fixture: bad_init mode, exiting with error\n")
    sys.exit(1)

if mode == "hang_init":
    # TOOL-03 R4 round 3：启动后挂起，不响应 initialize。
    # 让 manager 的 startup_timeout 触发并验证子进程被回收。
    import time
    sys.stderr.write("fixture: hang_init mode, sleeping 30s\n")
    sys.stderr.flush()
    # 仅睡短时间，让测试不要真等 30s；manager 的 startup_timeout 触发后会 cancel
    try:
        time.sleep(30)
    except KeyboardInterrupt:
        pass
    sys.exit(2)


def _make_server():
    """按 mcp 版本构造 server。"""
    # 优先 mcp 2.x
    try:
        from mcp.server.mcpserver import MCPServer

        server = MCPServer("fixture-server")

        @server.tool()
        def echo(message: str) -> str:
            """纯计算 echo，无副作用。"""
            return f"echo: {message}"

        return server
    except ImportError:
        pass

    # 回退 mcp 1.x
    from mcp.server.fastmcp import FastMCP

    app = FastMCP("fixture-server")

    @app.tool()
    def echo(message: str) -> str:
        """纯计算 echo，无副作用。"""
        return f"echo: {message}"

    return app


if __name__ == "__main__":
    server = _make_server()
    # 优先 stdio_async（mcp 2.x），回退 run（mcp 1.x）
    run_stdio = getattr(server, "run_stdio_async", None)
    if run_stdio is not None:
        import asyncio
        asyncio.run(run_stdio())
    else:
        server.run()
