"""Workspace 文件服务（19E 阶段）。

职责：
- 逻辑路径规范化与安全校验
- uploads / artifacts 根目录隔离
- 上传大小限制
- 二进制流式原子写入
- 文件列表
- 下载路径解析
- 删除上传文件
- 领域异常

不 import FastAPI，不处理 HTTP 状态码。
"""
from __future__ import annotations

import mimetypes
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from mini_deerflow.sandbox_files import (
    SandboxDirectoryTooLargeError,
    SandboxFileAlreadyExistsError,
    SandboxFileError,
    SandboxFileOperations,
    SandboxFileTooLargeError,
    SandboxNotADirectoryError,
    SandboxNotAFileError,
)
from mini_deerflow.sandbox_path import (
    ResolvedSandboxPath,
    SandboxPathNotFoundError,
    SandboxPathResolver,
    SandboxPathViolationError,
    WorkspacePathResolver,
)


# --------------------------------------------------------------------------- 领域异常


class WorkspaceServiceError(Exception):
    """领域错误基类。"""


class UploadFileTooLargeError(WorkspaceServiceError):
    """上传文件超出大小限制。"""
    def __init__(self, size: int, limit: int) -> None:
        self.size = size
        self.limit = limit
        super().__init__(f"文件大小 {size} 字节超出限制 {limit} 字节")


class FileAlreadyExistsError(WorkspaceServiceError):
    """文件已存在（禁止覆盖）。"""


class AtomicCommitError(WorkspaceServiceError):
    """原子提交失败（link 不可用或 rename 失败，且非目标已存在）。"""


class FileNotFoundError(WorkspaceServiceError):
    """文件不存在。"""


class PathViolationError(WorkspaceServiceError):
    """路径违反安全规则。"""


class NotAFileError(WorkspaceServiceError):
    """目标不是普通文件。"""


class NotADirectoryError(WorkspaceServiceError):
    """目标不是目录。"""


class DirectoryNotEmptyError(WorkspaceServiceError):
    """目录非空。"""


class DirectoryTooLargeError(WorkspaceServiceError):
    """目录条目数超限。"""


class AtomicCommitError(WorkspaceServiceError):
    """原子提交失败（非目标已存在的其他系统错误）。"""


# --------------------------------------------------------------------------- FileMetadata


@dataclass(frozen=True)
class FileEntry:
    """文件或目录条目元数据（不可变）。"""
    name: str
    relative_path: Path
    kind: Literal["file", "directory", "symlink", "other"]
    size_bytes: int | None = None


# --------------------------------------------------------------------------- WorkspaceFileService


class WorkspaceFileService:
    """Workspace 文件操作服务。

    封装 SandboxFileSystem，提供：
    - uploads / artifacts 子目录隔离
    - 上传大小限制
    - 二进制流式原子写入（分块，不读全部到内存）
    - 文件列表
    - 下载路径解析
    - 删除上传文件

    不 import FastAPI，不处理 HTTP 状态码。
    """

    UPLOADS = "uploads"
    ARTIFACTS = "artifacts"
    # 每个上传块 64 KB
    _CHUNK_SIZE = 65536

    def __init__(
        self,
        resolver: SandboxPathResolver,
        max_upload_bytes: int,
        max_list_entries: int = 1000,
        *,
        allow_overwrite: bool = False,
    ) -> None:
        """初始化服务。

        Args:
            resolver: Workspace 沙箱路径解析器（根目录）。
            max_upload_bytes: 单次上传最大字节数。
            max_list_entries: 文件列表最大条目数。
            allow_overwrite: 是否允许覆盖已存在文件（默认 False）。

        注意：每个子目录（uploads / artifacts）拥有独立的解析器，
        隔离其安全边界，防止 ../ 在两个逻辑区域之间跳转。
        """
        self._resolver = resolver
        self._max_upload = max_upload_bytes
        self._max_list = max_list_entries
        self._allow_overwrite = allow_overwrite
        self._root = resolver.root

        # uploads 和 artifacts 使用独立解析器（隔离安全边界）
        # 必须在 WorkspacePathResolver 构造前创建子目录（resolver 要求目录已存在）
        uploads_dir = self._root / self.UPLOADS
        artifacts_dir = self._root / self.ARTIFACTS
        uploads_dir.mkdir(parents=True, exist_ok=True)
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        self._uploads_resolver = WorkspacePathResolver(uploads_dir)
        self._artifacts_resolver = WorkspacePathResolver(artifacts_dir)

        # _ensure_* 只返回已创建的目录路径
        self._uploads_initialized = True
        self._artifacts_initialized = True

    # ---------------------------------------------------------------- internal helpers

    def _ensure_subdir(self, sub: str) -> Path:
        """确保子目录存在，返回绝对路径。"""
        base = self._root / sub
        base.mkdir(parents=True, exist_ok=True)
        return base

    def _ensure_uploads(self) -> Path:
        return self._uploads_resolver.root

    def _ensure_artifacts(self) -> Path:
        return self._artifacts_resolver.root

    def _guess_media_type(self, filename: str) -> str:
        """从文件名推断 MIME 类型。"""
        mime, _ = mimetypes.guess_type(filename, strict=False)
        return mime or "application/octet-stream"

    def _classify_file_entry(self, path: Path, rel: Path) -> FileEntry:
        """分类文件条目。"""
        try:
            stat = path.stat()
            if path.is_symlink():
                kind = "symlink"
            elif path.is_file():
                kind = "file"
            elif path.is_dir():
                kind = "directory"
            else:
                kind = "other"
            return FileEntry(
                name=path.name,
                relative_path=rel,
                kind=kind,
                size_bytes=stat.st_size if kind == "file" else None,
            )
        except OSError:
            return FileEntry(name=path.name, relative_path=rel, kind="other")

    # ---------------------------------------------------------------- upload（原子二进制流式写入）

    def upload_streaming(
        self,
        source_filename: str,
        file_obj,
        *,
        path: str | None = None,
    ) -> tuple[Path, int, str]:
        """流式上传文件（不加载完整文件到内存）。

        原子写入流程：
        1. 分块读取 file_obj，每次最多 64 KiB
        2. 在目标目录创建临时文件
        3. 分块写入，实时统计大小
        4. 超限立即中止并删除临时文件
        5. flush + fsync + os.link 原子提交（no-clobber）
        6. 失败时删除临时文件

        Args:
            source_filename: 原始文件名（用于推断 MIME 和默认目标路径）。
            file_obj: 具备 read(n) 方法的文件对象（如 SpooledTemporaryFile）。
            path: 可选的相对路径；若 None 则使用 source_filename。

        Returns:
            (relative_path, size, media_type) 元组。

        Raises:
            UploadFileTooLargeError: 超出大小限制。
            FileAlreadyExistsError: 文件已存在且禁止覆盖。
            PathViolationError: 路径违反安全规则。
            NotADirectoryError: 目标父目录不是目录。
        """
        if path is None:
            path = source_filename

        # 解析目标路径（在 uploads 子目录内）
        resolved = self._uploads_resolver.resolve(path, must_exist=False)
        target = resolved.absolute_path
        parent = target.parent

        # 确保父目录存在（自动创建）
        if not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)
        elif not parent.is_dir():
            raise NotADirectoryError(f"父路径不是目录: {resolved.relative_path}")

        # 覆盖检查：target 已存在时拒绝
        if target.exists():
            if target.is_dir():
                raise NotADirectoryError(f"目标是目录: {resolved.relative_path}")
            if not self._allow_overwrite:
                raise FileAlreadyExistsError(f"文件已存在: {resolved.relative_path}")

        # 创建临时文件（同目录，保证同一文件系统 → os.link 原子）
        fd, temp_name = tempfile.mkstemp(
            prefix=".mdf_up_", suffix=".tmp", dir=parent
        )
        temp_path = Path(temp_name)
        total_written = 0

        try:
            with os.fdopen(fd, "wb") as fh:
                while True:
                    chunk = file_obj.read(self._CHUNK_SIZE)
                    if not chunk:
                        break
                    # 预判：写入此 chunk 后是否超限
                    if self._max_upload > 0 and total_written + len(chunk) > self._max_upload:
                        raise UploadFileTooLargeError(
                            total_written + len(chunk), self._max_upload
                        )
                    fh.write(chunk)
                    total_written += len(chunk)
                # flush + fsync 在 with 退出前执行，fh 仍打开
                fh.flush()
                os.fsync(fh.fileno())

            # 大小限制最终校验（写入已全部完成）
            if self._max_upload > 0 and total_written > self._max_upload:
                raise UploadFileTooLargeError(total_written, self._max_upload)

            # 原子 no-clobber 提交：
            #
            # POSIX（os.name != "nt"）：优先 os.link。
            #   - link 成功 → 两文件名指向同一 inode，temp 残留需 unlink()
            #   - link 失败 + FileExistsError → 目标已存在，明确抛领域异常
            #   - link 失败 + 其他 OSError → rename 可能覆盖已有目标，禁止回退
            #
            # Windows（os.name == "nt"）：os.link 始终失败，回退 os.rename。
            #   - Windows rename 在目标已存在时抛 FileExistsError（不会覆盖），
            #     等同 no-clobber 语义。
            #   - 回退成功后 temp 已移走，无需 unlink()
            try:
                os.link(temp_path, target)
            except FileExistsError as exc:
                raise FileAlreadyExistsError(
                    f"文件已存在: {resolved.relative_path}"
                ) from exc
            except OSError as exc:
                if os.name != "nt":
                    # POSIX rename 可能覆盖已有目标，不允许回退
                    raise AtomicCommitError(
                        f"原子提交失败（link 不可用）: {exc.strerror}"
                    ) from exc
                # Windows fallback：rename 在目标存在时抛 FileExistsError（不覆盖）
                try:
                    os.rename(temp_path, target)
                except FileExistsError as conflict:
                    raise FileAlreadyExistsError(
                        f"文件已存在: {resolved.relative_path}"
                    ) from conflict
                except OSError as commit_error:
                    raise AtomicCommitError(
                        f"原子提交失败: {commit_error.strerror}"
                    ) from commit_error
            else:
                # link 成功后删除临时文件；目标文件仍然存在
                temp_path.unlink()
                temp_path = None

        except Exception:
            raise
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink()
                except OSError:
                    pass

        return (
            resolved.relative_path,
            total_written,
            self._guess_media_type(source_filename),
        )

    # ---------------------------------------------------------------- list files

    def list_uploads(self) -> list[FileEntry]:
        """列出 uploads 目录中的条目。"""
        base = self._ensure_uploads()
        return self._list_dir(base, Path(self.UPLOADS))

    def list_artifacts(self) -> list[FileEntry]:
        """列出 artifacts 目录中的条目。"""
        base = self._ensure_artifacts()
        return self._list_dir(base, Path(self.ARTIFACTS))

    def _list_dir(self, base: Path, rel_base: Path) -> list[FileEntry]:
        """列出目录条目，稳定排序，不返回绝对路径。"""
        if not base.is_dir():
            return []
        entries: list[FileEntry] = []
        try:
            for name in sorted(base.iterdir(), key=lambda p: p.name.casefold()):
                rel = rel_base / name.name
                entries.append(self._classify_file_entry(name, rel))
                if len(entries) >= self._max_list:
                    raise DirectoryTooLargeError(f"目录条目过多（上限 {self._max_list}）")
        except OSError:
            pass
        return entries

    # ---------------------------------------------------------------- download

    def resolve_upload(self, rel: str | Path) -> tuple[Path, int, str]:
        """解析上传文件路径（使用 uploads 独立解析器）。"""
        resolved = self._uploads_resolver.resolve(rel, must_exist=False)
        target = resolved.absolute_path

        if not target.exists():
            raise FileNotFoundError(f"文件不存在: {rel}")
        if not target.is_file():
            raise NotAFileError(f"目标不是普通文件: {rel}")

        try:
            size = target.stat().st_size
        except OSError as e:
            raise FileNotFoundError(f"无法获取文件大小: {rel}") from e

        return (target, size, self._guess_media_type(target.name))

    def resolve_artifact(self, rel: str | Path) -> tuple[Path, int, str]:
        """解析 Artifact 文件路径（使用 artifacts 独立解析器）。"""
        resolved = self._artifacts_resolver.resolve(rel, must_exist=False)
        target = resolved.absolute_path

        if not target.exists():
            raise FileNotFoundError(f"Artifact 不存在: {rel}")
        if not target.is_file():
            raise NotAFileError(f"Artifact 不是普通文件: {rel}")

        try:
            size = target.stat().st_size
        except OSError as e:
            raise FileNotFoundError(f"无法获取文件大小: {rel}") from e

        return (target, size, self._guess_media_type(target.name))

    # ---------------------------------------------------------------- delete

    def delete_upload(self, rel: str | Path) -> None:
        """删除 uploads 中的文件（使用 uploads 独立解析器）。"""
        resolved = self._uploads_resolver.resolve(rel, must_exist=False)
        target = resolved.absolute_path

        if not target.exists():
            raise FileNotFoundError(f"文件不存在: {rel}")
        if target.is_dir():
            raise NotAFileError(f"目标是目录，不能删除: {rel}")

        try:
            target.unlink()
        except OSError as e:
            raise WorkspaceServiceError(f"删除失败: {rel}: {e}") from e

    # ---------------------------------------------------------------- 路径映射异常

    @staticmethod
    def map_error(exc: Exception) -> Exception:
        """将底层 Sandbox/FileSystem 异常映射为领域异常。"""
        if isinstance(exc, SandboxPathViolationError):
            return PathViolationError(str(exc))
        if isinstance(exc, SandboxPathNotFoundError):
            return FileNotFoundError(str(exc))
        if isinstance(exc, SandboxNotAFileError):
            return NotAFileError(str(exc))
        if isinstance(exc, SandboxNotADirectoryError):
            return NotADirectoryError(str(exc))
        if isinstance(exc, SandboxFileAlreadyExistsError):
            return FileAlreadyExistsError(str(exc))
        if isinstance(exc, SandboxFileTooLargeError):
            return UploadFileTooLargeError(
                0, 0
            )  # 保留上限信息需另外处理
        if isinstance(exc, SandboxDirectoryTooLargeError):
            return DirectoryTooLargeError(str(exc))
        if isinstance(exc, WorkspaceServiceError):
            return exc
        return exc
