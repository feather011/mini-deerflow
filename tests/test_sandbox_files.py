"""受控沙箱文件系统测试（16B 阶段）。"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mini_deerflow.sandbox_files import (
    SandboxDirectoryEntry,
    SandboxDirectoryTooLargeError,
    SandboxFileAlreadyExistsError,
    SandboxFileEncodingError,
    SandboxFileError,
    SandboxFileOperations,
    SandboxFileSystem,
    SandboxFileTooLargeError,
    SandboxNotADirectoryError,
    SandboxNotAFileError,
    SandboxWriteResult,
)
from mini_deerflow.sandbox_path import (
    SandboxPathNotFoundError,
    SandboxPathViolationError,
    WorkspacePathResolver,
)


def _rmtree_ignore(path: Path) -> None:
    """安全清理可能含有 symlink 的目录，忽略清理失败。"""
    try:
        shutil.rmtree(path)
    except OSError:
        pass


# --- 测试基类 ---


class _SandboxTestCase(unittest.TestCase):
    """每个测试使用独立的临时工作区。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.resolver = WorkspacePathResolver(self.root)
        self.fs = SandboxFileSystem(self.resolver)

    def write(self, relative: str, content: str) -> Path:
        """在工作区中直接写文件（绕过 fs，用于构造前置状态）。

        使用 newline="" 避免 Windows 将 \\n 自动转换为 \\r\\n，
        保证测试内容与预期完全一致。
        """
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="")
        return path

    def make_symlink(self, link_relative: str, target: Path) -> bool:
        """在工作区内创建符号链接；不支持时抛出异常（不静默失败）。"""
        link = self.root / link_relative
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            link.symlink_to(target, target_is_directory=target.is_dir())
        except (OSError, NotImplementedError):
            raise  # 不静默失败，让调用方 skipTest


# --- 构造函数 ---


class TestConstructor(unittest.TestCase):
    """SandboxFileSystem 构造参数验证。"""

    def _resolver(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return WorkspacePathResolver(tmp.name)

    def test_normal_init(self):
        """默认参数正常初始化。"""
        fs = SandboxFileSystem(self._resolver())
        self.assertEqual(fs.max_read_bytes, 1_048_576)
        self.assertEqual(fs.max_write_bytes, 1_048_576)
        self.assertEqual(fs.max_list_entries, 1_000)

    def test_root_exposed(self):
        """root 属性透传 Resolver。"""
        resolver = self._resolver()
        fs = SandboxFileSystem(resolver)
        self.assertEqual(fs.root, resolver.root)

    def test_custom_limits(self):
        """自定义限制。"""
        fs = SandboxFileSystem(
            self._resolver(),
            max_read_bytes=10,
            max_write_bytes=20,
            max_list_entries=3,
        )
        self.assertEqual(fs.max_read_bytes, 10)
        self.assertEqual(fs.max_write_bytes, 20)
        self.assertEqual(fs.max_list_entries, 3)

    def test_zero_limits_rejected(self):
        """0 抛 ValueError。"""
        for kwargs in (
            {"max_read_bytes": 0},
            {"max_write_bytes": 0},
            {"max_list_entries": 0},
        ):
            with self.subTest(**kwargs):
                with self.assertRaises(ValueError):
                    SandboxFileSystem(self._resolver(), **kwargs)

    def test_negative_limits_rejected(self):
        """负数抛 ValueError。"""
        for kwargs in (
            {"max_read_bytes": -1},
            {"max_write_bytes": -100},
            {"max_list_entries": -5},
        ):
            with self.subTest(**kwargs):
                with self.assertRaises(ValueError):
                    SandboxFileSystem(self._resolver(), **kwargs)

    def test_bool_limits_rejected(self):
        """bool 视为非法类型，抛 TypeError。"""
        for kwargs in (
            {"max_read_bytes": True},
            {"max_write_bytes": False},
            {"max_list_entries": True},
        ):
            with self.subTest(**kwargs):
                with self.assertRaises(TypeError):
                    SandboxFileSystem(self._resolver(), **kwargs)

    def test_float_limits_rejected(self):
        """float 抛 TypeError。"""
        for kwargs in (
            {"max_read_bytes": 1.5},
            {"max_write_bytes": 100.0},
            {"max_list_entries": 2.5},
        ):
            with self.subTest(**kwargs):
                with self.assertRaises(TypeError):
                    SandboxFileSystem(self._resolver(), **kwargs)

    def test_str_limits_rejected(self):
        """str 抛 TypeError。"""
        for kwargs in (
            {"max_read_bytes": "100"},
            {"max_write_bytes": "abc"},
            {"max_list_entries": "10"},
        ):
            with self.subTest(**kwargs):
                with self.assertRaises(TypeError):
                    SandboxFileSystem(self._resolver(), **kwargs)

    def test_invalid_resolver_rejected(self):
        """resolver 不满足接口时抛 TypeError。"""
        with self.assertRaises(TypeError):
            SandboxFileSystem(object())  # type: ignore[arg-type]

    def test_init_does_not_touch_filesystem(self):
        """构造时不创建文件或目录。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        before = set(root.iterdir())
        SandboxFileSystem(WorkspacePathResolver(root))
        self.assertEqual(set(root.iterdir()), before)

    def test_satisfies_protocol(self):
        """SandboxFileSystem 满足 SandboxFileOperations 协议。"""
        self.assertIsInstance(SandboxFileSystem(self._resolver()), SandboxFileOperations)


# --- Resolved 值对象 ---


class TestSandboxDirectoryEntry(unittest.TestCase):
    """SandboxDirectoryEntry 不变量测试。"""

    def test_valid_file_entry(self):
        """普通文件条目。"""
        entry = SandboxDirectoryEntry(
            name="a.txt",
            relative_path=Path("a.txt"),
            kind="file",
            size_bytes=10,
        )
        self.assertEqual(entry.name, "a.txt")

    def test_directory_entry_no_size(self):
        """目录条目 size_bytes 为 None。"""
        entry = SandboxDirectoryEntry(
            name="d", relative_path=Path("d"), kind="directory", size_bytes=None
        )
        self.assertIsNone(entry.size_bytes)

    def test_frozen(self):
        """frozen=True。"""
        entry = SandboxDirectoryEntry(
            name="a", relative_path=Path("a"), kind="file", size_bytes=1
        )
        with self.assertRaises(AttributeError):
            entry.name = "b"  # type: ignore[misc]

    def test_empty_name_rejected(self):
        """name 为空被拒绝。"""
        with self.assertRaises(TypeError):
            SandboxDirectoryEntry(
                name="", relative_path=Path("a"), kind="file", size_bytes=1
            )

    def test_absolute_relative_path_rejected(self):
        """relative_path 不得是绝对路径。"""
        with self.assertRaises(ValueError):
            SandboxDirectoryEntry(
                name="a", relative_path=Path("C:/x/a"), kind="file", size_bytes=1
            )

    def test_unknown_kind_rejected(self):
        """未知 kind 被拒绝。"""
        with self.assertRaises(ValueError):
            SandboxDirectoryEntry(
                name="a",
                relative_path=Path("a"),
                kind="socket",  # type: ignore[arg-type]
                size_bytes=None,
            )

    def test_non_file_with_size_rejected(self):
        """非 file 条目不能带 size_bytes。"""
        with self.assertRaises(ValueError):
            SandboxDirectoryEntry(
                name="d", relative_path=Path("d"), kind="directory", size_bytes=5
            )

    def test_negative_size_rejected(self):
        """负数 size 被拒绝。"""
        with self.assertRaises(ValueError):
            SandboxDirectoryEntry(
                name="a", relative_path=Path("a"), kind="file", size_bytes=-1
            )


class TestSandboxWriteResult(unittest.TestCase):
    """SandboxWriteResult 不变量测试。"""

    def test_valid(self):
        """正常创建。"""
        result = SandboxWriteResult(
            relative_path=Path("a.txt"), bytes_written=5, created=True
        )
        self.assertTrue(result.created)

    def test_frozen(self):
        """frozen=True。"""
        result = SandboxWriteResult(
            relative_path=Path("a.txt"), bytes_written=5, created=True
        )
        with self.assertRaises(AttributeError):
            result.bytes_written = 9  # type: ignore[misc]

    def test_absolute_path_rejected(self):
        """绝对路径被拒绝。"""
        with self.assertRaises(ValueError):
            SandboxWriteResult(
                relative_path=Path("C:/x/a.txt"), bytes_written=1, created=True
            )

    def test_non_bool_created_rejected(self):
        """created 必须是 bool。"""
        with self.assertRaises(TypeError):
            SandboxWriteResult(
                relative_path=Path("a.txt"),
                bytes_written=1,
                created="yes",  # type: ignore[arg-type]
            )


# --- read_text ---


class TestReadText(_SandboxTestCase):
    """read_text 测试。"""

    def test_plain_utf8(self):
        """普通 UTF-8 内容。"""
        self.write("a.txt", "hello")
        self.assertEqual(self.fs.read_text("a.txt"), "hello")

    def test_chinese(self):
        """中文内容。"""
        self.write("a.txt", "你好世界")
        self.assertEqual(self.fs.read_text("a.txt"), "你好世界")

    def test_multiline(self):
        """多行内容。"""
        self.write("a.txt", "line1\nline2\nline3")
        self.assertEqual(self.fs.read_text("a.txt"), "line1\nline2\nline3")

    def test_empty_file(self):
        """空文件返回空字符串。"""
        self.write("a.txt", "")
        self.assertEqual(self.fs.read_text("a.txt"), "")

    def test_nested_file(self):
        """嵌套目录中的文件。"""
        self.write("src/deep/a.txt", "nested")
        self.assertEqual(self.fs.read_text("src/deep/a.txt"), "nested")

    def test_accepts_path_object(self):
        """接受 Path 输入。"""
        self.write("a.txt", "x")
        self.assertEqual(self.fs.read_text(Path("a.txt")), "x")

    def test_missing_file(self):
        """文件不存在。"""
        with self.assertRaises(SandboxPathNotFoundError):
            self.fs.read_text("missing.txt")

    def test_target_is_directory(self):
        """目标是目录。"""
        (self.root / "d").mkdir()
        with self.assertRaises(SandboxNotADirectoryError):
            self.fs.read_text("d")

    def test_invalid_utf8(self):
        """非法 UTF-8 字节。"""
        (self.root / "bin.dat").write_bytes(b"\xff\xfe\x00bad")
        with self.assertRaises(SandboxFileEncodingError):
            self.fs.read_text("bin.dat")

    def test_exactly_at_limit(self):
        """文件大小恰好等于上限。"""
        fs = SandboxFileSystem(self.resolver, max_read_bytes=10)
        self.write("a.txt", "0123456789")
        self.assertEqual(fs.read_text("a.txt"), "0123456789")

    def test_exceeds_limit(self):
        """文件超过上限。"""
        fs = SandboxFileSystem(self.resolver, max_read_bytes=10)
        self.write("a.txt", "0123456789A")
        with self.assertRaises(SandboxFileTooLargeError):
            fs.read_text("a.txt")

    def test_actual_read_limit_guard(self):
        """stat 之后文件增长时，仍被实际读取长度拒绝。

        这是关键防护：不能只依赖 stat().st_size。
        """
        fs = SandboxFileSystem(self.resolver, max_read_bytes=10)
        self.write("a.txt", "0123456789ABCDEF")

        # 伪造 stat 返回较小的大小，模拟 stat 之后文件增长
        real_stat = Path.stat

        def fake_stat(self, *args, **kwargs):
            actual = real_stat(self, *args, **kwargs)
            if self.name == "a.txt":
                # 篡改 st_size
                return os.stat_result(
                    (actual.st_mode, 0, 0, 1, 0, 0, 5) + tuple(actual)[7:]
                )
            return actual

        with patch.object(Path, "stat", fake_stat):
            with self.assertRaises(SandboxFileTooLargeError):
                fs.read_text("a.txt")

    def test_path_traversal_rejected(self):
        """路径穿越。"""
        with self.assertRaises(SandboxPathViolationError):
            self.fs.read_text("../secret.txt")

    def test_windows_absolute_rejected(self):
        """Windows 绝对路径。"""
        with self.assertRaises(SandboxPathViolationError):
            self.fs.read_text("C:/Windows/System32/config/SAM")

    def test_symlink_inside_root(self):
        """指向 Root 内文件的符号链接可以读取。"""
        self.write("real.txt", "inside")
        try:
            self.make_symlink("link.txt", self.root / "real.txt")
        except (OSError, NotImplementedError):
            self.skipTest("符号链接不支持")
        self.assertEqual(self.fs.read_text("link.txt"), "inside")

    def test_symlink_outside_root_rejected(self):
        """指向 Root 外的符号链接被拒绝。"""
        outside_dir = Path(self._tmp.name).parent / "outside_16b"
        outside_dir.mkdir(exist_ok=True)
        self.addCleanup(_rmtree_ignore, outside_dir)
        (outside_dir / "secret.txt").write_text("secret", encoding="utf-8")

        try:
            self.make_symlink("escape", outside_dir)
        except (OSError, NotImplementedError):
            self.skipTest("符号链接不支持")

        with self.assertRaises(SandboxPathViolationError):
            self.fs.read_text("escape/secret.txt")

    def test_does_not_modify_file(self):
        """读取不修改文件。"""
        self.write("a.txt", "content")
        before = (self.root / "a.txt").stat().st_mtime_ns
        self.fs.read_text("a.txt")
        self.assertEqual((self.root / "a.txt").stat().st_mtime_ns, before)

    def test_does_not_create_missing_parent(self):
        """读取失败时不创建父目录。"""
        with self.assertRaises(SandboxPathNotFoundError):
            self.fs.read_text("nodir/a.txt")
        self.assertFalse((self.root / "nodir").exists())


# --- write_text ---


class TestWriteText(_SandboxTestCase):
    """write_text 测试。"""

    def test_new_file(self):
        """创建新文件。"""
        result = self.fs.write_text("a.txt", "hello")
        self.assertTrue(result.created)
        self.assertEqual(result.relative_path, Path("a.txt"))
        self.assertEqual((self.root / "a.txt").read_text(encoding="utf-8"), "hello")

    def test_empty_content(self):
        """空字符串创建空文件。"""
        result = self.fs.write_text("empty.txt", "")
        self.assertTrue(result.created)
        self.assertEqual(result.bytes_written, 0)
        self.assertEqual((self.root / "empty.txt").read_text(encoding="utf-8"), "")

    def test_chinese_byte_count(self):
        """中文按真实 UTF-8 字节计数。"""
        result = self.fs.write_text("a.txt", "中文")
        self.assertEqual(result.bytes_written, 6)

    def test_exactly_at_write_limit(self):
        """内容恰好等于上限。"""
        fs = SandboxFileSystem(self.resolver, max_write_bytes=5)
        result = fs.write_text("a.txt", "12345")
        self.assertEqual(result.bytes_written, 5)

    def test_exceeds_write_limit(self):
        """内容超过上限。"""
        fs = SandboxFileSystem(self.resolver, max_write_bytes=5)
        with self.assertRaises(SandboxFileTooLargeError):
            fs.write_text("a.txt", "123456")

    def test_exceeds_limit_creates_nothing(self):
        """超限时目标不存在（不创建文件）。"""
        fs = SandboxFileSystem(self.resolver, max_write_bytes=3)
        with self.assertRaises(SandboxFileTooLargeError):
            fs.write_text("big.txt", "123456")
        self.assertFalse((self.root / "big.txt").exists())

    def test_exceeds_limit_keeps_old_file(self):
        """超限时旧文件不变。"""
        self.write("a.txt", "old")
        fs = SandboxFileSystem(self.resolver, max_write_bytes=3)
        with self.assertRaises(SandboxFileTooLargeError):
            fs.write_text("a.txt", "123456")
        self.assertEqual((self.root / "a.txt").read_text(encoding="utf-8"), "old")

    def test_nested_target(self):
        """写入嵌套目标。"""
        (self.root / "src").mkdir()
        result = self.fs.write_text("src/a.txt", "x")
        self.assertEqual(result.relative_path, Path("src/a.txt"))

    def test_missing_parent(self):
        """父目录不存在时失败。"""
        with self.assertRaises(SandboxNotADirectoryError):
            self.fs.write_text("nodir/a.txt", "x")
        self.assertFalse((self.root / "nodir").exists())

    def test_target_is_directory(self):
        """目标是目录。"""
        (self.root / "d").mkdir()
        with self.assertRaises(SandboxNotAFileError):
            self.fs.write_text("d", "x")

    def test_existing_without_overwrite(self):
        """已存在且 overwrite=False。"""
        self.write("a.txt", "old")
        with self.assertRaises(SandboxFileAlreadyExistsError):
            self.fs.write_text("a.txt", "new")
        self.assertEqual((self.root / "a.txt").read_text(encoding="utf-8"), "old")

    def test_existing_with_overwrite(self):
        """已存在且 overwrite=True。"""
        self.write("a.txt", "old")
        result = self.fs.write_text("a.txt", "new", overwrite=True)
        self.assertFalse(result.created)
        self.assertEqual((self.root / "a.txt").read_text(encoding="utf-8"), "new")

    def test_content_must_be_str(self):
        """content 必须是 str。"""
        with self.assertRaises(TypeError):
            self.fs.write_text("a.txt", 123)  # type: ignore[arg-type]

    def test_overwrite_must_be_bool(self):
        """overwrite 必须是 bool。"""
        with self.assertRaises(TypeError):
            self.fs.write_text("a.txt", "x", overwrite="yes")  # type: ignore[arg-type]

    def test_path_traversal_rejected(self):
        """路径穿越。"""
        with self.assertRaises(SandboxPathViolationError):
            self.fs.write_text("../evil.txt", "x")

    def test_windows_absolute_rejected(self):
        """Windows 绝对路径。"""
        with self.assertRaises(SandboxPathViolationError):
            self.fs.write_text("C:/evil.txt", "x")

    def test_symlink_parent_escaping(self):
        """通过外部符号链接父目录写入被拒绝。"""
        outside_dir = Path(self._tmp.name).parent / "outside_16b_write"
        outside_dir.mkdir(exist_ok=True)
        self.addCleanup(_rmtree_ignore, outside_dir)

        try:
            self.make_symlink("escape", outside_dir)
        except (OSError, NotImplementedError):
            self.skipTest("符号链接不支持")

        with self.assertRaises(SandboxPathViolationError):
            self.fs.write_text("escape/evil.txt", "x")
        self.assertFalse((outside_dir / "evil.txt").exists())

    def test_atomic_no_temp_left_on_success(self):
        """成功后没有临时文件残留。"""
        self.fs.write_text("a.txt", "content")
        leftovers = [p.name for p in self.root.iterdir() if p.name != "a.txt"]
        self.assertEqual(leftovers, [])

    def test_write_failure_keeps_old_file(self):
        """写入失败时旧文件不变。

        在 Windows 上必须 mock handle.write 而非 os.fdopen，
        因为 fdopen 失败时 fd 未关闭，finally 清理时无法 unlink。
        """
        self.write("a.txt", "old")

        class FailingHandle:
            """在 __enter__ 中关闭 mkstemp 返回的 fd，write 时失败。"""

            def __init__(self, fd: int):
                self._fd = fd

            def __enter__(self):
                return self

            def __exit__(self, *args):
                os.close(self._fd)

            def write(self, data):
                raise OSError("模拟写入失败")

            def flush(self):
                pass

            def fileno(self):
                return self._fd

        def failing_fdopen(fd, mode="r", **kwargs):
            return FailingHandle(fd)

        self.write("a.txt", "old")

        with patch("os.fdopen", failing_fdopen):
            with self.assertRaises(SandboxFileError):
                self.fs.write_text("a.txt", "new", overwrite=True)

        self.assertEqual((self.root / "a.txt").read_text(encoding="utf-8"), "old")

    def test_replace_failure_keeps_old_file(self):
        """os.replace 失败时旧文件不变。"""
        self.write("a.txt", "old")

        with patch("os.replace", side_effect=OSError("模拟替换失败")):
            with self.assertRaises(SandboxFileError):
                self.fs.write_text("a.txt", "new", overwrite=True)

        self.assertEqual((self.root / "a.txt").read_text(encoding="utf-8"), "old")

    def test_replace_failure_removes_temp(self):
        """os.replace 失败后无临时文件残留。"""
        self.write("a.txt", "old")

        with patch("os.replace", side_effect=OSError("模拟替换失败")):
            with self.assertRaises(SandboxFileError):
                self.fs.write_text("a.txt", "new", overwrite=True)

        leftovers = [p.name for p in self.root.iterdir() if p.name != "a.txt"]
        self.assertEqual(leftovers, [])

    def test_does_not_modify_content_object(self):
        """不修改传入的 content。"""
        content = "original"
        self.fs.write_text("a.txt", content)
        self.assertEqual(content, "original")


# --- make_directory ---


class TestMakeDirectory(_SandboxTestCase):
    """make_directory 测试。"""

    def test_single_level(self):
        """单层创建。"""
        result = self.fs.make_directory("newdir")
        self.assertTrue((self.root / "newdir").is_dir())
        self.assertEqual(result.relative_path, Path("newdir"))

    def test_nested_without_parents_fails(self):
        """parents=False 时父目录不存在则失败。"""
        with self.assertRaises(SandboxNotADirectoryError):
            self.fs.make_directory("a/b")
        self.assertFalse((self.root / "a").exists())

    def test_parents_true(self):
        """parents=True 时创建缺失父目录。"""
        result = self.fs.make_directory("a/b/c", parents=True)
        self.assertTrue((self.root / "a" / "b" / "c").is_dir())
        self.assertEqual(result.relative_path, Path("a/b/c"))

    def test_exist_ok_false_on_existing(self):
        """exist_ok=False 时已存在目录失败。"""
        (self.root / "d").mkdir()
        with self.assertRaises(SandboxFileError):
            self.fs.make_directory("d")

    def test_exist_ok_true_on_existing(self):
        """exist_ok=True 时已存在目录成功。"""
        (self.root / "d").mkdir()
        result = self.fs.make_directory("d", exist_ok=True)
        self.assertEqual(result.relative_path, Path("d"))

    def test_target_is_file(self):
        """目标是普通文件。"""
        self.write("a.txt", "x")
        with self.assertRaises(SandboxNotADirectoryError):
            self.fs.make_directory("a.txt")

    def test_parents_must_be_bool(self):
        """parents 必须是 bool。"""
        with self.assertRaises(TypeError):
            self.fs.make_directory("d", parents="yes")  # type: ignore[arg-type]

    def test_exist_ok_must_be_bool(self):
        """exist_ok 必须是 bool。"""
        with self.assertRaises(TypeError):
            self.fs.make_directory("d", exist_ok=1)  # type: ignore[arg-type]

    def test_path_traversal_rejected(self):
        """路径穿越。"""
        with self.assertRaises(SandboxPathViolationError):
            self.fs.make_directory("../evil")

    def test_cannot_create_root(self):
        """不允许创建 Root 本身。"""
        with self.assertRaises(SandboxPathViolationError):
            self.fs.make_directory(".")

    def test_symlink_parent_escaping(self):
        """通过外部符号链接创建目录被拒绝。"""
        outside_dir = Path(self._tmp.name).parent / "outside_16b_mkdir"
        outside_dir.mkdir(exist_ok=True)
        self.addCleanup(_rmtree_ignore, outside_dir)

        try:
            self.make_symlink("escape", outside_dir)
        except (OSError, NotImplementedError):
            self.skipTest("符号链接不支持")

        with self.assertRaises(SandboxPathViolationError):
            self.fs.make_directory("escape/evil")
        self.assertFalse((outside_dir / "evil").exists())

    def test_returns_resolved_path(self):
        """返回 ResolvedSandboxPath。"""
        result = self.fs.make_directory("d")
        self.assertEqual(result.requested, "d")
        self.assertTrue(result.absolute_path.is_absolute())
        self.assertEqual(result.relative_path, Path("d"))


# --- list_directory ---


class TestListDirectory(_SandboxTestCase):
    """list_directory 测试。"""

    def test_empty_root(self):
        """空目录返回空列表。"""
        self.assertEqual(self.fs.list_directory(), [])

    def test_single_file(self):
        """单个文件。"""
        self.write("a.txt", "x")
        entries = self.fs.list_directory()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].name, "a.txt")
        self.assertEqual(entries[0].kind, "file")
        self.assertEqual(entries[0].size_bytes, 1)

    def test_mixed_kinds(self):
        """多种类型。"""
        self.write("file.txt", "x")
        (self.root / "dir").mkdir()
        entries = {e.name: e.kind for e in self.fs.list_directory()}
        self.assertEqual(entries["file.txt"], "file")
        self.assertEqual(entries["dir"], "directory")

    def test_chinese_names(self):
        """中文名称。"""
        self.write("说明.md", "内容")
        entries = self.fs.list_directory()
        self.assertEqual(entries[0].name, "说明.md")

    def test_hidden_files_included(self):
        """包含隐藏文件，不做静默过滤。"""
        self.write(".hidden", "x")
        self.write("visible.txt", "y")
        entries = {e.name for e in self.fs.list_directory()}
        self.assertIn(".hidden", entries)

    def test_not_recursive(self):
        """不递归。"""
        self.write("sub/deep.txt", "x")
        entries = self.fs.list_directory()
        self.assertEqual([e.name for e in entries], ["sub"])

    def test_subdirectory_listing(self):
        """列出子目录。"""
        self.write("sub/a.txt", "x")
        self.write("sub/b.txt", "y")
        entries = self.fs.list_directory("sub")
        self.assertEqual([e.name for e in entries], ["a.txt", "b.txt"])

    def test_subdirectory_relative_paths(self):
        """子目录条目的 relative_path 带前缀。"""
        self.write("sub/a.txt", "x")
        entries = self.fs.list_directory("sub")
        self.assertEqual(entries[0].relative_path, Path("sub/a.txt"))

    def test_stable_sort(self):
        """按 (casefold, name) 稳定排序。

        Windows 文件系统大小写不敏感，不能使用 a.txt/A.txt 共存。
        使用互不冲突的名称：b.txt, A.txt, c.txt, D.txt。
        """
        for name in ("b.txt", "A.txt", "c.txt", "D.txt"):
            self.write(name, "x")
        names = [e.name for e in self.fs.list_directory()]
        self.assertEqual(names, ["A.txt", "b.txt", "c.txt", "D.txt"])

    def test_returns_new_list(self):
        """返回新 list，不复用内部状态。"""
        self.write("a.txt", "x")
        first = self.fs.list_directory()
        second = self.fs.list_directory()
        self.assertIsNot(first, second)
        first.clear()
        self.assertEqual(len(second), 1)

    def test_no_absolute_paths(self):
        """不返回绝对路径。"""
        self.write("sub/a.txt", "x")
        entries = self.fs.list_directory("sub")
        self.assertFalse(entries[0].relative_path.is_absolute())

    def test_no_leaked_target(self):
        """不返回符号链接目标。"""
        outside_dir = Path(self._tmp.name).parent / "outside_16b_list"
        outside_dir.mkdir(exist_ok=True)
        self.addCleanup(_rmtree_ignore, outside_dir)
        (outside_dir / "secret.txt").write_text("s", encoding="utf-8")

        try:
            self.make_symlink("escape", outside_dir)
        except (OSError, NotImplementedError):
            self.skipTest("符号链接不支持")

        entries = self.fs.list_directory()
        escape = [e for e in entries if e.name == "escape"][0]
        self.assertEqual(escape.kind, "symlink")
        # 不泄露外部目标
        self.assertNotIn("outside_16b_list", str(escape.relative_path))

    def test_symlink_shown_as_symlink(self):
        """即使指向 Root 内，链接仍显示为 symlink。"""
        (self.root / "real").mkdir()
        try:
            self.make_symlink("link", self.root / "real")
        except (OSError, NotImplementedError):
            self.skipTest("符号链接不支持")
        entries = {e.name: e.kind for e in self.fs.list_directory()}
        self.assertEqual(entries["link"], "symlink")
        self.assertEqual(entries["real"], "directory")

    def test_symlink_has_no_size(self):
        """符号链接条目 size_bytes 为 None。"""
        (self.root / "real").mkdir()
        try:
            self.make_symlink("link", self.root / "real")
        except (OSError, NotImplementedError):
            self.skipTest("符号链接不支持")
        link = [e for e in self.fs.list_directory() if e.name == "link"][0]
        self.assertIsNone(link.size_bytes)

    def test_missing_directory(self):
        """目录不存在。"""
        with self.assertRaises(SandboxPathNotFoundError):
            self.fs.list_directory("missing")

    def test_target_is_file(self):
        """目标是文件。"""
        self.write("a.txt", "x")
        with self.assertRaises(SandboxNotADirectoryError):
            self.fs.list_directory("a.txt")

    def test_exceeds_max_list_entries(self):
        """条目数超限。"""
        fs = SandboxFileSystem(self.resolver, max_list_entries=2)
        for i in range(5):
            self.write(f"f{i}.txt", "x")
        with self.assertRaises(SandboxDirectoryTooLargeError):
            fs.list_directory()

    def test_at_max_list_entries(self):
        """条目数恰好等于上限。"""
        fs = SandboxFileSystem(self.resolver, max_list_entries=3)
        for i in range(3):
            self.write(f"f{i}.txt", "x")
        self.assertEqual(len(fs.list_directory()), 3)

    def test_no_truncation_on_limit(self):
        """超限时抛异常而非静默截断。"""
        fs = SandboxFileSystem(self.resolver, max_list_entries=1)
        for i in range(3):
            self.write(f"f{i}.txt", "x")
        try:
            result = fs.list_directory()
        except SandboxDirectoryTooLargeError:
            return
        self.fail(f"应抛异常而不是返回截断列表: {result}")

    def test_path_traversal_rejected(self):
        """路径穿越。"""
        with self.assertRaises(SandboxPathViolationError):
            self.fs.list_directory("../")

    def test_dot_rejected(self):
        """不接受 "." 绕过 16A 规则。"""
        with self.assertRaises(SandboxPathViolationError):
            self.fs.list_directory(".")

    def test_does_not_read_file_contents(self):
        """列目录不读取文件内容。"""
        # 非法 UTF-8 的文件也能被列出
        (self.root / "bin.dat").write_bytes(b"\xff\xfe\x00")
        entries = self.fs.list_directory()
        self.assertEqual(entries[0].kind, "file")


# --- 异常层次 ---


class TestExceptionHierarchy(unittest.TestCase):
    """文件异常继承关系。"""

    def test_all_inherit_base(self):
        """所有文件异常继承 SandboxFileError。"""
        for cls in (
            SandboxNotAFileError,
            SandboxNotADirectoryError,
            SandboxFileTooLargeError,
            SandboxFileAlreadyExistsError,
            SandboxFileEncodingError,
            SandboxDirectoryTooLargeError,
        ):
            with self.subTest(cls.__name__):
                self.assertTrue(issubclass(cls, SandboxFileError))

    def test_path_errors_not_wrapped(self):
        """路径异常不是文件异常的子类（不应被包装混淆）。"""
        self.assertFalse(issubclass(SandboxPathViolationError, SandboxFileError))


if __name__ == "__main__":
    unittest.main()
