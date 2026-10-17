"""MCP → 项目内部工具的适配层（22 阶段）。

职责边界：**只做纯转换**，不持有 MCP session、不碰事件循环、不读环境变量。

```
mcp.types.Tool  ──adapt()──►  ToolSpec（给模型看）
                             Tool   （给 Agent 执行，handler 回调 invoker）
```

### 命名空间

工具名固定为 `mcp__<server>__<tool>`：

- 不同服务器的同名工具（都叫 `echo`）因此不会撞车
- 名字带 `mcp__` 前缀，模型与日志里一眼可辨"这是外部 MCP 工具"
- 名字会参与模型请求，服务器名改名等于工具改名，属**显式**行为而非隐式覆盖

### 不做的事

- 不读取/转发 MCP 子进程的环境变量（那是 manager 的职责）
- 不在适配层做网络 I/O（`invoker` 由 manager 注入，可能同步/异步桥接）
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, TYPE_CHECKING

from mini_deerflow.model_errors import ModelProtocolError
from mini_deerflow.models import ToolSpec
from mini_deerflow.tools import Tool

if TYPE_CHECKING:
    from mini_deerflow.tool_contracts import ToolMetadata

# 工具命名空间前缀；模型与日志中可据此识别外部工具
MCP_TOOL_PREFIX = "mcp__"


def qualify_tool_name(server_name: str, tool_name: str) -> str:
    """`server` + `tool` → `mcp__server__tool`。"""
    if not server_name or not tool_name:
        raise ModelProtocolError("服务器名与工具名都不能为空")
    if "__" in tool_name:
        # 工具名里出现分隔符会破坏解析，属于供应商侧的非法命名
        raise ModelProtocolError(f"工具名不能包含 '__': {tool_name!r}")
    return f"{MCP_TOOL_PREFIX}{server_name}__{tool_name}"


def truncate_output(text: str, max_bytes: int) -> str:
    """按**字节**截断工具输出，超限部分替换为提示。

    按字节而不是字符数截断：MCP 返回的可能含多字节文本，
    按字符数截断会在 UTF-8 边界上把代理项切坏。
    """
    if max_bytes <= 0:
        return ""
    raw = text.encode("utf-8", errors="ignore")
    if len(raw) <= max_bytes:
        return text
    clipped = raw[:max_bytes].decode("utf-8", errors="ignore")
    return f"{clipped}\n…[输出超过 {max_bytes} 字节，已截断]"


def validate_arguments(spec: ToolSpec, arguments: Mapping[str, Any]) -> None:
    """按 input_schema 校验必填参数；缺失即抛 ModelProtocolError。

    在**发起 MCP 调用之前**校验，避免把明显非法的参数送到子进程。
    """
    schema = spec.input_schema
    if not isinstance(schema, Mapping):
        raise ModelProtocolError(f"工具 {spec.name} 的 input_schema 必须是对象")

    required = schema.get("required")
    if not isinstance(required, list):
        return

    missing = [key for key in required if key not in arguments]
    if missing:
        raise ModelProtocolError(
            f"工具 {spec.name} 缺少必填参数: {', '.join(map(str, missing))}"
        )


# invoker: (qualified_name, arguments, timeout) -> str
ToolInvoker = Callable[[str, Mapping[str, Any], float], str]


class MCPToolAdapter:
    """把单个 MCP 服务器的工具适配成项目内部的 ToolSpec + Tool。"""

    def __init__(
        self,
        server_name: str,
        call_timeout: float,
        max_output_bytes: int,
        invoker: ToolInvoker,
    ) -> None:
        self._server_name = server_name
        self._call_timeout = call_timeout
        self._max_output_bytes = max_output_bytes
        self._invoker = invoker

    @property
    def server_name(self) -> str:
        return self._server_name

    def to_spec(self, mcp_tool: Any) -> ToolSpec:
        """MCP Tool → ToolSpec（给模型的 provider-neutral 描述）。"""
        name = getattr(mcp_tool, "name", None)
        if not isinstance(name, str) or not name:
            raise ModelProtocolError("MCP 工具缺少 name")

        schema = getattr(mcp_tool, "input_schema", None)
        if schema is None:
            schema = getattr(mcp_tool, "inputSchema", None)
        if not isinstance(schema, dict):
            raise ModelProtocolError(
                f"MCP 工具 {name} 的 input_schema 必须是对象"
            )

        description = getattr(mcp_tool, "description", None) or ""
        return ToolSpec(
            name=qualify_tool_name(self._server_name, name),
            description=str(description),
            input_schema=dict(schema),
        )

    def to_tool(self, mcp_tool: Any) -> tuple[ToolSpec, Tool]:
        """MCP Tool → (ToolSpec, Tool)；Tool.handler 回调注入的 invoker。"""
        spec = self.to_spec(mcp_tool)
        original_name = getattr(mcp_tool, "name", "")

        def handler(arguments: dict[str, object]) -> str:
            # 参数校验放在真正发请求之前
            validate_arguments(spec, arguments)
            raw = self._invoker(
                spec.name,
                dict(arguments),
                self._call_timeout,
            )
            return truncate_output(raw, self._max_output_bytes)

        return spec, Tool(
            name=spec.name,
            description=spec.description,
            parameters=dict(spec.input_schema),
            handler=handler,
        )


def detect_conflicts(existing: set[str], qualified_name: str) -> None:
    """检测工具名冲突。

    已注册的名字里出现同一个 qualified_name 时抛错——静默覆盖会让
    模型以为调用的是 A 服务器的工具、实际执行了 B 的。
    """
    if qualified_name in existing:
        raise ModelProtocolError(f"工具名冲突: {qualified_name}")


__all__ = [
    "MCP_TOOL_PREFIX",
    "MCPToolAdapter",
    "ToolInvoker",
    "detect_conflicts",
    "qualify_tool_name",
    "truncate_output",
    "validate_arguments",
]
