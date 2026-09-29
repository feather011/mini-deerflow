"""Docker 沙箱隔离执行器测试（16D 阶段）。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mini_deerflow.sandbox_command import (
    CommandRequest,
    CommandResult,
)
from mini_deerflow.sandbox_docker import (
    ContainerCommand,
    DockerCleanupError,
    DockerCommandNotAllowedError,
    DockerConfigurationError,
    DockerSandboxConfig,
    DockerSandboxError,
    DockerSandboxRunner,
)
from mini_deerflow.sandbox_path import WorkspacePathResolver


# --- 测试基类 ---


class _DockerTestCase(unittest.TestCase):
    """每个测试使用独立工作区。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.resolver = WorkspacePathResolver(self.root)

    def config(self, **kwargs) -> DockerSandboxConfig:
        defaults = dict(image="python:3.12-slim")
        defaults.update(kwargs)
        return DockerSandboxConfig(**defaults)

    def container_cmd(
        self,
        name: str = "python",
        prefix: tuple[str, ...] = ("/usr/local/bin/python",),
    ) -> ContainerCommand:
        return ContainerCommand(name=name, argv_prefix=prefix)

    def recording_runner(self, result: CommandResult | None = None) -> "_RecordingRunner":
        return _RecordingRunner(result)

    def runner(
        self,
        *container_commands: ContainerCommand,
        config: DockerSandboxConfig | None = None,
        docker_name: str = "docker",
        container_name_factory=None,
    ) -> DockerSandboxRunner:
        recording = _RecordingRunner()
        cfg = config or self.config()
        return DockerSandboxRunner(
            self.resolver,
            recording,
            list(container_commands),
            cfg,
            docker_command_name=docker_name,
            container_name_factory=container_name_factory,
        )


# --- 录制型 Runner（用于测试） ---


class _RecordingRunner:
    """录制型 host_runner：记录收到的请求，可注入结果或错误。"""

    def __init__(
        self,
        result: CommandResult | None = None,
        error: Exception | None = None,
    ) -> None:
        self.requests: list[CommandRequest] = []
        self._result = result
        self._error = error

    def run(self, request: CommandRequest) -> CommandResult:
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        if self._result is not None:
            return self._result
        # 默认：成功返回空结果
        return CommandResult(
            command=request.command,
            args=request.args,
            cwd=Path("."),
            exit_code=0,
            stdout="",
            stderr="",
            duration_seconds=0.1,
        )


# --- ContainerCommand 测试 ---


class TestContainerCommand(_DockerTestCase):
    """ContainerCommand 构造和验证。"""

    def test_valid(self):
        """正常构造。"""
        cmd = ContainerCommand(
            name="python",
            argv_prefix=("/usr/local/bin/python",),
        )
        self.assertEqual(cmd.name, "python")
        self.assertEqual(cmd.argv_prefix, ("/usr/local/bin/python",))

    def test_with_args(self):
        """带固定参数。"""
        cmd = ContainerCommand(
            name="pytest",
            argv_prefix=("/usr/local/bin/python", "-m", "pytest"),
        )
        self.assertEqual(cmd.argv_prefix, ("/usr/local/bin/python", "-m", "pytest"))

    def test_frozen(self):
        """frozen=True。"""
        cmd = ContainerCommand(name="python", argv_prefix=("/usr/local/bin/python",))
        with self.assertRaises(AttributeError):
            cmd.name = "evil"  # type: ignore[misc]

    def test_empty_name_rejected(self):
        """空 name 被拒绝。"""
        with self.assertRaises(TypeError):
            ContainerCommand(name="", argv_prefix=("/bin/python",))

    def test_non_str_name_rejected(self):
        """非字符串 name 被拒绝。"""
        with self.assertRaises(TypeError):
            ContainerCommand(name=123, argv_prefix=("/bin/python",))  # type: ignore

    def test_empty_prefix_rejected(self):
        """空 prefix 被拒绝。"""
        with self.assertRaises(TypeError):
            ContainerCommand(name="python", argv_prefix=())

    def test_non_tuple_prefix_rejected(self):
        """非 tuple prefix 被拒绝。"""
        with self.assertRaises(TypeError):
            ContainerCommand(name="python", argv_prefix=["/bin/python"])  # type: ignore

    def test_prefix_element_not_str(self):
        """prefix 元素非字符串被拒绝。"""
        with self.assertRaises(TypeError):
            ContainerCommand(name="python", argv_prefix=(123,))  # type: ignore

    def test_prefix_element_empty_str(self):
        """prefix 元素是空字符串被拒绝。"""
        with self.assertRaises(TypeError):
            ContainerCommand(name="python", argv_prefix=("/bin/python", ""))

    def test_first_not_posix_absolute(self):
        """第一项不是 POSIX 绝对路径被拒绝。"""
        with self.assertRaises(ValueError):
            ContainerCommand(name="python", argv_prefix=("bin/python",))

    def test_first_starts_with_dash(self):
        """第一项以 - 开头被拒绝。"""
        with self.assertRaises(ValueError):
            ContainerCommand(name="python", argv_prefix=("-python",))

    def test_nul_in_prefix(self):
        """prefix 含 NUL 被拒绝。"""
        with self.assertRaises(TypeError):
            ContainerCommand(name="python", argv_prefix=("/bin/py\x00thon",))


# --- DockerSandboxConfig 测试 ---


class TestDockerSandboxConfig(_DockerTestCase):
    """DockerSandboxConfig 构造和验证。"""

    def test_defaults(self):
        """默认安全值。"""
        cfg = DockerSandboxConfig(image="python:3.12-slim")
        self.assertEqual(cfg.image, "python:3.12-slim")
        self.assertEqual(cfg.container_workspace, "/workspace")
        self.assertEqual(cfg.memory_limit, "256m")
        self.assertEqual(cfg.cpu_limit, 1.0)
        self.assertEqual(cfg.pids_limit, 128)
        self.assertEqual(cfg.tmpfs_size, "64m")
        self.assertEqual(cfg.user, "65534:65534")
        self.assertTrue(cfg.workspace_writable)

    def test_custom(self):
        """自定义参数。"""
        cfg = DockerSandboxConfig(
            image="ubuntu:22.04",
            container_workspace="/app",
            memory_limit="512m",
            cpu_limit=2.0,
            pids_limit=64,
            tmpfs_size="128m",
            user="1000:1000",
            workspace_writable=False,
        )
        self.assertEqual(cfg.cpu_limit, 2.0)
        self.assertFalse(cfg.workspace_writable)

    def test_frozen(self):
        """frozen=True。"""
        cfg = DockerSandboxConfig(image="python:3.12-slim")
        with self.assertRaises(AttributeError):
            cfg.image = "malicious"  # type: ignore[misc]

    def test_empty_image_rejected(self):
        """空 image 被拒绝。"""
        with self.assertRaises(TypeError):
            DockerSandboxConfig(image="")

    def test_image_starts_with_dash(self):
        """image 以 - 开头被拒绝。"""
        with self.assertRaises(ValueError):
            DockerSandboxConfig(image="-malicious")

    def test_image_with_whitespace(self):
        """image 含空白被拒绝。"""
        with self.assertRaises(ValueError):
            DockerSandboxConfig(image="python:3.12 slim")

    def test_workspace_not_absolute(self):
        """workspace 非绝对路径被拒绝。"""
        with self.assertRaises(ValueError):
            DockerSandboxConfig(image="python", container_workspace="workspace")

    def test_workspace_is_root(self):
        """workspace 是 / 被拒绝。"""
        with self.assertRaises(ValueError):
            DockerSandboxConfig(image="python", container_workspace="/")

    def test_workspace_with_dotdot(self):
        """workspace 含 .. 被拒绝。"""
        with self.assertRaises(ValueError):
            DockerSandboxConfig(image="python", container_workspace="/../etc")

    def test_workspace_starts_with_dash(self):
        """workspace 以 - 开头被拒绝。"""
        with self.assertRaises(ValueError):
            DockerSandboxConfig(image="python", container_workspace="/-evil")

    def test_memory_format_invalid(self):
        """memory 格式不安全被拒绝。"""
        for bad in ("", "256", "256mb", "abc", "-100m"):
            with self.subTest(memory_limit=bad):
                with self.assertRaises(ValueError):
                    DockerSandboxConfig(image="python", memory_limit=bad)

    def test_cpu_zero_rejected(self):
        """CPU 0 被拒绝。"""
        with self.assertRaises(ValueError):
            DockerSandboxConfig(image="python", cpu_limit=0)

    def test_cpu_negative_rejected(self):
        """负 CPU 被拒绝。"""
        with self.assertRaises(ValueError):
            DockerSandboxConfig(image="python", cpu_limit=-1.0)

    def test_cpu_bool_rejected(self):
        """bool CPU 被拒绝。"""
        with self.assertRaises(TypeError):
            DockerSandboxConfig(image="python", cpu_limit=True)  # type: ignore

    def test_cpu_too_large(self):
        """CPU 超过 64 被拒绝。"""
        with self.assertRaises(ValueError):
            DockerSandboxConfig(image="python", cpu_limit=100.0)

    def test_pids_zero_rejected(self):
        """PIDs 0 被拒绝。"""
        with self.assertRaises(ValueError):
            DockerSandboxConfig(image="python", pids_limit=0)

    def test_pids_negative_rejected(self):
        """负 PIDs 被拒绝。"""
        with self.assertRaises(ValueError):
            DockerSandboxConfig(image="python", pids_limit=-1)

    def test_pids_bool_rejected(self):
        """bool PIDs 被拒绝。"""
        with self.assertRaises(TypeError):
            DockerSandboxConfig(image="python", pids_limit=True)  # type: ignore

    def test_tmpfs_format_invalid(self):
        """tmpfs 格式不安全被拒绝。"""
        for bad in ("", "64", "64mb", "-1g"):
            with self.subTest(tmpfs_size=bad):
                with self.assertRaises(ValueError):
                    DockerSandboxConfig(image="python", tmpfs_size=bad)

    def test_root_user_rejected(self):
        """root 用户被拒绝。"""
        for root in ("0", "0:0", "root", "root:root"):
            with self.subTest(user=root):
                with self.assertRaises(ValueError):
                    DockerSandboxConfig(image="python", user=root)

    def test_user_starts_with_dash(self):
        """user 以 - 开头被拒绝。"""
        with self.assertRaises(ValueError):
            DockerSandboxConfig(image="python", user="-1:1")

    def test_writable_not_bool(self):
        """workspace_writable 不是 bool 被拒绝。"""
        with self.assertRaises(TypeError):
            DockerSandboxConfig(image="python", workspace_writable="yes")  # type: ignore


# --- Runner 初始化测试 ---


class TestRunnerInit(_DockerTestCase):
    """DockerSandboxRunner 初始化。"""

    def test_empty_allowlist(self):
        """空白名单合法。"""
        r = self.runner()
        self.assertEqual(len(r._container_commands), 0)

    def test_single_command(self):
        """单个命令。"""
        cmd = self.container_cmd()
        r = self.runner(cmd)
        self.assertIn("python", r._container_commands)

    def test_multiple_commands(self):
        """多个命令。"""
        cmd1 = self.container_cmd("python", ("/bin/python3",))
        cmd2 = self.container_cmd("pytest", ("/bin/python3", "-m", "pytest"))
        r = self.runner(cmd1, cmd2)
        self.assertEqual(len(r._container_commands), 2)

    def test_duplicate_name_rejected(self):
        """重复命令名被拒绝。"""
        cmd1 = self.container_cmd("python")
        cmd2 = self.container_cmd("python")
        with self.assertRaises(ValueError):
            self.runner(cmd1, cmd2)

    def test_invalid_resolver_rejected(self):
        """无效 resolver 被拒绝。"""
        recording = _RecordingRunner()
        with self.assertRaises(TypeError):
            DockerSandboxRunner(
                object(),  # type: ignore
                recording,
                [],
                self.config(),
            )

    def test_invalid_host_runner_rejected(self):
        """无效 host_runner 被拒绝。"""
        with self.assertRaises(TypeError):
            DockerSandboxRunner(self.resolver, object(), [], self.config())  # type: ignore

    def test_config_copy(self):
        """配置被深复制（frozen=True 时无需复制）。"""
        cfg = self.config()
        r = self.runner(config=cfg)
        # frozen=True dataclass 是不可变的，可以直接引用
        self.assertEqual(r._config.image, cfg.image)

    def test_factory_injected(self):
        """可注入名称工厂。"""
        factory_calls = []
        factory = lambda: (factory_calls.append(1) or "mini-test")
        r = self.runner(self.container_cmd(), container_name_factory=factory)
        r.run(CommandRequest(command="python", args=()))
        self.assertEqual(len(factory_calls), 1)

    def test_does_not_execute_docker_at_init(self):
        """构造时不执行 docker。"""
        r = self.runner()
        self.assertEqual(len(r._host_runner.requests), 0)


# --- Docker argv 测试 ---


class TestDockerArgv(_DockerTestCase):
    """Docker argv 安全参数构建。"""

    def _run_and_get_args(self, **kwargs) -> list[str]:
        """执行并返回记录的 docker argv。"""
        recording = _RecordingRunner()
        cfg = self.config(**kwargs)
        runner = DockerSandboxRunner(
            self.resolver,
            recording,
            [self.container_cmd()],
            cfg,
        )
        runner.run(CommandRequest(command="python"))
        return list(recording.requests[-1].args)

    def test_contains_run(self):
        """包含 run 子命令。"""
        args = self._run_and_get_args()
        self.assertEqual(args[0], "run")

    def test_contains_rm(self):
        """包含 --rm。"""
        args = self._run_and_get_args()
        self.assertIn("--rm", args)

    def test_contains_unique_name(self):
        """包含唯一容器名。"""
        args = self._run_and_get_args()
        name_idx = args.index("--name")
        name = args[name_idx + 1]
        self.assertTrue(name.startswith("mini-deerflow-"))

    def test_network_none(self):
        """网络隔离。"""
        args = self._run_and_get_args()
        self.assertIn("--network", args)
        self.assertIn("none", args)

    def test_read_only(self):
        """根文件系统只读。"""
        args = self._run_and_get_args()
        self.assertIn("--read-only", args)

    def test_cap_drop_all(self):
        """移除所有 capabilities。"""
        args = self._run_and_get_args()
        self.assertIn("--cap-drop", args)
        self.assertIn("ALL", args)

    def test_no_new_privileges(self):
        """禁用新权限。"""
        args = self._run_and_get_args()
        self.assertIn("--security-opt", args)
        self.assertIn("no-new-privileges", args)

    def test_pids_limit(self):
        """PID 限制。"""
        args = self._run_and_get_args(pids_limit=64)
        self.assertIn("--pids-limit", args)
        self.assertIn("64", args)

    def test_memory_limit(self):
        """内存限制。"""
        args = self._run_and_get_args(memory_limit="512m")
        self.assertIn("--memory", args)
        self.assertIn("512m", args)

    def test_cpus(self):
        """CPU 限制。"""
        args = self._run_and_get_args(cpu_limit=2.0)
        self.assertIn("--cpus", args)
        self.assertIn("2.0", args)

    def test_tmpfs(self):
        """tmpfs 挂载。"""
        args = self._run_and_get_args(tmpfs_size="128m")
        self.assertIn("--tmpfs", args)
        # 检查格式
        self.assertTrue(any("128m" in a for a in args))

    def test_non_root_user(self):
        """非 root 用户。"""
        args = self._run_and_get_args(user="1000:1000")
        self.assertIn("--user", args)
        self.assertIn("1000:1000", args)

    def test_workspace_mount(self):
        """workspace bind mount。"""
        args = self._run_and_get_args()
        self.assertIn("--mount", args)
        mount_idx = args.index("--mount")
        mount = args[mount_idx + 1]
        self.assertIn("type=bind", mount)
        self.assertIn(f"source=", mount)
        self.assertIn(f"target=/workspace", mount)

    def test_workspace_readonly(self):
        """workspace 只读挂载。"""
        args = self._run_and_get_args(workspace_writable=False)
        mount_idx = args.index("--mount")
        mount = args[mount_idx + 1]
        self.assertIn("readonly", mount)

    def test_workdir(self):
        """workdir 设置。"""
        args = self._run_and_get_args()
        self.assertIn("--workdir", args)
        self.assertIn("/workspace", args)

    def test_fixed_env(self):
        """固定环境变量。"""
        args = self._run_and_get_args()
        self.assertIn("--env", args)
        self.assertIn("PYTHONUTF8=1", args)
        self.assertIn("PYTHONIOENCODING=utf-8", args)

    def test_pull_never(self):
        """禁止隐式 pull。"""
        args = self._run_and_get_args()
        self.assertIn("--pull", args)
        self.assertIn("never", args)

    def test_image(self):
        """使用配置镜像。"""
        args = self._run_and_get_args(image="ubuntu:22.04")
        self.assertIn("ubuntu:22.04", args)

    def test_container_command_prefix(self):
        """容器命令前缀。"""
        cmd = self.container_cmd("pytest", ("/usr/local/bin/python", "-m", "pytest"))
        recording = _RecordingRunner()
        runner = DockerSandboxRunner(
            self.resolver, recording, [cmd], self.config()
        )
        runner.run(CommandRequest(command="pytest"))
        args = list(recording.requests[-1].args)
        # container command prefix 在 image 之后
        img_idx = args.index("python:3.12-slim")
        self.assertIn("/usr/local/bin/python", args[img_idx + 1:])

    def test_user_args_last(self):
        """用户参数在最后。"""
        cmd = self.container_cmd()
        recording = _RecordingRunner()
        runner = DockerSandboxRunner(
            self.resolver, recording, [cmd], self.config()
        )
        runner.run(CommandRequest(
            command="python",
            args=("-c", "print('hello')"),
        ))
        args = list(recording.requests[-1].args)
        # 用户参数在最后
        self.assertEqual(args[-1], "print('hello')")
        self.assertEqual(args[-2], "-c")

    def test_no_dangerous_params(self):
        """不包含禁用参数。"""
        args = self._run_and_get_args()
        dangerous = [
            "--privileged",
            "--network=host",
            "--pid=host",
            "--ipc=host",
            "--uts=host",
            "--cap-add",
            "seccomp=unconfined",
        ]
        for param in dangerous:
            self.assertNotIn(param, args)

    def test_no_sensitive_env(self):
        """不包含敏感环境变量。"""
        args = self._run_and_get_args()
        args_str = " ".join(args)
        dangerous = ["API_KEY", "TOKEN", "AWS_", "GITHUB_TOKEN"]
        for sensitive in dangerous:
            self.assertNotIn(sensitive, args_str)


# --- 参数安全测试 ---


class TestArgumentSafety(_DockerTestCase):
    """用户参数无法成为 Docker 参数。"""

    def test_args_like_docker_flag_last(self):
        """用户 args 含 --privileged 但在最后。"""
        cmd = self.container_cmd()
        recording = _RecordingRunner()
        runner = DockerSandboxRunner(
            self.resolver, recording, [cmd], self.config()
        )
        runner.run(CommandRequest(
            command="python",
            args=("-c", "--privileged", "--network=host"),
        ))
        args = list(recording.requests[-1].args)
        img_idx = args.index("python:3.12-slim")
        user_args = args[img_idx + 1:]
        # 用户参数是容器程序参数，不是 docker 参数
        self.assertNotIn("--privileged", args[:img_idx])
        self.assertIn("--privileged", user_args)

    def test_args_like_mount(self):
        """用户 args 含 -v 但在容器命令参数中。"""
        cmd = self.container_cmd()
        recording = _RecordingRunner()
        runner = DockerSandboxRunner(
            self.resolver, recording, [cmd], self.config()
        )
        runner.run(CommandRequest(
            command="python",
            args=("-c", "-v", "/etc/passwd"),
        ))
        args = list(recording.requests[-1].args)
        # -v 不出现在 docker run 参数中
        docker_args = args[:args.index("python:3.12-slim")]
        self.assertNotIn("-v", docker_args)

    def test_args_special_chars(self):
        """用户 args 含 Shell 特殊字符。"""
        cmd = self.container_cmd()
        recording = _RecordingRunner()
        runner = DockerSandboxRunner(
            self.resolver, recording, [cmd], self.config()
        )
        runner.run(CommandRequest(
            command="python",
            args=("-c", "print('a;b&|')"),
        ))
        args = list(recording.requests[-1].args)
        # 所有参数作为列表传递，不经过 Shell
        self.assertIn("print('a;b&|')", args)


# --- 路径映射测试 ---


class TestPathMapping(_DockerTestCase):
    """cwd 映射和路径处理。"""

    def test_cwd_none(self):
        """cwd=None 使用 workspace root。"""
        cmd = self.container_cmd()
        recording = _RecordingRunner()
        runner = DockerSandboxRunner(
            self.resolver, recording, [cmd], self.config()
        )
        runner.run(CommandRequest(command="python"))
        args = list(recording.requests[-1].args)
        workdir_idx = args.index("--workdir")
        self.assertEqual(args[workdir_idx + 1], "/workspace")

    def test_cwd_subdirectory(self):
        """子目录 cwd。"""
        subdir = self.root / "sub"
        subdir.mkdir()
        cmd = self.container_cmd()
        recording = _RecordingRunner()
        runner = DockerSandboxRunner(
            self.resolver, recording, [cmd], self.config()
        )
        runner.run(CommandRequest(command="python", cwd="sub"))
        args = list(recording.requests[-1].args)
        workdir_idx = args.index("--workdir")
        self.assertEqual(args[workdir_idx + 1], "/workspace/sub")

    def test_cwd_nonexistent(self):
        """cwd 不存在。"""
        from mini_deerflow.sandbox_path import SandboxPathNotFoundError
        r = self.runner(self.container_cmd())
        with self.assertRaises(SandboxPathNotFoundError):
            r.run(CommandRequest(command="python", cwd="missing"))

    def test_cwd_is_file(self):
        """cwd 是文件。"""
        from mini_deerflow.sandbox_files import SandboxNotADirectoryError
        (self.root / "file.txt").touch()
        r = self.runner(self.container_cmd())
        with self.assertRaises(SandboxNotADirectoryError):
            r.run(CommandRequest(command="python", cwd="file.txt"))

    def test_cwd_traversal_rejected(self):
        """cwd 路径穿越被拒绝。"""
        from mini_deerflow.sandbox_path import SandboxPathViolationError
        r = self.runner(self.container_cmd())
        with self.assertRaises(SandboxPathViolationError):
            r.run(CommandRequest(command="python", cwd="../evil"))

    def test_cwd_symlink_outside_rejected(self):
        """cwd 指向外部的符号链接被拒绝。"""
        from mini_deerflow.sandbox_path import SandboxPathViolationError
        outside = Path(self._tmp.name).parent / "outside_docker"
        outside.mkdir(exist_ok=True)
        link = self.root / "escape"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("符号链接不支持")

        r = self.runner(self.container_cmd())
        with self.assertRaises(SandboxPathViolationError):
            r.run(CommandRequest(command="python", cwd="escape"))

    def test_chinese_cwd(self):
        """中文路径。"""
        subdir = self.root / "项目"
        subdir.mkdir()
        cmd = self.container_cmd()
        recording = _RecordingRunner()
        runner = DockerSandboxRunner(
            self.resolver, recording, [cmd], self.config()
        )
        runner.run(CommandRequest(command="python", cwd="项目"))
        args = list(recording.requests[-1].args)
        workdir_idx = args.index("--workdir")
        self.assertIn("/workspace/项目", args[workdir_idx + 1])

    def test_root_with_space(self):
        """Root 含空格时正确处理。"""
        root_space = Path(self._tmp.name) / "my workspace"
        root_space.mkdir()
        resolver = WorkspacePathResolver(root_space)
        cmd = self.container_cmd()
        recording = _RecordingRunner()
        runner = DockerSandboxRunner(
            resolver, recording, [cmd], self.config()
        )
        runner.run(CommandRequest(command="python"))
        args = list(recording.requests[-1].args)
        mount_idx = args.index("--mount")
        mount = args[mount_idx + 1]
        # argv list 传递，不需要引号
        self.assertIn("my workspace", mount)

    def test_root_with_comma_rejected(self):
        """Root 含逗号时明确拒绝。"""
        root_comma = Path(self._tmp.name) / "folder,name"
        root_comma.mkdir()
        resolver = WorkspacePathResolver(root_comma)
        cmd = self.container_cmd()
        recording = _RecordingRunner()
        runner = DockerSandboxRunner(
            resolver, recording, [cmd], self.config()
        )
        with self.assertRaises(DockerConfigurationError):
            runner.run(CommandRequest(command="python"))

    def test_result_cwd_relative(self):
        """返回的 cwd 是相对路径。"""
        subdir = self.root / "sub"
        subdir.mkdir()
        cmd = self.container_cmd()
        recording = _RecordingRunner(CommandResult(
            command="docker", args=(), cwd=Path("."),
            exit_code=0, stdout="", stderr="", duration_seconds=0.1,
        ))
        runner = DockerSandboxRunner(
            self.resolver, recording, [cmd], self.config()
        )
        result = runner.run(CommandRequest(command="python", cwd="sub"))
        self.assertEqual(result.cwd, Path("sub"))
        self.assertFalse(result.cwd.is_absolute())


# --- 结果映射测试 ---


class TestResultMapping(_DockerTestCase):
    """CommandResult 隐藏 Docker 实现细节。"""

    def test_command_mapped(self):
        """command 映射回逻辑容器命令。"""
        recording = _RecordingRunner(CommandResult(
            command="docker", args=(), cwd=Path("."),
            exit_code=0, stdout="hello", stderr="", duration_seconds=0.1,
        ))
        runner = DockerSandboxRunner(
            self.resolver, recording, [self.container_cmd()], self.config()
        )
        result = runner.run(CommandRequest(command="python"))
        self.assertEqual(result.command, "python")

    def test_docker_command_not_exposed(self):
        """不暴露 docker 字符串。"""
        recording = _RecordingRunner(CommandResult(
            command="docker", args=(), cwd=Path("."),
            exit_code=0, stdout="", stderr="", duration_seconds=0.1,
        ))
        runner = DockerSandboxRunner(
            self.resolver, recording, [self.container_cmd()], self.config()
        )
        result = runner.run(CommandRequest(command="python"))
        self.assertNotEqual(result.command, "docker")

    def test_docker_args_not_exposed(self):
        """不暴露完整 docker argv。"""
        recording = _RecordingRunner(CommandResult(
            command="docker", args=(), cwd=Path("."),
            exit_code=0, stdout="", stderr="", duration_seconds=0.1,
        ))
        runner = DockerSandboxRunner(
            self.resolver, recording, [self.container_cmd()], self.config()
        )
        result = runner.run(CommandRequest(command="python"))
        self.assertEqual(len(result.args), 0)

    def test_nonzero_exit_preserved(self):
        """非零 exit_code 保留。"""
        recording = _RecordingRunner(CommandResult(
            command="docker", args=(), cwd=Path("."),
            exit_code=1, stdout="error", stderr="", duration_seconds=0.1,
        ))
        runner = DockerSandboxRunner(
            self.resolver, recording, [self.container_cmd()], self.config()
        )
        result = runner.run(CommandRequest(command="python"))
        self.assertEqual(result.exit_code, 1)

    def test_stdout_preserved(self):
        """stdout 保留。"""
        recording = _RecordingRunner(CommandResult(
            command="docker", args=(), cwd=Path("."),
            exit_code=0, stdout="container output", stderr="", duration_seconds=0.1,
        ))
        runner = DockerSandboxRunner(
            self.resolver, recording, [self.container_cmd()], self.config()
        )
        result = runner.run(CommandRequest(command="python"))
        self.assertEqual(result.stdout, "container output")

    def test_request_not_modified(self):
        """request 不被修改。"""
        recording = _RecordingRunner()
        runner = DockerSandboxRunner(
            self.resolver, recording, [self.container_cmd()], self.config()
        )
        req = CommandRequest(command="python", args=("-c", "print(1)"))
        orig_args = req.args
        runner.run(req)
        self.assertEqual(req.args, orig_args)


# --- 异常与清理测试 ---


class TestExceptionAndCleanup(_DockerTestCase):
    """异常处理和容器清理。"""

    def test_unknown_command(self):
        """未知容器命令。"""
        r = self.runner()
        with self.assertRaises(DockerCommandNotAllowedError) as ctx:
            r.run(CommandRequest(command="unknown"))
        self.assertEqual(ctx.exception.command, "unknown")

    def test_host_runner_error_propagated(self):
        """host_runner 异常原样传播。"""
        from mini_deerflow.sandbox_command import CommandTimeoutError
        error = CommandTimeoutError("docker", 10.0, "", "")
        recording = _RecordingRunner(error=error)
        runner = DockerSandboxRunner(
            self.resolver, recording, [self.container_cmd()], self.config()
        )
        with self.assertRaises(CommandTimeoutError):
            runner.run(CommandRequest(command="python"))

    def test_timeout_triggers_cleanup(self):
        """超时后触发清理。"""
        from mini_deerflow.sandbox_command import CommandTimeoutError
        error = CommandTimeoutError("docker", 10.0, "", "")
        recording = _RecordingRunner(error=error)
        runner = DockerSandboxRunner(
            self.resolver,
            recording,
            [self.container_cmd()],
            self.config(),
            container_name_factory=lambda: "mini-test-123",
        )
        with self.assertRaises(CommandTimeoutError):
            runner.run(CommandRequest(command="python"))

        # 清理请求被发送
        self.assertEqual(len(recording.requests), 2)
        cleanup_req = recording.requests[-1]
        self.assertEqual(cleanup_req.command, "docker")
        self.assertIn("rm", cleanup_req.args)
        self.assertIn("-f", cleanup_req.args)
        self.assertIn("mini-test-123", cleanup_req.args)

    def test_output_limit_triggers_cleanup(self):
        """输出超限后触发清理。"""
        from mini_deerflow.sandbox_command import CommandOutputLimitError
        error = CommandOutputLimitError("docker", 1024, "", "")
        recording = _RecordingRunner(error=error)
        runner = DockerSandboxRunner(
            self.resolver,
            recording,
            [self.container_cmd()],
            self.config(),
            container_name_factory=lambda: "mini-test-456",
        )
        with self.assertRaises(CommandOutputLimitError):
            runner.run(CommandRequest(command="python"))

        self.assertEqual(len(recording.requests), 2)
        cleanup_req = recording.requests[-1]
        self.assertIn("mini-test-456", cleanup_req.args)

    def test_normal_result_no_extra_cleanup(self):
        """正常结果不额外清理。"""
        recording = _RecordingRunner(CommandResult(
            command="docker", args=(), cwd=Path("."),
            exit_code=0, stdout="", stderr="", duration_seconds=0.5,
        ))
        runner = DockerSandboxRunner(
            self.resolver,
            recording,
            [self.container_cmd()],
            self.config(),
        )
        runner.run(CommandRequest(command="python"))
        # 只有一条请求（docker run），--rm 会自动清理
        self.assertEqual(len(recording.requests), 1)

    def test_cleanup_only_targets_current_container(self):
        """清理只针对当前容器名。"""
        from mini_deerflow.sandbox_command import CommandTimeoutError
        error = CommandTimeoutError("docker", 10.0, "", "")
        recording = _RecordingRunner(error=error)
        runner = DockerSandboxRunner(
            self.resolver,
            recording,
            [self.container_cmd()],
            self.config(),
            container_name_factory=lambda: "mini-test-other",
        )
        with self.assertRaises(CommandTimeoutError):
            runner.run(CommandRequest(command="python"))

        cleanup_req = recording.requests[-1]
        self.assertIn("mini-test-other", cleanup_req.args)
        self.assertNotIn("mini-deerflow-", cleanup_req.args)

    def test_cleanup_uses_short_timeout(self):
        """清理使用短超时。"""
        from mini_deerflow.sandbox_command import CommandTimeoutError
        error = CommandTimeoutError("docker", 10.0, "", "")
        recording = _RecordingRunner(error=error)
        runner = DockerSandboxRunner(
            self.resolver, recording, [self.container_cmd()], self.config()
        )
        with self.assertRaises(CommandTimeoutError):
            runner.run(CommandRequest(command="python"))

        cleanup_req = recording.requests[-1]
        self.assertEqual(cleanup_req.timeout_seconds, 5.0)

    def test_invalid_name_factory(self):
        """非法名称工厂。"""
        def bad_factory() -> str:
            return "BAD NAME with spaces"
        runner = DockerSandboxRunner(
            self.resolver,
            _RecordingRunner(),
            [self.container_cmd()],
            self.config(),
            container_name_factory=bad_factory,
        )
        with self.assertRaises(DockerConfigurationError):
            runner.run(CommandRequest(command="python"))


# --- 渐进式架构测试 ---


class TestProgressiveArchitecture(_DockerTestCase):
    """验证不直接使用 subprocess。"""

    def test_uses_host_runner(self):
        """使用注入的 host_runner。"""
        recording = _RecordingRunner()
        runner = DockerSandboxRunner(
            self.resolver, recording, [self.container_cmd()], self.config()
        )
        runner.run(CommandRequest(command="python"))
        self.assertEqual(len(recording.requests), 1)

    def test_command_is_docker_name(self):
        """请求的 command 是 docker 逻辑名称。"""
        recording = _RecordingRunner()
        runner = DockerSandboxRunner(
            self.resolver,
            recording,
            [self.container_cmd()],
            self.config(),
            docker_command_name="docker",
        )
        runner.run(CommandRequest(command="python"))
        self.assertEqual(recording.requests[-1].command, "docker")


# --- 异常层次测试 ---


class TestExceptionHierarchy(_DockerTestCase):
    """异常继承关系。"""

    def test_all_inherit_base(self):
        """所有 Docker 异常继承 DockerSandboxError。"""
        for cls in (
            DockerCommandNotAllowedError,
            DockerConfigurationError,
            DockerCleanupError,
        ):
            with self.subTest(cls.__name__):
                self.assertTrue(issubclass(cls, DockerSandboxError))


if __name__ == "__main__":
    unittest.main()
