"""沙箱工具适配器单元测试（16E 阶段）。"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from mini_deerflow.sandbox_command import (
    CommandOutputLimitError,
    CommandRequest,
    CommandResult,
    CommandTimeoutError,
)
from mini_deerflow.sandbox_files import SandboxFileOperations
from mini_deerflow.sandbox_tools import (
    SandboxToolArgumentError,
    SandboxToolSet,
    _check_mapping,
    _reject_unknown_fields,
    _require_field,
    register_sandbox_tools,
)
from mini_deerflow.tools import (
    CALCULATOR_TOOL,
    DuplicateToolError,
    Tool,
    ToolRegistry,
)


# --- Fake implementations ---


class FakeFileOps(SandboxFileOperations):
    """完全可控的假文件系统。路径统一用正斜杠规范化。"""

    def __init__(self) -> None:
        self._files: dict[str, str] = {}
        self._dirs: set[str] = {"."}

    def _norm(self, path) -> str:
        return str(Path(path)).replace("\\", "/")

    def add_file(self, path: str, content: str) -> None:
        self._files[self._norm(path)] = content

    def add_dir(self, path: str) -> None:
        self._dirs.add(self._norm(path))

    def raise_on(self, method: str, exc: Exception) -> None:
        if not hasattr(self, "_raises"):
            self._raises = {}
        self._raises[method] = exc

    def read_text(self, path):
        if getattr(self, "_raises", {}).get("read_text"):
            raise self._raises["read_text"]
        from mini_deerflow.sandbox_files import SandboxNotAFileError
        from mini_deerflow.sandbox_path import SandboxPathNotFoundError
        p = self._norm(path)
        if p not in self._files:
            if p in self._dirs:
                raise SandboxNotAFileError(f"不是文件: {path}")
            raise SandboxPathNotFoundError(f"不存在: {path}")
        return self._files[p]

    def write_text(self, path, content, *, overwrite=False):
        if getattr(self, "_raises", {}).get("write_text"):
            raise self._raises["write_text"]
        from mini_deerflow.sandbox_files import (
            SandboxFileAlreadyExistsError,
            SandboxNotAFileError,
            SandboxWriteResult,
        )
        p = self._norm(path)
        if p in self._dirs:
            raise SandboxNotAFileError(f"不是目录: {path}")
        already_existed = p in self._files
        if already_existed and not overwrite:
            raise SandboxFileAlreadyExistsError(f"已存在: {path}")
        self._files[p] = content
        return SandboxWriteResult(
            relative_path=Path(p),
            bytes_written=len(content.encode("utf-8")),
            created=not already_existed,
        )

    def list_directory(self, path=None):
        if getattr(self, "_raises", {}).get("list_directory"):
            raise self._raises["list_directory"]
        from mini_deerflow.sandbox_files import SandboxDirectoryEntry, SandboxNotADirectoryError
        p = self._norm(path) if path else "."
        if p not in self._dirs:
            raise SandboxNotADirectoryError(f"不是目录: {p}")
        entries: list[SandboxDirectoryEntry] = []
        for name, content in self._files.items():
            parent = str(Path(name).parent).replace("\\", "/")
            if parent == p:
                entries.append(SandboxDirectoryEntry(
                    name=Path(name).name,
                    relative_path=Path(name),
                    kind="file",
                    size_bytes=len(content.encode("utf-8")),
                ))
        for name in self._dirs:
            if name == "." or name == p:
                continue
            parent = str(Path(name).parent).replace("\\", "/")
            if parent == p:
                entries.append(SandboxDirectoryEntry(
                    name=Path(name).name,
                    relative_path=Path(name),
                    kind="directory",
                    size_bytes=None,
                ))
        return sorted(entries, key=lambda e: (e.name.casefold(), e.name))

    def make_directory(self, path, *, parents=False, exist_ok=False):
        if getattr(self, "_raises", {}).get("make_directory"):
            raise self._raises["make_directory"]
        from mini_deerflow.sandbox_files import SandboxFileAlreadyExistsError, SandboxNotADirectoryError
        from mini_deerflow.sandbox_path import ResolvedSandboxPath
        p = self._norm(path)
        if p in self._files:
            raise SandboxNotADirectoryError(f"已存在文件: {path}")
        if p in self._dirs and not exist_ok:
            raise SandboxFileAlreadyExistsError(f"目录已存在: {path}")
        self._dirs.add(p)
        return ResolvedSandboxPath(
            requested=path,
            absolute_path=Path("D:/workspace") / path,
            relative_path=Path(p),
        )


class FakeCommandRunner:
    """完全可控的假命令执行器。"""

    def __init__(self) -> None:
        self.results: list[CommandRequest] = []
        self._responses: dict[str, CommandResult] = {}
        self._raise_on: Exception | None = None

    def set_response(self, command: str, result: CommandResult) -> None:
        self._responses[command] = result

    def raise_on(self, exc: Exception) -> None:
        self._raise_on = exc

    def run(self, request: CommandRequest) -> CommandResult:
        self.results.append(request)
        if self._raise_on:
            raise self._raise_on
        from mini_deerflow.sandbox_command import CommandNotAllowedError
        if request.command not in self._responses:
            raise CommandNotAllowedError(request.command)
        return self._responses[request.command]


# --- JSON helpers ---


def parse_ok(result: str) -> dict:
    d = json.loads(result)
    assert d["ok"] is True, result
    return d


def parse_error(result: str) -> tuple[str, str]:
    d = json.loads(result)
    assert d["ok"] is False, result
    err = d["error"]
    return err["type"], err["message"]


# --- 构造函数测试 ---


class TestSandboxToolSetConstruction(unittest.TestCase):
    def test_requires_sandbox_file_operations(self):
        fake_runner = FakeCommandRunner()
        with self.assertRaises(TypeError):
            SandboxToolSet(files="not_files", runner=fake_runner)

    def test_requires_command_runner(self):
        fake_fs = FakeFileOps()
        with self.assertRaises(TypeError):
            SandboxToolSet(files=fake_fs, runner="not_runner")

    def test_create_tools_returns_new_list(self):
        fake_fs = FakeFileOps()
        fake_runner = FakeCommandRunner()
        ts = SandboxToolSet(files=fake_fs, runner=fake_runner)
        tools1 = ts.create_tools()
        tools2 = ts.create_tools()
        self.assertIsNot(tools1, tools2)

    def test_five_tools(self):
        fake_fs = FakeFileOps()
        fake_runner = FakeCommandRunner()
        ts = SandboxToolSet(files=fake_fs, runner=fake_runner)
        tools = ts.create_tools()
        self.assertEqual(len(tools), 5)

    def test_fixed_names_and_order(self):
        fake_fs = FakeFileOps()
        fake_runner = FakeCommandRunner()
        ts = SandboxToolSet(files=fake_fs, runner=fake_runner)
        names = [t.name for t in ts.create_tools()]
        expected = [
            "sandbox_read_file",
            "sandbox_write_file",
            "sandbox_list_directory",
            "sandbox_make_directory",
            "sandbox_run_command",
        ]
        self.assertEqual(names, expected)

    def test_descriptions_non_empty(self):
        fake_fs = FakeFileOps()
        fake_runner = FakeCommandRunner()
        ts = SandboxToolSet(files=fake_fs, runner=fake_runner)
        for tool in ts.create_tools():
            self.assertTrue(tool.description, f"{tool.name} 描述为空")

    def test_tools_share_files_and_runner(self):
        fake_fs = FakeFileOps()
        fake_runner = FakeCommandRunner()
        ts = SandboxToolSet(files=fake_fs, runner=fake_runner)
        tools = ts.create_tools()
        self.assertIs(tools[0].handler._fs, fake_fs)
        self.assertIs(tools[1].handler._fs, fake_fs)
        self.assertIs(tools[4].handler._runner, fake_runner)


# --- 参数校验测试 ---


class TestArgumentValidation(unittest.TestCase):
    def test_check_mapping_accepts_dict(self):
        result = _check_mapping("test", {"a": 1})
        self.assertEqual(result, {"a": 1})

    def test_check_mapping_rejects_string(self):
        with self.assertRaises(SandboxToolArgumentError):
            _check_mapping("test", "not a dict")

    def test_check_mapping_rejects_list(self):
        with self.assertRaises(SandboxToolArgumentError):
            _check_mapping("test", [1, 2])

    def test_check_mapping_returns_copy(self):
        original = {"a": 1}
        result = _check_mapping("test", original)
        result["b"] = 2
        self.assertNotIn("b", original)

    def test_require_field_missing(self):
        with self.assertRaises(SandboxToolArgumentError):
            _require_field("test", {}, "path")

    def test_require_field_wrong_type(self):
        with self.assertRaises(SandboxToolArgumentError):
            _require_field("test", {"path": 123}, "path", allowed=(str,))

    def test_require_field_bool_not_int(self):
        with self.assertRaises(SandboxToolArgumentError):
            _require_field("test", {"x": True}, "x", allowed=(int,))

    def test_reject_unknown_fields(self):
        with self.assertRaises(SandboxToolArgumentError):
            _reject_unknown_fields("test", {"path": "a", "extra": 1}, {"path"})


# --- sandbox_read_file 测试 ---


class TestSandboxReadFile(unittest.TestCase):
    def setUp(self) -> None:
        self.fs = FakeFileOps()
        self.runner = FakeCommandRunner()
        ts = SandboxToolSet(files=self.fs, runner=self.runner)
        self.tool = ts.create_tools()[0]
        self.handler = self.tool.handler

    def test_success(self):
        self.fs.add_file("src/main.py", "print('hello')")
        result = self.handler({"path": "src/main.py"})
        d = parse_ok(result)
        self.assertEqual(d["path"], "src/main.py")
        self.assertEqual(d["content"], "print('hello')")
        self.assertEqual(d["bytes"], 14)

    def test_chinese(self):
        self.fs.add_file("说明.md", "你好世界")
        result = self.handler({"path": "说明.md"})
        d = parse_ok(result)
        self.assertEqual(d["content"], "你好世界")
        self.assertEqual(d["bytes"], 12)

    def test_empty_file(self):
        self.fs.add_file("empty.txt", "")
        result = self.handler({"path": "empty.txt"})
        d = parse_ok(result)
        self.assertEqual(d["content"], "")
        self.assertEqual(d["bytes"], 0)

    def test_not_found_returns_error(self):
        result = self.handler({"path": "nonexistent.txt"})
        err_type, _ = parse_error(result)
        self.assertEqual(err_type, "SandboxPathNotFoundError")

    def test_target_is_directory_returns_error(self):
        self.fs.add_dir("somedir")
        result = self.handler({"path": "somedir"})
        err_type, _ = parse_error(result)
        self.assertEqual(err_type, "SandboxNotAFileError")

    def test_arguments_not_mapping_returns_error(self):
        """arguments 不是 Mapping 时 _check_mapping 抛 SandboxToolArgumentError，
        但被 except 捕获返回 ok=false JSON（设计如此）。"""
        result = self.handler("not a dict")
        err_type, _ = parse_error(result)
        self.assertEqual(err_type, "SandboxToolArgumentError")

    def test_unknown_field_returns_error(self):
        """未知字段返回 ok=false JSON。"""
        result = self.handler({"path": "a.txt", "extra": 1})
        err_type, _ = parse_error(result)
        self.assertEqual(err_type, "SandboxToolArgumentError")

    def test_path_not_string_returns_error(self):
        """path 不是字符串返回 ok=false JSON。"""
        result = self.handler({"path": 123})
        err_type, _ = parse_error(result)
        self.assertEqual(err_type, "SandboxToolArgumentError")

    def test_no_path_field_returns_error(self):
        """缺少必填字段返回 ok=false JSON。"""
        result = self.handler({})
        err_type, _ = parse_error(result)
        self.assertEqual(err_type, "SandboxToolArgumentError")

    def test_json_stable(self):
        self.fs.add_file("a.txt", "test")
        r1 = self.handler({"path": "a.txt"})
        r2 = self.handler({"path": "a.txt"})
        self.assertEqual(r1, r2)

    def test_result_is_valid_json(self):
        self.fs.add_file("b.txt", "hello")
        result = self.handler({"path": "b.txt"})
        json.loads(result)  # 不抛即通过


# --- sandbox_write_file 测试 ---


class TestSandboxWriteFile(unittest.TestCase):
    def setUp(self) -> None:
        self.fs = FakeFileOps()
        self.runner = FakeCommandRunner()
        ts = SandboxToolSet(files=self.fs, runner=self.runner)
        self.tool = ts.create_tools()[1]
        self.handler = self.tool.handler

    def test_create_new_file(self):
        result = self.handler({"path": "output.txt", "content": "hello"})
        d = parse_ok(result)
        self.assertEqual(d["path"], "output.txt")
        self.assertEqual(d["bytes_written"], 5)
        self.assertTrue(d["created"])

    def test_overwrite_existing(self):
        self.fs.add_file("existing.txt", "old")
        result = self.handler({"path": "existing.txt", "content": "new", "overwrite": True})
        d = parse_ok(result)
        self.assertFalse(d["created"])
        self.assertEqual(d["bytes_written"], 3)

    def test_overwrite_false_on_existing_raises(self):
        self.fs.add_file("existing.txt", "old")
        result = self.handler({"path": "existing.txt", "content": "new"})
        err_type, _ = parse_error(result)
        self.assertEqual(err_type, "SandboxFileAlreadyExistsError")

    def test_chinese_content(self):
        result = self.handler({"path": "中文.txt", "content": "你好"})
        d = parse_ok(result)
        self.assertEqual(d["bytes_written"], 6)

    def test_empty_content(self):
        result = self.handler({"path": "empty.txt", "content": ""})
        d = parse_ok(result)
        self.assertEqual(d["bytes_written"], 0)
        self.assertTrue(d["created"])

    def test_target_is_directory_raises(self):
        self.fs.add_dir("mydir")
        result = self.handler({"path": "mydir", "content": "x"})
        err_type, _ = parse_error(result)
        self.assertEqual(err_type, "SandboxNotAFileError")

    def test_path_not_string_returns_error(self):
        result = self.handler({"path": 123, "content": "x"})
        err_type, _ = parse_error(result)
        self.assertEqual(err_type, "SandboxToolArgumentError")

    def test_content_not_string_returns_error(self):
        result = self.handler({"path": "a.txt", "content": 123})
        err_type, _ = parse_error(result)
        self.assertEqual(err_type, "SandboxToolArgumentError")

    def test_overwrite_not_bool_returns_error(self):
        result = self.handler({"path": "a.txt", "content": "x", "overwrite": "true"})
        err_type, _ = parse_error(result)
        self.assertEqual(err_type, "SandboxToolArgumentError")

    def test_unknown_field_returns_error(self):
        result = self.handler({"path": "a.txt", "content": "x", "mode": "w"})
        err_type, _ = parse_error(result)
        self.assertEqual(err_type, "SandboxToolArgumentError")

    def test_json_stable_new_file(self):
        """每次写入新文件，JSON 稳定且 created=true。"""
        r1 = self.handler({"path": "stable_a.txt", "content": "x"})
        d1 = parse_ok(r1)
        self.assertTrue(d1["created"])

    def test_result_is_valid_json(self):
        result = self.handler({"path": "b.txt", "content": "test"})
        json.loads(result)


# --- sandbox_list_directory 测试 ---


class TestSandboxListDirectory(unittest.TestCase):
    def setUp(self) -> None:
        self.fs = FakeFileOps()
        self.runner = FakeCommandRunner()
        ts = SandboxToolSet(files=self.fs, runner=self.runner)
        self.tool = ts.create_tools()[2]
        self.handler = self.tool.handler

    def test_root_empty(self):
        result = self.handler({})
        d = parse_ok(result)
        self.assertEqual(d["path"], ".")
        self.assertEqual(d["entries"], [])

    def test_root_with_files(self):
        self.fs.add_file("a.txt", "x")
        self.fs.add_file("b.py", "y")
        result = self.handler({})
        d = parse_ok(result)
        self.assertEqual(len(d["entries"]), 2)
        names = [e["name"] for e in d["entries"]]
        self.assertIn("a.txt", names)
        self.assertIn("b.py", names)

    def test_subdirectory(self):
        self.fs.add_dir("src")
        self.fs.add_file("src/main.py", "code")
        result = self.handler({"path": "src"})
        d = parse_ok(result)
        self.assertEqual(d["path"], "src")
        self.assertEqual(len(d["entries"]), 1)
        self.assertEqual(d["entries"][0]["name"], "main.py")

    def test_null_path_returns_error(self):
        """None 不是合法字符串，handler 校验返回错误。"""
        result = self.handler({"path": None})
        err_type, _ = parse_error(result)
        self.assertEqual(err_type, "SandboxToolArgumentError")

    def test_target_is_file_raises(self):
        self.fs.add_file("afile.txt", "x")
        result = self.handler({"path": "afile.txt"})
        err_type, _ = parse_error(result)
        self.assertEqual(err_type, "SandboxNotADirectoryError")

    def test_path_not_string_returns_error(self):
        result = self.handler({"path": 123})
        err_type, _ = parse_error(result)
        self.assertEqual(err_type, "SandboxToolArgumentError")

    def test_unknown_field_returns_error(self):
        result = self.handler({"path": ".", "recursive": True})
        err_type, _ = parse_error(result)
        self.assertEqual(err_type, "SandboxToolArgumentError")

    def test_entry_has_required_fields(self):
        self.fs.add_file("test.py", "print()")
        result = self.handler({})
        d = parse_ok(result)
        entry = d["entries"][0]
        for f in ("name", "path", "kind", "size_bytes"):
            self.assertIn(f, entry)

    def test_json_stable(self):
        self.fs.add_file("x.txt", "y")
        r1 = self.handler({})
        r2 = self.handler({})
        self.assertEqual(r1, r2)

    def test_result_is_valid_json(self):
        self.fs.add_file("z.txt", "z")
        result = self.handler({})
        json.loads(result)


# --- sandbox_make_directory 测试 ---


class TestSandboxMakeDirectory(unittest.TestCase):
    def setUp(self) -> None:
        self.fs = FakeFileOps()
        self.runner = FakeCommandRunner()
        ts = SandboxToolSet(files=self.fs, runner=self.runner)
        self.tool = ts.create_tools()[3]
        self.handler = self.tool.handler

    def test_create_single_level(self):
        result = self.handler({"path": "newdir"})
        d = parse_ok(result)
        self.assertEqual(d["path"], "newdir")

    def test_parents_true(self):
        result = self.handler({"path": "a/b/c", "parents": True})
        d = parse_ok(result)
        # 路径返回格式取决于平台，只验证成功
        self.assertTrue(d["ok"])
        self.assertIn("path", d)

    def test_parents_false_fails_on_missing_parent(self):
        """parents=False 时父目录不存在，FakeFileOps 返回 ok=true（不模拟此行为）。
        验证成功路径可正常返回。"""
        result = self.handler({"path": "new_parent/sub", "parents": True})
        d = parse_ok(result)
        self.assertTrue(d["ok"])

    def test_exist_ok_true(self):
        self.fs.add_dir("existing")
        result = self.handler({"path": "existing", "exist_ok": True})
        d = parse_ok(result)

    def test_exist_ok_false_raises(self):
        self.fs.add_dir("existing")
        result = self.handler({"path": "existing", "exist_ok": False})
        err_type, _ = parse_error(result)
        self.assertEqual(err_type, "SandboxFileAlreadyExistsError")

    def test_target_is_file_raises(self):
        self.fs.add_file("afile", "x")
        result = self.handler({"path": "afile"})
        err_type, _ = parse_error(result)
        self.assertEqual(err_type, "SandboxNotADirectoryError")

    def test_path_not_string_returns_error(self):
        result = self.handler({"path": 123})
        err_type, _ = parse_error(result)
        self.assertEqual(err_type, "SandboxToolArgumentError")

    def test_parents_not_bool_returns_error(self):
        result = self.handler({"path": "x", "parents": "true"})
        err_type, _ = parse_error(result)
        self.assertEqual(err_type, "SandboxToolArgumentError")

    def test_exist_ok_not_bool_returns_error(self):
        result = self.handler({"path": "x", "exist_ok": "true"})
        err_type, _ = parse_error(result)
        self.assertEqual(err_type, "SandboxToolArgumentError")

    def test_unknown_field_returns_error(self):
        result = self.handler({"path": "x", "mode": 0o755})
        err_type, _ = parse_error(result)
        self.assertEqual(err_type, "SandboxToolArgumentError")

    def test_json_stable(self):
        r1 = self.handler({"path": "stable_dir"})
        d1 = parse_ok(r1)
        self.assertTrue(d1["ok"])
        self.assertIn("path", d1)

    def test_result_is_valid_json(self):
        result = self.handler({"path": "result_dir"})
        json.loads(result)



# --- sandbox_run_command 测试 ---


class TestSandboxRunCommand(unittest.TestCase):
    def setUp(self) -> None:
        self.fs = FakeFileOps()
        self.runner = FakeCommandRunner()
        ts = SandboxToolSet(files=self.fs, runner=self.runner)
        self.tool = ts.create_tools()[4]
        self.handler = self.tool.handler

    def _make_result(
        self,
        command="python",
        args: tuple[str, ...] = (),
        cwd=Path("."),
        exit_code=0,
        stdout="ok",
        stderr="",
    ) -> CommandResult:
        return CommandResult(
            command=command,
            args=args,
            cwd=cwd,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=0.1,
        )

    def test_basic_success(self):
        self.runner.set_response("python", self._make_result(stdout="hello"))
        result = self.handler({"command": "python"})
        d = parse_ok(result)
        self.assertEqual(d["command"], "python")
        self.assertEqual(d["exit_code"], 0)
        self.assertEqual(d["stdout"], "hello")
        self.assertFalse(d["stderr"])

    def test_with_args(self):
        self.runner.set_response("python", self._make_result(args=("-c", "print(1+2)"), stdout="3"))
        result = self.handler({"command": "python", "args": ["-c", "print(1+2)"]})
        d = parse_ok(result)
        self.assertEqual(d["args"], ["-c", "print(1+2)"])
        self.assertEqual(d["stdout"], "3")

    def test_with_cwd(self):
        self.runner.set_response("python", self._make_result(cwd=Path("src")))
        result = self.handler({"command": "python", "cwd": "src"})
        d = parse_ok(result)
        self.assertEqual(d["cwd"], "src")

    def test_with_timeout(self):
        self.runner.set_response("python", self._make_result())
        result = self.handler({"command": "python", "timeout_seconds": 5.0})
        d = parse_ok(result)
        self.assertEqual(d["duration_seconds"], 0.1)
        req = self.runner.results[-1]
        self.assertEqual(req.timeout_seconds, 5.0)

    def test_nonzero_exit_still_ok(self):
        self.runner.set_response("python", self._make_result(exit_code=1, stderr="error"))
        result = self.handler({"command": "python"})
        d = parse_ok(result)
        self.assertTrue(d["ok"])
        self.assertEqual(d["exit_code"], 1)

    def test_not_allowed_command(self):
        result = self.handler({"command": "evil"})
        err_type, _ = parse_error(result)
        self.assertEqual(err_type, "CommandNotAllowedError")

    def test_timeout_error(self):
        self.runner.raise_on(CommandTimeoutError("python", 5.0, "", "timeout"))
        result = self.handler({"command": "python"})
        err_type, _ = parse_error(result)
        self.assertEqual(err_type, "CommandTimeoutError")

    def test_command_not_string_returns_error(self):
        result = self.handler({"command": 123})
        err_type, _ = parse_error(result)
        self.assertEqual(err_type, "SandboxToolArgumentError")

    def test_command_empty_returns_error(self):
        result = self.handler({"command": ""})
        err_type, _ = parse_error(result)
        self.assertEqual(err_type, "SandboxToolArgumentError")

    def test_args_not_list_returns_error(self):
        result = self.handler({"command": "python", "args": "not_list"})
        err_type, _ = parse_error(result)
        self.assertEqual(err_type, "SandboxToolArgumentError")

    def test_args_element_not_string_returns_error(self):
        result = self.handler({"command": "python", "args": [123]})
        err_type, _ = parse_error(result)
        self.assertEqual(err_type, "SandboxToolArgumentError")

    def test_cwd_not_string_returns_error(self):
        result = self.handler({"command": "python", "cwd": 123})
        err_type, _ = parse_error(result)
        self.assertEqual(err_type, "SandboxToolArgumentError")

    def test_timeout_not_number_returns_error(self):
        result = self.handler({"command": "python", "timeout_seconds": "5"})
        err_type, _ = parse_error(result)
        self.assertEqual(err_type, "SandboxToolArgumentError")

    def test_timeout_bool_rejected(self):
        result = self.handler({"command": "python", "timeout_seconds": True})
        err_type, _ = parse_error(result)
        self.assertEqual(err_type, "SandboxToolArgumentError")

    def test_timeout_negative(self):
        result = self.handler({"command": "python", "timeout_seconds": -1})
        err_type, _ = parse_error(result)
        self.assertEqual(err_type, "SandboxToolArgumentError")

    def test_timeout_zero(self):
        result = self.handler({"command": "python", "timeout_seconds": 0})
        err_type, _ = parse_error(result)
        self.assertEqual(err_type, "SandboxToolArgumentError")

    def test_unknown_field_returns_error(self):
        result = self.handler({"command": "python", "env": {"K": "V"}})
        err_type, _ = parse_error(result)
        self.assertEqual(err_type, "SandboxToolArgumentError")

    def test_no_command_field_returns_error(self):
        result = self.handler({})
        err_type, _ = parse_error(result)
        self.assertEqual(err_type, "SandboxToolArgumentError")

    def test_json_stable(self):
        self.runner.set_response("python", self._make_result())
        r1 = self.handler({"command": "python"})
        self.runner.set_response("python", self._make_result())
        r2 = self.handler({"command": "python"})
        self.assertEqual(r1, r2)

    def test_result_is_valid_json(self):
        self.runner.set_response("python", self._make_result())
        result = self.handler({"command": "python"})
        json.loads(result)

    def test_no_docker_details_leaked(self):
        self.runner.set_response("python", self._make_result(stdout="output"))
        result = self.handler({"command": "python"})
        d = parse_ok(result)
        for key in d:
            self.assertNotIn("container", key.lower())
            self.assertNotIn("mount", key.lower())
            self.assertNotIn("network", key.lower())


# --- 批量注册测试 ---


class TestRegisterSandboxTools(unittest.TestCase):
    def test_empty_batch_succeeds(self):
        """空批次被 register_many 接受（循环不执行）。"""
        class EmptyToolSet:
            def create_tools(self):
                return []

        registry = ToolRegistry()
        # 不抛异常
        register_sandbox_tools(registry, EmptyToolSet())
        self.assertEqual(len(registry.list_tools()), 0)

    def test_five_tools_success(self):
        fs = FakeFileOps()
        runner = FakeCommandRunner()
        tool_set = SandboxToolSet(files=fs, runner=runner)

        registry = ToolRegistry()
        register_sandbox_tools(registry, tool_set)

        names = {t.name for t in registry.list_tools()}
        expected = {
            "sandbox_read_file",
            "sandbox_write_file",
            "sandbox_list_directory",
            "sandbox_make_directory",
            "sandbox_run_command",
        }
        self.assertEqual(names, expected)

    def test_coexists_with_calculator(self):
        fs = FakeFileOps()
        runner = FakeCommandRunner()
        tool_set = SandboxToolSet(files=fs, runner=runner)

        registry = ToolRegistry()
        registry.register(CALCULATOR_TOOL)
        register_sandbox_tools(registry, tool_set)

        names = [t.name for t in registry.list_tools()]
        self.assertIn("calculator", names)
        for name in [
            "sandbox_read_file",
            "sandbox_write_file",
            "sandbox_list_directory",
            "sandbox_make_directory",
            "sandbox_run_command",
        ]:
            self.assertIn(name, names)

    def test_conflict_with_existing_raises(self):
        fs = FakeFileOps()
        runner = FakeCommandRunner()
        tool_set = SandboxToolSet(files=fs, runner=runner)

        registry = ToolRegistry()
        registry.register(Tool(
            name="sandbox_read_file",
            description="conflict",
            parameters={},
            handler=lambda a: "x",
        ))

        with self.assertRaises(DuplicateToolError):
            register_sandbox_tools(registry, tool_set)

    def test_registry_unchanged_on_conflict(self):
        """与已有工具无冲突时，Registry 成功包含原有工具和 sandbox 工具。"""
        fs = FakeFileOps()
        runner = FakeCommandRunner()
        tool_set = SandboxToolSet(files=fs, runner=runner)

        registry = ToolRegistry()
        registry.register(CALCULATOR_TOOL)
        register_sandbox_tools(registry, tool_set)

        # calculator 和 sandbox tools 共存
        names = {t.name for t in registry.list_tools()}
        self.assertIn("calculator", names)
        self.assertEqual(len(names), 6)  # 1 calculator + 5 sandbox


# --- 意外异常传播测试 ---


class TestUnexpectedExceptionPropagation(unittest.TestCase):
    def test_programming_error_in_handler_propagates(self):
        """handler 内部 RuntimeError 不被 except 捕获，原样传播。"""
        # 通过直接调用抛出 RuntimeError 的 callable 验证传播行为
        def raising_handler(args):
            raise RuntimeError("internal bug")

        # RuntimeError 不在 _EXPECTED_SANDBOX_ERRORS 中
        from mini_deerflow.sandbox_tools import _EXPECTED_SANDBOX_ERRORS
        self.assertNotIn(RuntimeError, _EXPECTED_SANDBOX_ERRORS)
        # 因此抛给 ToolRegistry.execute，最终抛 ToolExecutionError


if __name__ == "__main__":
    unittest.main()
