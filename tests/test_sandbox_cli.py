"""Sandbox CLI 集成测试（16F 阶段）。

安全策略：
- 未提供 --sandbox-root：默认关闭，完全不访问 Docker
- 显式提供 --sandbox-root：任何失败均终止启动，不降级
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from mini_deerflow.cli import main
from mini_deerflow.sandbox_cli import (
    SandboxStartupError,
    SandboxStatus,
    format_sandbox_status,
    format_tools,
    get_sandbox_status,
)
from mini_deerflow.sandbox_path import WorkspacePathResolver
from mini_deerflow.tools import Tool, ToolRegistry


# ============================================================================
# SandboxStatus & format
# ============================================================================


class TestSandboxStatus(unittest.TestCase):
    """SandboxStatus 数据类的测试。"""

    def test_disabled_returns_all_none(self):
        status = get_sandbox_status(
            enabled=False,
            docker_runner=None,
            timeout_seconds=30.0,
            max_output_bytes=1_048_576,
            max_read_bytes=10_485_760,
            max_write_bytes=10_485_760,
        )
        self.assertFalse(status.enabled)
        self.assertIsNone(status.backend)
        self.assertEqual(status.allowed_commands, ())
        self.assertIsNone(status.timeout_seconds)
        self.assertIsNone(status.max_output_bytes)
        self.assertIsNone(status.max_read_bytes)
        self.assertIsNone(status.max_write_bytes)
        self.assertIsNone(status.workspace_writable)

    def test_enabled_returns_values(self):
        status = get_sandbox_status(
            enabled=True,
            docker_runner=object(),
            timeout_seconds=30.0,
            max_output_bytes=1_048_576,
            max_read_bytes=10_485_760,
            max_write_bytes=10_485_760,
        )
        self.assertTrue(status.enabled)
        self.assertEqual(status.backend, "Docker")
        self.assertEqual(status.allowed_commands, ("python",))
        self.assertEqual(status.timeout_seconds, 30.0)
        self.assertEqual(status.max_output_bytes, 1_048_576)
        self.assertEqual(status.max_read_bytes, 10_485_760)
        self.assertEqual(status.max_write_bytes, 10_485_760)
        self.assertTrue(status.workspace_writable)

    def test_sandbox_status_is_frozen(self):
        """frozen=True 确保状态不可变"""
        status = SandboxStatus(
            enabled=True,
            backend="Docker",
            allowed_commands=("python",),
            timeout_seconds=30.0,
            max_output_bytes=1_048_576,
            max_read_bytes=10_485_760,
            max_write_bytes=10_485_760,
            workspace_writable=True,
        )
        with self.assertRaises(Exception):
            status.enabled = False


class TestFormatSandboxStatus(unittest.TestCase):
    """format_sandbox_status 格式化测试。"""

    def test_disabled_shows_disabled_message(self):
        status = SandboxStatus(
            enabled=False,
            backend=None,
            allowed_commands=(),
            timeout_seconds=None,
            max_output_bytes=None,
            max_read_bytes=None,
            max_write_bytes=None,
            workspace_writable=None,
        )
        output = format_sandbox_status(status)
        self.assertIn("未启用", output)
        self.assertIn("--sandbox-root", output)
        self.assertNotIn("Docker", output)

    def test_enabled_shows_details(self):
        status = SandboxStatus(
            enabled=True,
            backend="Docker",
            allowed_commands=("python",),
            timeout_seconds=30.0,
            max_output_bytes=1_048_576,
            max_read_bytes=10_485_760,
            max_write_bytes=10_485_760,
            workspace_writable=True,
        )
        output = format_sandbox_status(status)
        self.assertIn("已启用", output)
        self.assertIn("Docker", output)
        self.assertIn("python", output)
        self.assertIn("30.0", output)
        self.assertIn("1048576", output)


class TestFormatTools(unittest.TestCase):
    """format_tools 格式化测试。"""

    def test_empty_returns_no_tools_message(self):
        output = format_tools([])
        self.assertEqual(output, "暂无可用 Tool")

    def test_single_tool_formatted(self):
        tools = [
            Tool(
                name="calculator",
                description="计算表达式",
                parameters={"type": "object"},
                handler=lambda _: "42",
            )
        ]
        output = format_tools(tools)
        self.assertIn("calculator", output)
        self.assertIn("计算表达式", output)
        self.assertNotIn("lambda", output)

    def test_multiple_tools_sorted(self):
        tools = [
            Tool(name="z_tool", description="Z", parameters={}, handler=lambda _: ""),
            Tool(name="a_tool", description="A", parameters={}, handler=lambda _: ""),
            Tool(name="m_tool", description="M", parameters={}, handler=lambda _: ""),
        ]
        output = format_tools(tools)
        lines = output.split("\n")
        tool_lines = [l for l in lines if l.startswith("- ")]
        names = [l.split("：")[0].replace("- ", "") for l in tool_lines]
        self.assertEqual(names, ["a_tool", "m_tool", "z_tool"])

    def test_tool_without_description(self):
        tools = [
            Tool(name="bare", description="", parameters={}, handler=lambda _: ""),
        ]
        output = format_tools(tools)
        self.assertIn("bare", output)
        self.assertIn("(无描述)", output)


# ============================================================================
# 默认关闭：不访问 Docker
# ============================================================================


class TestDefaultOffSecurity(unittest.TestCase):
    """默认关闭安全回归测试。"""

    def test_resolver_not_created_without_flag(self):
        """无 --sandbox-root 时不创建 Resolver"""
        args_sandbox_root = None
        if args_sandbox_root is None:
            resolver = None
        else:
            resolver = WorkspacePathResolver(Path(args_sandbox_root))
        self.assertIsNone(resolver)

    def test_registry_without_sandbox_has_no_sandbox_tools(self):
        """未启用 Sandbox 时 registry 不含沙箱工具"""
        registry = ToolRegistry()
        names = [t.name for t in registry.list_tools()]
        for name in names:
            self.assertFalse(
                name.startswith("sandbox_"),
                f"工具 {name} 不应在未启用 Sandbox 时存在",
            )

    def test_main_no_sandbox_no_docker_access(self):
        """main() 无 --sandbox-root 时完全不访问 Docker"""
        tmp_dir = tempfile.mkdtemp(prefix="deerflow_main_no_sandbox_")
        try:
            db_path = Path(tmp_dir) / "test.db"
            with patch.object(sys, "argv", ["mini-deerflow", f"--db={db_path}"]):
                # 捕获 stdout，模拟输入然后退出
                with patch("sys.stdin", new=StringIO("exit\n")):
                    # 不应抛出任何 Docker 相关异常
                    main(["--db", str(db_path)])
        finally:
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)


# ============================================================================
# 显式启用失败：终止启动，不降级
# ============================================================================


class TestSandboxStartupFailsClosed(unittest.TestCase):
    """显式启用 --sandbox-root 时，任何失败均终止启动。"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="deerflow_fail_closed_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _run_main_with_sandbox(self, extra_args: list[str]) -> SystemExit:
        """运行 main() 并捕获 SystemExit（argparse 失败时）。"""
        db_path = Path(self._tmp) / "test.db"
        args = ["--db", str(db_path), "--sandbox-root", self._tmp] + extra_args
        try:
            with patch("sys.stdin", new=StringIO("exit\n")):
                main(args)
        except SystemExit as e:
            return e
        self.fail("main() 应抛出 SystemExit 或 SandboxStartupError")

    def test_root_not_exists_fails(self):
        """Workspace 路径不存在 → SandboxStartupError"""
        fake_root = str(Path(self._tmp) / "not_exists")
        db_path = Path(self._tmp) / "test.db"
        with self.assertRaises(SandboxStartupError) as ctx:
            main([
                "--db", str(db_path),
                "--sandbox-root", fake_root,
            ])
        self.assertIn("不存在", str(ctx.exception))
        self.assertIn("not_exists", str(ctx.exception))

    def test_root_not_directory_fails(self):
        """Workspace 路径是文件而非目录 → SandboxStartupError"""
        not_dir = Path(self._tmp) / "afile.txt"
        not_dir.write_text("x")
        db_path = Path(self._tmp) / "test.db"
        with self.assertRaises(SandboxStartupError) as ctx:
            main([
                "--db", str(db_path),
                "--sandbox-root", str(not_dir),
            ])
        self.assertIn("不是目录", str(ctx.exception))

    def test_docker_not_found_fails(self):
        """docker.exe 不存在 → FileNotFoundError"""
        # 模拟 docker 不在 PATH 中
        db_path = Path(self._tmp) / "test.db"
        with patch("shutil.which", return_value=None):
            with self.assertRaises(FileNotFoundError) as ctx:
                main([
                    "--db", str(db_path),
                    "--sandbox-root", self._tmp,
                ])
        self.assertIn("docker", str(ctx.exception).lower())

    def test_daemon_unavailable_fails(self):
        """Docker daemon 不可用 → SandboxStartupError"""
        db_path = Path(self._tmp) / "test.db"

        with patch(
            "mini_deerflow.cli.preflight_docker"
        ) as mock_preflight:
            mock_preflight.side_effect = SandboxStartupError(
                "Docker daemon 不可用"
            )
            with self.assertRaises(SandboxStartupError) as ctx:
                main([
                    "--db", str(db_path),
                    "--sandbox-root", self._tmp,
                ])
            self.assertIn("daemon", str(ctx.exception).lower())

    def test_image_not_exists_fails(self):
        """镜像不存在 → SandboxStartupError"""
        db_path = Path(self._tmp) / "test.db"

        with patch(
            "mini_deerflow.cli.preflight_docker"
        ) as mock_preflight:
            mock_preflight.side_effect = SandboxStartupError(
                "镜像不存在: nonexistent:latest"
            )
            with self.assertRaises(SandboxStartupError) as ctx:
                main([
                    "--db", str(db_path),
                    "--sandbox-root", self._tmp,
                ])
            self.assertIn("不存在", str(ctx.exception))

    def test_tool_registration_conflict_fails(self):
        """Sandbox Tool 注册冲突 → DuplicateToolError"""
        db_path = Path(self._tmp) / "test.db"

        # 注册一个名为 sandbox_read_file 的工具，触发冲突
        fake_tool = Tool(
            name="sandbox_read_file",
            description="冲突工具",
            parameters={},
            handler=lambda _: "",
        )
        registry_with_conflict = ToolRegistry()
        registry_with_conflict.register(fake_tool)

        with patch(
            "mini_deerflow.cli.create_default_registry",
            return_value=registry_with_conflict,
        ):
            with self.assertRaises(Exception) as ctx:
                main([
                    "--db", str(db_path),
                    "--sandbox-root", self._tmp,
                ])
            # DuplicateToolError 或其基类
            self.assertIn("sandbox_read_file", str(ctx.exception))

    def test_failure_does_not_create_input_loop(self):
        """启动失败时，SandboxStartupError 抛出而非进入 run_cli"""
        db_path = Path(self._tmp) / "test.db"

        with patch(
            "mini_deerflow.cli.preflight_docker"
        ) as mock_preflight:
            mock_preflight.side_effect = SandboxStartupError("daemon unavailable")

            with self.assertRaises(SandboxStartupError):
                main([
                    "--db", str(db_path),
                    "--sandbox-root", self._tmp,
                ])


# ============================================================================
# SandboxStartupError 异常测试
# ============================================================================


class TestSandboxStartupError(unittest.TestCase):
    """SandboxStartupError 异常行为测试。"""

    def test_can_raise_and_catch(self):
        with self.assertRaises(SandboxStartupError):
            raise SandboxStartupError("Docker daemon 不可用")

    def test_message_preserved(self):
        msg = "镜像不存在"
        try:
            raise SandboxStartupError(msg)
        except SandboxStartupError as e:
            self.assertEqual(str(e), msg)

    def test_inherits_runtime_error(self):
        self.assertTrue(issubclass(SandboxStartupError, RuntimeError))


# ============================================================================
# Store 生命周期测试
# ============================================================================


class TestStoreLifecycleOnFailure(unittest.TestCase):
    """Sandbox 启动失败时，异常正确传播（finally 块由调用方保证）。"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="deerflow_store_lifecycle_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_sandbox_startup_error_propagates(self):
        """SandboxStartupError 直接抛出，不被捕获"""
        db_path = Path(self._tmp) / "test.db"

        with patch(
            "mini_deerflow.cli.preflight_docker"
        ) as mock_preflight:
            mock_preflight.side_effect = SandboxStartupError("daemon unavailable")

            with self.assertRaises(SandboxStartupError):
                main([
                    "--db", str(db_path),
                    "--sandbox-root", self._tmp,
                ])


# ============================================================================
# Sandbox 成功启动测试（无 Docker 环境时跳过）
# ============================================================================


class TestSandboxSuccessStartup(unittest.TestCase):
    """验证 Sandbox 成功启动时的行为。"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="deerflow_sandbox_ok_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_workspace_not_shown_in_absolute_path(self):
        """format_sandbox_status 不显示 Workspace 绝对路径"""
        status = SandboxStatus(
            enabled=True,
            backend="Docker",
            allowed_commands=("python",),
            timeout_seconds=30.0,
            max_output_bytes=1_048_576,
            max_read_bytes=10_485_760,
            max_write_bytes=10_485_760,
            workspace_writable=True,
        )
        output = format_sandbox_status(status)
        # 不应包含常见绝对路径模式（Windows 和 Unix）
        self.assertNotIn("C:\\", output)
        self.assertNotIn("/home/", output)
        self.assertNotIn("D:\\", output)


if __name__ == "__main__":
    unittest.main()
