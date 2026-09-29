"""Docker 沙箱隔离执行器（16D 阶段）。

本模块在 LocalCommandRunner 之上包装 Docker 容器隔离，
提供比本地进程更强的安全边界。

警告：DockerSandboxRunner 依赖 Docker daemon 和容器镜像，不是绝对安全边界。
仍需信任 Docker 运行时、内核和 bind mount 的 Workspace 内容。
生产环境应考虑 rootless Docker、用户命名空间、seccomp/AppArmor 和镜像 digest 固定。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Protocol, Sequence, runtime_checkable

from mini_deerflow.sandbox_command import (
    CommandNotAllowedError,
    CommandRequest,
    CommandResult,
    CommandRunner,
)
from mini_deerflow.sandbox_path import (
    SandboxPathResolver,
    SandboxPathViolationError,
)


# --- 异常体系 ---


class DockerSandboxError(Exception):
    """Docker 沙箱错误的基类。"""


class DockerCommandNotAllowedError(DockerSandboxError):
    """容器命令名称不在白名单中时抛出。"""

    def __init__(self, command: str) -> None:
        self.command = command
        super().__init__(f"未知容器命令: '{command}'")


class DockerConfigurationError(DockerSandboxError):
    """Docker 配置错误时抛出。"""

    def __init__(self, reason: str) -> None:
        super().__init__(f"Docker 配置错误: {reason}")


class DockerCleanupError(DockerSandboxError):
    """容器清理失败时抛出。"""

    def __init__(self, container_name: str, reason: str) -> None:
        self.container_name = container_name
        super().__init__(f"清理容器 '{container_name}' 失败: {reason}")


# --- 数据模型 ---


@dataclass(frozen=True)
class ContainerCommand:
    """不可变的容器内命令定义。

    argv_prefix 是容器内 POSIX 绝对路径的固定前缀。
    """

    name: str
    argv_prefix: tuple[str, ...]

    def __post_init__(self) -> None:
        # name 验证
        if not isinstance(self.name, str):
            raise TypeError(
                f"name 必须是 str，收到: {type(self.name).__name__}"
            )
        if not self.name:
            raise TypeError("name 不能为空字符串")

        # argv_prefix 验证
        if not isinstance(self.argv_prefix, tuple):
            raise TypeError(
                f"argv_prefix 必须是 tuple，收到: {type(self.argv_prefix).__name__}"
            )
        if not self.argv_prefix:
            raise TypeError("argv_prefix 不能为空")

        for i, elem in enumerate(self.argv_prefix):
            if not isinstance(elem, str):
                raise TypeError(
                    f"argv_prefix[{i}] 必须是 str，收到: {type(elem).__name__}"
                )
            if not elem:
                raise TypeError(f"argv_prefix[{i}] 不能是空字符串")
            if "\x00" in elem:
                raise TypeError(f"argv_prefix[{i}] 不能包含 NUL 字符")

        # argv_prefix[0] 必须是容器内 POSIX 绝对路径
        first = self.argv_prefix[0]
        if not first.startswith("/"):
            raise ValueError(
                f"argv_prefix[0] 必须是 POSIX 绝对路径，收到: {first}"
            )
        if first.startswith("-"):
            raise ValueError(
                f"argv_prefix[0] 不能以 '-' 开头，收到: {first}"
            )


@dataclass(frozen=True)
class DockerSandboxConfig:
    """不可变的 Docker 安全配置。

    所有参数经过验证，确保默认配置使用安全值。
    """

    image: str
    container_workspace: str = "/workspace"
    memory_limit: str = "256m"
    cpu_limit: float = 1.0
    pids_limit: int = 128
    tmpfs_size: str = "64m"
    user: str = "65534:65534"
    workspace_writable: bool = True

    # 合法 memory/tempfs 格式：整数 + k/m/g 后缀
    _MEMORY_PATTERN = __import__("re").compile(r"^\d+[kKmMgG]$")

    def __post_init__(self) -> None:
        # image 验证
        if not isinstance(self.image, str):
            raise TypeError(
                f"image 必须是 str，收到: {type(self.image).__name__}"
            )
        if not self.image:
            raise TypeError("image 不能为空")
        if "\x00" in self.image:
            raise TypeError("image 不能包含 NUL 字符")
        if self.image.startswith("-"):
            raise ValueError(f"image 不能以 '-' 开头: {self.image}")
        if any(c.isspace() for c in self.image):
            raise ValueError(f"image 不能包含空白字符: {self.image}")

        # container_workspace 验证
        if not isinstance(self.container_workspace, str):
            raise TypeError(
                f"container_workspace 必须是 str，收到: {type(self.container_workspace).__name__}"
            )
        if not self.container_workspace.startswith("/"):
            raise ValueError(
                f"container_workspace 必须是 POSIX 绝对路径，收到: {self.container_workspace}"
            )
        if self.container_workspace == "/":
            raise ValueError("container_workspace 不能是根目录 '/'")
        parts = self.container_workspace.split("/")
        if ".." in parts:
            raise ValueError(
                f"container_workspace 不能包含 '..' 组件: {self.container_workspace}"
            )
        for part in parts:
            if part and part.startswith("-"):
                raise ValueError(
                    f"container_workspace 不能以 '-' 开头: {self.container_workspace}"
                )

        # memory_limit 验证
        if not isinstance(self.memory_limit, str):
            raise TypeError(
                f"memory_limit 必须是 str，收到: {type(self.memory_limit).__name__}"
            )
        if not self._MEMORY_PATTERN.match(self.memory_limit):
            raise ValueError(
                f"memory_limit 格式不安全，收到: {self.memory_limit}"
            )

        # cpu_limit 验证
        if isinstance(self.cpu_limit, bool):
            raise TypeError("cpu_limit 不能是 bool")
        if not isinstance(self.cpu_limit, (int, float)):
            raise TypeError(
                f"cpu_limit 必须是数字，收到: {type(self.cpu_limit).__name__}"
            )
        if self.cpu_limit <= 0 or self.cpu_limit > 64:
            raise ValueError(f"cpu_limit 必须在 0-64 之间，收到: {self.cpu_limit}")

        # pids_limit 验证
        if isinstance(self.pids_limit, bool):
            raise TypeError("pids_limit 不能是 bool")
        if not isinstance(self.pids_limit, int):
            raise TypeError(
                f"pids_limit 必须是 int，收到: {type(self.pids_limit).__name__}"
            )
        if self.pids_limit <= 0:
            raise ValueError(f"pids_limit 必须大于 0，收到: {self.pids_limit}")

        # tmpfs_size 验证
        if not isinstance(self.tmpfs_size, str):
            raise TypeError(
                f"tmpfs_size 必须是 str，收到: {type(self.tmpfs_size).__name__}"
            )
        if not self._MEMORY_PATTERN.match(self.tmpfs_size):
            raise ValueError(
                f"tmpfs_size 格式不安全，收到: {self.tmpfs_size}"
            )

        # user 验证
        if not isinstance(self.user, str):
            raise TypeError(
                f"user 必须是 str，收到: {type(self.user).__name__}"
            )
        if not self.user:
            raise TypeError("user 不能为空")
        if self.user.startswith("-"):
            raise ValueError(f"user 不能以 '-' 开头: {self.user}")
        # 不允许 root
        if self.user in ("0", "0:0", "root", "root:root"):
            raise ValueError(f"user 不能是 root: {self.user}")

        # workspace_writable 验证
        if not isinstance(self.workspace_writable, bool):
            raise TypeError(
                f"workspace_writable 必须是 bool，收到: {type(self.workspace_writable).__name__}"
            )


# --- 内部辅助 ---


_CONTAINER_NAME_PREFIX = "mini-deerflow-"
_NAME_PATTERN = __import__("re").compile(
    r"^[a-z0-9][a-z0-9-]*$"
)


def _validate_container_name(name: str) -> None:
    """验证容器名格式。"""
    if not name:
        raise ValueError("容器名不能为空")
    if not _NAME_PATTERN.match(name):
        raise ValueError(
            f"容器名格式不安全（只能包含小写字母、数字、连字符，不能以 '-' 开头）: {name}"
        )


# --- DockerSandboxRunner ---


class DockerSandboxRunner(CommandRunner):
    """Docker 容器隔离的命令执行器。

    架构：DockerSandboxRunner 作为 CommandRunner 的装饰器，
    将容器命令请求转换为 Docker CLI 调用，委托给 host_runner 执行。

    执行流程：
    1. 验证容器命令名称（白名单）
    2. 验证并映射 cwd
    3. 生成唯一容器名
    4. 构建安全的 Docker CLI 参数
    5. 通过 host_runner 执行
    6. 映射 CommandResult 返回
    7. 异常时尝试清理容器
    """

    def __init__(
        self,
        resolver: SandboxPathResolver,
        host_runner: CommandRunner,
        container_commands: Sequence[ContainerCommand],
        config: DockerSandboxConfig,
        *,
        docker_command_name: str = "docker",
        container_name_factory: Callable[[], str] | None = None,
    ) -> None:
        # resolver 验证
        if not hasattr(resolver, "root") or not hasattr(resolver, "resolve"):
            raise TypeError(
                f"resolver 必须实现 SandboxPathResolver，收到: {type(resolver).__name__}"
            )
        self._resolver = resolver

        # host_runner 验证
        if not isinstance(host_runner, CommandRunner):
            raise TypeError(
                f"host_runner 必须实现 CommandRunner，收到: {type(host_runner).__name__}"
            )
        self._host_runner = host_runner

        # container_commands 验证
        if isinstance(container_commands, str):
            raise TypeError("container_commands 不能是 str")
        names: set[str] = set()
        for cmd in container_commands:
            if not isinstance(cmd, ContainerCommand):
                raise TypeError(
                    f"container_commands 元素必须是 ContainerCommand，"
                    f"收到: {type(cmd).__name__}"
                )
            if cmd.name in names:
                raise ValueError(f"重复的容器命令名称: {cmd.name}")
            names.add(cmd.name)

        self._container_commands = {cmd.name: cmd for cmd in container_commands}

        # config 验证（深复制）
        if not isinstance(config, DockerSandboxConfig):
            raise TypeError(
                f"config 必须是 DockerSandboxConfig，收到: {type(config).__name__}"
            )
        self._config = config

        # docker_command_name 验证
        if not isinstance(docker_command_name, str):
            raise TypeError(
                f"docker_command_name 必须是 str，收到: {type(docker_command_name).__name__}"
            )
        self._docker_command_name = docker_command_name

        # container_name_factory 验证
        if container_name_factory is None:
            self._name_factory: Callable[[], str] = (
                lambda: _CONTAINER_NAME_PREFIX + uuid.uuid4().hex
            )
        else:
            if not callable(container_name_factory):
                raise TypeError(
                    "container_name_factory 必须是 Callable[[], str]"
                )
            self._name_factory = container_name_factory

    def run(self, request: CommandRequest) -> CommandResult:
        """执行容器内命令。"""
        # 1. 查找容器命令
        container_cmd = self._container_commands.get(request.command)
        if container_cmd is None:
            raise DockerCommandNotAllowedError(request.command)

        # 2. 验证并映射 cwd
        # 提前检查 Root 路径安全性（逗号会导致 mount 参数错误）
        root_str = str(self._resolver.root)
        if "," in root_str:
            raise DockerConfigurationError(
                f"Workspace 路径包含逗号，无法生成安全的 Docker mount: {root_str}"
            )
        workspace_relative_cwd = self._resolve_cwd(request.cwd)

        # 3. 生成容器名并验证
        container_name = self._name_factory()
        try:
            _validate_container_name(container_name)
        except ValueError as e:
            raise DockerConfigurationError(f"container_name_factory 返回非法名称: {e}") from e

        # 4. 构建 Docker CLI 参数
        try:
            docker_args = self._build_docker_args(
                container_cmd, request, container_name, workspace_relative_cwd
            )
        except ValueError as e:
            raise DockerConfigurationError(str(e)) from e

        # 5. 通过 host_runner 执行
        host_request = CommandRequest(
            command=self._docker_command_name,
            args=tuple(docker_args),
            cwd=None,
            timeout_seconds=request.timeout_seconds,
        )

        container_exists = True  # 容器可能在 docker run 失败前已创建

        try:
            host_result = self._host_runner.run(host_request)
        except Exception as original_error:
            # 6. 异常时尝试清理
            self._cleanup(container_name)
            raise original_error

        # 7. 映射结果：隐藏 Docker 实现细节
        return CommandResult(
            command=request.command,
            args=request.args,
            cwd=workspace_relative_cwd,
            exit_code=host_result.exit_code,
            stdout=host_result.stdout,
            stderr=host_result.stderr,
            duration_seconds=host_result.duration_seconds,
        )

    def _resolve_cwd(self, cwd: str | Path | None) -> Path:
        """验证并映射 cwd。返回相对于 workspace 的 Path。"""
        if cwd is None:
            return Path(".")

        resolved = self._resolver.resolve(cwd, must_exist=True)
        if not resolved.absolute_path.is_dir():
            from mini_deerflow.sandbox_files import SandboxNotADirectoryError
            raise SandboxNotADirectoryError(
                f"cwd 不是目录: '{resolved.requested}'"
            )

        return resolved.relative_path

    def _build_docker_args(
        self,
        container_cmd: ContainerCommand,
        request: CommandRequest,
        container_name: str,
        workspace_relative_cwd: Path,
    ) -> list[str]:
        """构建安全的 Docker CLI 参数列表。"""
        root = str(self._resolver.root)
        cfg = self._config

        # workspace POSIX 路径
        if workspace_relative_cwd == Path("."):
            container_cwd = cfg.container_workspace
        else:
            container_cwd = str(
                PurePosixPath(cfg.container_workspace) / workspace_relative_cwd.as_posix()
            )

        # build docker args
        args: list[str] = [
            "run",
            "--rm",
            "--name", container_name,
            "--network", "none",
            "--read-only",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--pids-limit", str(cfg.pids_limit),
            "--memory", cfg.memory_limit,
            "--cpus", str(cfg.cpu_limit),
            "--tmpfs", f"/tmp:rw,nosuid,nodev,size={cfg.tmpfs_size}",
            "--user", cfg.user,
            "--workdir", container_cwd,
            "--env", "PYTHONUTF8=1",
            "--env", "PYTHONIOENCODING=utf-8",
            "--pull", "never",
        ]

        # workspace mount
        mount_access = "" if cfg.workspace_writable else ",readonly"
        args.extend([
            "--mount",
            f"type=bind,source={root},target={cfg.container_workspace}{mount_access}",
        ])

        # image
        args.append(cfg.image)

        # container command prefix + user args
        # 注意：用户 args 必须放在 image 之后，确保它们是 ENTRYPOINT/CMD 的参数，
        # 而不是 docker run 自己的参数
        args.extend(container_cmd.argv_prefix)
        args.extend(request.args)

        return args

    def _cleanup(self, container_name: str) -> None:
        """清理容器：执行 docker rm -f <name>。"""
        cleanup_request = CommandRequest(
            command=self._docker_command_name,
            args=("rm", "-f", container_name),
            cwd=None,
            timeout_seconds=5.0,
        )
        try:
            self._host_runner.run(cleanup_request)
        except Exception as cleanup_error:
            # 清理失败作为 note 附加，不覆盖原始异常
            pass
