"""同步 Agent ↔ 异步 MCP 的事件循环桥（22 阶段）。

### 为什么需要它

`Agent` / `ToolRegistry.execute()` 是**同步**调用，而 MCP SDK 是**异步**的。
最容易犯的错是在 handler 里写 `asyncio.run(...)`——那会每次工具调用
新建并销毁一个事件循环，而 MCP 的 session / 传输流绑定在别的循环上，
跨循环调用会直接报错，且每次调用都要重建连接。

### 做法

专用线程上跑**一个长期存在的事件循环**，同步侧用
`asyncio.run_coroutine_threadsafe()` 把协程投递过去：

```
Agent (同步线程)
   └─ portal.run(coro)
        └─ run_coroutine_threadsafe(coro, loop)  ──→  [MCP 事件循环线程]
                                                     └─ 客户端 session 在这里常驻
```

生命周期：`start()` → 多次 `run()` → `close()`；`close()` **幂等**。
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any, Callable, Coroutine


class PortalClosedError(RuntimeError):
    """桥已关闭，无法再提交协程。"""


class PortalTimeoutError(TimeoutError):
    """协程在指定超时内没有返回。"""


class AsyncPortal:
    """长期事件循环线程；同步调用方通过 run() 提交协程。"""

    def __init__(self, name: str = "mini-deerflow-mcp") -> None:
        self._loop = asyncio.new_event_loop()
        self._closed = False
        self._lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._run_loop, name=name, daemon=True
        )
        self._thread.start()

    @property
    def closed(self) -> bool:
        return self._closed

    def _run_loop(self) -> None:
        # 把 __init__ 里创建的 loop 设为**本线程**的当前 loop，然后常驻运行。
        # 不用 asyncio.get_event_loop()：在新线程上它是弃用 API，且行为依赖
        # 是否已有 loop，容易在不同 Python 版本上不一致。
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def run(
        self,
        coro: Coroutine[Any, Any, Any],
        timeout: float | None = None,
    ) -> Any:
        """把协程提交到事件循环线程并同步等待结果。

        Args:
            coro: 要执行的协程。
            timeout: 超时秒数；超时会取消该协程再抛 PortalTimeoutError。

        Raises:
            PortalClosedError / PortalTimeoutError / 协程自身的异常。
        """
        with self._lock:
            if self._closed:
                # 关闭后不提交：否则协程永远等不到执行，形成悬挂任务
                coro.close()
                raise PortalClosedError("MCP 事件循环桥已关闭")
            future = asyncio.run_coroutine_threadsafe(coro, self._loop)

        try:
            return future.result(timeout=timeout)
        except FutureTimeoutError:
            future.cancel()
            raise PortalTimeoutError(f"MCP 操作超过 {timeout} 秒未返回") from None

    def call(self, fn: Callable[..., Any], *args: Any) -> None:
        """从同步线程调度一个无返回值的回调到事件循环线程。

        典型用途：触发会话任务内部创建的 `asyncio.Event.set()`。
        事件对象归事件循环所有，因此**必须**通过
        `call_soon_threadsafe` 触发，不能在别的线程上直接 set。
        """
        with self._lock:
            if self._closed:
                raise PortalClosedError("MCP 事件循环桥已关闭")
            self._loop.call_soon_threadsafe(fn, *args)

    def run_sync(
        self,
        factory: Callable[[], Coroutine[Any, Any, Any]],
        timeout: float | None = None,
    ) -> Any:
        """先构造协程再提交；比 run() 更适合"只在确有必要时才创建协程"的调用方。"""
        return self.run(factory(), timeout=timeout)

    def submit(self, coro: Coroutine[Any, Any, Any]) -> Future:
        """提交协程但**不等待**，返回 concurrent.futures.Future。

        用于需要由**同一个长驻任务**持有生命周期的场景
        （如 anyio 的 `async with` 要求 `__aenter__` / `__aexit__` 在同一任务执行）。
        """
        with self._lock:
            if self._closed:
                coro.close()
                raise PortalClosedError("MCP 事件循环桥已关闭")
            return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def wait(self, future: Future, timeout: float | None = None) -> Any:
        """等待 submit() 返回的 Future；桥已关闭时立即失败而非悬挂。"""
        with self._lock:
            if self._closed:
                raise PortalClosedError("MCP 事件循环桥已关闭")
        try:
            return future.result(timeout=timeout)
        except FutureTimeoutError as exc:
            raise PortalTimeoutError(f"等待 MCP 任务超过 {timeout} 秒") from exc

    def close(self, join_timeout: float = 5.0) -> None:
        """关闭桥（幂等）：先排空未完成协程，再停止循环并回收线程。

        先排空很重要——直接停循环会让被 cancel 的协程来不及收尾，
        退出时打印 "Task was destroyed but it is pending" 噪声，
        也意味着超时取消没有真正落地。
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            loop, thread = self._loop, self._thread

        try:
            future = asyncio.run_coroutine_threadsafe(self._drain(loop), loop)
            future.result(timeout=join_timeout)
        except Exception:
            # 排空失败不应阻止关闭；后面仍会 stop + join
            pass

        try:
            loop.call_soon_threadsafe(loop.stop)
        except RuntimeError:
            # 循环已经不在运行（极端情况下已被外部关闭）
            pass
        thread.join(timeout=join_timeout)
        if not thread.is_alive():
            try:
                loop.close()
            except RuntimeError:
                pass

    @staticmethod
    async def _drain(loop: asyncio.AbstractEventLoop) -> None:
        """取消并等待该循环上所有未完成任务。

        必须排除当前任务：`_drain` 自己就跑在这个循环上，
        把自己也 cancel 掉会让 Task.cancel() 递归调用自身
        （RecursionError），同时 gather 永远等不到完成。
        """
        current = asyncio.current_task(loop)
        pending = [
            task
            for task in asyncio.all_tasks(loop)
            if not task.done() and task is not current
        ]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
