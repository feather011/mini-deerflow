"""本地 MCP 人工 smoke（Phase 22）。

**显式执行、不进 pytest、不访问外部网络** —— 只连仓库自带的确定性测试
服务器 `tests/fixtures/mcp_test_server.py`（stdio，本地子进程）。

    uv run python scripts/mcp_smoke.py

验证四件事：
1. 启动并发现工具（命名空间 `mcp__smoke__*`）
2. `add` / `echo` 真实执行成功
3. 关闭后**没有残留子进程**
4. 输出不含环境变量值 / API Key

退出码：0 通过；1 失败。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import psutil

from mini_deerflow.mcp_config import MCPServerConfig
from mini_deerflow.mcp_manager import MCPClientManager
from mini_deerflow.tools import create_default_registry

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_PATH = REPO_ROOT / "tests" / "fixtures" / "mcp_test_server.py"


def _child_pids() -> set[int]:
    """当前进程的子进程 PID 集合（用于断言关闭后无残留）。"""
    try:
        return {c.pid for c in psutil.Process(os.getpid()).children(recursive=True)}
    except psutil.Error:  # pragma: no cover - 环境不支持时跳过
        return set()


def main() -> int:
    if not SERVER_PATH.is_file():
        print(f"测试 MCP 服务器不存在: {SERVER_PATH}")
        return 1

    # 只在启动前打快照：残差 = 启动后仍在、关闭后也没消失的进程
    before = _child_pids()

    config = MCPServerConfig(
        name="smoke",
        command=sys.executable,
        args=(str(SERVER_PATH),),
        startup_timeout=30.0,
        call_timeout=15.0,
    )
    registry = create_default_registry()
    manager = MCPClientManager([config])

    failures: list[str] = []
    started: set[int] = set()
    try:
        count = manager.start(registry)
        started = _child_pids() - before
        names = sorted(t.name for t in registry.list_tools())
        # 统一口径：count 是本次发现的 MCP 工具数，len(names) 是整个
        # ToolRegistry（含既有本地 calculator）的总数，两者不要混着说。
        print(f"[1/4] 发现 {count} 个 MCP 工具，"
              f"ToolRegistry 总计 {len(names)} 个工具（含 calculator）。")
        print(f"      工具清单: {names}")

        expected = {"mcp__smoke__add", "mcp__smoke__echo",
                    "mcp__smoke__failing_tool", "mcp__smoke__wait"}
        if not expected.issubset(set(names)):
            failures.append(f"工具发现不完整: 缺 {sorted(expected - set(names))}")

        if not started:
            failures.append("未观察到 MCP 子进程（发现环节可能没真正连接）")

        # [2/4] 真实执行
        add_result = registry.execute("mcp__smoke__add", {"a": 17, "b": 25})
        echo_result = registry.execute("mcp__smoke__echo", {"text": "MCP_SMOKE_OK"})
        print(f"[2/4] add(17, 25) = {add_result}")
        print(f"      echo       = {echo_result}")
        if add_result.strip() != "42":
            failures.append(f"add 结果应为 42，实际 {add_result!r}")
        if "MCP_SMOKE_OK" not in echo_result:
            failures.append(f"echo 结果未回显: {echo_result!r}")

        # 参数错误路径（协议层稳定语义）
        try:
            registry.execute("mcp__smoke__add", {"a": 1})
        except Exception as exc:  # noqa: BLE001 - smoke 要的是"必须抛"
            print(f"[3/4] 缺参被拒绝: {type(exc).__name__}")
        else:
            failures.append("缺少必填参数时没有报错")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"启动/执行失败: {type(exc).__name__}: {exc}")
    finally:
        manager.stop()

    # [4/4] 关闭后无残留
    leftover = _child_pids() - before
    # 上面可能已被 OS 回收；再等一拍避免竞态
    if leftover:
        import time

        time.sleep(1.0)
        leftover = _child_pids() - before
    print(f"[4/4] 启动期间子进程 {len(started)} 个，关闭后残留 {len(leftover)} 个")
    if leftover:
        failures.append(f"关闭后仍有残留子进程: {sorted(leftover)}")

    if failures:
        print("\nMCP smoke 失败：")
        for item in failures:
            print(f"  - {item}")
        return 1

    print("\nMCP smoke 通过（本地 stdio，零外网，未输出任何环境变量值）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
