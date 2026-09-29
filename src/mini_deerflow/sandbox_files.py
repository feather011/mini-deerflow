"""受控沙箱文件系统（16B 阶段）。

本模块在 16A 路径边界之上提供受控的文件操作：读文本、写文本、
列目录、建目录。所有不可信路径必须经过 SandboxPathResolver。

安全边界说明
------------
SandboxFileSystem 是**应用层文件防护**，不是操作系统安全边界。
它不能彻底阻止：

- 校验后路径被恶意替换（TOCTOU 竞态）
- 同权限进程直接访问 Root 外的文件
- 所有 Windows Junction / Reparse Point 竞态
- 恶意本地进程修改符号链接
- 磁盘配额耗尽
- 进程级资源攻击

所有路径必须经过 Resolver；不可信命令仍必须进入后续 Docker Sandbox。
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from mini_deerflow.sandbox_path import (
    ResolvedSandboxPath,
    SandboxPathNotFoundError,
    SandboxPathResolver,
)

# 临时文件名前缀：使用下划线开头，避免与用户文件冲突，
# 且不含 Windows 保留设备名与冒号（Resolver 会拒绝这些字符）
_TEMP_PREFIX = ".mdf_tmp_"
_TEMP_SUFFIX = ".tmp"


# --- 异常体系 ---


class SandboxFileError(Exception):
    """沙箱文件操作错误的基类。"""


class SandboxNotAFileError(SandboxFileError):
    """目标不是普通文件时抛出。"""


class SandboxNotADirectoryError(SandboxFileError):
    """目标不是目录时抛出。"""


class SandboxFileTooLargeError(SandboxFileError):
    """文件超出大小限制时抛出。"""


class SandboxFileAlreadyExistsError(SandboxFileError):
    """目标已存在且不允许覆盖时抛出。"""


class SandboxFileEncodingError(SandboxFileError):
    """内容不是合法 UTF-8 时抛出。"""


class SandboxDirectoryTooLargeError(SandboxFileError):
    """目录条目数超出限制时抛出。"""


# --- 不可变结果对象 ---


@dataclass(frozen=True)
class SandboxDirectoryEntry:
    """目录中的一个条目。

    只暴露名称与相对路径，不暴露绝对路径，也不暴露符号链接目标。
    """

    name: str
    relative_path: Path
    kind: Literal["file", "directory", "symlink", "other"]
    size_bytes: int | None

    def __post_init__(self) -> None:
        """验证内部不变量。"""
        if not isinstance(self.name, str) or not self.name:
            raise TypeError("name 必须是非空字符串")

        if not isinstance(self.relative_path, Path):
            raise TypeError(
                f"relative_path 必须是 Path，收到: {type(self.relative_path).__name__}"
            )
        if self.relative_path.is_absolute():
            raise ValueError(
                f"relative_path 必须是相对路径，收到: {self.relative_path}"
            )

        if self.kind not in ("file", "directory", "symlink", "other"):
            raise ValueError(f"未知 kind: {self.kind!r}")

        if self.size_bytes is not None:
            if isinstance(self.size_bytes, bool) or not isinstance(self.size_bytes, int):
                raise TypeError("size_bytes 必须是 int 或 None")
            if self.size_bytes < 0:
                raise ValueError("size_bytes 不能是负数")
            if self.kind != "file":
                raise ValueError("只有 file 类型的条目才能提供 size_bytes")


@dataclass(frozen=True)
class SandboxWriteResult:
    """写入操作的结果。"""

    relative_path: Path
    bytes_written: int
    created: bool

    def __post_init__(self) -> None:
        """验证内部不变量。"""
        if not isinstance(self.relative_path, Path):
            raise TypeError(
                f"relative_path 必须是 Path，收到: {type(self.relative_path).__name__}"
            )
        if self.relative_path.is_absolute():
            raise ValueError(
                f"relative_path 必须是相对路径，收到: {self.relative_path}"
            )

        if isinstance(self.bytes_written, bool) or not isinstance(self.bytes_written, int):
            raise TypeError("bytes_written 必须是 int")
        if self.bytes_written < 0:
            raise ValueError("bytes_written 不能是负数")

        if not isinstance(self.created, bool):
            raise TypeError("created 必须是 bool")


# --- Protocol ---


@runtime_checkable
class SandboxFileOperations(Protocol):
    """沙箱文件操作接口。

    Tool 层依赖此协议，而不是具体的 SandboxFileSystem 实现。
    """

    def read_text(self, path: str | Path) -> str:
        """读取沙箱内文件的 UTF-8 文本内容。"""
        ...

    def write_text(
        self,
        path: str | Path,
        content: str,
        *,
        overwrite: bool = False,
    ) -> SandboxWriteResult:
        """原子写入沙箱内文件。"""
        ...

    def list_directory(
        self,
        path: str | Path | None = None,
    ) -> list[SandboxDirectoryEntry]:
        """列出目录条目，不递归、不跟随符号链接。"""
        ...

    def make_directory(
        self,
        path: str | Path,
        *,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> ResolvedSandboxPath:
        """在沙箱内创建目录。"""
        ...


# --- 内部工具 ---


def _validate_limit(name: str, value: object) -> int:
    """验证资源限制参数：必须是正整数，bool 与非 int 抛 TypeError。"""
    if isinstance(value, bool):
        raise TypeError(f"{name} 不能是 bool 类型")
    if not isinstance(value, int):
        raise TypeError(
            f"{name} 必须是 int，收到: {type(value).__name__}"
        )
    if value <= 0:
        raise ValueError(f"{name} 必须大于 0，收到: {value}")
    return value


# --- SandboxFileSystem ---


class SandboxFileSystem:
    """受控的沙箱文件系统实现。"""

    def __init__(
        self,
        resolver: SandboxPathResolver,
        *,
        max_read_bytes: int = 1_048_576,
        max_write_bytes: int = 1_048_576,
        max_list_entries: int = 1_000,
    ) -> None:
        """初始化文件系统。

        Args:
            resolver: 16A 提供的路径解析器。
            max_read_bytes: 单次读取的最大字节数。
            max_write_bytes: 单次写入的最大 UTF-8 字节数。
            max_list_entries: 单次列目录的最大条目数。

        构造函数不读取目录内容，也不创建任何文件。
        """
        if not hasattr(resolver, "root") or not hasattr(resolver, "resolve"):
            raise TypeError(
                f"resolver 必须实现 SandboxPathResolver，收到: {type(resolver).__name__}"
            )

        self._resolver = resolver
        self._max_read_bytes = _validate_limit("max_read_bytes", max_read_bytes)
        self._max_write_bytes = _validate_limit("max_write_bytes", max_write_bytes)
        self._max_list_entries = _validate_limit("max_list_entries", max_list_entries)

    @property
    def root(self) -> Path:
        """沙箱根目录的绝对路径。"""
        return self._resolver.root

    @property
    def max_read_bytes(self) -> int:
        """单次读取的字节上限。"""
        return self._max_read_bytes

    @property
    def max_write_bytes(self) -> int:
        """单次写入的字节上限。"""
        return self._max_write_bytes

    @property
    def max_list_entries(self) -> int:
        """单次列目录的条目数上限。"""
        return self._max_list_entries

    # --- read_text ---

    def read_text(self, path: str | Path) -> str:
        """读取沙箱内文件的 UTF-8 文本。

        执行顺序：
        1. Resolver 解析路径（must_exist=True）
        2. 验证目标是普通文件
        3. stat 大小提前拒绝明显超限
        4. 二进制打开，最多读取 max_read_bytes + 1 字节
        5. 超限抛 SandboxFileTooLargeError
        6. UTF-8 解码，失败抛 SandboxFileEncodingError

        Args:
            path: 沙箱内的相对路径。

        Returns:
            文件的 UTF-8 文本内容。

        Raises:
            SandboxPathViolationError: 路径违反沙箱边界。
            SandboxPathNotFoundError: 目标不存在。
            SandboxNotAFileError: 目标不是普通文件。
            SandboxFileTooLargeError: 文件超出读取上限。
            SandboxFileEncodingError: 内容不是合法 UTF-8。
        """
        resolved = self._resolver.resolve(path, must_exist=True)
        target = resolved.absolute_path
        label = resolved.requested

        # 1. 验证是普通文件
        if target.is_dir():
            raise SandboxNotADirectoryError(f"路径不是文件: '{label}'")
        if not target.is_file():
            raise SandboxNotAFileError(f"路径不是普通文件: '{label}'")

        # 2. stat 提前拒绝明显超限的文件
        try:
            size = target.stat().st_size
        except OSError as e:
            raise SandboxFileError(f"无法获取文件信息: '{label}': {e}") from e

        if size > self._max_read_bytes:
            raise SandboxFileTooLargeError(
                f"文件过大: '{label}'（{size} 字节，上限 {self._max_read_bytes} 字节）"
            )

        # 3. 以二进制模式打开，最多读取上限 + 1 字节
        #    不能只依赖 stat：文件可能在 stat 之后增长
        try:
            with open(target, "rb") as handle:
                data = handle.read(self._max_read_bytes + 1)
        except OSError as e:
            raise SandboxFileError(f"读取文件失败: '{label}': {e}") from e

        if len(data) > self._max_read_bytes:
            raise SandboxFileTooLargeError(
                f"文件过大: '{label}'（超过 {self._max_read_bytes} 字节）"
            )

        # 4. UTF-8 解码
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as e:
            raise SandboxFileEncodingError(
                f"文件不是合法 UTF-8 文本: '{label}': {e}"
            ) from e

    # --- write_text ---

    def write_text(
        self,
        path: str | Path,
        content: str,
        *,
        overwrite: bool = False,
    ) -> SandboxWriteResult:
        """原子写入沙箱内文件。

        先检查内容大小（超限时完全不创建文件），再通过同目录临时文件 +
        os.replace 保证原子替换。

        Args:
            path: 沙箱内的相对路径。
            content: 要写入的文本。
            overwrite: 是否允许覆盖已存在的文件。

        Returns:
            SandboxWriteResult。

        Raises:
            SandboxPathViolationError: 路径违反沙箱边界。
            SandboxNotAFileError: 目标是目录。
            SandboxFileAlreadyExistsError: 目标已存在且 overwrite=False。
            SandboxFileTooLargeError: 内容超出写入上限。
            SandboxNotADirectoryError: 父路径不是目录。
            SandboxFileError: 其他 IO 错误。
        """
        # 1. 参数验证
        if not isinstance(content, str):
            raise TypeError(
                f"content 必须是 str，收到: {type(content).__name__}"
            )
        if not isinstance(overwrite, bool):
            raise TypeError(
                f"overwrite 必须是 bool，收到: {type(overwrite).__name__}"
            )

        # 2. UTF-8 编码并检查大小
        #    必须在创建任何文件之前完成
        try:
            data = content.encode("utf-8")
        except UnicodeEncodeError as e:
            raise SandboxFileEncodingError(f"内容不是合法的 UTF-8 文本: {e}") from e

        if len(data) > self._max_write_bytes:
            raise SandboxFileTooLargeError(
                f"内容过大（{len(data)} 字节，上限 {self._max_write_bytes} 字节）"
            )

        # 3. 解析路径
        resolved = self._resolver.resolve(path, must_exist=False)
        target = resolved.absolute_path
        label = resolved.requested

        # 4. 验证父目录
        parent = target.parent
        if not parent.exists():
            raise SandboxNotADirectoryError(f"父目录不存在: '{label}'")
        if not parent.is_dir():
            raise SandboxNotADirectoryError(f"父路径不是目录: '{label}'")

        # 5. 再次确认父目录仍在 Root 内
        if not parent.is_relative_to(self.root):
            raise SandboxNotADirectoryError(f"父目录超出沙箱边界: '{label}'")

        # 6. 目标存在性检查
        created = True
        if target.exists():
            if target.is_dir():
                raise SandboxNotAFileError(f"目标是目录，不能作为文件写入: '{label}'")
            if not overwrite:
                raise SandboxFileAlreadyExistsError(f"文件已存在: '{label}'")
            created = False

        # 7. 同目录临时文件 + os.replace 原子替换
        temp_path: Path | None = None
        try:
            fd, temp_name = tempfile.mkstemp(
                prefix=_TEMP_PREFIX, suffix=_TEMP_SUFFIX, dir=parent
            )
            temp_path = Path(temp_name)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                try:
                    os.fsync(handle.fileno())
                except OSError:
                    # 某些文件系统不支持 fsync，不影响原子替换语义
                    pass
            os.replace(temp_path, target)
            temp_path = None
        except OSError as e:
            raise SandboxFileError(f"写入文件失败: '{label}': {e}") from e
        finally:
            # 失败时清理残留临时文件
            if temp_path is not None:
                try:
                    temp_path.unlink()
                except OSError:
                    pass

        return SandboxWriteResult(
            relative_path=resolved.relative_path,
            bytes_written=len(data),
            created=created,
        )

    # --- list_directory ---

    def list_directory(
        self,
        path: str | Path | None = None,
    ) -> list[SandboxDirectoryEntry]:
        """列出目录条目。

        不递归、不跟随符号链接、不返回绝对路径或链接目标。
        超过 max_list_entries 时抛异常，不静默截断。

        Args:
            path: 目录的相对路径；None 表示沙箱根目录。

        Returns:
            SandboxDirectoryEntry 列表，按 (name.casefold(), name) 排序。

        Raises:
            SandboxPathViolationError: 路径违反沙箱边界。
            SandboxPathNotFoundError: 目录不存在。
            SandboxNotADirectoryError: 目标不是目录。
            SandboxDirectoryTooLargeError: 条目数超限。
        """
        if path is None:
            # 根目录：直接使用 Resolver 的 root，不经过相对路径校验
            base = self.root
            base_relative = Path()
            label = "."
        else:
            resolved = self._resolver.resolve(path, must_exist=True)
            base = resolved.absolute_path
            base_relative = resolved.relative_path
            label = resolved.requested

        if not base.is_dir():
            raise SandboxNotADirectoryError(f"路径不是目录: '{label}'")

        entries: list[SandboxDirectoryEntry] = []
        try:
            with os.scandir(base) as scanner:
                for dir_entry in scanner:
                    kind = _classify_entry(dir_entry)
                    if kind == "file":
                        try:
                            size = dir_entry.stat(follow_symlinks=False).st_size
                        except OSError:
                            size = None
                    else:
                        size = None

                    entries.append(
                        SandboxDirectoryEntry(
                            name=dir_entry.name,
                            relative_path=base_relative / dir_entry.name,
                            kind=kind,
                            size_bytes=size,
                        )
                    )

                    if len(entries) > self._max_list_entries:
                        raise SandboxDirectoryTooLargeError(
                            f"目录条目过多: '{label}'"
                            f"（上限 {self._max_list_entries} 条）"
                        )
        except OSError as e:
            raise SandboxFileError(f"读取目录失败: '{label}': {e}") from e

        # 稳定排序：先 casefold 再原始名称，避免大小写混排
        entries.sort(key=lambda e: (e.name.casefold(), e.name))
        return entries

    # --- make_directory ---

    def make_directory(
        self,
        path: str | Path,
        *,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> ResolvedSandboxPath:
        """在沙箱内创建目录。

        默认只创建一层（parents=False），父目录不存在时失败。
        不允许创建 Sandbox Root 本身。

        Args:
            path: 目录的相对路径。
            parents: 是否创建缺失的父目录。
            exist_ok: 目录已存在时是否视为成功。

        Returns:
            创建后通过 Resolver 重新验证得到的 ResolvedSandboxPath。

        Raises:
            SandboxPathViolationError: 路径违反沙箱边界。
            SandboxNotADirectoryError: 目标已存在但是普通文件。
            SandboxFileError: 其他 IO 错误。
        """
        if not isinstance(parents, bool):
            raise TypeError(
                f"parents 必须是 bool，收到: {type(parents).__name__}"
            )
        if not isinstance(exist_ok, bool):
            raise TypeError(
                f"exist_ok 必须是 bool，收到: {type(exist_ok).__name__}"
            )

        resolved = self._resolver.resolve(path, must_exist=False)
        target = resolved.absolute_path
        label = resolved.requested

        # 不允许创建 Root 本身
        if resolved.relative_path == Path():
            raise SandboxNotADirectoryError(
                "不允许创建沙箱根目录本身"
            )

        if target.exists():
            if not target.is_dir():
                raise SandboxNotADirectoryError(f"目标已存在且不是目录: '{label}'")
            if not exist_ok:
                raise SandboxFileError(f"目录已存在: '{label}'")
        else:
            parent = target.parent
            if not parent.exists():
                if not parents:
                    raise SandboxNotADirectoryError(f"父目录不存在: '{label}'")
            elif not parent.is_dir():
                raise SandboxNotADirectoryError(f"父路径不是目录: '{label}'")

            try:
                if parents:
                    target.mkdir(parents=True, exist_ok=exist_ok)
                else:
                    target.mkdir()
            except FileExistsError as e:
                raise SandboxFileError(f"目录已存在: '{label}': {e}") from e
            except OSError as e:
                raise SandboxFileError(f"创建目录失败: '{label}': {e}") from e

        # 创建后再次通过 Resolver 验证
        return self._resolver.resolve(path, must_exist=True)


# --- 内部辅助 ---


def _classify_entry(dir_entry: os.DirEntry[str]) -> Literal["file", "directory", "symlink", "other"]:
    """使用 lstat 语义分类目录条目，不跟随符号链接。"""
    if dir_entry.is_symlink():
        return "symlink"
    if dir_entry.is_file(follow_symlinks=False):
        return "file"
    if dir_entry.is_dir(follow_symlinks=False):
        return "directory"
    return "other"
