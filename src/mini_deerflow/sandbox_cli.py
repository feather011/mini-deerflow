"""Sandbox CLI 装配模块（16F 阶段）。

负责 CLI 参数解析、工厂创建、/tools 和 /sandbox 命令。
不包含 Agent 循环，不包含 CLI 主循环。
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from mini_deerflow.sandbox_command import (
    AllowedCommand,
    CommandRequest,
    LocalCommandRunner,
)
from mini_deerflow.sandbox_docker import (
    ContainerCommand,
    DockerSandboxConfig,
    DockerSandboxRunner,
)
from mini_deerflow.sandbox_files import SandboxFileSystem
from mini_deerflow.sandbox_path import WorkspacePathResolver
from mini_deerflow.sandbox_tools import SandboxToolSet
from mini_deerflow.tools import Tool

if TYPE_CHECKING:
    from mini_deerflow.skill_catalog import SkillCatalog
    from mini_deerflow.thread_store import SQLiteThreadStore


# --- Sandbox 状态数据类 ---


@dataclass(frozen=True)
class SandboxStatus:
    """Sandbox 运行时的只读状态快照。"""
    enabled: bool
    backend: str | None  # None when disabled
    allowed_commands: tuple[str, ...]  # empty when disabled
    timeout_seconds: float | None  # None when disabled
    max_output_bytes: int | None  # None when disabled
    max_read_bytes: int | None
    max_write_bytes: int | None
    workspace_writable: bool | None  # None when disabled


# --- 工厂 ---


def create_sandbox_runtime(
    root: Path,
    *,
    image: str,
    timeout_seconds: float,
    max_output_bytes: int,
    max_read_bytes: int,
    max_write_bytes: int,
) -> tuple[SandboxFileSystem, DockerSandboxRunner, LocalCommandRunner, SandboxToolSet]:
    """创建完整的 Sandbox 运行时组件。

    装配顺序：
    1. WorkspacePathResolver（路径边界）
    2. SandboxFileSystem（文件操作）
    3. 定位 docker.exe
    4. LocalCommandRunner（只允许 docker，用于预检和容器管理）
    5. DockerSandboxConfig（安全配置）
    6. ContainerCommand 白名单（只允许 python）
    7. DockerSandboxRunner（隔离执行）
    8. SandboxToolSet（工具包装）

    返回 (files, docker_runner, local_runner, tool_set)。
    local_runner 用于预检 Docker daemon，docker_runner 用于执行容器命令。

    失败时异常原样传播。
    """
    # 1. Resolver
    resolver = WorkspacePathResolver(root)

    # 2. FileSystem
    files = SandboxFileSystem(
        resolver,
        max_read_bytes=max_read_bytes,
        max_write_bytes=max_write_bytes,
        max_list_entries=1_000,
    )

    # 3. 定位 docker.exe
    docker_path = shutil.which("docker")
    if docker_path is None:
        raise FileNotFoundError(
            "Docker CLI 不可用：未找到 docker.exe。"
            "请确保 Docker 已安装并位于 PATH 中。"
        )
    docker_abs = Path(docker_path).resolve()
    if not docker_abs.exists():
        raise FileNotFoundError(f"Docker CLI 路径不存在: {docker_abs}")
    if not docker_abs.is_file():
        raise ValueError(f"Docker CLI 路径不是普通文件: {docker_abs}")

    # 4. LocalCommandRunner（只允许 docker）
    docker_cmd = AllowedCommand(
        name="docker",
        argv_prefix=(str(docker_abs),),
    )
    local_runner = LocalCommandRunner(
        resolver=resolver,
        allowed_commands=[docker_cmd],
        default_timeout_seconds=timeout_seconds,
        max_timeout_seconds=60.0,
        max_output_bytes=max_output_bytes,
    )

    # 5. DockerSandboxConfig
    config = DockerSandboxConfig(image=image)

    # 6. ContainerCommand 白名单
    container_commands = [
        ContainerCommand(
            name="python",
            argv_prefix=("/usr/local/bin/python",),
        ),
    ]

    # 7. DockerSandboxRunner
    docker_runner = DockerSandboxRunner(
        resolver=resolver,
        host_runner=local_runner,
        container_commands=container_commands,
        config=config,
    )

    # 8. SandboxToolSet
    tool_set = SandboxToolSet(files=files, runner=docker_runner)

    return files, docker_runner, local_runner, tool_set


class SandboxStartupError(RuntimeError):
    """Sandbox 启动失败：所有失败原样传播，不降级。"""

    pass


def preflight_docker(
    local_runner: LocalCommandRunner,
    image: str,
    timeout: float = 10.0,
) -> None:
    """Docker 启动预检。

    使用 LocalCommandRunner 验证：
    1. Docker daemon 可用（docker info）
    2. 指定镜像存在（docker image inspect）

    任何失败均抛出 SandboxStartupError。
    不自动 pull。
    """
    # docker info
    info_req = CommandRequest(
        command="docker",
        args=("info", "--format", "{{.ServerVersion}}"),
        cwd=None,
        timeout_seconds=timeout,
    )
    info_result = local_runner.run(info_req)
    if info_result.exit_code != 0:
        raise SandboxStartupError(
            f"Docker daemon 不可用：docker info 退出码 {info_result.exit_code}。"
            f"stderr: {info_result.stderr[:200]}"
        )

    # docker image inspect
    inspect_req = CommandRequest(
        command="docker",
        args=("image", "inspect", image),
        cwd=None,
        timeout_seconds=timeout,
    )
    inspect_result = local_runner.run(inspect_req)
    if inspect_result.exit_code != 0:
        raise SandboxStartupError(
            f"镜像不存在：{image}。"
            f"请提前拉取：docker pull {image}"
        )


def get_sandbox_status(
    enabled: bool,
    docker_runner: DockerSandboxRunner | None,
    timeout_seconds: float,
    max_output_bytes: int,
    max_read_bytes: int,
    max_write_bytes: int,
) -> SandboxStatus:
    """从运行时组件构建状态快照。"""
    if not enabled:
        return SandboxStatus(
            enabled=False,
            backend=None,
            allowed_commands=(),
            timeout_seconds=None,
            max_output_bytes=None,
            max_read_bytes=None,
            max_write_bytes=None,
            workspace_writable=None,
        )
    return SandboxStatus(
        enabled=True,
        backend="Docker",
        allowed_commands=("python",),
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
        max_read_bytes=max_read_bytes,
        max_write_bytes=max_write_bytes,
        workspace_writable=True,
    )


# --- /tools 命令 ---


def format_tools(tools: list[Tool]) -> str:
    """格式化工具列表为空字符串表示无工具。

    纯函数：不执行工具、不调用 Agent、不读取文件。
    按 name 稳定排序。
    """
    if not tools:
        return "暂无可用 Tool"

    # 按 name 升序排序
    sorted_tools = sorted(tools, key=lambda t: t.name)

    lines = ["可用 Tools："]
    for t in sorted_tools:
        desc = t.description or "(无描述)"
        lines.append(f"- {t.name}：{desc}")

    return "\n".join(lines)


# --- /sandbox 命令 ---


def format_sandbox_status(status: SandboxStatus) -> str:
    """格式化 Sandbox 状态。

    纯函数：不调用 Docker、不访问文件系统。
    不显示 Workspace 绝对路径，不显示 Docker 路径。
    """
    if not status.enabled:
        return (
            "Sandbox：未启用\n"
            "使用 --sandbox-root <path> 显式启用"
        )

    return (
        "Sandbox：已启用\n"
        f"执行后端：{status.backend}\n"
        "网络：禁用\n"
        "RootFS：只读（workspace 可写）\n"
        f"允许命令：{', '.join(status.allowed_commands)}\n"
        f"默认超时：{status.timeout_seconds} 秒\n"
        f"最大输出：{status.max_output_bytes} 字节\n"
        f"最大读取：{status.max_read_bytes} 字节\n"
        f"最大写入：{status.max_write_bytes} 字节"
    )


# --- help 更新文本 ---


SANDBOX_HELP_ADDITION = (
    "  /tools               查看当前可用工具\n"
    "  /sandbox             查看 Sandbox 状态\n"
)


SANDBOX_NOTE = (
    "\n"
    "Sandbox 默认关闭，使用 --sandbox-root <path> 显式启用。\n"
)
