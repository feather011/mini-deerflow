"""受控本地命令执行器（16C 阶段）。

本模块提供结构化的白名单命令执行能力，所有路径均经过 SandboxPathResolver。

警告：LocalCommandRunner 是**应用层进程包装**，不是操作系统安全边界。
它不能阻止已允许的程序：读取 Root 外文件、访问网络、消耗资源、创建子进程。
命令白名单不等于安全隔离。不可信代码必须使用后续 DockerSandboxRunner。
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol, Sequence, runtime_checkable

from mini_deerflow.sandbox_files import SandboxNotADirectoryError
from mini_deerflow.sandbox_path import (
    ResolvedSandboxPath,
    SandboxPathResolver,
    SandboxPathViolationError,
)


# --- 异常体系 ---


class SandboxCommandError(Exception):
    """命令执行错误的基类。"""


class CommandNotAllowedError(SandboxCommandError):
    """命令名称不在白名单中时抛出。"""

    def __init__(self, command: str) -> None:
        self.command = command
        super().__init__(f"未知命令: '{command}'")


class CommandTimeoutError(SandboxCommandError):
    """命令执行超时时抛出。"""

    def __init__(
        self,
        command: str,
        timeout_seconds: float,
        stdout: str,
        stderr: str,
    ) -> None:
        self.command = command
        self.timeout_seconds = timeout_seconds
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(
            f"命令 '{command}' 执行超时（{timeout_seconds} 秒）"
        )


class CommandOutputLimitError(SandboxCommandError):
    """输出超过限制时抛出。"""

    def __init__(
        self,
        command: str,
        limit_bytes: int,
        stdout: str,
        stderr: str,
    ) -> None:
        self.command = command
        self.limit_bytes = limit_bytes
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(
            f"命令 '{command}' 输出超限（{limit_bytes} 字节）"
        )


class CommandStartError(SandboxCommandError):
    """进程启动失败时抛出。"""

    def __init__(self, command: str, reason: str) -> None:
        self.command = command
        self.reason = reason
        super().__init__(f"命令 '{command}' 启动失败: {reason}")


class CommandTerminationError(SandboxCommandError):
    """进程树终止失败时抛出。"""

    def __init__(self, command: str, reason: str) -> None:
        self.command = command
        self.reason = reason
        super().__init__(f"命令 '{command}' 终止失败: {reason}")


# --- 数据模型 ---


@dataclass(frozen=True)
class AllowedCommand:
    """不可变的白名单命令定义。

    name 是逻辑名称，argv_prefix 锁定到绝对路径的可执行文件。
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
                raise TypeError(
                    f"argv_prefix[{i}] 不能是空字符串"
                )

        # argv_prefix[0] 必须是绝对路径的普通文件
        executable = Path(self.argv_prefix[0])
        if not executable.is_absolute():
            raise ValueError(
                f"argv_prefix[0] 必须是绝对路径，收到: {self.argv_prefix[0]}"
            )
        try:
            exists = executable.exists()
        except OSError as e:
            raise ValueError(
                f"argv_prefix[0] 存在性检查失败: {self.argv_prefix[0]}: {e}"
            ) from e
        if not exists:
            raise ValueError(
                f"argv_prefix[0] 不存在: {self.argv_prefix[0]}"
            )
        if not executable.is_file():
            raise ValueError(
                f"argv_prefix[0] 不是普通文件: {self.argv_prefix[0]}"
            )


@dataclass(frozen=True)
class CommandRequest:
    """不可变的命令执行请求。"""

    command: str
    args: tuple[str, ...] = ()
    cwd: str | Path | None = None
    timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.command, str):
            raise TypeError(
                f"command 必须是 str，收到: {type(self.command).__name__}"
            )
        if not self.command:
            raise TypeError("command 不能为空字符串")

        if not isinstance(self.args, tuple):
            raise TypeError(
                f"args 必须是 tuple，收到: {type(self.args).__name__}"
            )
        for i, arg in enumerate(self.args):
            if not isinstance(arg, str):
                raise TypeError(
                    f"args[{i}] 必须是 str，收到: {type(arg).__name__}"
                )
            if "\x00" in arg:
                raise TypeError(f"args[{i}] 不能包含 NUL 字符")

        if self.cwd is not None:
            if not isinstance(self.cwd, (str, Path)):
                raise TypeError(
                    f"cwd 必须是 str、Path 或 None，收到: {type(self.cwd).__name__}"
                )

        if self.timeout_seconds is not None:
            if isinstance(self.timeout_seconds, bool):
                raise TypeError("timeout_seconds 不能是 bool")
            if not isinstance(self.timeout_seconds, (int, float)):
                raise TypeError(
                    f"timeout_seconds 必须是数字，收到: {type(self.timeout_seconds).__name__}"
                )
            if self.timeout_seconds <= 0:
                raise ValueError(
                    f"timeout_seconds 必须大于 0，收到: {self.timeout_seconds}"
                )


@dataclass(frozen=True)
class CommandResult:
    """不可变的命令执行结果。"""

    command: str
    args: tuple[str, ...]
    cwd: Path
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float

    def __post_init__(self) -> None:
        if not isinstance(self.cwd, Path):
            raise TypeError(
                f"cwd 必须是 Path，收到: {type(self.cwd).__name__}"
            )
        if self.cwd.is_absolute():
            raise ValueError(f"cwd 必须是相对路径，收到: {self.cwd}")

        if isinstance(self.duration_seconds, bool) or not isinstance(
            self.duration_seconds, (int, float)
        ):
            raise TypeError("duration_seconds 必须是数字")
        if self.duration_seconds < 0:
            raise ValueError(
                f"duration_seconds 不能为负，收到: {self.duration_seconds}"
            )


# --- Protocol ---


@runtime_checkable
class CommandRunner(Protocol):
    """命令执行器协议。"""

    def run(self, request: CommandRequest) -> CommandResult:
        """执行命令并返回结果。"""
        ...


# --- 内部工具 ---


def _build_minimal_env() -> dict[str, str]:
    """构建最小环境变量，避免泄漏凭证。

    仅复制运行所需的系统变量，不复制任何 API Key 或用户凭证。
    """
    minimal: dict[str, str] = {}
    # 系统关键变量
    for key in ("SystemRoot", "WINDIR", "TEMP", "TMP"):
        val = os.environ.get(key)
        if val:
            minimal[key] = val
    # Python UTF-8 模式
    minimal["PYTHONUTF8"] = "1"
    minimal["PYTHONIOENCODING"] = "utf-8"
    # Windows-specific: 确保使用 UTF-8 控制台输出
    if sys.platform == "win32":
        minimal["PYTHONLEGACYWINDOWSSTDIO"] = "utf-8"
    return minimal


# --- LocalCommandRunner ---


class LocalCommandRunner:
    """本地命令执行器实现。

    执行流程：
    1. 查找白名单命令
    2. 验证 cwd（通过 Resolver）
    3. 构建最小环境
    4. subprocess.Popen(shell=False)
    5. 后台线程读取输出（带硬上限）
    6. 等待或超时
    7. 组装 CommandResult
    """

    def __init__(
        self,
        resolver: SandboxPathResolver,
        allowed_commands: Sequence[AllowedCommand],
        *,
        default_timeout_seconds: float = 10.0,
        max_timeout_seconds: float = 60.0,
        max_output_bytes: int = 1_048_576,
        base_environment: Mapping[str, str] | None = None,
    ) -> None:
        # resolver 验证
        if not hasattr(resolver, "root") or not hasattr(resolver, "resolve"):
            raise TypeError(
                f"resolver 必须实现 SandboxPathResolver，收到: {type(resolver).__name__}"
            )
        self._resolver = resolver

        # allowed_commands 验证
        if isinstance(allowed_commands, str):
            raise TypeError("allowed_commands 不能是 str")
        names: set[str] = set()
        for cmd in allowed_commands:
            if not isinstance(cmd, AllowedCommand):
                raise TypeError(
                    f"allowed_commands 元素必须是 AllowedCommand，"
                    f"收到: {type(cmd).__name__}"
                )
            if cmd.name in names:
                raise ValueError(f"重复的命令名称: {cmd.name}")
            names.add(cmd.name)

        self._allowed_commands = {cmd.name: cmd for cmd in allowed_commands}

        # 超时验证
        if isinstance(default_timeout_seconds, bool):
            raise TypeError("default_timeout_seconds 不能是 bool")
        if not isinstance(default_timeout_seconds, (int, float)):
            raise TypeError(
                f"default_timeout_seconds 必须是数字，"
                f"收到: {type(default_timeout_seconds).__name__}"
            )
        if default_timeout_seconds <= 0:
            raise ValueError(
                f"default_timeout_seconds 必须大于 0，"
                f"收到: {default_timeout_seconds}"
            )

        if isinstance(max_timeout_seconds, bool):
            raise TypeError("max_timeout_seconds 不能是 bool")
        if not isinstance(max_timeout_seconds, (int, float)):
            raise TypeError(
                f"max_timeout_seconds 必须是数字，"
                f"收到: {type(max_timeout_seconds).__name__}"
            )
        if max_timeout_seconds <= 0:
            raise ValueError(
                f"max_timeout_seconds 必须大于 0，收到: {max_timeout_seconds}"
            )

        if default_timeout_seconds > max_timeout_seconds:
            raise ValueError(
                f"default_timeout_seconds ({default_timeout_seconds}) "
                f"不得大于 max_timeout_seconds ({max_timeout_seconds})"
            )

        self._default_timeout = default_timeout_seconds
        self._max_timeout = max_timeout_seconds

        # 输出限制验证
        if isinstance(max_output_bytes, bool):
            raise TypeError("max_output_bytes 不能是 bool")
        if not isinstance(max_output_bytes, int):
            raise TypeError(
                f"max_output_bytes 必须是 int，收到: {type(max_output_bytes).__name__}"
            )
        if max_output_bytes <= 0:
            raise ValueError(
                f"max_output_bytes 必须大于 0，收到: {max_output_bytes}"
            )

        self._max_output = max_output_bytes

        # 环境变量验证和复制
        if base_environment is None:
            self._base_env: dict[str, str] | None = None  # 使用最小环境
        else:
            if not isinstance(base_environment, Mapping):
                raise TypeError(
                    f"base_environment 必须是 Mapping，"
                    f"收到: {type(base_environment).__name__}"
                )
            for k, v in base_environment.items():
                if not isinstance(k, str):
                    raise TypeError(f"环境变量名称必须是 str，收到: {type(k).__name__}")
                if not isinstance(v, str):
                    raise TypeError(
                        f"环境变量值必须是 str，收到: {type(v).__name__}"
                    )
                if "\x00" in k or "\x00" in v:
                    raise TypeError("环境变量不能包含 NUL 字符")
            # 深复制防止调用方修改影响 Runner
            self._base_env = dict(base_environment)

    def run(self, request: CommandRequest) -> CommandResult:
        """执行命令并返回结果。"""
        # 1. 查找命令
        allowed = self._allowed_commands.get(request.command)
        if allowed is None:
            raise CommandNotAllowedError(request.command)

        # 2. 确定超时
        timeout = request.timeout_seconds
        if timeout is None:
            timeout = self._default_timeout
        elif timeout > self._max_timeout:
            raise CommandTimeoutError(
                request.command,
                self._max_timeout,
                "",
                f"请求超时 {timeout} 秒超过上限 {self._max_timeout} 秒",
            )

        # 3. 解析 cwd
        if request.cwd is None:
            cwd_abs = self._resolver.root
            cwd_rel: Path = Path(".")
        else:
            resolved = self._resolver.resolve(request.cwd, must_exist=True)
            cwd_abs = resolved.absolute_path
            if not cwd_abs.is_dir():
                raise SandboxNotADirectoryError(
                    f"cwd 不是目录: '{resolved.requested}'"
                )
            cwd_rel = resolved.relative_path

        # 4. 构建 argv
        argv = list(allowed.argv_prefix) + list(request.args)

        # 5. 构建环境
        if self._base_env is None:
            env = _build_minimal_env()
        else:
            env = dict(self._base_env)

        # 6. 执行
        return self._execute(argv, request.command, request.args, cwd_abs, cwd_rel, timeout, env)

    def _execute(
        self,
        argv: list[str],
        command: str,
        args: tuple[str, ...],
        cwd_abs: Path,
        cwd_rel: Path,
        timeout: float,
        env: dict[str, str],
    ) -> CommandResult:
        """内部执行逻辑，包含进程管理和输出收集。"""
        # 共享状态
        stdout_buf: list[bytes] = []
        stderr_buf: list[bytes] = []
        total_bytes: int = 0
        lock = threading.Lock()
        stop_event = threading.Event()

        def read_stream(handle, buf_list: list[bytes], is_stdout: bool) -> None:
            """后台线程：从 pipe 读取数据直到 EOF 或超限。"""
            nonlocal total_bytes
            try:
                while not stop_event.is_set():
                    chunk = handle.read(4096)
                    if not chunk:
                        break
                    with lock:
                        if total_bytes < self._max_output:
                            remaining = self._max_output - total_bytes
                            buf_list.append(chunk[:remaining])
                            total_bytes += len(chunk)
                        else:
                            total_bytes += len(chunk)
                            stop_event.set()
            finally:
                try:
                    handle.close()
                except Exception:
                    pass

        start_time = time.monotonic()
        process: subprocess.Popen[bytes] | None = None

        try:
            # 7. 启动进程（shell=False）
            try:
                process = subprocess.Popen(
                    argv,
                    cwd=str(cwd_abs),
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    shell=False,
                )
            except OSError as e:
                raise CommandStartError(command, str(e)) from e

            # 8. 启动输出读取线程
            stdout_thread = threading.Thread(
                target=read_stream,
                args=(process.stdout, stdout_buf, True),
                daemon=True,
            )
            stderr_thread = threading.Thread(
                target=read_stream,
                args=(process.stderr, stderr_buf, False),
                daemon=True,
            )
            stdout_thread.start()
            stderr_thread.start()

            # 9. 等待进程结束或超时
            try:
                exit_code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                # 10. 超时：首先设置停止标志
                stop_event.set()

                # 关闭 pipe，强制解除 reader thread 的阻塞 read()
                self._close_pipes(process)

                # 终止进程树
                self._terminate_process_tree(process, command)
                exit_code = -1

                # 等待线程退出（此时 read 已不会阻塞）
                stdout_thread.join(timeout=2.0)
                stderr_thread.join(timeout=2.0)

                # 收集已读输出
                stdout_bytes = b"".join(stdout_buf)
                stderr_bytes = b"".join(stderr_buf)
                raise CommandTimeoutError(
                    command,
                    timeout,
                    stdout_bytes.decode("utf-8", errors="replace"),
                    stderr_bytes.decode("utf-8", errors="replace"),
                )

            # 11. 等待读取线程完成
            stdout_thread.join(timeout=5)
            stderr_thread.join(timeout=5)
            stop_event.set()

            # 12. 检查输出超限
            stdout_bytes = b"".join(stdout_buf)
            stderr_bytes = b"".join(stderr_buf)

            if total_bytes > self._max_output:
                raise CommandOutputLimitError(
                    command,
                    self._max_output,
                    stdout_bytes.decode("utf-8", errors="replace"),
                    stderr_bytes.decode("utf-8", errors="replace"),
                )

            duration = time.monotonic() - start_time

            return CommandResult(
                command=command,
                args=args,
                cwd=cwd_rel,
                exit_code=exit_code,
                stdout=stdout_bytes.decode("utf-8", errors="replace"),
                stderr=stderr_bytes.decode("utf-8", errors="replace"),
                duration_seconds=round(duration, 3),
            )

        finally:
            # 清理：确保 pipe 和进程被关闭
            if process is not None:
                try:
                    if process.stdout:
                        process.stdout.close()
                except Exception:
                    pass
                try:
                    if process.stderr:
                        process.stderr.close()
                except Exception:
                    pass
                try:
                    if process.stdin:
                        process.stdin.close()
                except Exception:
                    pass
                # 如果进程还没退出，再终止一次
                if process.poll() is None:
                    self._terminate_process_tree(process, command)

    def _close_pipes(self, process: subprocess.Popen[bytes]) -> None:
        """关闭进程的所有 pipe，强制解除 reader thread 的阻塞 read()。"""
        for handle in (process.stdout, process.stderr, process.stdin):
            if handle:
                try:
                    handle.close()
                except Exception:
                    pass

    def _terminate_process_tree(
        self, process: subprocess.Popen[bytes], command: str
    ) -> None:
        if process.poll() is not None:
            return

        pid = process.pid

        try:
            if sys.platform == "win32":
                self._terminate_windows(pid, command)
            else:
                self._terminate_posix(pid, command)
        except Exception:
            # 尽力而为，失败不影响主错误
            pass

        # 最终 fallback：确保进程结束
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
                process.wait(timeout=3)
            except Exception:
                pass

    def _terminate_windows(self, pid: int, command: str) -> None:
        """Windows：使用 taskkill /T /F 终止进程树。"""
        system_root = os.environ.get("SystemRoot")
        if system_root:
            taskkill = Path(system_root) / "System32" / "taskkill.exe"
            if taskkill.exists():
                try:
                    # shell=False，taskkill 自己不通过 Shell 执行危险命令
                    subprocess.run(
                        [str(taskkill), "/PID", str(pid), "/T", "/F"],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        shell=False,
                        timeout=5,
                    )
                    return
                except (OSError, subprocess.TimeoutExpired):
                    pass

        # Fallback：直接 kill
        try:
            process_ref = subprocess.Popen(
                [sys.executable, "-c",
                 f"import os, signal; os.kill({pid}, signal.SIGTERM)"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                timeout=3,
            )
            process_ref.wait()
        except Exception:
            pass

    def _terminate_posix(self, pid: int, command: str) -> None:
        """POSIX：使用 SIGKILL 终止进程组。"""
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
