"""MCPClientManager：MCP 生命周期管理（22 阶段）。

职责：
- Runtime 启动时连接全部启用的本地 stdio 服务器并发现工具
- 把工具适配成项目内部 Tool 注册进 ToolRegistry
- Runtime 关闭时关闭 session 与子进程
- 生命周期**幂等**：重复 start / stop 不产生重复连接或残留
- `required` 服务器启动失败 → fail-closed（整体启动失败）
- `optional` 服务器启动失败 → 记录**安全**错误日志后跳过

安全边界：
- `command` / `args` **只来自配置**，没有任何 HTTP 入口能控制它们
- 子进程环境 = SDK 的安全继承集合 + 配置白名单；父进程密钥不下发
- 错误日志只含服务器名与异常类型名，不含 env、不含完整命令参数

### 关键实现约束：cancel scope 必须同任务进出

anyio 的 cancel scope 绑定到"进入它的那个任务"。若 A 任务 `__aenter__`、
B 任务 `__aexit__`，会抛
`RuntimeError: Attempted to exit cancel scope in a different task than it was entered in`。

因此每个服务器跑一个**长驻任务** `_session_task`，由它独占整个
`async with Client(...)` 的进入与退出；`list_tools()` / `call_tool()`
从其他任务发起，只操作传输流、不进出 cancel scope，是安全的。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import timedelta
from typing import Any, Mapping

import anyio
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, get_default_environment, stdio_client
try:
    from mcp.shared.exceptions import MCPError as _MCPBaseError
except ImportError:
    from mcp.shared.exceptions import McpError as _MCPBaseError  # type: ignore[attr-defined,assignment]

from mini_deerflow.mcp_adapter import MCPToolAdapter, detect_conflicts
from mini_deerflow.mcp_bridge import (
    AsyncPortal,
    PortalClosedError,
    PortalTimeoutError,
)
from mini_deerflow.mcp_config import MCPServerConfig
from mini_deerflow.tools import ToolRegistry

_LOGGER = logging.getLogger("mini_deerflow.mcp")

# 外层兜底比协议层超时多等的秒数：保证协议层超时**必然先到**，
# 使"调用超时"只有一种异常形态（见 _invoke 文档）
_TIMEOUT_BACKSTOP = 2.0


class MCPStartupError(RuntimeError):
    """required MCP 服务器启动失败（fail-closed）。"""


def build_child_env(whitelist: Mapping[str, str]) -> dict[str, str]:
    """构造 MCP 子进程环境 = SDK 安全继承集合 + 配置白名单。

    SDK 的 `get_default_environment()` 只含 PATH / SYSTEMROOT 等启动进程
    必需的系统变量，**不含任何应用密钥**。父进程其余变量一律不传，
    因此 MCP 子进程拿不到项目的 API Key。
    """
    env = dict(get_default_environment())
    env.update(whitelist)
    return env


async def _session_task(
    params: StdioServerParameters,
    call_timeout: float,
    init_timeout: float,
    ctx: dict[str, Any],
    ready: threading.Event,
    errors: list[BaseException],
) -> None:
    """长驻会话任务：**整个 `async with` 生命周期都由这一个任务持有**。

    anyio 的 cancel scope 绑定到"进入它的那个任务"——若 A 任务 `__aenter__`、
    B 任务 `__aexit__`，会直接抛
    `RuntimeError: Attempted to exit cancel scope in a different task`。

    其他任务只调用 `list_tools()` / `call_tool()`（只碰传输流，不进出
    cancel scope），因此跨任务调用是安全的。
    """
    # asyncio.Event 必须在**事件循环线程内**创建；同步侧通过
    # portal.call(event.set) 触发，不会跨线程改状态。
    _LOGGER.debug("MCP session task starting: server=%s", getattr(params, 'command', params.args[-1] if hasattr(params, 'args') else 'unknown'))
    stopping = asyncio.Event()
    ctx["stopping"] = stopping
    try:
        # MCP 2.x: stdio_client 是 async generator，yield (read_stream, write_stream, process)
        async with stdio_client(params) as (read_stream, write_stream):
            client = ClientSession(
                read_stream,
                write_stream,
                read_timeout_seconds=timedelta(seconds=init_timeout),
            )
            async with client:
                # MCP 2.x: ClientSession 不会自动初始化，需要显式调用
                await client.initialize()
                ctx["client"] = client
                ctx["init_ok"] = True  # 标记初始化成功，cleanup 时的 BrokenResourceError 不影响
                ready.set()
                await stopping.wait()
    except BaseException as exc:  # noqa: BLE001 - 记录后原样上抛
        # 忽略 subprocess 正常退出导致的 BrokenResourceError（stdio_client 清理竞态，
        # 不影响实际工具调用）。BrokenResourceError 可能直接抛出，也可能被包在
        # ExceptionGroup 里。
        should_raise = True
        if isinstance(exc, ExceptionGroup):
            broken = [e for e in exc.exceptions if isinstance(e, BrokenResourceError)]
            if broken and len(broken) == len(exc.exceptions):
                # 全部子异常都是 BrokenResourceError → 忽略
                should_raise = False
            elif broken:
                # 混合异常：只去掉 BrokenResourceError，其余上抛
                remaining = [e for e in exc.exceptions if not isinstance(e, BrokenResourceError)]
                if not remaining:
                    should_raise = False
                elif remaining:
                    exc = ExceptionGroup(str(exc), remaining)  # type: ignore
        if should_raise:
            errors.append(exc)
            raise
        # BrokenResourceError 被忽略：finally 照常执行，但 ready 置位后
        # _start_one 会看到 ctx["init_ok"] 不为 True → 走 client is None 分支
    finally:
        # 连接失败时也走到这里，确保等待方不会卡到超时
        ready.set()


def _safe_error(exc: BaseException) -> str:
    """只保留异常类型名，避免把命令行、env 或供应商回显写进日志。"""
    return type(exc).__name__


class _Session:
    """一个已启动服务器的运行期记录。

    `stopping` 由长驻会话任务在事件循环内创建，
    同步侧只能通过 `portal.call(event.set)` 触发。
    """

    __slots__ = ("client", "future", "stopping", "errors")

    def __init__(self, future: Any, errors: list[BaseException]) -> None:
        self.client: Client | None = None
        self.stopping: Any | None = None
        self.future = future
        self.errors = errors


class MCPClientManager:
    """管理一组本地 stdio MCP 服务器的完整生命周期。"""

    def __init__(self, configs: list[MCPServerConfig] | None = None) -> None:
        self._configs = list(configs or [])
        self._portal: AsyncPortal | None = None
        self._sessions: dict[str, _Session] = {}
        # qualified_name → (原始服务器名, 原始工具名)
        self._tools: dict[str, tuple[str, str]] = {}
        self._started = False

    # ---------------------------------------------------------------- 状态

    @property
    def started(self) -> bool:
        return self._started

    @property
    def enabled(self) -> bool:
        return bool(self._configs)

    @property
    def registered_names(self) -> tuple[str, ...]:
        """已注册的 qualified 工具名（排序，便于断言与日志）。"""
        return tuple(sorted(self._tools))

    @property
    def running_servers(self) -> tuple[str, ...]:
        return tuple(sorted(self._sessions))

    # ---------------------------------------------------------------- 启动

    def start(self, registry: ToolRegistry) -> int:
        """连接所有启用的服务器并注册工具。返回注册工具数。

        幂等：已启动或没有配置时直接返回 0，不重复建连接。

        Raises:
            MCPStartupError: 任一 required 服务器启动失败。
        """
        if self._started or not self._configs:
            return 0

        # 桥只建一次：所有服务器共享同一个事件循环线程
        self._portal = AsyncPortal(name="mini-deerflow-mcp")
        registered = 0
        failed_required: list[str] = []

        for config in self._configs:
            try:
                registered += self._start_one(config, registry)
            except BaseException as exc:  # noqa: BLE001 - 逐服务器隔离失败
                # 解包 ExceptionGroup，逐个记录子异常
                if isinstance(exc, ExceptionGroup):
                    for sub_exc in exc.exceptions:
                        _LOGGER.warning(
                            "MCP 服务器启动失败: server=%s required=%s error=%s",
                            config.name,
                            config.required,
                            _safe_error(sub_exc),
                        )
                    exc = exc.exceptions[0]  # type: ignore
                _LOGGER.warning(
                    "MCP 服务器启动失败: server=%s required=%s error=%s",
                    config.name,
                    config.required,
                    _safe_error(exc),
                    exc_info=True,
                )
                if config.required:
                    failed_required.append(config.name)
                    failed_required.append(config.name)

        self._started = True

        if failed_required:
            # fail-closed：required 未就绪就整体失败，
            # 否则 Agent 会"以为自己有这些工具"却永远调不到。
            self.stop()
            raise MCPStartupError(
                "required MCP 服务器启动失败: " + ", ".join(sorted(failed_required))
            )

        return registered

    def _start_one(self, config: MCPServerConfig, registry: ToolRegistry) -> int:
        """连接单个服务器并注册其全部工具。"""
        assert self._portal is not None

        params = StdioServerParameters(
            command=config.command,
            args=list(config.args),
            env=build_child_env(config.env),
        )

        ctx: dict[str, Any] = {}
        ready = threading.Event()
        errors: list[BaseException] = []

        future = self._portal.submit(
            _session_task(params, config.call_timeout, config.startup_timeout, ctx, ready, errors)
        )
        session = _Session(future, errors)

        if not ready.wait(timeout=config.startup_timeout):
            self._signal_stop(session)
            self._discard(config.name)
            raise MCPStartupError(
                f"服务器 {config.name} 在 {config.startup_timeout}s 内未就绪"
            )

        if errors:
            self._discard(config.name)
            raise errors[0]

        session.client = ctx.get("client")
        session.stopping = ctx.get("stopping")
        init_ok = ctx.get("init_ok")
        if not init_ok or session.client is None:
            _LOGGER.warning(
                "MCP 服务器初始化未完成: server=%s init_ok=%s client=%s errors=%s",
                config.name, init_ok, session.client is not None, errors,
            )
            self._discard(config.name)
            raise MCPStartupError(f"服务器 {config.name} 连接后未完成初始化")

        self._sessions[config.name] = session

        try:
            tools = self._portal.run(
                session.client.list_tools(),
                timeout=config.startup_timeout,
            )
        except Exception:
            self.stop()
            raise

        mcp_tools = getattr(tools, "tools", None) or []
        _LOGGER.info(
            "MCP 服务器已就绪: server=%s tools=%s",
            config.name,
            [t.name for t in mcp_tools],
        )
        adapter = MCPToolAdapter(
            server_name=config.name,
            call_timeout=config.call_timeout,
            max_output_bytes=config.max_output_bytes,
            invoker=self._invoke,
        )

        existing = {tool.name for tool in registry.list_tools()}
        registered = 0
        for mcp_tool in mcp_tools:
            spec, tool = adapter.to_tool(mcp_tool)
            detect_conflicts(existing, spec.name)
            if spec.name in self._tools:
                raise MCPStartupError(f"工具名冲突: {spec.name}")
            self._tools[spec.name] = (config.name, getattr(mcp_tool, "name", ""))
            try:
                registry.register(tool)
            except Exception:
                del self._tools[spec.name]
                raise
            existing.add(spec.name)
            registered += 1

        _LOGGER.info(
            "MCP 服务器已连接: server=%s tools=%d", config.name, registered
        )
        return registered

    def _discard(self, server: str) -> None:
        """丢弃一个未就绪的会话记录（避免半开状态被后续复用）。"""
        self._sessions.pop(server, None)

    def _signal_stop(self, session: _Session) -> None:
        """让长驻会话任务退出 `async with`（由它自己执行 `__aexit__`）。

        事件对象属于事件循环，必须经 `portal.call` 调度过去。
        """
        if self._portal is None or session.stopping is None:
            return
        try:
            self._portal.call(session.stopping.set)
        except Exception:  # noqa: BLE001 - 关闭期失败不阻塞后续清理
            pass

    # ---------------------------------------------------------------- 调用

    async def _call_with_timeout(self, coro: Coroutine, timeout: float) -> Any:
        """带独立 timeout scope 的协程调用。

        独立 scope 意味着：cancel 只传播到本协程，不杀死外层 session。
        """
        try:
            async with asyncio.timeout(timeout):
                return await coro
        except asyncio.TimeoutError:
            raise TimeoutError(f"MCP 操作超过 {timeout} 秒未返回") from None

    def _invoke(self, qualified_name: str, arguments: Mapping[str, Any],
                timeout: float) -> str:
        """Tool.handler 的回调：同步调用异步 MCP。

        走长期事件循环桥，**不在这里新建事件循环**。

        超时为什么有两层、且必须归一：
        - `read_timeout_seconds` 是协议层超时，会干净地结束这次请求；
        - portal.run(timeout=...) 是外层兜底，防止事件循环整体卡死。
        两者若都取同一个值，**谁先到就有随机性**，于是同一个"调用超时"
        有时抛 TimeoutError、有时抛 MCP 协议错误。这里把外层放宽
        +_TIMEOUT_BACKSTOP 秒（协议层先到），并把协议层的
        REQUEST_TIMEOUT 也映射成 TimeoutError，让调用方只面对一种语义。
        """
        entry = self._tools.get(qualified_name)
        if entry is None:
            raise KeyError(f"未知 MCP 工具: {qualified_name}")
        server_name, original_name = entry

        portal = self._portal
        session = self._sessions.get(server_name)
        if portal is None or portal.closed or session is None or session.client is None:
            raise PortalClosedError("MCP 客户端管理器未启动或已关闭")

        try:
            coro = session.client.call_tool(
                original_name,
                dict(arguments),
                read_timeout_seconds=timedelta(seconds=timeout),
            )
            # 独立 timeout scope：只 cancel 这一个 tool call，不杀死 session
            result = portal.run(
                self._call_with_timeout(coro, timeout + _TIMEOUT_BACKSTOP),
                timeout=timeout + _TIMEOUT_BACKSTOP + 5,
            )
        except PortalTimeoutError as exc:
            raise TimeoutError(str(exc)) from None
        except asyncio.CancelledError:
            # Tool call 被独立 scope cancel，不杀死 session
            raise TimeoutError(f"MCP 操作超过 {timeout} 秒未返回") from None
        except _MCPBaseError as exc:
            # MCP 层也可能有超时（如 "Timed out while waiting for response"）
            if "timed out" in str(exc).lower() or "timeout" in str(exc).lower():
                raise TimeoutError(str(exc)) from None
            raise

        return _render_result(result)

    # ---------------------------------------------------------------- 关闭

    def stop(self) -> None:
        """关闭全部会话与事件循环桥。幂等，可重复调用。

        关闭顺序很重要：先逐个让**长驻任务自己**退出 `async with`
        （cancel scope 必须同任务进出），全部收尾后才关事件循环桥。
        """
        portal, self._portal = self._portal, None
        sessions, self._sessions = self._sessions, {}
        self._tools.clear()
        self._started = False

        if portal is None:
            return

        for name in sorted(sessions):
            session = sessions[name]
            try:
                if session.stopping is not None:
                    portal.call(session.stopping.set)
                portal.wait(session.future, timeout=10.0)
            except Exception as exc:  # noqa: BLE001 - 关闭失败不应中断 shutdown
                _LOGGER.warning(
                    "MCP 服务器关闭失败: server=%s error=%s",
                    name,
                    _safe_error(exc),
                )

        # 桥最后关：前面所有收尾都要跑在它上面
        portal.close()


def _render_result(result: Any) -> str:
    """CallToolResult → 项目侧的字符串工具结果。

    - is_error=True 时显式带上错误标记，让模型与日志都能看见失败
    - 只取 text 类型块；二进制块转为占位说明，避免把 base64 灌进上下文
    """
    if result is None:
        return ""

    is_error = bool(getattr(result, "is_error", False))
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            parts.append(getattr(block, "text", "") or "")
        elif block_type == "image":
            parts.append("[二进制内容：图片]")
        elif block_type == "resource":
            parts.append("[二进制内容：资源]")

    text = "\n".join(part for part in parts if part)
    if is_error:
        return text or "工具调用失败"
    return text


def find_server_config(configs: list[MCPServerConfig], name: str) -> MCPServerConfig:
    """按名字取服务器配置；找不到抛 LookupError。"""
    for config in configs:
        if config.name == name:
            return config
    raise LookupError(f"未找到 MCP 服务器配置: {name}")


__all__ = [
    "MCPClientManager",
    "MCPStartupError",
    "build_child_env",
    "find_server_config",
]
