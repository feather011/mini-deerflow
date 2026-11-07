"""TOOL-06 搜索 MCP 夹具服务器（仅测试，确定性，不访问外部网络）。

一个工具：`search(query, top_k)`，返回 **JSON 字符串**——与真实搜索类 MCP
服务器的返回形态一致（`_invoke` 的返回值本来就是 `str`）。

    {"hits": [{"title": ..., "url": ..., "snippet": ...}, ...]}

用法：

    python tests/fixtures/mcp_search_server.py
"""

from __future__ import annotations

import json

try:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server
except ModuleNotFoundError:  # mcp 2.x：FastMCP 更名为 MCPServer
    from mcp.server.mcpserver import MCPServer as _Server

# 确定性结果：按 query 返回固定命中，便于断言 title / url / snippet
_FIXTURE_HITS = [
    {
        "title": "Mini DeerFlow",
        "url": "https://example.invalid/mini-deerflow",
        "snippet": "多窗口开发协作的 Agent 应用",
    },
    {
        "title": "harnessagent",
        "url": "https://example.invalid/harnessagent",
        "snippet": "编码任务执行 Agent",
    },
    {
        "title": "TOOL-06 网络只读",
        "url": "https://example.invalid/tool-06",
        "snippet": "web_search / web_read 工具与 SSRF 防护",
    },
]


def search(query: str, top_k: int = 5) -> str:
    """按 query 关键字做确定性「搜索」，返回 JSON 字符串。"""
    if not query.strip():
        return json.dumps({"hits": []}, ensure_ascii=False)
    hits = [h for h in _FIXTURE_HITS if query.lower() in h["title"].lower()]
    if not hits:
        hits = list(_FIXTURE_HITS)
    return json.dumps({"hits": hits[: max(1, top_k)]}, ensure_ascii=False)


mcp = _Server("mini-deerflow-search-fixture")
mcp.add_tool(search)

if __name__ == "__main__":
    mcp.run()
