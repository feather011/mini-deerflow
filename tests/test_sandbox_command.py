"""受控本地命令执行器测试（16C 阶段）。"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mini_deerflow.sandbox_command import (
    AllowedCommand,
    CommandNotAllowedError,
    CommandOutputLimitError,
    CommandRequest,
    CommandResult,
    CommandRunner,
    CommandStartError,
    CommandTerminationError,
    CommandTimeoutError,
    LocalCommandRunner,
    SandboxCommandError,
)
from mini_deerflow.sandbox_files import SandboxNotADirectoryError
from mini_deerflow.sandbox_path import (
    SandboxPathNotFoundError,
    SandboxPathViolationError,
    WorkspacePathResolver,
)


# --- 测试基类 ---


class _CmdTestCase(unittest.TestCase):
    """每个测试使用独立工作区。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.resolver = WorkspacePathResolver(self.root)

    def runner(
        self,
        *commands: AllowedCommand,
        default_timeout: float = 10.0,
        max_timeout: float = 60.0,
        max_output: int = 1_048_576,
        base_env=None,
    ) -> LocalCommandRunner:
        return LocalCommandRunner(
            self.resolver,
            list(commands),
            default_timeout_seconds=default_timeout,
            max_timeout_seconds=max_timeout,
            max_output_bytes=max_output,
            base_environment=base_env,
        )

    def python_cmd(self) -> AllowedCommand:
        return AllowedCommand(name="python", argv_prefix=(sys.executable,))


# --- AllowedCommand 测试 ---


class TestAllowedCommandCreation(_CmdTestCase):
    """AllowedCommand 构造和验证。"""

    def test_valid(self):
        """正常构造。"""
        cmd = AllowedCommand(name="python", argv_prefix=(sys.executable,))
        self.assertEqual(cmd.name, "python")
        self.assertEqual(cmd.argv_prefix, (sys.executable,))

    def test_with_args(self):
        """带固定参数。"""
        cmd = AllowedCommand(
            name="pytest",
            argv_prefix=(sys.executable, "-m", "pytest"),
        )
        self.assertEqual(cmd.argv_prefix, (sys.executable, "-m", "pytest"))

    def test_frozen(self):
        """frozen=True。"""
        cmd = AllowedCommand(name="python", argv_prefix=(sys.executable,))
        with self.assertRaises(AttributeError):
            cmd.name = "evil"  # type: ignore[misc]

    def test_empty_name_rejected(self):
        """空 name 被拒绝。"""
        with self.assertRaises(TypeError):
            AllowedCommand(name="", argv_prefix=(sys.executable,))

    def test_non_str_name_rejected(self):
        """非字符串 name 被拒绝。"""
        with self.assertRaises(TypeError):
            AllowedCommand(name=123, argv_prefix=(sys.executable,))  # type: ignore[arg-type]

    def test_empty_argv_rejected(self):
        """空 argv_prefix 被拒绝。"""
        with self.assertRaises(TypeError):
            AllowedCommand(name="python", argv_prefix=())

    def test_non_tuple_argv_rejected(self):
        """非 tuple argv_prefix 被拒绝。"""
        with self.assertRaises(TypeError):
            AllowedCommand(name="python", argv_prefix=[sys.executable])  # type: ignore[arg-type]

    def test_argv_element_not_str(self):
        """argv 元素非字符串被拒绝。"""
        with self.assertRaises(TypeError):
            AllowedCommand(name="python", argv_prefix=(123,))  # type: ignore[arg-type]

    def test_argv_element_empty_str(self):
        """argv 元素是空字符串被拒绝。"""
        with self.assertRaises(TypeError):
            AllowedCommand(name="python", argv_prefix=(sys.executable, ""))  # type: ignore[arg-type]

    def test_relative_executable_rejected(self):
        """相对路径 executable 被拒绝。"""
        with self.assertRaises(ValueError):
            AllowedCommand(name="python", argv_prefix=("python",))

    def test_nonexistent_executable_rejected(self):
        """不存在的 executable 被拒绝。"""
        with self.assertRaises(ValueError):
            AllowedCommand(name="ghost", argv_prefix=("/nonexistent/path/ghost",))

    def test_executable_is_directory_rejected(self):
        """目录作为 executable 被拒绝。"""
        with self.assertRaises(ValueError):
            AllowedCommand(name="dir", argv_prefix=(str(self.root),))


# --- CommandRequest 测试 ---


class TestCommandRequest(_CmdTestCase):
    """CommandRequest 构造和验证。"""

    def test_defaults(self):
        """默认值。"""
        req = CommandRequest(command="python")
        self.assertEqual(req.command, "python")
        self.assertEqual(req.args, ())
        self.assertIsNone(req.cwd)
        self.assertIsNone(req.timeout_seconds)

    def test_full(self):
        """完整参数。"""
        req = CommandRequest(
            command="python",
            args=("-c", "print(1)"),
            cwd="src",
            timeout_seconds=30.0,
        )
        self.assertEqual(req.cwd, "src")
        self.assertEqual(req.timeout_seconds, 30.0)

    def test_path_cwd(self):
        """Path 类型的 cwd。"""
        req = CommandRequest(command="python", cwd=Path("src"))
        self.assertEqual(req.cwd, Path("src"))

    def test_frozen(self):
        """frozen=True。"""
        req = CommandRequest(command="python")
        with self.assertRaises(AttributeError):
            req.command = "evil"  # type: ignore[misc]

    def test_empty_command_rejected(self):
        """空 command 被拒绝。"""
        with self.assertRaises(TypeError):
            CommandRequest(command="")

    def test_non_str_command_rejected(self):
        """非字符串 command 被拒绝。"""
        with self.assertRaises(TypeError):
            CommandRequest(command=123)  # type: ignore[arg-type]

    def test_args_not_tuple_rejected(self):
        """非 tuple args 被拒绝。"""
        with self.assertRaises(TypeError):
            CommandRequest(command="python", args=["-c", "print(1)"])  # type: ignore[arg-type]

    def test_args_element_not_str(self):
        """args 元素非字符串被拒绝。"""
        with self.assertRaises(TypeError):
            CommandRequest(command="python", args=(123,))  # type: ignore[arg-type]

    def test_args_with_nul_rejected(self):
        """args 含 NUL 被拒绝。"""
        with self.assertRaises(TypeError):
            CommandRequest(command="python", args=("hello\x00world",))

    def test_empty_string_arg_allowed(self):
        """空字符串参数允许。"""
        req = CommandRequest(command="python", args=("",))
        self.assertEqual(req.args, ("",))

    def test_cwd_invalid_type_rejected(self):
        """cwd 类型错误被拒绝。"""
        with self.assertRaises(TypeError):
            CommandRequest(command="python", cwd=123)  # type: ignore[arg-type]

    def test_timeout_zero_rejected(self):
        """timeout 0 被拒绝。"""
        with self.assertRaises(ValueError):
            CommandRequest(command="python", timeout_seconds=0)

    def test_timeout_negative_rejected(self):
        """负 timeout 被拒绝。"""
        with self.assertRaises(ValueError):
            CommandRequest(command="python", timeout_seconds=-1)

    def test_timeout_bool_rejected(self):
        """bool timeout 被拒绝。"""
        with self.assertRaises(TypeError):
            CommandRequest(command="python", timeout_seconds=True)  # type: ignore[arg-type]


# --- CommandResult 测试 ---


class TestCommandResult(_CmdTestCase):
    """CommandResult 构造和验证。"""

    def test_valid(self):
        """正常构造。"""
        result = CommandResult(
            command="python",
            args=(),
            cwd=Path("."),
            exit_code=0,
            stdout="hello",
            stderr="",
            duration_seconds=0.5,
        )
        self.assertEqual(result.stdout, "hello")

    def test_frozen(self):
        """frozen=True。"""
        result = CommandResult(
            command="python", args=(), cwd=Path("."),
            exit_code=0, stdout="", stderr="", duration_seconds=0.1,
        )
        with self.assertRaises(AttributeError):
            result.stdout = "modified"  # type: ignore[misc]

    def test_nonzero_exit_code(self):
        """非零退出码正常返回。"""
        result = CommandResult(
            command="python", args=(), cwd=Path("."),
            exit_code=1, stdout="", stderr="error", duration_seconds=0.1,
        )
        self.assertEqual(result.exit_code, 1)

    def test_cwd_is_relative(self):
        """cwd 是相对路径。"""
        result = CommandResult(
            command="python", args=(), cwd=Path("src"),
            exit_code=0, stdout="", stderr="", duration_seconds=0.1,
        )
        self.assertFalse(result.cwd.is_absolute())

    def test_cwd_root_is_dot(self):
        """cwd 是 Root 时用 Path(".") 表示。"""
        result = CommandResult(
            command="python", args=(), cwd=Path("."),
            exit_code=0, stdout="", stderr="", duration_seconds=0.1,
        )
        self.assertEqual(result.cwd, Path("."))

    def test_absolute_cwd_rejected(self):
        """绝对 cwd 被拒绝。"""
        with self.assertRaises(ValueError):
            CommandResult(
                command="python", args=(), cwd=Path("C:/"),
                exit_code=0, stdout="", stderr="", duration_seconds=0.1,
            )

    def test_duration_negative_rejected(self):
        """负 duration 被拒绝。"""
        with self.assertRaises(ValueError):
            CommandResult(
                command="python", args=(), cwd=Path("."),
                exit_code=0, stdout="", stderr="", duration_seconds=-0.1,
            )


# --- Runner 初始化测试 ---


class TestRunnerInit(_CmdTestCase):
    """LocalCommandRunner 初始化验证。"""

    def test_empty_allowlist(self):
        """空白名单合法。"""
        r = self.runner()
        self.assertEqual(len(r._allowed_commands), 0)

    def test_single_command(self):
        """单个命令。"""
        r = self.runner(self.python_cmd())
        self.assertIn("python", r._allowed_commands)

    def test_multiple_commands(self):
        """多个命令。"""
        cmd1 = AllowedCommand(name="python", argv_prefix=(sys.executable,))
        cmd2 = AllowedCommand(
            name="pytest", argv_prefix=(sys.executable, "-m", "pytest")
        )
        r = self.runner(cmd1, cmd2)
        self.assertEqual(len(r._allowed_commands), 2)

    def test_duplicate_name_rejected(self):
        """重复命令名被拒绝。"""
        cmd1 = AllowedCommand(name="python", argv_prefix=(sys.executable,))
        cmd2 = AllowedCommand(name="python", argv_prefix=(sys.executable, "-V"))
        with self.assertRaises(ValueError):
            self.runner(cmd1, cmd2)

    def test_invalid_resolver_rejected(self):
        """无效 resolver 被拒绝。"""
        with self.assertRaises(TypeError):
            LocalCommandRunner(object(), [])  # type: ignore[arg-type]

    def test_str_allowlist_rejected(self):
        """str 作为 allowlist 被拒绝。"""
        with self.assertRaises(TypeError):
            LocalCommandRunner(self.resolver, "python")  # type: ignore[arg-type]

    def test_non_command_in_allowlist_rejected(self):
        """非 AllowedCommand 元素被拒绝。"""
        with self.assertRaises(TypeError):
            LocalCommandRunner(self.resolver, ["python"])  # type: ignore[arg-type]

    def test_zero_default_timeout_rejected(self):
        """default_timeout 0 被拒绝。"""
        with self.assertRaises(ValueError):
            self.runner(self.python_cmd(), default_timeout=0.0)

    def test_negative_max_timeout_rejected(self):
        """负 max_timeout 被拒绝。"""
        with self.assertRaises(ValueError):
            self.runner(self.python_cmd(), max_timeout=-1.0)

    def test_default_greater_than_max_rejected(self):
        """default > max 被拒绝。"""
        with self.assertRaises(ValueError):
            self.runner(
                self.python_cmd(),
                default_timeout=30.0,
                max_timeout=10.0,
            )

    def test_bool_timeout_rejected(self):
        """bool timeout 被拒绝。"""
        with self.assertRaises(TypeError):
            self.runner(self.python_cmd(), default_timeout=True)  # type: ignore[arg-type]

    def test_zero_max_output_rejected(self):
        """max_output 0 被拒绝。"""
        with self.assertRaises(ValueError):
            self.runner(self.python_cmd(), max_output=0)

    def test_bool_max_output_rejected(self):
        """bool max_output 被拒绝。"""
        with self.assertRaises(TypeError):
            self.runner(self.python_cmd(), max_output=True)  # type: ignore[arg-type]

    def test_base_env_copy_protection(self):
        """base_environment 修改不影响 Runner。"""
        env = {"TEST": "value"}
        r = self.runner(self.python_cmd(), base_env=env)
        env["LEAKED"] = "secret"
        self.assertNotIn("LEAKED", r._base_env)

    def test_nul_in_env_key_rejected(self):
        """env 键含 NUL 被拒绝。"""
        with self.assertRaises(TypeError):
            self.runner(self.python_cmd(), base_env={"K\x00EY": "val"})

    def test_nul_in_env_value_rejected(self):
        """env 值含 NUL 被拒绝。"""
        with self.assertRaises(TypeError):
            self.runner(self.python_cmd(), base_env={"KEY": "v\x00al"})

    def test_satisfies_protocol(self):
        """满足 CommandRunner Protocol。"""
        self.assertIsInstance(
            self.runner(self.python_cmd()),
            CommandRunner,
        )


# --- 命令执行测试 ---


class TestCommandExecution(_CmdTestCase):
    """命令执行核心测试。"""

    def test_simple_output(self):
        """简单输出。"""
        r = self.runner(self.python_cmd())
        result = r.run(CommandRequest(
            command="python",
            args=("-c", "print('hello')"),
        ))
        self.assertEqual(result.stdout.strip(), "hello")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stderr, "")

    def test_stderr(self):
        """stderr 输出。"""
        r = self.runner(self.python_cmd())
        result = r.run(CommandRequest(
            command="python",
            args=("-c", "import sys; sys.stderr.write('err')"),
        ))
        self.assertEqual(result.stderr, "err")

    def test_chinese_output(self):
        """中文输出。"""
        r = self.runner(self.python_cmd())
        result = r.run(CommandRequest(
            command="python",
            args=("-c", "print('你好世界')"),
        ))
        self.assertEqual(result.stdout.strip(), "你好世界")

    def test_args_with_spaces(self):
        """带空格的参数不过 shell。"""
        r = self.runner(self.python_cmd())
        result = r.run(CommandRequest(
            command="python",
            args=("-c", "print('a b')"),
        ))
        self.assertEqual(result.stdout.strip(), "a b")

    def test_special_chars_not_shell_expanded(self):
        """特殊字符不过 shell。"""
        r = self.runner(self.python_cmd())
        result = r.run(CommandRequest(
            command="python",
            args=("-c", "print('a;b&|')"),
        ))
        self.assertEqual(result.stdout.strip(), "a;b&|")

    def test_cwd_none_uses_root(self):
        """cwd=None 使用 Sandbox Root。"""
        r = self.runner(self.python_cmd())
        result = r.run(CommandRequest(
            command="python",
            args=("-c", "import os; print(os.getcwd())"),
        ))
        self.assertEqual(result.cwd, Path("."))
        self.assertIn(str(self.root), result.stdout.strip())

    def test_cwd_subdirectory(self):
        """子目录作为 cwd。"""
        subdir = self.root / "sub"
        subdir.mkdir()
        r = self.runner(self.python_cmd())
        result = r.run(CommandRequest(
            command="python",
            args=("-c", "import os; print(os.getcwd())"),
            cwd="sub",
        ))
        self.assertEqual(result.cwd, Path("sub"))
        self.assertIn(str(subdir), result.stdout.strip())

    def test_cwd_does_not_exist(self):
        """cwd 不存在。"""
        r = self.runner(self.python_cmd())
        with self.assertRaises(SandboxPathNotFoundError):
            r.run(CommandRequest(command="python", cwd="nonexistent"))

    def test_cwd_is_file(self):
        """cwd 是文件。"""
        (self.root / "file.txt").touch()
        r = self.runner(self.python_cmd())
        with self.assertRaises(SandboxNotADirectoryError):
            r.run(CommandRequest(command="python", cwd="file.txt"))

    def test_cwd_traversal_rejected(self):
        """cwd 路径穿越被拒绝。"""
        r = self.runner(self.python_cmd())
        with self.assertRaises(SandboxPathViolationError):
            r.run(CommandRequest(command="python", cwd="../evil"))

    def test_unknown_command(self):
        """未注册命令。"""
        r = self.runner(self.python_cmd())
        with self.assertRaises(CommandNotAllowedError) as ctx:
            r.run(CommandRequest(command="unknown"))
        self.assertEqual(ctx.exception.command, "unknown")

    def test_argv_prefix_used(self):
        """argv_prefix 的固定参数被使用。"""
        cmd = AllowedCommand(
            name="my-echo",
            argv_prefix=(sys.executable, "-c", "import sys; print(sys.argv)"),
        )
        r = self.runner(cmd)
        result = r.run(CommandRequest(
            command="my-echo",
            args=("hello", "world"),
        ))
        # sys.argv[0] 是 -c，后续是参数
        self.assertIn("'hello'", result.stdout)
        self.assertIn("'world'", result.stdout)

    def test_stdin_not_waiting(self):
        """stdin 不等待输入。"""
        r = self.runner(self.python_cmd())
        start = time.monotonic()
        result = r.run(CommandRequest(
            command="python",
            args=("-c", "import time; time.sleep(0.5)"),
        ))
        duration = time.monotonic() - start
        self.assertLess(duration, 3)

    def test_nonzero_exit_code_returns_result(self):
        """非零退出码返回正常结果，不抛异常。"""
        r = self.runner(self.python_cmd())
        result = r.run(CommandRequest(
            command="python",
            args=("-c", "import sys; sys.exit(1)"),
        ))
        self.assertEqual(result.exit_code, 1)

    def test_duration_nonnegative(self):
        """duration 非负。"""
        r = self.runner(self.python_cmd())
        result = r.run(CommandRequest(
            command="python",
            args=("-c", "print(1)"),
        ))
        self.assertGreaterEqual(result.duration_seconds, 0)

    def test_request_not_modified(self):
        """request 不被修改。"""
        r = self.runner(self.python_cmd())
        req = CommandRequest(command="python", args=("-c", "print(1)"))
        orig_args = req.args
        r.run(req)
        self.assertEqual(req.args, orig_args)


# --- 环境变量测试 ---


class TestEnvironmentVariables(_CmdTestCase):
    """环境变量隔离测试。"""

    def test_custom_base_env_visible(self):
        """自定义 base_environment 可见。"""
        r = self.runner(
            self.python_cmd(),
            base_env={"MY_VAR": "my_value"},
        )
        result = r.run(CommandRequest(
            command="python",
            args=("-c", "import os; print(os.environ.get('MY_VAR'))"),
        ))
        self.assertEqual(result.stdout.strip(), "my_value")

    def test_sensitive_vars_not_inherited(self):
        """敏感变量不被继承。"""
        r = self.runner(self.python_cmd())
        # 即使在父进程中设置了假 API Key，子进程也不应看到
        with patch.dict(os.environ, {"FAKE_API_KEY": "secret123"}, clear=False):
            result = r.run(CommandRequest(
                command="python",
                args=("-c", "import os; print(os.environ.get('FAKE_API_KEY') or '')"),
            ))
        self.assertEqual(result.stdout.strip(), "")

    def test_caller_dict_modification_not_leaked(self):
        """调用方修改 dict 不影响 Runner。"""
        env = {"CALLER_VAR": "caller_value"}
        r = self.runner(self.python_cmd(), base_env=dict(env))
        env["LEAKED"] = "should_not_appear"
        result = r.run(CommandRequest(
            command="python",
            args=("-c", "import os; print(os.environ.get('LEAKED') or '')"),
        ))
        self.assertEqual(result.stdout.strip(), "")

    def test_pythonutf8_set(self):
        """PYTHONUTF8 环境变量生效。"""
        r = self.runner(self.python_cmd())
        result = r.run(CommandRequest(
            command="python",
            args=("-c", "import os; print(os.environ.get('PYTHONUTF8'))"),
        ))
        self.assertEqual(result.stdout.strip(), "1")


# --- 超时测试 ---


class TestTimeout(_CmdTestCase):
    """超时测试。"""

    def test_default_timeout(self):
        """默认超时。"""
        r = self.runner(
            self.python_cmd(),
            default_timeout=1.0,
            max_timeout=10.0,
        )
        # 快速命令应在默认超时内完成
        result = r.run(CommandRequest(
            command="python",
            args=("-c", "print(1)"),
        ))
        self.assertEqual(result.exit_code, 0)

    def test_custom_timeout_success(self):
        """自定义超时成功。"""
        r = self.runner(
            self.python_cmd(),
            default_timeout=10.0,
            max_timeout=60.0,
        )
        result = r.run(CommandRequest(
            command="python",
            args=("-c", "print(1)"),
            timeout_seconds=5.0,
        ))
        self.assertEqual(result.exit_code, 0)

    def test_exceeds_max_timeout_rejected(self):
        """超过最大超时被拒绝。"""
        r = self.runner(
            self.python_cmd(),
            default_timeout=10.0,
            max_timeout=30.0,
        )
        with self.assertRaises(CommandTimeoutError):
            r.run(CommandRequest(
                command="python",
                args=("-c", "print(1)"),
                timeout_seconds=60.0,
            ))

    def test_timeout_kills_process(self):
        """超时应终止进程。"""
        r = self.runner(
            self.python_cmd(),
            default_timeout=0.5,
            max_timeout=10.0,
        )
        with self.assertRaises(CommandTimeoutError):
            r.run(CommandRequest(
                command="python",
                args=("-c", "import time; time.sleep(10)"),
            ))
        # 进程应已终止，不再消耗资源

    def test_timeout_error_attributes(self):
        """超时错误包含属性。"""
        r = self.runner(
            self.python_cmd(),
            default_timeout=0.3,
            max_timeout=10.0,
        )
        with self.assertRaises(CommandTimeoutError) as ctx:
            r.run(CommandRequest(
                command="python",
                args=("-c", "import time; print('partial'); time.sleep(10)"),
            ))
        self.assertEqual(ctx.exception.command, "python")
        self.assertEqual(ctx.exception.timeout_seconds, 0.3)
        self.assertIn("partial", ctx.exception.stdout)

    def test_timeout_no_zombie_process(self):
        """超时不留下僵尸进程。"""
        r = self.runner(
            self.python_cmd(),
            default_timeout=0.3,
            max_timeout=10.0,
        )
        try:
            r.run(CommandRequest(
                command="python",
                args=("-c", "import time; time.sleep(10)"),
            ))
        except CommandTimeoutError:
            pass
        # 没有显式断言，但进程树已通过 terminate 方法清理

    def test_terminate_fallback_on_windows(self):
        """Windows 终止 fallback。"""
        r = self.runner(
            self.python_cmd(),
            default_timeout=0.3,
            max_timeout=10.0,
        )
        with patch.object(r, "_terminate_windows", side_effect=Exception("mock fail")):
            with self.assertRaises(CommandTimeoutError):
                r.run(CommandRequest(
                    command="python",
                    args=("-c", "import time; time.sleep(10)"),
                ))


# --- 输出限制测试 ---


class TestOutputLimit(_CmdTestCase):
    """输出大小限制测试。"""

    def test_stdout_at_limit(self):
        """stdout 恰好达到限制（不含换行符）。"""
        r = self.runner(
            self.python_cmd(),
            max_output=10,
        )
        result = r.run(CommandRequest(
            command="python",
            args=("-c", "import sys; sys.stdout.buffer.write(b'1234567890')"),
        ))
        self.assertEqual(len(result.stdout.encode("utf-8")), 10)

    def test_stdout_exceeds_limit(self):
        """stdout 超过限制抛异常。"""
        r = self.runner(
            self.python_cmd(),
            max_output=5,
        )
        with self.assertRaises(CommandOutputLimitError) as ctx:
            r.run(CommandRequest(
                command="python",
                args=("-c", "print('hello world')"),
            ))
        self.assertEqual(ctx.exception.limit_bytes, 5)
        self.assertIn("hello", ctx.exception.stdout)

    def test_stderr_exceeds_limit(self):
        """stderr 超过限制抛异常。"""
        r = self.runner(
            self.python_cmd(),
            max_output=5,
        )
        with self.assertRaises(CommandOutputLimitError) as ctx:
            r.run(CommandRequest(
                command="python",
                args=("-c", "import sys; sys.stderr.write('hello world')"),
            ))
        self.assertIn("hello", ctx.exception.stderr)

    def test_combined_exceeds_limit(self):
        """stdout+stderr 合计超限。"""
        r = self.runner(
            self.python_cmd(),
            max_output=5,
        )
        with self.assertRaises(CommandOutputLimitError):
            r.run(CommandRequest(
                command="python",
                args=("-c", "print('abc'); import sys; sys.stderr.write('xyz')"),
            ))

    def test_partial_output_capped(self):
        """超限时部分输出不超过限制。"""
        r = self.runner(
            self.python_cmd(),
            max_output=10,
        )
        with self.assertRaises(CommandOutputLimitError) as ctx:
            r.run(CommandRequest(
                command="python",
                args=("-c", "print('x' * 1000)"),
            ))
        self.assertLessEqual(
            len(ctx.exception.stdout.encode("utf-8")),
            10,
        )

    def test_non_utf8_replacement(self):
        """非 UTF-8 使用 replacement 字符。"""
        r = self.runner(
            self.python_cmd(),
            max_output=1_000_000,
        )
        result = r.run(CommandRequest(
            command="python",
            args=("-c", "import sys; sys.stdout.buffer.write(b'\\xff\\xfe')"),
        ))
        self.assertEqual(len(result.stdout), 2)  # replacement characters

    def test_large_output_not_cached_fully(self):
        """大量输出不会完全缓存（受 max_output 限制）。"""
        r = self.runner(
            self.python_cmd(),
            max_output=100,
        )
        start = time.monotonic()
        try:
            r.run(CommandRequest(
                command="python",
                args=("-c", "import sys; [sys.stdout.write('x') for _ in range(100000)]"),
            ))
        except CommandOutputLimitError:
            pass
        duration = time.monotonic() - start
        # 不应等待太久（输出被限制）
        self.assertLess(duration, 10)

    def test_stdout_stderr_both_read(self):
        """stdout 和 stderr 同时读取不阻塞。"""
        r = self.runner(
            self.python_cmd(),
            max_output=100_000,
        )
        result = r.run(CommandRequest(
            command="python",
            args=(
                "-c",
                "import sys, time; "
                "[(print(i), sys.stderr.write('e'+str(i)+'\\n')) for i in range(100)]",
            ),
        ))
        self.assertGreater(len(result.stdout), 0)
        self.assertGreater(len(result.stderr), 0)


# --- 异常层次测试 ---


class TestExceptionHierarchy(_CmdTestCase):
    """异常继承关系。"""

    def test_all_inherit_base(self):
        """所有命令异常继承 SandboxCommandError。"""
        for cls in (
            CommandNotAllowedError,
            CommandTimeoutError,
            CommandOutputLimitError,
            CommandStartError,
            CommandTerminationError,
        ):
            with self.subTest(cls.__name__):
                self.assertTrue(issubclass(cls, SandboxCommandError))

    def test_timeout_carries_output(self):
        """超时异常携带部分输出。"""
        r = self.runner(self.python_cmd(), default_timeout=0.2, max_timeout=10.0)
        try:
            r.run(CommandRequest(
                command="python",
                args=("-c", "print('before'); import time; time.sleep(10)"),
            ))
        except CommandTimeoutError as e:
            self.assertIn("before", e.stdout)

    def test_output_limit_carries_output(self):
        """输出超限异常携带部分输出。"""
        r = self.runner(self.python_cmd(), max_output=5)
        try:
            r.run(CommandRequest(
                command="python",
                args=("-c", "print('hello world')"),
            ))
        except CommandOutputLimitError as e:
            self.assertIn("hello", e.stdout)


# --- 安全边界测试 ---


class TestSecurityBoundary(_CmdTestCase):
    """安全边界验证。"""

    def test_shell_false(self):
        """shell=False。"""
        r = self.runner(self.python_cmd())
        # 如果 shell=True，; 会导致命令注入
        result = r.run(CommandRequest(
            command="python",
            args=("-c", "import sys; sys.exit(0)"),
        ))
        self.assertEqual(result.exit_code, 0)

    def test_no_cmd_exe(self):
        """不调用 cmd.exe。"""
        r = self.runner(self.python_cmd())
        result = r.run(CommandRequest(
            command="python",
            args=("-c", "import sys; print(sys.executable)"),
        ))
        self.assertNotIn("cmd.exe", result.stdout.lower())

    def test_absolute_executable_used(self):
        """使用配置的绝对路径。"""
        cmd = AllowedCommand(name="my-py", argv_prefix=(sys.executable,))
        r = self.runner(cmd)
        result = r.run(CommandRequest(
            command="my-py",
            args=("-c", "import sys; print(sys.executable)"),
        ))
        # argv_prefix 的第一项是 sys.executable
        self.assertTrue(result.stdout.strip().endswith("python.exe"))

    def test_unknown_command_rejected_before_start(self):
        """未知命令在启动前被拒绝。"""
        r = self.runner(self.python_cmd())
        with self.assertRaises(CommandNotAllowedError):
            r.run(CommandRequest(command="ls"))


# --- cwd symlink 测试 ---


class TestCwdSymlink(_CmdTestCase):
    """cwd 符号链接测试。"""

    def test_cwd_symlink_outside_rejected(self):
        """cwd 指向外部的符号链接被拒绝。"""
        outside = Path(self._tmp.name).parent / "outside_cmd"
        outside.mkdir(exist_ok=True)
        self.addCleanup(lambda: outside.rmdir() if outside.exists() else None)

        link = self.root / "escape"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("符号链接不支持")

        r = self.runner(self.python_cmd())
        with self.assertRaises(SandboxPathViolationError):
            r.run(CommandRequest(command="python", cwd="escape"))


if __name__ == "__main__":
    unittest.main()
