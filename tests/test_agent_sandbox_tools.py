"""Agent 沙箱工具集成测试（16E 阶段）。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from collections import deque
from pathlib import Path

from mini_deerflow.agent import Agent
from mini_deerflow.models import FinalAnswer, Message, ToolCall
from mini_deerflow.sandbox_command import CommandRequest, CommandResult
from mini_deerflow.sandbox_files import SandboxFileSystem
from mini_deerflow.sandbox_path import WorkspacePathResolver
from mini_deerflow.sandbox_tools import SandboxToolSet
from mini_deerflow.thread_store import InMemoryThreadStore
from mini_deerflow.tools import ToolRegistry


# --- Fake command runner for testing ---


class FakeCommandRunner:
    """记录每次调用，可预设返回值。"""

    def __init__(self) -> None:
        self.calls: list[CommandRequest] = []
        self._responses: dict[str, CommandResult] = {}
        self._raise_on: type[Exception] | None = None

    def set_response(self, command: str, result: CommandResult) -> None:
        self._responses[command] = result

    def run(self, request: CommandRequest) -> CommandResult:
        self.calls.append(request)
        if self._raise_on:
            raise self._raise_on(f"simulated: {request.command}")
        return self._responses.get(
            request.command,
            CommandResult(
                command=request.command,
                args=request.args,
                cwd=Path("."),
                exit_code=0,
                stdout="",
                stderr="",
                duration_seconds=0.0,
            ),
        )


# --- Scripted model ---


class ScriptedModel:
    """按预定序列返回决策，并记录收到的消息。"""

    def __init__(self, decisions: list[FinalAnswer | ToolCall]) -> None:
        # deque 从左端 popleft()，保持调用方指定的顺序：[ToolCall, FinalAnswer]
        self._queue: deque[FinalAnswer | ToolCall] = deque(decisions)
        self.received_messages: list[list[Message]] = []

    def decide(self, messages: list[Message]) -> FinalAnswer | ToolCall:
        self.received_messages.append(list(messages))
        return self._queue.popleft()


class TestAgentSandboxToolsIntegration(unittest.TestCase):
    """验证 Agent 通过 ToolRegistry 使用 Sandbox Tool 的完整流程。"""

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="deerflow_tooltest_")
        self._workspace = Path(self._tmp)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _make_agent(self, command_runner: FakeCommandRunner | None = None) -> tuple:
        """返回 (agent, model, runner, store)。"""
        resolver = WorkspacePathResolver(self._workspace)
        fs = SandboxFileSystem(
            resolver,
            max_read_bytes=1_048_576,
            max_write_bytes=1_048_576,
            max_list_entries=1_000,
        )
        runner = command_runner or FakeCommandRunner()
        tool_set = SandboxToolSet(files=fs, runner=runner)
        registry = ToolRegistry()
        registry.register_many(tool_set.create_tools())
        store = InMemoryThreadStore()
        store.create("test")
        model = ScriptedModel([])
        agent = Agent(
            model=model,
            registry=registry,
            store=store,
            max_steps=5,
        )
        return agent, model, runner, store

    def test_read_file_success(self):
        (self._workspace / "data.txt").write_text("hello world", encoding="utf-8")
        agent, model, _, _ = self._make_agent()
        model._queue = deque([
            ToolCall(name="sandbox_read_file", arguments={"path": "data.txt"}),
            FinalAnswer(content="读取成功"),
        ])
        result, _ = agent.run("test", "读取文件")
        self.assertEqual(result, "读取成功")
        tool_msg = model.received_messages[-1][-1]
        d = json.loads(tool_msg.content)
        self.assertTrue(d["ok"])
        self.assertEqual(d["content"], "hello world")

    def test_write_file_success(self):
        agent, model, _, _ = self._make_agent()
        model._queue = deque([
            ToolCall(
                name="sandbox_write_file",
                arguments={"path": "output.txt", "content": "test data"},
            ),
            FinalAnswer(content="写入成功"),
        ])
        agent.run("test", "写文件")
        written = (self._workspace / "output.txt").read_text(encoding="utf-8")
        self.assertEqual(written, "test data")

    def test_list_directory_success(self):
        (self._workspace / "a.txt").write_text("x", encoding="utf-8")
        (self._workspace / "b.py").write_text("y", encoding="utf-8")
        agent, model, _, _ = self._make_agent()
        model._queue = deque([
            ToolCall(name="sandbox_list_directory", arguments={}),
            FinalAnswer(content="列目录完成"),
        ])
        agent.run("test", "列出目录")
        tool_msg = model.received_messages[-1][-1]
        d = json.loads(tool_msg.content)
        self.assertTrue(d["ok"])
        self.assertEqual(len(d["entries"]), 2)

    def test_make_directory_success(self):
        agent, model, _, _ = self._make_agent()
        model._queue = deque([
            ToolCall(
                name="sandbox_make_directory",
                arguments={"path": "src/tests", "parents": True},
            ),
            FinalAnswer(content="目录创建成功"),
        ])
        agent.run("test", "创建目录")
        self.assertTrue((self._workspace / "src" / "tests").is_dir())

    def test_run_command_success(self):
        runner = FakeCommandRunner()
        runner.set_response(
            "python",
            CommandResult(
                command="python",
                args=("-c", "print(1+2)"),
                cwd=Path("."),
                exit_code=0,
                stdout="3\n",
                stderr="",
                duration_seconds=0.05,
            ),
        )
        agent, model, _, _ = self._make_agent(runner)
        model._queue = deque([
            ToolCall(
                name="sandbox_run_command",
                arguments={"command": "python", "args": ["-c", "print(1+2)"]},
            ),
            FinalAnswer(content="命令执行成功"),
        ])
        agent.run("test", "运行命令")
        tool_msg = model.received_messages[-1][-1]
        d = json.loads(tool_msg.content)
        self.assertTrue(d["ok"])
        self.assertEqual(d["stdout"], "3\n")

    def test_run_command_nonzero_exit(self):
        runner = FakeCommandRunner()
        runner.set_response(
            "python",
            CommandResult(
                command="python",
                args=("-c", "exit(1)"),
                cwd=Path("."),
                exit_code=1,
                stdout="",
                stderr="error",
                duration_seconds=0.01,
            ),
        )
        agent, model, _, _ = self._make_agent(runner)
        model._queue = deque([
            ToolCall(name="sandbox_run_command", arguments={"command": "python"}),
            FinalAnswer(content="命令失败了但我知道"),
        ])
        agent.run("test", "尝试运行")
        tool_msg = model.received_messages[-1][-1]
        d = json.loads(tool_msg.content)
        self.assertTrue(d["ok"])
        self.assertEqual(d["exit_code"], 1)

    def test_sandbox_rejection_returns_ok_false(self):
        agent, model, _, _ = self._make_agent()
        model._queue = deque([
            ToolCall(
                name="sandbox_read_file",
                arguments={"path": "../secret.txt"},
            ),
            FinalAnswer(content="安全边界阻止了访问"),
        ])
        result, _ = agent.run("test", "尝试越界")
        tool_msg = model.received_messages[-1][-1]
        d = json.loads(tool_msg.content)
        self.assertFalse(d["ok"])
        self.assertEqual(d["error"]["type"], "SandboxPathViolationError")
        self.assertIn("安全边界", result)

    def test_tool_message_is_valid_json(self):
        (self._workspace / "data.json").write_text(
            '{"key": "value"}', encoding="utf-8"
        )
        agent, model, _, _ = self._make_agent()
        model._queue = deque([
            ToolCall(name="sandbox_read_file", arguments={"path": "data.json"}),
            FinalAnswer(content="收到 JSON"),
        ])
        agent.run("test", "读取 JSON")
        tool_msg = model.received_messages[-1][-1]
        parsed = json.loads(tool_msg.content)
        self.assertIsInstance(parsed, dict)

    def test_tool_message_no_absolute_paths(self):
        (self._workspace / "internal.txt").write_text("secret", encoding="utf-8")
        agent, model, _, _ = self._make_agent()
        model._queue = deque([
            ToolCall(name="sandbox_read_file", arguments={"path": "internal.txt"}),
            FinalAnswer(content="完成"),
        ])
        agent.run("test", "读取")
        tool_msg = model.received_messages[-1][-1]
        content = tool_msg.content
        # 不应暴露工作区外的路径
        self.assertNotIn("C:\\Windows", content)
        self.assertNotIn("D:\\", content)

    def test_tool_message_no_docker_args(self):
        runner = FakeCommandRunner()
        runner.set_response(
            "python",
            CommandResult(
                command="python",
                args=(),
                cwd=Path("."),
                exit_code=0,
                stdout="",
                stderr="",
                duration_seconds=0.0,
            ),
        )
        agent, model, _, _ = self._make_agent(runner)
        model._queue = deque([
            ToolCall(name="sandbox_run_command", arguments={"command": "python"}),
            FinalAnswer(content="完成"),
        ])
        agent.run("test", "运行")
        tool_msg = model.received_messages[-1][-1]
        content = tool_msg.content
        for forbidden in ("--rm", "--network", "--memory", "--cap-drop", "--mount"):
            self.assertNotIn(forbidden, content)

    def test_thread_saves_successful_tool_result(self):
        (self._workspace / "saved.txt").write_text("content", encoding="utf-8")
        agent, model, _, store = self._make_agent()
        model._queue = deque([
            ToolCall(name="sandbox_read_file", arguments={"path": "saved.txt"}),
            FinalAnswer(content="完成"),
        ])
        agent.run("test", "读取")
        snapshot = store.get("test").snapshot()
        roles = [m.role for m in snapshot]
        self.assertIn("tool", roles)
        tool_msg = next(m for m in snapshot if m.role == "tool")
        self.assertIn("saved.txt", tool_msg.content)

    def test_tool_call_message_order(self):
        (self._workspace / "order.txt").write_text("x", encoding="utf-8")
        agent, model, _, store = self._make_agent()
        model._queue = deque([
            ToolCall(name="sandbox_read_file", arguments={"path": "order.txt"}),
            FinalAnswer(content="结束"),
        ])
        agent.run("test", "读取")
        snapshot = store.get("test").snapshot()
        roles = [m.role for m in snapshot]
        self.assertEqual(roles, ["user", "tool", "assistant"])

    def test_unknown_tool_raises(self):
        from mini_deerflow.tools import ToolNotFoundError

        class BadModel:
            def decide(self, messages):
                return ToolCall(name="sandbox_nonexistent", arguments={})

        agent, model, _, store = self._make_agent()
        bad_agent = Agent(
            model=BadModel(),
            registry=agent._registry,
            store=store,
            max_steps=1,
        )
        with self.assertRaises(ToolNotFoundError):
            bad_agent.run("test", "未知工具")

    def test_external_side_effects_persist(self):
        """文件写入不随 Thread 回滚。Agent 异常时文件仍存在。"""

        class FailingModel:
            calls = 0

            def decide(self, messages):
                FailingModel.calls += 1
                if FailingModel.calls == 1:
                    return ToolCall(
                        name="sandbox_write_file",
                        arguments={"path": "side_effect.txt", "content": "written"},
                    )
                raise RuntimeError("model crashed after side effect")

        agent, model, _, store = self._make_agent()
        agent._max_steps = 2
        agent._model = FailingModel()

        with self.assertRaises(RuntimeError):
            agent.run("test", "写入后崩溃")

        # 文件写入仍然存在（外部副作用不自动回滚）
        self.assertTrue((self._workspace / "side_effect.txt").exists())
        self.assertEqual(
            (self._workspace / "side_effect.txt").read_text(encoding="utf-8"),
            "written",
        )
        # Thread 未提交
        self.assertEqual(store.get("test").message_count, 0)


if __name__ == "__main__":
    unittest.main()
