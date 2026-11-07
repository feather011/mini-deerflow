"""Windows 下 harness 子进程不应弹出可见控制台窗口（回归）。

背景：工具执行期间桌面会弹出黑色命令行窗口，标题是
`...\\.venv\\Scripts\\python.exe`。根因有两层：

1. **标志位互斥**：`CREATE_NO_WINDOW` 与 `DETACHED_PROCESS` 同时指定时，
   Windows 会**忽略** `CREATE_NO_WINDOW`（MSDN："This flag is ignored if
   … used with either CREATE_NEW_CONSOLE or DETACHED_PROCESS"）。
   代码原先三个标志全给，等于只剩下 `DETACHED_PROCESS`。
2. **uv venv 的双层进程**：`.venv\\Scripts\\python.exe` 是转发器，会再 fork
   出真正的解释器。`DETACHED_PROCESS` 只作用在转发器那一层；第二层解释器
   作为控制台程序发现无可继承的控制台，就自己新分配一个 → 弹出可见窗口。

因此标志位必须是 `CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`，
**不含** `DETACHED_PROCESS`。

这些断言只检查标志位本身，不启动真实子进程、不消耗额度。
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from mini_deerflow.harness_runner import _windows_creation_flags


pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="Windows 专属的 creationflags 行为",
)


class TestWindows创建标志:
    def test_不含_DETACHED_PROCESS(self) -> None:
        """带 DETACHED_PROCESS 会让 CREATE_NO_WINDOW 失效 → 弹窗。"""
        flags = _windows_creation_flags()
        detached = getattr(subprocess, "DETACHED_PROCESS", 0)
        assert not flags & detached, (
            "creationflags 不得包含 DETACHED_PROCESS："
            "Windows 会因此忽略 CREATE_NO_WINDOW 并弹出可见控制台窗口"
        )

    def test_含_CREATE_NO_WINDOW(self) -> None:
        """没有它就没有「不分配控制台窗口」的保证。"""
        flags = _windows_creation_flags()
        no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        assert flags & no_window

    def test_含_CREATE_NEW_PROCESS_GROUP(self) -> None:
        """进程组隔离必须保留（taskkill /T 与组语义依赖它）。"""
        flags = _windows_creation_flags()
        new_group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        assert flags & new_group

    def test_标志组合完全等于期望值(self) -> None:
        """精确锁定组合，防止后人「顺手」再加回 DETACHED_PROCESS。"""
        expected = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        assert _windows_creation_flags() == expected
