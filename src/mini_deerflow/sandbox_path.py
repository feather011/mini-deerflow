"""沙箱路径安全解析（16A 阶段）。

本模块提供路径边界验证，防止通过相对路径 `..`、符号链接等手段
逃逸到沙箱根目录之外。

警告：本模块是路径防护组件，不是操作系统级安全边界。
它不能消除 TOCTOU 竞态、符号链接替换攻击等底层风险。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PureWindowsPath, PurePosixPath
from typing import Protocol, runtime_checkable

# Windows 保留设备名（不区分大小写）
_WINDOWS_RESERVED_NAMES: frozenset[str] = frozenset({
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
})


# --- 异常体系 ---


class SandboxPathError(ValueError):
    """沙箱路径相关错误的基类。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class SandboxPathViolationError(SandboxPathError):
    """路径违反沙箱边界时抛出。"""

    def __init__(self, requested: str, reason: str) -> None:
        self.requested = requested
        self.reason = reason
        super().__init__(f"非法路径 '{requested}': {reason}")


class SandboxPathNotFoundError(SandboxPathError):
    """路径不存在时抛出（must_exist=True 但目标缺失）。"""

    def __init__(self, requested: str) -> None:
        self.requested = requested
        super().__init__(f"路径不存在: {requested}")


class SandboxRootError(SandboxPathError):
    """沙箱根目录配置错误时抛出。"""

    def __init__(self, root: str | Path, reason: str) -> None:
        self.root = root
        self.path = Path(root) if isinstance(root, str) else root
        super().__init__(f"沙箱根目录错误 '{root}': {reason}")


# --- ResolvedSandboxPath ---


@dataclass(frozen=True)
class ResolvedSandboxPath:
    """不可变的解析后路径对象。"""

    requested: str
    """调用方原始请求的相对路径文本。"""

    absolute_path: Path
    """解析后的绝对路径。"""

    relative_path: Path
    """相对于沙箱根目录的规范路径。"""

    def __post_init__(self) -> None:
        """验证内部不变量。"""
        # requested 验证
        if not isinstance(self.requested, str):
            raise TypeError(
                f"requested 必须是 str，收到: {type(self.requested).__name__}"
            )
        if not self.requested:
            raise TypeError("requested 不能为空字符串")

        # absolute_path 验证
        if not isinstance(self.absolute_path, Path):
            raise TypeError(
                f"absolute_path 必须是 Path，收到: {type(self.absolute_path).__name__}"
            )
        if not self.absolute_path.is_absolute():
            raise ValueError(
                f"absolute_path 必须是绝对路径，收到: {self.absolute_path}"
            )

        # relative_path 验证
        if not isinstance(self.relative_path, Path):
            raise TypeError(
                f"relative_path 必须是 Path，收到: {type(self.relative_path).__name__}"
            )
        if self.relative_path.is_absolute():
            raise ValueError(
                f"relative_path 必须是相对路径，收到: {self.relative_path}"
            )
        # 检查是否包含 .. 组件
        parts = self.relative_path.parts
        if ".." in parts:
            raise ValueError(
                f"relative_path 包含 '..' 组件，收到: {self.relative_path}"
            )


# --- Protocol ---


@runtime_checkable
class SandboxPathResolver(Protocol):
    """路径解析器协议。"""

    @property
    def root(self) -> Path:
        """返回沙箱根目录的绝对路径。"""
        ...

    def resolve(
        self,
        requested: str | Path,
        *,
        must_exist: bool = False,
    ) -> ResolvedSandboxPath:
        """将相对路径解析为沙箱内的安全路径。"""
        ...


# --- 内部工具函数 ---


def _is_windows_reserved_name(name: str) -> bool:
    """检查名称是否为 Windows 保留设备名（忽略大小写和扩展名）。"""
    # 提取基础名称（去除扩展名）
    if "." in name:
        base = name.rsplit(".", 1)[0]
    else:
        base = name
    # 去除尾随空格和点
    base = base.rstrip(" ").rstrip(".")
    return base.upper() in _WINDOWS_RESERVED_NAMES


def _contains_windows_reserved_name(path: str | Path) -> bool:
    """检查路径任一组件是否为 Windows 保留设备名。"""
    if isinstance(path, Path):
        parts = path.parts
    else:
        # 同时检查 Windows 和 POSIX 分隔符
        parts = PureWindowsPath(path).parts
        # 如果是纯相对路径，PureWindowsPath 会把第一部分当作 drive
        # 实际检查时排除 drive 部分
        for part in parts:
            if part and not part.endswith(":") and _is_windows_reserved_name(part):
                return True
        # 再用 POSIX 方式检查
        parts = path.replace("\\", "/").split("/")

    for part in parts:
        if part and _is_windows_reserved_name(part):
            return True
    return False


def _is_absolute_path(requested: str) -> bool:
    """检查路径是否为任何形式的绝对路径。"""
    # POSIX 绝对路径
    if requested.startswith("/"):
        return True

    # Windows 绝对路径
    win_path = PureWindowsPath(requested)
    if win_path.is_absolute():
        return True
    if win_path.drive and not win_path.root:
        # C:相对路径（带盘符的相对路径）
        return True

    return False


def _is_unc_path(requested: str) -> bool:
    """检查路径是否为 UNC 路径。

    UNC 路径特征：
    - 以 \\ 开头（Windows 风格）
    - 以 // 开头（POSIX 风格，但 Windows 客户端可接受）
    """
    # 明确的 UNC 前缀
    if requested.startswith("\\\\") or requested.startswith("//"):
        return True
    return False


def _contains_ads(path: str) -> bool:
    """检查路径是否包含 NTFS Alternate Data Stream 语法。"""
    # file.txt:stream
    if ":" in path:
        # 排除 Windows drive letter (C:\path)
        parts = path.split(":")
        # 如果第一部分是单字母且第二部分以 \ 或 / 开头，是盘符
        if len(parts[0]) == 1 and len(parts) > 1 and parts[1][0:1] in ("\\", "/"):
            return False
        return True
    return False


def _normalize_path_input(requested: str) -> str:
    """规范化路径输入，返回干净的处理后字符串。"""
    # 移除 NUL 字符
    if "\x00" in requested:
        return requested.replace("\x00", "")
    return requested


def _validate_requested_path(requested: str) -> None:
    """验证请求路径是否符合安全要求，失败时抛出异常。"""
    original = requested
    requested = _normalize_path_input(requested)

    # 1. 空字符串
    if not requested:
        raise SandboxPathViolationError(original, "路径不能为空")

    # 2. 全空白
    if not requested.strip():
        raise SandboxPathViolationError(original, "路径不能全为空白")

    # 3. NUL 字符（原始值检查）
    if "\x00" in original:
        raise SandboxPathViolationError(original, "路径不能包含 NUL 字符")

    # 4. 当前目录
    if requested == ".":
        raise SandboxPathViolationError(original, "不允许访问根目录")

    # 5. 父目录
    if ".." in requested.split("/") or ".." in requested.split("\\"):
        raise SandboxPathViolationError(original, "路径不能包含 '..' 组件")

    # 6. UNC 路径（优先于 Windows 绝对路径检测）
    if _is_unc_path(requested):
        raise SandboxPathViolationError(original, "不允许 UNC 路径")

    # 7. POSIX 绝对路径
    if requested.startswith("/"):
        raise SandboxPathViolationError(original, "不允许绝对路径")

    # 8. Windows 绝对路径（包括 drive-relative）
    if _is_absolute_path(requested):
        raise SandboxPathViolationError(original, "不允许 Windows 绝对路径")

    # 9. NTFS Alternate Data Stream
    if _contains_ads(requested):
        raise SandboxPathViolationError(original, "不允许 NTFS Alternate Data Stream")

    # 10. Windows 保留设备名
    if _contains_windows_reserved_name(requested):
        raise SandboxPathViolationError(original, "不允许 Windows 保留设备名")

    # 11. 尾随点或空格
    stripped = requested.rstrip()
    if stripped != requested and stripped:
        # 有尾随空白
        raise SandboxPathViolationError(original, "路径末尾不能有空格")
    if requested.endswith("."):
        raise SandboxPathViolationError(original, "路径末尾不能有点")

    # 12. Windows 分隔符统一化检查
    # 混合分隔符如 a\b/c 允许（会被正确处理），但检查是否有隐式绝对路径
    normalized = requested.replace("\\", "/")
    if normalized.startswith("/"):
        raise SandboxPathViolationError(original, "不允许绝对路径")


# --- WorkspacePathResolver ---


class WorkspacePathResolver:
    """沙箱路径解析器实现。"""

    def __init__(self, root: str | Path) -> None:
        """初始化解析器。

        Args:
            root: 沙箱根目录路径。

        Raises:
            SandboxRootError: root 不存在、不是目录或无法访问。
        """
        # 1. 转换为 Path
        try:
            root_path = Path(root)
        except Exception as e:
            raise SandboxRootError(root, f"路径格式错误: {e}") from e

        # 2. 检查是否存在
        try:
            exists = root_path.exists()
        except OSError as e:
            raise SandboxRootError(root, f"无法访问: {e}") from e

        if not exists:
            raise SandboxRootError(root, "路径不存在")

        # 3. 检查是否为目录
        if not root_path.is_dir():
            raise SandboxRootError(root, "路径不是目录")

        # 4. 规范化并解析符号链接
        try:
            resolved_root = root_path.resolve(strict=True)
        except OSError as e:
            raise SandboxRootError(root, f"无法解析: {e}") from e

        # 5. 保存为绝对路径
        self._root: Path = resolved_root

    @property
    def root(self) -> Path:
        """返回沙箱根目录的绝对路径。"""
        return self._root

    def resolve(
        self,
        requested: str | Path,
        *,
        must_exist: bool = False,
    ) -> ResolvedSandboxPath:
        """将相对路径解析为沙箱内的安全路径。

        Args:
            requested: 要解析的相对路径。
            must_exist: 是否要求目标必须存在。

        Returns:
            ResolvedSandboxPath 对象。

        Raises:
            SandboxPathViolationError: 路径违反安全规则。
            SandboxPathNotFoundError: must_exist=True 但路径不存在。
            SandboxPathError: 其他路径错误。
        """
        # 1. 转换为字符串
        if isinstance(requested, Path):
            requested_str = requested.as_posix()
        else:
            requested_str = requested

        # 2. 验证请求路径
        _validate_requested_path(requested_str)

        # 3. 拼接 root 和相对路径
        try:
            joined = self._root / requested_str
        except Exception as e:
            raise SandboxPathError(f"路径拼接失败: {e}") from e

        # 4. 解析符号链接并规范化
        try:
            # 使用 strict=False 允许目标不存在
            resolved = joined.resolve(strict=False)
        except OSError as e:
            raise SandboxPathError(f"路径解析失败: {e}") from e

        # 5. 验证最终路径在 root 内
        try:
            is_inside = resolved.is_relative_to(self._root)
        except ValueError:
            # is_relative_to 在路径不在 root 内时抛出 ValueError
            is_inside = False

        if not is_inside:
            raise SandboxPathViolationError(
                requested_str,
                f"路径超出沙箱边界 (resolved: {resolved})"
            )

        # 6. 计算相对路径
        try:
            relative = resolved.relative_to(self._root)
        except ValueError:
            # 不应该发生，因为已经验证了 is_relative_to
            raise SandboxPathViolationError(
                requested_str,
                "无法计算相对路径"
            )

        # 7. 检查存在性
        if must_exist:
            if not resolved.exists():
                raise SandboxPathNotFoundError(requested_str)
            # 检查是否可以访问（无权限等情况）
            try:
                resolved.stat()
            except OSError as e:
                raise SandboxPathError(f"无法访问路径: {e}") from e

        return ResolvedSandboxPath(
            requested=requested_str,
            absolute_path=resolved,
            relative_path=relative,
        )
