"""沙箱工具适配器（16E 阶段）。

本模块将 SandboxFileSystem 和 CommandRunner 包装成 Tool，
注册到 ToolRegistry 供 Agent 使用。

安全边界说明
------------
SandboxToolSet 是工具层包装器，不是安全边界。
- File Tool 使用注入的 SandboxFileSystem（路径安全由 16A 保证）
- Command Tool 使用注入的 CommandRunner
- 本模块不创建 Docker Runner，不读取环境变量
- 生产中 sandbox_run_command 必须注入 DockerSandboxRunner，
  LocalCommandRunner 仅用于执行 Docker CLI 本身
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Callable, Literal, TypedDict

from mini_deerflow.sandbox_command import (
    CommandNotAllowedError,
    CommandOutputLimitError,
    CommandRequest,
    CommandResult,
    CommandRunner,
    CommandTimeoutError,
)
from mini_deerflow.sandbox_docker import (
    DockerCommandNotAllowedError,
    DockerSandboxError,
)
from mini_deerflow.sandbox_files import (
    SandboxDirectoryTooLargeError,
    SandboxFileAlreadyExistsError,
    SandboxFileError,
    SandboxFileOperations,
    SandboxFileTooLargeError,
    SandboxNotADirectoryError,
    SandboxNotAFileError,
    SandboxWriteResult,
)
from mini_deerflow.sandbox_path import SandboxPathError
from mini_deerflow.tools import Tool, ToolRegistry


# --- 异常体系 ---


class SandboxToolArgumentError(ValueError):
    """工具参数校验失败时抛出。"""

    def __init__(self, tool_name: str, field: str, reason: str) -> None:
        self.tool_name = tool_name
        self.field = field
        self.reason = reason
        super().__init__(f"[{tool_name}] 参数 '{field}': {reason}")


# --- 预期可转换的沙箱错误 ---


_EXPECTED_SANDBOX_ERRORS: tuple[type[Exception], ...] = (
    SandboxPathError,
    SandboxFileError,
    CommandTimeoutError,
    CommandOutputLimitError,
    CommandNotAllowedError,
    DockerCommandNotAllowedError,
    SandboxToolArgumentError,
)


# --- JSON 结果工厂 ---


def _ok(**kwargs: Any) -> str:
    """构建成功 JSON 响应。"""
    payload = {"ok": True}
    payload.update(kwargs)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _error(tool_name: str, exc: Exception, /) -> str:
    """构建错误 JSON 响应（预期沙箱错误）。"""
    return json.dumps(
        {
            "ok": False,
            "error": {
                "type": type(exc).__name__,
                "message": str(exc),
            },
        },
        ensure_ascii=False,
        sort_keys=True,
    )


# --- 参数校验公共函数 ---


def _check_mapping(
    tool_name: str,
    arguments: object,
) -> dict[str, object]:
    """验证 arguments 是 dict/Mapping，不合法则抛 SandboxToolArgumentError。"""
    if not isinstance(arguments, Mapping):
        raise SandboxToolArgumentError(
            tool_name,
            "arguments",
            f"必须是 dict，收到: {type(arguments).__name__}",
        )
    # 深复制防止意外修改
    return dict(arguments)


def _require_field(
    tool_name: str,
    args: dict[str, object],
    field: str,
    *,
    allowed: tuple[type, ...] | None = None,
) -> object:
    """提取必填字段，缺失或类型错误时抛 SandboxToolArgumentError。"""
    if field not in args:
        raise SandboxToolArgumentError(tool_name, field, "缺少必填字段")

    value = args[field]
    # 拒绝 bool 当作其他类型
    if allowed is not None:
        if isinstance(value, bool) and bool not in allowed:
            raise SandboxToolArgumentError(
                tool_name,
                field,
                f"bool 类型不合法（必须为: {', '.join(t.__name__ for t in allowed)}）",
            )
        if not isinstance(value, allowed):
            # bool 已在上方处理，此处使用 allowed 不含 bool
            allowed_clean = tuple(t for t in allowed if t is not bool)
            raise SandboxToolArgumentError(
                tool_name,
                field,
                f"必须是: {', '.join(t.__name__ for t in allowed_clean)}，"
                f"收到: {type(value).__name__}",
            )
    return value


def _reject_unknown_fields(
    tool_name: str,
    args: dict[str, object],
    allowed_fields: set[str],
) -> None:
    """拒绝未知字段（静默忽略是危险行为）。"""
    for key in args:
        if key not in allowed_fields:
            raise SandboxToolArgumentError(
                tool_name,
                key,
                f"不允许的字段（允许: {', '.join(sorted(allowed_fields))}）",
            )


# --- Tool 参数描述（用于 Tool.parameters） ---


_TOOL_PARAM_COMMON = {"additionalProperties": False}


# --- Handler 实现 ---


class _ReadFileHandler:
    __slots__ = ("_fs",)

    def __init__(self, fs: SandboxFileOperations) -> None:
        self._fs = fs

    def __call__(self, arguments: dict[str, object]) -> str:
        tool_name = "sandbox_read_file"
        try:
            args = _check_mapping(tool_name, arguments)
            _reject_unknown_fields(tool_name, args, {"path"})

            path = _require_field(tool_name, args, "path", allowed=(str,))

            # 业务逻辑
            content = self._fs.read_text(path)

            return _ok(
                path=str(path),
                content=content,
                bytes=len(content.encode("utf-8")),
            )

        except _EXPECTED_SANDBOX_ERRORS as e:
            return _error(tool_name, e)
        except Exception as e:
            raise  # 意外错误不吞掉


class _WriteFileHandler:
    __slots__ = ("_fs",)

    def __init__(self, fs: SandboxFileOperations) -> None:
        self._fs = fs

    def __call__(self, arguments: dict[str, object]) -> str:
        tool_name = "sandbox_write_file"
        try:
            args = _check_mapping(tool_name, arguments)
            _reject_unknown_fields(tool_name, args, {"path", "content", "overwrite"})

            path = _require_field(tool_name, args, "path", allowed=(str,))
            content = _require_field(tool_name, args, "content", allowed=(str,))

            # overwrite 默认 False，必须是真正 bool
            if "overwrite" in args:
                overwrite = _require_field(
                    tool_name, args, "overwrite", allowed=(bool,)
                )
            else:
                overwrite = False

            # 业务逻辑
            result: SandboxWriteResult = self._fs.write_text(
                path, content, overwrite=overwrite
            )

            return _ok(
                path=str(result.relative_path),
                bytes_written=result.bytes_written,
                created=result.created,
            )

        except _EXPECTED_SANDBOX_ERRORS as e:
            return _error(tool_name, e)
        except Exception as e:
            raise


class _ListDirectoryHandler:
    __slots__ = ("_fs",)

    def __init__(self, fs: SandboxFileOperations) -> None:
        self._fs = fs

    def __call__(self, arguments: dict[str, object]) -> str:
        tool_name = "sandbox_list_directory"
        try:
            args = _check_mapping(tool_name, arguments)
            _reject_unknown_fields(tool_name, args, {"path"})

            # path 可省略，缺省表示 Workspace Root
            if "path" in args:
                path: str | Path | None = _require_field(
                    tool_name, args, "path", allowed=(str,)
                )
            else:
                path = None

            # 业务逻辑
            entries = self._fs.list_directory(path)

            return _ok(
                path="." if path is None else str(path),
                entries=[
                    {
                        "name": e.name,
                        "path": str(e.relative_path),
                        "kind": e.kind,
                        "size_bytes": e.size_bytes,
                    }
                    for e in entries
                ],
            )

        except _EXPECTED_SANDBOX_ERRORS as e:
            return _error(tool_name, e)
        except Exception as e:
            raise


class _MakeDirectoryHandler:
    __slots__ = ("_fs",)

    def __init__(self, fs: SandboxFileOperations) -> None:
        self._fs = fs

    def __call__(self, arguments: dict[str, object]) -> str:
        tool_name = "sandbox_make_directory"
        try:
            args = _check_mapping(tool_name, arguments)
            _reject_unknown_fields(
                tool_name, args, {"path", "parents", "exist_ok"}
            )

            path = _require_field(tool_name, args, "path", allowed=(str,))

            # 布尔参数默认值
            if "parents" in args:
                parents: bool = _require_field(
                    tool_name, args, "parents", allowed=(bool,)
                )
            else:
                parents = False

            if "exist_ok" in args:
                exist_ok: bool = _require_field(
                    tool_name, args, "exist_ok", allowed=(bool,)
                )
            else:
                exist_ok = False

            # 业务逻辑
            resolved = self._fs.make_directory(path, parents=parents, exist_ok=exist_ok)

            return _ok(path=str(resolved.relative_path))

        except _EXPECTED_SANDBOX_ERRORS as e:
            return _error(tool_name, e)
        except Exception as e:
            raise


class _RunCommandHandler:
    __slots__ = ("_runner",)

    def __init__(self, runner: CommandRunner) -> None:
        self._runner = runner

    def __call__(self, arguments: dict[str, object]) -> str:
        tool_name = "sandbox_run_command"
        try:
            args = _check_mapping(tool_name, arguments)
            _reject_unknown_fields(
                tool_name, args, {"command", "args", "cwd", "timeout_seconds"}
            )

            command = _require_field(tool_name, args, "command", allowed=(str,))
            if not command:
                raise SandboxToolArgumentError(tool_name, "command", "不能为空")

            # args: list[str]，默认 []
            if "args" in args:
                raw_args = _require_field(tool_name, args, "args", allowed=(list,))
                if not isinstance(raw_args, list):
                    raise SandboxToolArgumentError(
                        tool_name, "args", f"必须是 list，收到: {type(raw_args).__name__}"
                    )
                for i, elem in enumerate(raw_args):
                    if not isinstance(elem, str):
                        raise SandboxToolArgumentError(
                            tool_name,
                            "args",
                            f"元素必须是 str，{i} 收到: {type(elem).__name__}",
                        )
                args_tuple: tuple[str, ...] = tuple(raw_args)
            else:
                args_tuple = ()

            # cwd: str | None，默认 None
            if "cwd" in args:
                cwd_val: str | None = _require_field(
                    tool_name, args, "cwd", allowed=(str, type(None))
                )
            else:
                cwd_val = None

            # timeout_seconds: int | float > 0，默认 None
            if "timeout_seconds" in args:
                timeout_raw = args["timeout_seconds"]
                if isinstance(timeout_raw, bool):
                    raise SandboxToolArgumentError(
                        tool_name, "timeout_seconds", "bool 类型不合法"
                    )
                if not isinstance(timeout_raw, (int, float)):
                    raise SandboxToolArgumentError(
                        tool_name,
                        "timeout_seconds",
                        f"必须是数字，收到: {type(timeout_raw).__name__}",
                    )
                if timeout_raw <= 0:
                    raise SandboxToolArgumentError(
                        tool_name,
                        "timeout_seconds",
                        f"必须大于 0，收到: {timeout_raw}",
                    )
                timeout_seconds: float | None = float(timeout_raw)
            else:
                timeout_seconds = None

            # 业务逻辑
            request = CommandRequest(
                command=command,
                args=args_tuple,
                cwd=cwd_val,
                timeout_seconds=timeout_seconds,
            )
            result: CommandResult = self._runner.run(request)

            return _ok(
                command=result.command,
                args=list(result.args),
                cwd=str(result.cwd),
                exit_code=result.exit_code,
                stdout=result.stdout,
                stderr=result.stderr,
                duration_seconds=result.duration_seconds,
            )

        except _EXPECTED_SANDBOX_ERRORS as e:
            return _error(tool_name, e)
        except Exception as e:
            raise


# --- SandboxToolSet ---


class SandboxToolSet:
    """沙箱工具集合协调器。

    持有 SandboxFileOperations 和 CommandRunner 实例，
    负责创建五个固定名称的 Tool 对象。

    注意：sandbox_run_command 必须注入 DockerSandboxRunner，
    LocalCommandRunner 仅用于执行 Docker CLI 本身。
    """

    def __init__(
        self,
        files: SandboxFileOperations,
        runner: CommandRunner,
    ) -> None:
        if not hasattr(files, "read_text") or not hasattr(files, "list_directory"):
            raise TypeError(
                "files 必须实现 SandboxFileOperations，"
                f"收到: {type(files).__name__}"
            )
        if not isinstance(runner, CommandRunner):
            raise TypeError(
                f"runner 必须实现 CommandRunner，收到: {type(runner).__name__}"
            )
        self._files = files
        self._runner = runner

    def create_tools(self) -> list[Tool]:
        """创建五个沙箱工具（每次返回新 list）。"""
        return [
            Tool(
                name="sandbox_read_file",
                description=(
                    "读取 Sandbox Workspace 内的 UTF-8 文本文件。"
                    "path 必须是相对路径，不允许绝对路径。"
                    "返回文件内容和字节数。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "相对于 Workspace Root 的文件路径",
                        }
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
                handler=_ReadFileHandler(self._files),
            ),
            Tool(
                name="sandbox_write_file",
                description=(
                    "在 Sandbox Workspace 内原子写入 UTF-8 文本。"
                    "默认 overwrite=False，不覆盖已有文件。"
                    "返回写入字节数和创建标志。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "相对于 Workspace Root 的文件路径",
                        },
                        "content": {
                            "type": "string",
                            "description": "要写入的文本内容",
                        },
                        "overwrite": {
                            "type": "boolean",
                            "description": "是否覆盖已有文件，默认 false",
                        },
                    },
                    "required": ["path", "content"],
                    "additionalProperties": False,
                },
                handler=_WriteFileHandler(self._files),
            ),
            Tool(
                name="sandbox_list_directory",
                description=(
                    "列出 Sandbox Workspace 内的目录条目（不递归）。"
                    "path 省略或 null 表示 Workspace Root。"
                    "返回条目名称、相对路径、类型和大小。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "相对于 Workspace Root 的目录路径，省略表示根目录",
                        }
                    },
                    "required": [],
                    "additionalProperties": False,
                },
                handler=_ListDirectoryHandler(self._files),
            ),
            Tool(
                name="sandbox_make_directory",
                description=(
                    "在 Sandbox Workspace 内创建目录。"
                    "parents=true 时创建缺失的父目录。"
                    "exist_ok=true 时已存在不报错。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "相对于 Workspace Root 的目录路径",
                        },
                        "parents": {
                            "type": "boolean",
                            "description": "创建缺失父目录，默认 false",
                        },
                        "exist_ok": {
                            "type": "boolean",
                            "description": "已存在不报错，默认 false",
                        },
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
                handler=_MakeDirectoryHandler(self._files),
            ),
            Tool(
                name="sandbox_run_command",
                description=(
                    "在隔离执行后端运行预先允许的逻辑命令。"
                    "command 是逻辑命令名，args 是参数列表，cwd 是工作目录。"
                    "timeout_seconds 限制执行时间。"
                    "不能执行任意 Shell 字符串。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "允许的命令名称",
                        },
                        "args": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "命令行参数列表",
                        },
                        "cwd": {
                            "type": "string",
                            "description": "相对于 Workspace Root 的工作目录，null 表示根目录",
                        },
                        "timeout_seconds": {
                            "type": "number",
                            "description": "超时秒数（正数）",
                        },
                    },
                    "required": ["command"],
                    "additionalProperties": False,
                },
                handler=_RunCommandHandler(self._runner),
            ),
        ]


# --- 批量注册 ---


def register_sandbox_tools(
    registry: ToolRegistry,
    tool_set: SandboxToolSet,
) -> None:
    """将 SandboxToolSet 中的全部工具原子注册到 registry。

    全部验证通过后才注册，任何失败时 registry 保持不变。
    """
    tools = tool_set.create_tools()

    # 预先检查：批次内名称重复
    seen_names: dict[str, Tool] = {}
    for tool in tools:
        if tool.name in seen_names:
            raise ValueError(
                f"工具名重复（批次内）: {tool.name}"
            )
        seen_names[tool.name] = tool

    # 预先检查：与已有工具冲突
    for tool in tools:
        try:
            registry.get(tool.name)
            raise ValueError(
                f"工具已存在: {tool.name}"
            )
        except Exception:
            pass  # 预期 ToolNotFoundError，其他异常会继续抛出

    # 全部通过后原子提交
    registry.register_many(tools)
