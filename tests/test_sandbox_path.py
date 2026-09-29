"""沙箱路径安全解析测试（16A 阶段）。"""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path, PureWindowsPath
from unittest.mock import patch

from mini_deerflow.sandbox_path import (
    SandboxPathError,
    SandboxPathNotFoundError,
    SandboxPathViolationError,
    SandboxRootError,
    ResolvedSandboxPath,
    SandboxPathResolver,
    WorkspacePathResolver,
    _is_windows_reserved_name,
    _contains_windows_reserved_name,
    _is_absolute_path,
    _is_unc_path,
    _contains_ads,
)


# --- ResolvedSandboxPath 测试 ---


class TestResolvedSandboxPathCreation(unittest.TestCase):
    """ResolvedSandboxPath 正常创建测试。"""

    def test_normal_creation(self):
        """正常创建所有字段。"""
        path = ResolvedSandboxPath(
            requested="src/main.py",
            absolute_path=Path("D:/workspace/src/main.py").absolute(),
            relative_path=Path("src/main.py"),
        )
        self.assertEqual(path.requested, "src/main.py")
        self.assertTrue(path.absolute_path.is_absolute())

    def test_frozen_immutable(self):
        """frozen=True 防止修改。"""
        path = ResolvedSandboxPath(
            requested="src/main.py",
            absolute_path=Path("D:/workspace/src/main.py").absolute(),
            relative_path=Path("src/main.py"),
        )
        with self.assertRaises(AttributeError):
            path.requested = "evil"

    def test_requested_must_be_str(self):
        """requested 必须是字符串。"""
        with self.assertRaises(TypeError):
            ResolvedSandboxPath(
                requested=123,  # type: ignore
                absolute_path=Path("D:/workspace/main.py"),
                relative_path=Path("main.py"),
            )

    def test_requested_empty_fails(self):
        """requested 为空时失败。"""
        with self.assertRaises(TypeError):
            ResolvedSandboxPath(
                requested="",  # 空字符串
                absolute_path=Path("D:/workspace/main.py"),
                relative_path=Path("main.py"),
            )

    def test_absolute_path_must_be_path(self):
        """absolute_path 必须是 Path 类型。"""
        with self.assertRaises(TypeError):
            ResolvedSandboxPath(
                requested="main.py",
                absolute_path="/workspace/main.py",  # str
                relative_path=Path("main.py"),
            )

    def test_absolute_path_must_be_absolute(self):
        """absolute_path 必须是绝对路径。"""
        with self.assertRaises(ValueError):
            ResolvedSandboxPath(
                requested="main.py",
                absolute_path=Path("main.py"),  # 相对路径
                relative_path=Path("main.py"),
            )

    def test_relative_path_must_be_path(self):
        """relative_path 必须是 Path 类型。"""
        with self.assertRaises(TypeError):
            ResolvedSandboxPath(
                requested="main.py",
                absolute_path=Path("D:/workspace/main.py"),
                relative_path="main.py",  # str
            )

    def test_relative_path_must_not_be_absolute(self):
        """relative_path 不能是绝对路径。"""
        with self.assertRaises(ValueError):
            ResolvedSandboxPath(
                requested="main.py",
                absolute_path=Path("D:/workspace/main.py"),
                relative_path=Path("C:/workspace/main.py"),  # Windows 绝对路径
            )

    def test_relative_path_cannot_contain_dotdot(self):
        """relative_path 包含 .. 时失败。"""
        with self.assertRaises(ValueError):
            ResolvedSandboxPath(
                requested="../secret",
                absolute_path=Path("D:/secret"),
                relative_path=Path("..") / "secret",
            )


# --- WorkspacePathResolver Root 测试 ---


class TestWorkspacePathResolverRoot(unittest.TestCase):
    """WorkspacePathResolver 初始化测试。"""

    def test_str_root(self):
        """字符串 root 正常工作。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            resolver = WorkspacePathResolver(tmpdir)
            self.assertTrue(resolver.root.is_absolute())

    def test_path_root(self):
        """Path root 正常工作。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            resolver = WorkspacePathResolver(Path(tmpdir))
            self.assertTrue(resolver.root.is_absolute())

    def test_root_normal(self):
        """正常目录可作为 root。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            resolver = WorkspacePathResolver(tmpdir)
            self.assertEqual(resolver.root, Path(tmpdir).resolve())

    def test_root_does_not_exist(self):
        """root 不存在时抛 SandboxRootError。"""
        with self.assertRaises(SandboxRootError) as ctx:
            WorkspacePathResolver("/nonexistent/path/12345")
        self.assertIn("不存在", str(ctx.exception))

    def test_root_is_file(self):
        """root 是文件时抛 SandboxRootError。"""
        with tempfile.NamedTemporaryFile() as f:
            with self.assertRaises(SandboxRootError) as ctx:
                WorkspacePathResolver(f.name)
            self.assertIn("不是目录", str(ctx.exception))

    def test_root_relative_path_becomes_absolute(self):
        """相对路径解析为绝对路径。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            old_cwd = os.getcwd()
            try:
                os.chdir(tmpdir)
                # 使用相对路径 "subdir" 假设它存在
                subdir = Path(tmpdir) / "subdir"
                subdir.mkdir()
                resolver = WorkspacePathResolver("subdir")
                self.assertTrue(resolver.root.is_absolute())
            finally:
                os.chdir(old_cwd)

    def test_root_saved_as_absolute(self):
        """root 保存为规范化的绝对路径。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            resolver = WorkspacePathResolver(tmpdir)
            self.assertTrue(resolver.root.is_absolute())
            self.assertEqual(resolver.root, Path(tmpdir).resolve())

    def test_root_symlink_resolved(self):
        """root 是符号链接时保存最终目标路径。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            real_dir = Path(tmpdir) / "real"
            real_dir.mkdir()
            link_dir = Path(tmpdir) / "link"
            try:
                link_dir.symlink_to(real_dir, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("符号链接不支持")

            resolver = WorkspacePathResolver(link_dir)
            self.assertEqual(resolver.root, real_dir.resolve())

    def test_root_property_readonly(self):
        """root 属性只读。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            resolver = WorkspacePathResolver(tmpdir)
            with self.assertRaises(AttributeError):
                resolver.root = Path("/other")


# --- WorkspacePathResolver 正常路径测试 ---


class TestWorkspacePathResolverNormalPaths(unittest.TestCase):
    """正常路径解析测试。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.resolver = WorkspacePathResolver(self.root)

    def test_single_file(self):
        """单层文件路径。"""
        (self.root / "main.py").touch()
        result = self.resolver.resolve("main.py")
        self.assertEqual(result.requested, "main.py")
        self.assertEqual(result.relative_path, Path("main.py"))
        self.assertTrue(result.absolute_path.is_absolute())

    def test_nested_path(self):
        """多层嵌套路径。"""
        (self.root / "src" / "utils" / "helper.py").parent.mkdir(parents=True)
        (self.root / "src" / "utils" / "helper.py").touch()
        result = self.resolver.resolve("src/utils/helper.py")
        self.assertEqual(result.requested, "src/utils/helper.py")
        self.assertEqual(result.relative_path, Path("src/utils/helper.py"))

    def test_chinese_path(self):
        """中文路径。"""
        (self.root / "项目" / "说明.md").parent.mkdir(parents=True)
        (self.root / "项目" / "说明.md").touch()
        result = self.resolver.resolve("项目/说明.md")
        self.assertEqual(result.requested, "项目/说明.md")

    def test_path_with_spaces(self):
        """包含空格的文件名。"""
        (self.root / "my file.txt").touch()
        result = self.resolver.resolve("my file.txt")
        self.assertEqual(result.requested, "my file.txt")

    def test_windows_separator(self):
        """Windows 反斜杠分隔符。"""
        (self.root / "src" / "main.py").parent.mkdir(parents=True)
        (self.root / "src" / "main.py").touch()
        result = self.resolver.resolve("src\\main.py")
        self.assertEqual(result.requested, "src\\main.py")
        self.assertEqual(result.relative_path, Path("src/main.py"))

    def test_existing_file(self):
        """已存在的文件。"""
        f = self.root / "exists.txt"
        f.touch()
        result = self.resolver.resolve("exists.txt", must_exist=True)
        self.assertEqual(result.requested, "exists.txt")

    def test_existing_directory(self):
        """已存在的目录。"""
        (self.root / "mydir").mkdir()
        result = self.resolver.resolve("mydir", must_exist=True)
        self.assertEqual(result.requested, "mydir")

    def test_nonexistent_target_must_exist_false(self):
        """must_exist=False 时目标可以不存在。"""
        result = self.resolver.resolve("new/file.txt", must_exist=False)
        self.assertEqual(result.requested, "new/file.txt")
        self.assertFalse(result.absolute_path.exists())

    def test_returns_correct_absolute_path(self):
        """返回正确的绝对路径。"""
        f = self.root / "src" / "main.py"
        f.parent.mkdir(parents=True)
        f.touch()
        result = self.resolver.resolve("src/main.py")
        self.assertEqual(result.absolute_path, f.resolve())

    def test_returns_correct_relative_path(self):
        """返回正确的相对路径。"""
        f = self.root / "src" / "main.py"
        f.parent.mkdir(parents=True)
        f.touch()
        result = self.resolver.resolve("src/main.py")
        self.assertEqual(result.relative_path, Path("src/main.py"))

    def test_does_not_modify_input(self):
        """不修改输入路径。"""
        self.resolver.resolve("file.txt")
        # 输入字符串不变
        self.assertFalse((self.root / "file.txt").exists())


# --- WorkspacePathResolver 拒绝路径测试 ---


class TestWorkspacePathResolverRejection(unittest.TestCase):
    """拒绝非法路径测试。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.resolver = WorkspacePathResolver(self.root)

    def test_empty_string(self):
        """空字符串被拒绝。"""
        with self.assertRaises(SandboxPathViolationError) as ctx:
            self.resolver.resolve("")
        self.assertIn("不能为空", str(ctx.exception))

    def test_whitespace_only(self):
        """全空白路径被拒绝。"""
        with self.assertRaises(SandboxPathViolationError) as ctx:
            self.resolver.resolve("   ")
        self.assertIn("不能全为空白", str(ctx.exception))

    def test_nul_character(self):
        """NUL 字符被拒绝。"""
        with self.assertRaises(SandboxPathViolationError):
            self.resolver.resolve("file\x00.txt")

    def test_dot_alone(self):
        """单点路径被拒绝。"""
        with self.assertRaises(SandboxPathViolationError) as ctx:
            self.resolver.resolve(".")
        self.assertIn("不允许访问根目录", str(ctx.exception))

    def test_dotdot(self):
        """双点路径被拒绝。"""
        with self.assertRaises(SandboxPathViolationError) as ctx:
            self.resolver.resolve("..")
        self.assertIn("路径不能包含 '..' 组件", str(ctx.exception))

    def test_dotdot_in_path(self):
        """路径中的 .. 被拒绝。"""
        with self.assertRaises(SandboxPathViolationError) as ctx:
            self.resolver.resolve("../secret")
        self.assertIn("路径不能包含 '..' 组件", str(ctx.exception))

    def test_dotdot_nested(self):
        """嵌套的 .. 被拒绝。"""
        with self.assertRaises(SandboxPathViolationError) as ctx:
            self.resolver.resolve("a/../../secret")
        self.assertIn("路径不能包含 '..' 组件", str(ctx.exception))

    def test_posix_absolute_path(self):
        """POSIX 绝对路径被拒绝。"""
        with self.assertRaises(SandboxPathViolationError) as ctx:
            self.resolver.resolve("/etc/passwd")
        self.assertIn("不允许绝对路径", str(ctx.exception))

    def test_windows_absolute_backslash(self):
        """Windows 反斜杠绝对路径被拒绝。"""
        with self.assertRaises(SandboxPathViolationError) as ctx:
            self.resolver.resolve("C:\\Windows\\System32")
        self.assertIn("不允许", str(ctx.exception))

    def test_windows_absolute_forward_slash(self):
        """Windows 正斜杠绝对路径被拒绝。"""
        with self.assertRaises(SandboxPathViolationError) as ctx:
            self.resolver.resolve("C:/Windows/System32")
        self.assertIn("不允许", str(ctx.exception))

    def test_windows_drive_relative(self):
        """Windows drive-relative 路径被拒绝。"""
        with self.assertRaises(SandboxPathViolationError) as ctx:
            self.resolver.resolve("C:secret.txt")
        self.assertIn("不允许", str(ctx.exception))

    def test_unc_backslash(self):
        """UNC 反斜杠路径被拒绝。"""
        with self.assertRaises(SandboxPathViolationError) as ctx:
            self.resolver.resolve("\\\\server\\share")
        self.assertIn("不允许 UNC 路径", str(ctx.exception))

    def test_unc_forward_slash(self):
        """UNC 正斜杠路径被拒绝。"""
        with self.assertRaises(SandboxPathViolationError) as ctx:
            self.resolver.resolve("//server/share")
        self.assertIn("不允许 UNC 路径", str(ctx.exception))

    def test_ads_colon(self):
        """NTFS Alternate Data Stream 被拒绝。"""
        with self.assertRaises(SandboxPathViolationError) as ctx:
            self.resolver.resolve("file.txt:secret")
        self.assertIn("不允许 NTFS Alternate Data Stream", str(ctx.exception))

    def test_trailing_dot(self):
        """尾随点被拒绝。"""
        with self.assertRaises(SandboxPathViolationError) as ctx:
            self.resolver.resolve("file.txt.")
        self.assertIn("路径末尾不能有点", str(ctx.exception))

    def test_trailing_space(self):
        """尾随空格被拒绝。"""
        with self.assertRaises(SandboxPathViolationError) as ctx:
            self.resolver.resolve("file.txt ")
        self.assertIn("路径末尾不能有空格", str(ctx.exception))

    def test_nul_reserved_name(self):
        """NUL 保留名被拒绝。"""
        with self.assertRaises(SandboxPathViolationError) as ctx:
            self.resolver.resolve("NUL")
        self.assertIn("不允许 Windows 保留设备名", str(ctx.exception))

    def test_nul_with_extension(self):
        """带扩展名的 NUL 被拒绝。"""
        with self.assertRaises(SandboxPathViolationError) as ctx:
            self.resolver.resolve("NUL.txt")
        self.assertIn("不允许 Windows 保留设备名", str(ctx.exception))

    def test_con_reserved_name(self):
        """CON 保留名被拒绝。"""
        with self.assertRaises(SandboxPathViolationError) as ctx:
            self.resolver.resolve("CON")
        self.assertIn("不允许 Windows 保留设备名", str(ctx.exception))

    def test_con_with_extension(self):
        """带扩展名的 CON 被拒绝。"""
        with self.assertRaises(SandboxPathViolationError) as ctx:
            self.resolver.resolve("CON.json")
        self.assertIn("不允许 Windows 保留设备名", str(ctx.exception))

    def test_prn_reserved_name(self):
        """PRN 保留名被拒绝。"""
        with self.assertRaises(SandboxPathViolationError) as ctx:
            self.resolver.resolve("PRN")
        self.assertIn("不允许 Windows 保留设备名", str(ctx.exception))

    def test_aux_reserved_name(self):
        """AUX 保留名被拒绝。"""
        with self.assertRaises(SandboxPathViolationError) as ctx:
            self.resolver.resolve("AUX")
        self.assertIn("不允许 Windows 保留设备名", str(ctx.exception))

    def test_com1_reserved_name(self):
        """COM1 保留名被拒绝。"""
        with self.assertRaises(SandboxPathViolationError) as ctx:
            self.resolver.resolve("COM1")
        self.assertIn("不允许 Windows 保留设备名", str(ctx.exception))

    def test_lpt1_reserved_name(self):
        """LPT1 保留名被拒绝。"""
        with self.assertRaises(SandboxPathViolationError) as ctx:
            self.resolver.resolve("LPT1")
        self.assertIn("不允许 Windows 保留设备名", str(ctx.exception))

    def test_com9_reserved_name(self):
        """COM9 保留名被拒绝。"""
        with self.assertRaises(SandboxPathViolationError) as ctx:
            self.resolver.resolve("COM9.log")
        self.assertIn("不允许 Windows 保留设备名", str(ctx.exception))

    def test_lpt9_reserved_name(self):
        """LPT9 保留名被拒绝。"""
        with self.assertRaises(SandboxPathViolationError) as ctx:
            self.resolver.resolve("LPT9.txt")
        self.assertIn("不允许 Windows 保留设备名", str(ctx.exception))

    def test_reserved_name_case_insensitive(self):
        """保留名大小写不敏感。"""
        with self.assertRaises(SandboxPathViolationError):
            self.resolver.resolve("nul")
        with self.assertRaises(SandboxPathViolationError):
            self.resolver.resolve("Con")
        with self.assertRaises(SandboxPathViolationError):
            self.resolver.resolve("com1")

    def test_console_txt_not_rejected(self):
        """console.txt 不是保留名。"""
        # 允许的普通名称
        (self.root / "console.txt").touch()
        result = self.resolver.resolve("console.txt")
        self.assertEqual(result.requested, "console.txt")


# --- 存在性测试 ---


class TestWorkspacePathResolverExistence(unittest.TestCase):
    """must_exist 参数测试。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.resolver = WorkspacePathResolver(self.root)

    def test_must_exist_true_and_exists(self):
        """must_exist=True 且存在时成功。"""
        (self.root / "exists.txt").touch()
        result = self.resolver.resolve("exists.txt", must_exist=True)
        self.assertEqual(result.requested, "exists.txt")

    def test_must_exist_true_and_not_exists(self):
        """must_exist=True 但不存在时抛 SandboxPathNotFoundError。"""
        with self.assertRaises(SandboxPathNotFoundError) as ctx:
            self.resolver.resolve("missing.txt", must_exist=True)
        self.assertIn("不存在", str(ctx.exception))

    def test_must_exist_false_and_not_exists(self):
        """must_exist=False 时不存在也成功。"""
        result = self.resolver.resolve("new.txt", must_exist=False)
        self.assertEqual(result.requested, "new.txt")
        self.assertFalse(result.absolute_path.exists())

    def test_nonexistent_nested_target(self):
        """不存在的嵌套目标。"""
        result = self.resolver.resolve("deep/nested/file.txt", must_exist=False)
        self.assertEqual(result.requested, "deep/nested/file.txt")
        self.assertFalse(result.absolute_path.exists())


# --- 边界测试 ---


class TestWorkspacePathResolverBoundary(unittest.TestCase):
    """路径边界测试。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.resolver = WorkspacePathResolver(self.root)

    def test_similar_prefix_directory(self):
        """相似前缀目录不在沙箱内。"""
        # workspace-evil 不应匹配 workspace
        evil_dir = self.root.parent / (self.root.name + "-evil")
        evil_dir.mkdir(exist_ok=True)
        self.addCleanup(evil_dir.rmdir)

        # 直接访问 workspace-evil 应该失败
        with self.assertRaises(SandboxPathViolationError):
            self.resolver.resolve("../" + evil_dir.name + "/file.txt")

    def test_path_inside_root(self):
        """Root 内路径成功。"""
        (self.root / "inside.txt").touch()
        result = self.resolver.resolve("inside.txt")
        self.assertTrue(result.absolute_path.is_relative_to(self.root))

    def test_path_outside_root(self):
        """Root 外路径被拒绝。"""
        with self.assertRaises(SandboxPathViolationError):
            self.resolver.resolve("../outside.txt")

    def test_symlink_inside_to_inside(self):
        """Root 内指向内部的符号链接可解析。"""
        (self.root / "real_dir").mkdir()
        (self.root / "real_dir" / "file.txt").touch()

        link = self.root / "link_dir"
        try:
            link.symlink_to(self.root / "real_dir", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("符号链接不支持")

        result = self.resolver.resolve("link_dir/file.txt")
        self.assertEqual(result.relative_path, Path("real_dir/file.txt"))

    def test_symlink_inside_to_outside(self):
        """Root 内指向外部的符号链接被拒绝。"""
        outside = Path(self._tmp.name).parent / "outside_dir"
        outside.mkdir(exist_ok=True)
        self.addCleanup(outside.rmdir)

        link = self.root / "escape_link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("符号链接不支持")

        with self.assertRaises(SandboxPathViolationError):
            self.resolver.resolve("escape_link/file.txt")

    def test_symlink_create_outside_via_link(self):
        """通过外部链接创建文件被拒绝。"""
        outside = Path(self._tmp.name).parent / "outside_target"
        outside.mkdir(exist_ok=True)
        self.addCleanup(outside.rmdir)

        link = self.root / "escape_link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("符号链接不支持")

        # 即使文件不存在，解析也应失败
        with self.assertRaises(SandboxPathViolationError):
            self.resolver.resolve("escape_link/newfile.txt")

    def test_broken_symlink(self):
        """损坏的符号链接给出明确错误。"""
        link = self.root / "broken_link"
        try:
            link.symlink_to(self.root.parent / "nonexistent", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("符号链接不支持")

        with self.assertRaises(SandboxPathViolationError):
            self.resolver.resolve("broken_link/file.txt")

    def test_resolve_does_not_leak_external_path(self):
        """成功的解析不泄漏外部路径。"""
        (self.root / "safe.txt").touch()
        result = self.resolver.resolve("safe.txt")
        # 相对路径是安全的
        self.assertFalse(result.relative_path.is_absolute())
        self.assertTrue(result.absolute_path.is_relative_to(self.root))


# --- Protocol 测试 ---


class TestSandboxPathResolverProtocol(unittest.TestCase):
    """Protocol 实现测试。"""

    def test_workspace_path_resolver_satisfies_protocol(self):
        """WorkspacePathResolver 满足 SandboxPathResolver 协议。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            resolver = WorkspacePathResolver(tmpdir)
            self.assertIsInstance(resolver, SandboxPathResolver)


# --- 内部工具函数测试 ---


class TestInternalHelpers(unittest.TestCase):
    """内部辅助函数测试。"""

    def test_is_windows_reserved_name(self):
        """_is_windows_reserved_name 正确识别。"""
        self.assertTrue(_is_windows_reserved_name("NUL"))
        self.assertTrue(_is_windows_reserved_name("nul"))
        self.assertTrue(_is_windows_reserved_name("NUL.txt"))
        self.assertTrue(_is_windows_reserved_name("CON.json"))
        self.assertTrue(_is_windows_reserved_name("COM1"))
        self.assertTrue(_is_windows_reserved_name("LPT9"))

    def test_is_windows_reserved_name_false(self):
        """_is_windows_reserved_name 正确识别非保留名。"""
        self.assertFalse(_is_windows_reserved_name("console.txt"))
        self.assertFalse(_is_windows_reserved_name("comment.py"))
        self.assertFalse(_is_windows_reserved_name("command"))
        self.assertFalse(_is_windows_reserved_name("normal_file"))

    def test_is_absolute_path(self):
        """_is_absolute_path 正确识别绝对路径。"""
        self.assertTrue(_is_absolute_path("/etc/passwd"))
        self.assertTrue(_is_absolute_path("C:\\Windows"))
        self.assertTrue(_is_absolute_path("C:/Windows"))
        self.assertTrue(_is_absolute_path("C:file.txt"))

    def test_is_absolute_path_false(self):
        """_is_absolute_path 正确识别相对路径。"""
        self.assertFalse(_is_absolute_path("src/main.py"))
        self.assertFalse(_is_absolute_path("file.txt"))

    def test_is_unc_path(self):
        """_is_unc_path 正确识别 UNC 路径。"""
        self.assertTrue(_is_unc_path("\\\\server\\share"))
        self.assertTrue(_is_unc_path("//server/share"))

    def test_is_unc_path_false(self):
        """_is_unc_path 正确识别非 UNC 路径。"""
        self.assertFalse(_is_unc_path("src/file.py"))
        self.assertFalse(_is_unc_path("file.txt"))

    def test_contains_ads(self):
        """_contains_ads 正确识别 ADS。"""
        self.assertTrue(_contains_ads("file.txt:stream"))
        self.assertTrue(_contains_ads("data:$INDEX_ALLOCATION"))

    def test_contains_ads_false(self):
        """_contains_ads 正确识别非 ADS。"""
        self.assertFalse(_contains_ads("C:\\path\\file.txt"))  # 盘符
        self.assertFalse(_contains_ads("normal/path/file.txt"))


# --- 异常体系测试 ---


class TestExceptionHierarchy(unittest.TestCase):
    """异常层次结构测试。"""

    def test_exception_hierarchy(self):
        """异常继承关系正确。"""
        self.assertTrue(issubclass(SandboxPathViolationError, SandboxPathError))
        self.assertTrue(issubclass(SandboxPathNotFoundError, SandboxPathError))
        self.assertTrue(issubclass(SandboxRootError, SandboxPathError))

    def test_sandbox_path_violation_error_attributes(self):
        """SandboxPathViolationError 有 requested 和 reason 属性。"""
        err = SandboxPathViolationError("test.txt", "危险路径")
        self.assertEqual(err.requested, "test.txt")
        self.assertEqual(err.reason, "危险路径")
        self.assertIn("test.txt", str(err))

    def test_sandbox_path_not_found_error_attributes(self):
        """SandboxPathNotFoundError 有 requested 属性。"""
        err = SandboxPathNotFoundError("missing.txt")
        self.assertEqual(err.requested, "missing.txt")
        self.assertIn("missing.txt", str(err))

    def test_sandbox_root_error_attributes(self):
        """SandboxRootError 有 root 和 path 属性。"""
        err = SandboxRootError("/bad/root", "不存在")
        self.assertEqual(str(err.root), "/bad/root")
        self.assertEqual(err.path, Path("/bad/root"))
        self.assertIn("/bad/root", str(err))


# --- 路径对象输入测试 ---


class TestPathObjectInput(unittest.TestCase):
    """Path 对象输入测试。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.resolver = WorkspacePathResolver(self.root)

    def test_path_object_input(self):
        """接受 Path 对象输入。"""
        (self.root / "file.txt").touch()
        result = self.resolver.resolve(Path("file.txt"))
        self.assertEqual(result.requested, "file.txt")

    def test_path_object_stored_as_string(self):
        """Path 输入的 requested 保存为字符串。"""
        (self.root / "file.txt").touch()
        result = self.resolver.resolve(Path("file.txt"))
        self.assertIsInstance(result.requested, str)


if __name__ == "__main__":
    unittest.main()
