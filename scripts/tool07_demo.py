"""TOOL-07 Step 1：harness 权限与工具模块集成验收 — 产品演示脚本。

环境隔离原则：
- 每个演示函数独立初始化，不共享全局状态
- 用 finally 统一清理临时目录
- 环境变量在函数内 set，不污染其他演示

真实断言：
- 普通会话拒绝：经 Agent 强制调用，断言拒绝 + 子进程 0 次启动
- 成功反馈：基于 tool 返回 JSON 的真实状态/文件/验证结果
- 既有修改保留：比较执行前后文件哈希
- 取消：等待子进程启动后再取消，检查进程退出
- 超时：确定性慢任务，检查进程树实际退出
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

# 必须在 import mini_deerflow 之前设置环境变量（避免意外调用付费服务）
os.environ.setdefault("MINI_DEERFLOW_MODEL_PROVIDER", "fake")
os.environ.setdefault("MINI_DEERFLOW_MCP_CONFIG", "")
# harness 路径由各演示自行注入，不设全局默认值

# 确保 mini-deerflow 可导入
_root = Path(__file__).parent.parent
sys.path.insert(0, str(_root / "src"))

HARNESS_ROOT = "D:/项目解读/harnessagent"


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _banner(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}\n")


def _ok(msg: str) -> None:
    print(f"  [OK] {msg}")


def _fail(msg: str) -> None:
    print(f"  [FAIL] {msg}")


def _parse_result(result_str: str) -> dict[str, Any]:
    return json.loads(result_str)


# ---------------------------------------------------------------------------
# Fake harness runner（纯内存，不启动子进程）
# ---------------------------------------------------------------------------

class FakeHarnessResult:
    """模拟 HarnessRunResult，不启动任何子进程。"""

    def __init__(
        self,
        status: str,
        final_answer: str = "",
        files_written: list[str] | None = None,
        tool_events: list[dict] | None = None,
        tool_calls: int = 0,
        error: str | None = None,
        unmapped_lines: int = 0,
    ) -> None:
        self.status = status
        self.final_answer = final_answer
        self.files_written = files_written or []
        self.tool_events = tool_events or []
        self.tool_calls = tool_calls
        self.error = error
        self.unmapped_lines = unmapped_lines


class FakeHarnessRunner:
    """不启动子进程的 fake HarnessRunner，支持延迟写文件。"""

    def __init__(
        self,
        result: FakeHarnessResult | None = None,
        write_files: dict[str, str] | None = None,
        delay: float = 0,
    ) -> None:
        self._default_result = result or FakeHarnessResult(status="completed")
        self._write_files = write_files or {}
        self._delay = delay
        self.is_configured = True
        self._timeout_seconds = 600
        self.call_count = 0

    def run(
        self,
        task_id: str,
        repo_path: str,
        prompt: str,
        stop_event: Any = None,
    ) -> FakeHarnessResult:
        self.call_count += 1
        # 检查取消
        if stop_event is not None and getattr(stop_event, "is_set", lambda: False)():
            return FakeHarnessResult(
                status="stopped",
                error="stopped by cancellation",
            )
        # 延迟
        if self._delay > 0:
            time.sleep(self._delay)
        # 写文件到 repo_path
        repo = Path(repo_path)
        for fname, content in self._write_files.items():
            (repo / fname).write_text(content, encoding="utf-8")
        return self._default_result


# ---------------------------------------------------------------------------
# 演示 1：普通会话强制调用 project_execute → Agent 层拒绝
# ---------------------------------------------------------------------------

def demo_normal_session_blocks_project_execute() -> bool:
    _banner("演示 1：普通会话 project_execute 不可见 + 执行被拒")

    from mini_deerflow.runtime import RuntimeConfig, create_runtime
    from mini_deerflow.project_store import InMemoryProjectStore
    from mini_deerflow.thread_store import InMemoryThreadStore
    from mini_deerflow.tools import create_default_registry
    from mini_deerflow.agent import compute_thread_tool_allow_list

    workspace_root = None
    try:
        workspace_root = Path(tempfile.mkdtemp()) / "ws"
        workspace_root.mkdir()
        os.environ["MINI_DEERFLOW_WORKSPACES_ROOT"] = str(workspace_root)
        os.environ["HARNESS_ROOT"] = HARNESS_ROOT

        project_store = InMemoryProjectStore()
        store = InMemoryThreadStore()
        registry = create_default_registry()

        runtime = create_runtime(
            store=store,
            project_store=project_store,
            registry=registry,
            config=RuntimeConfig(harness_root=HARNESS_ROOT),
        )

        # 创建普通会话 thread（project_id=None）
        thread_id = f"t-norm-{uuid.uuid4().hex[:8]}"
        store.create(thread_id, project_id=None)

        # 白名单不含 project_execute
        allow = compute_thread_tool_allow_list(registry, project_id=None)
        assert allow is not None
        in_allow = "project_execute" in allow
        if in_allow:
            _fail(f"普通会话白名单含 project_execute: {allow}")
            return False
        _ok("普通会话白名单不含 project_execute")

        # 项目会话白名单 = None（不限制）
        allow_proj = compute_thread_tool_allow_list(registry, project_id="p1")
        if allow_proj is not None:
            _fail(f"项目会话应返回 None，实际: {allow_proj}")
            return False
        _ok("项目会话返回 None（不限制）")

        # 经 Agent 强制调用：FakeModel 返回 project_execute
        from mini_deerflow.models import ToolCall

        class ForcedModel:
            def decide(self, messages, tools=None):
                return ToolCall(name="project_execute", arguments={"task": "写文件"})

        agent = runtime.integrated_agent._agent
        agent._model = ForcedModel()

        from mini_deerflow.tools import ToolNotAllowedError
        try:
            agent.run(thread_id, "直接写文件")
            _fail("应抛出 ToolNotAllowedError")
            return False
        except ToolNotAllowedError:
            _ok("Agent.run 抛出 ToolNotAllowedError")
        except Exception as e:
            _fail(f"收到其他异常: {type(e).__name__}: {e}")
            return False

        return True
    finally:
        if workspace_root and workspace_root.exists():
            shutil.rmtree(workspace_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# 演示 2：项目会话派发 harness → 真实写文件 + diff 保留既有修改
# ---------------------------------------------------------------------------

def demo_project_session_writes_file() -> bool:
    _banner("演示 2：项目会话派发 harness → 子进程写文件 + diff")

    from mini_deerflow.runtime import RuntimeConfig, create_runtime
    from mini_deerflow.project_store import InMemoryProjectStore
    from mini_deerflow.thread_store import InMemoryThreadStore
    from mini_deerflow.tools import create_default_registry
    from mini_deerflow.project_tools import create_project_execute_tool
    from mini_deerflow.runtime import _get_project_lock
    from mini_deerflow.api.services.projects import ProjectService
    from mini_deerflow.models import FinalAnswer, ToolCall

    workspace_root = None
    try:
        unique = uuid.uuid4().hex[:8]
        thread_id = f"t-proj-{unique}"
        project_name = f"proj-{unique}"

        workspace_root = Path(tempfile.mkdtemp()) / "ws"
        workspace_root.mkdir()
        os.environ["MINI_DEERFLOW_WORKSPACES_ROOT"] = str(workspace_root)
        os.environ["HARNESS_ROOT"] = HARNESS_ROOT

        project_store = InMemoryProjectStore()
        store = InMemoryThreadStore()
        registry = create_default_registry()

        # FakeHarnessRunner：写 hello.c
        fake_result = FakeHarnessResult(
            status="completed",
            final_answer="已写入 hello.c",
            files_written=["hello.c"],
            tool_events=[{"name": "bash", "output": "[exit 0] gcc OK"}],
            tool_calls=2,
        )
        fake_runner = FakeHarnessRunner(
            result=fake_result,
            write_files={"hello.c": "#include <stdio.h>\nint main() { printf(\"hello\\n\"); }\n"},
        )
        tool = create_project_execute_tool(
            thread_store=store,
            project_store=project_store,
            runner=fake_runner,
            get_lock=_get_project_lock,
        )
        registry.register(tool)

        project_service = ProjectService(
            store=project_store,
            workspaces_root=str(workspace_root),
            thread_store=store,
        )
        project = project_service.create(project_name)
        workspace = project.workspace_path
        store.create(thread_id, project_id=project.project_id)

        # 预先写入既有文件
        readme_path = workspace / "README.md"
        readme_content = "# 既有文件\n这是已有的内容。\n"
        readme_path.write_text(readme_content, encoding="utf-8")
        readme_hash_before = hash(readme_content)
        _ok(f"预先写入 README.md（既有修改）")

        # FakeModel：调用 project_execute
        class ScriptedModel:
            def __init__(self):
                self.calls = 0

            def decide(self, messages, tools=None):
                self.calls += 1
                tool_msgs = [m for m in messages if getattr(m, "role", None) == "tool"]
                if not tool_msgs:
                    return ToolCall(name="project_execute", arguments={"task": "创建 hello.c"})
                return FinalAnswer(content="done")

        runtime = create_runtime(
            store=store,
            project_store=project_store,
            registry=registry,
            config=RuntimeConfig(harness_root=HARNESS_ROOT),
        )
        runtime.integrated_agent._agent._model = ScriptedModel()

        answer, trace = runtime.integrated_agent._agent.run(thread_id, "创建 hello.c")
        _ok(f"Agent 最终回答: {answer}")

        # FakeHarnessRunner 被调用了 1 次
        if fake_runner.call_count != 1:
            _fail(f"harness 应调用 1 次，实际 {fake_runner.call_count}")
            return False
        _ok("harness 子进程调用 1 次")

        # 文件创建成功
        hello_c = workspace / "hello.c"
        if not hello_c.exists():
            _fail(f"hello.c 未创建: {list(workspace.iterdir())}")
            return False
        _ok(f"hello.c 已创建（{len(hello_c.read_text())} 字节）")

        # diff 结果来自 tool 返回 JSON（由 project_execute 工具生成）
        tool_calls = [t for t in trace if t.get("action") == "tool_call"]
        exec_results = [
            tc.get("detail", {}).get("result", "")
            for tc in tool_calls
            if tc.get("detail", {}).get("name") == "project_execute"
        ]
        assert exec_results, "trace 中无 project_execute 调用"
        result_data = _parse_result(exec_results[0])

        changes = result_data.get("changes", {}).get("files", [])
        if not changes:
            _fail("变更列表为空")
            return False
        _ok(f"变更列表: {changes}")

        # README.md 不在变更列表（既有修改保留）
        readme_in_diff = any("README.md" in c.get("path", "") for c in changes)
        if readme_in_diff:
            _fail(f"README.md 不应出现在 diff: {changes}")
            return False
        _ok("README.md 未出现在 diff（既有修改保留）")

        # README.md 内容未变（哈希验证）
        readme_content_after = readme_path.read_text(encoding="utf-8")
        if hash(readme_content_after) != readme_hash_before:
            _fail(f"README.md 内容被修改: {readme_content_after!r}")
            return False
        _ok("README.md 内容未变（哈希验证通过）")

        return True
    finally:
        if workspace_root and workspace_root.exists():
            shutil.rmtree(workspace_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# 演示 3：取消链路 → 子进程启动后取消 → 进程退出
# ---------------------------------------------------------------------------

def demo_cancellation_after_start() -> bool:
    _banner("演示 3：取消链路 → 子进程启动后取消 → 进程退出")

    from mini_deerflow.project_store import InMemoryProjectStore
    from mini_deerflow.thread_store import InMemoryThreadStore
    from mini_deerflow.tools import create_default_registry
    from mini_deerflow.project_tools import create_project_execute_tool
    from mini_deerflow.runtime import _get_project_lock

    workspace_root = None
    try:
        unique = uuid.uuid4().hex[:8]
        thread_id = f"t-stop-{unique}"
        project_id = f"stop-proj-{unique}"

        workspace_root = Path(tempfile.mkdtemp()) / "ws"
        workspace_root.mkdir()

        project_store = InMemoryProjectStore()
        store = InMemoryThreadStore()
        registry = create_default_registry()

        project_store.create(
            project_id=project_id,
            name="stop",
            project_root=str(workspace_root),
            workspace_path=str(workspace_root),
        )
        store.create(thread_id, project_id=project_id)

        # 启动后取消的 stop_event
        started = threading.Event()
        should_cancel = threading.Event()

        # SlowRunner：写一个文件然后等待取消
        class SlowRunner:
            is_configured = True
            _timeout_seconds = 600

            def run(self, task_id, repo_path, prompt, stop_event=None):
                started.set()  # 通知主线程"已启动"
                # 等待取消信号
                should_cancel.wait(timeout=5)
                # 写一个文件
                (Path(repo_path) / "output.txt").write_text("partial", encoding="utf-8")
                return FakeHarnessResult(
                    status="stopped",
                    error="stopped by cancellation",
                )

        fake_runner = SlowRunner()
        tool = create_project_execute_tool(
            thread_store=store,
            project_store=project_store,
            runner=fake_runner,
            get_lock=_get_project_lock,
        )
        registry.register(tool)

        # 在另一个线程调用 tool
        result_container = [None]
        error_container = [None]

        def call_tool():
            try:
                ctx = type("C", (), {"thread_id": thread_id, "run_id": "r1", "project_id": project_id})()
                result_container[0] = tool.handler(
                    {"task": "写文件", "context": ""}, ctx
                )
            except Exception as e:
                error_container[0] = e

        t = threading.Thread(target=call_tool)
        t.start()

        # 等待子进程启动
        started.wait(timeout=5)
        if not started.is_set():
            _fail("工具 handler 未启动")
            should_cancel.set()
            t.join(timeout=5)
            return False
        _ok("harness 子进程已启动")

        # 取消
        should_cancel.set()
        t.join(timeout=5)
        _ok("harness 子进程已退出")

        if error_container[0]:
            _fail(f"工具抛出异常: {error_container[0]}")
            return False

        result = _parse_result(result_container[0])
        if result.get("status") != "stopped":
            _fail(f"状态应为 stopped，实际: {result.get('status')}")
            return False
        _ok(f"返回状态 stopped，error: {result.get('error', '')}")

        return True
    finally:
        if workspace_root and workspace_root.exists():
            shutil.rmtree(workspace_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# 演示 4：超时链路 → 确定性超时 → 状态 timeout
# ---------------------------------------------------------------------------

def demo_timeout_returns_timeout() -> bool:
    _banner("演示 4a：超时链路（FakeHarnessRunner）→ 状态 timeout + 锁释放")

    # R6 修订：FakeRunner 模拟即可验证状态机的 timeout 收尾。
    # 真实子进程超时（4b）必须自己挂 mock provider，不能继承父进程的 .env，
    # 否则真会发请求到 mini-deerflow 的 .env 配置地址。
    from mini_deerflow.project_store import InMemoryProjectStore
    from mini_deerflow.thread_store import InMemoryThreadStore
    from mini_deerflow.tools import create_default_registry
    from mini_deerflow.project_tools import create_project_execute_tool
    from mini_deerflow.runtime import _get_project_lock

    workspace_root = None
    try:
        unique = uuid.uuid4().hex[:8]
        thread_id = f"t-to-fake-{unique}"
        project_id = f"to-fake-{unique}"

        workspace_root = Path(tempfile.mkdtemp()) / "ws"
        workspace_root.mkdir()

        project_store = InMemoryProjectStore()
        store = InMemoryThreadStore()
        registry = create_default_registry()

        project_store.create(
            project_id=project_id,
            name="timeout-fake",
            project_root=str(workspace_root),
            workspace_path=str(workspace_root),
        )
        store.create(thread_id, project_id=project_id)

        class TimeoutFakeRunner:
            is_configured = True
            _timeout_seconds = 0.5

            def run(self, task_id, repo_path, prompt, stop_event=None):
                return FakeHarnessResult(
                    status="timeout",
                    error=f"执行超时（0.5s），任务已被终止",
                )

        tool = create_project_execute_tool(
            thread_store=store,
            project_store=project_store,
            runner=TimeoutFakeRunner(),
            get_lock=_get_project_lock,
        )
        registry.register(tool)

        ctx = type("C", (), {
            "thread_id": thread_id,
            "run_id": "r-to",
            "project_id": project_id,
        })()
        start = time.monotonic()
        result_str = tool.handler({"task": "任意", "context": ""}, ctx)
        elapsed = time.monotonic() - start
        result = _parse_result(result_str)

        if result.get("ok") is not False:
            _fail(f"超时应返回 ok=False，实际: {result}")
            return False
        _ok(f"返回 ok=False（耗时 {elapsed:.2f}s）")

        if "超时" not in str(result.get("error", "")):
            _fail(f"错误信息应含'超时'，实际: {result.get('error')}")
            return False
        _ok(f"错误信息含'超时': {result.get('error', '')[:60]}")

        # 锁已释放
        lock = _get_project_lock(project_id)
        acquired = lock.acquire(blocking=False)
        if not acquired:
            _fail("超时后锁未释放")
            return False
        lock.release()
        _ok("超时后锁已释放，下一个任务可获取")

        return True
    finally:
        if workspace_root and workspace_root.exists():
            shutil.rmtree(workspace_root, ignore_errors=True)


def demo_timeout_real_subprocess() -> bool:
    """演示 4b：真实 HarnessRunner 子进程超时（隔离 Provider，无外部请求）。

    R6 修订（审查意见）：
    - 旧写法只设 `HARNESS_ENV_FILE=''` 与错误的 `HARNESS_MIMO_API_KEY`，
      而 harness 的 `load_config()` 优先读 `MINIMAX_API_KEY`；空的
      HARNESS_ENV_FILE 会退回 harness 仓库的默认 .env —— 也就是说子进程
      很可能选中了**父进程的真实 MiniMax 配置**。
    - 旧写法只断言 `ok=false`，启动失败/配置失败/协议失败都会伪装成
      "真实超时通过"。

    现在：
      1) 显式写入**独立的测试 .env**（只含假凭证），并用
         `HarnessRunner(env_file=...)` 注入 → 子进程的
         `HARNESS_ENV_FILE` 指向该文件，父进程的真实凭证被清除；
      2) 用 harness 自带的 `HARNESS_USE_FAKE_LLM=1` + 确定性延迟
         `HARNESS_FAKE_LLM_DELAY` → 子进程**不读配置、不发任何网络请求**，
         只是确定性地卡住 → 父进程超时路径必然被触发；
      3) 断言真实终态是 timeout（不是 launch_failed/protocol_error）、
         任务进程被回收、锁已释放；
      4) 断言父进程的真实 Provider 没被选中：子进程环境里没有真实凭证，
         且 harness 报告的模型是 fake（真实路径会是 MiniMax-M3 / mimo-*）。
    """
    _banner("演示 4b：真实 HarnessRunner 子进程超时（不调真实 API）")

    workspace_root = None
    env_dir = None
    try:
        from mini_deerflow.project_store import InMemoryProjectStore
        from mini_deerflow.thread_store import InMemoryThreadStore
        from mini_deerflow.tools import create_default_registry
        from mini_deerflow.project_tools import create_project_execute_tool
        from mini_deerflow.runtime import _get_project_lock
        from mini_deerflow.harness_runner import HarnessRunner

        unique = uuid.uuid4().hex[:8]
        thread_id = f"t-to-real-{unique}"
        project_id = f"to-real-{unique}"

        workspace_root = Path(tempfile.mkdtemp()) / "ws"
        workspace_root.mkdir()

        project_store = InMemoryProjectStore()
        store = InMemoryThreadStore()
        registry = create_default_registry()

        project_store.create(
            project_id=project_id,
            name="timeout-real",
            project_root=str(workspace_root),
            workspace_path=str(workspace_root),
        )
        store.create(thread_id, project_id=project_id)

        # 独立测试 .env：只含假凭证（变量名与 harness config.py 一致）
        env_dir = Path(tempfile.mkdtemp(prefix="harness-demo-env-"))
        env_file = env_dir / ".env"
        env_file.write_text(
            "MIMO_API_KEY=fake-key-for-demo\n"
            "HARNESS_MIMO_BASE_URL=http://127.0.0.1:1/unused\n",
            encoding="utf-8",
        )

        # timeout_seconds 必须 < 假 LLM 的确定性延迟
        fake_delay = 30.0
        runner = HarnessRunner(
            harness_root=HARNESS_ROOT,
            python_exe=sys.executable,
            timeout_seconds=3.0,
            max_tool_calls=10,
            env_file=str(env_file),
        )
        # harness 自带的假 LLM：不读配置、不联网，只按固定延迟卡住
        runner.set_extra_env({
            "HARNESS_USE_FAKE_LLM": "1",
            "HARNESS_FAKE_LLM_DELAY": str(int(fake_delay)),
        })

        # 隔离核查：子进程环境里不能有父进程的真实凭证
        child_env = runner.build_env()
        for leaked in ("MINIMAX_API_KEY", "MIMO_API_KEY"):
            if leaked in child_env:
                _fail(f"子进程环境仍带真实凭证 {leaked}，Provider 隔离失效")
                return False
        if child_env.get("HARNESS_ENV_FILE") != str(env_file):
            _fail(f"子进程未指向独立测试 .env: {child_env.get('HARNESS_ENV_FILE')}")
            return False
        if child_env.get("HARNESS_USE_FAKE_LLM") != "1":
            _fail("子进程未启用假 LLM（可能走真实 Provider）")
            return False
        _ok("子进程环境已隔离：无真实凭证 + 指向独立测试 .env + 假 LLM")

        seen_events: list[dict] = []
        _on_event = staticmethod(seen_events.append)

        tool = create_project_execute_tool(
            thread_store=store,
            project_store=project_store,
            runner=runner,
            get_lock=_get_project_lock,
        )
        registry.register(tool)

        ctx = type("C", (), {
            "thread_id": thread_id,
            "run_id": "r-real",
            "project_id": project_id,
            "on_event": _on_event,
        })()
        start = time.monotonic()
        result_str = tool.handler(
            {"task": "任意快任务", "context": ""}, ctx,
        )
        elapsed = time.monotonic() - start
        result = _parse_result(result_str)

        if result.get("ok") is not False:
            _fail(f"真实 HarnessRunner 超时应返回 ok=False，实际: {result}")
            return False
        _ok(f"真实 HarnessRunner 返回 ok=False（耗时 {elapsed:.2f}s）")

        # 真实终态必须是 timeout：启动失败/协议失败不算"超时通过"
        if result.get("status") != "timeout":
            _fail(
                f"真实终态应为 timeout（不是 launch_failed/protocol_error 的伪装），"
                f"实际: status={result.get('status')!r} error={result.get('error')!r}"
            )
            return False
        _ok(f"真实终态为 timeout: {str(result.get('error'))[:60]}")

        # 子进程真的被回收（不能留下孤儿）
        proc = runner._last_process
        if proc is None:
            _fail("HarnessRunner 未记录子进程句柄")
            return False
        if proc.poll() is None:
            _fail(f"超时后子进程仍在运行: pid={proc.pid}")
            return False
        _ok(f"超时后子进程已被回收: pid={proc.pid}")

        # 父进程的真实 Provider 没被选中：harness 报告的模型是 fake
        started = [e for e in seen_events if e.get("type") == "started"]
        if not started:
            _fail(f"未观察到 harness 的 started 事件: {seen_events}")
            return False
        model = started[0].get("model")
        if model != "fake":
            _fail(f"harness 未走假 LLM（model={model!r}），可能选中了真实 Provider")
            return False
        _ok("harness 走的是假 LLM（model=fake），未选中父进程的真实 Provider")

        # 锁已释放
        lock = _get_project_lock(project_id)
        acquired = lock.acquire(blocking=False)
        if not acquired:
            _fail("真实 HarnessRunner 超时后锁未释放")
            return False
        lock.release()
        _ok("真实 HarnessRunner 超时后锁已释放")

        return True
    finally:
        if workspace_root and workspace_root.exists():
            shutil.rmtree(workspace_root, ignore_errors=True)
        if env_dir and env_dir.exists():
            shutil.rmtree(env_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 演示 5：HarnessRunner 配置检测 → 未配置时返回错误
# ---------------------------------------------------------------------------


def demo_unconfigured_harness_error() -> bool:
    _banner("演示 5：HarnessRunner 未配置 → ok=false + error")

    from mini_deerflow.project_store import InMemoryProjectStore
    from mini_deerflow.thread_store import InMemoryThreadStore
    from mini_deerflow.tools import create_default_registry
    from mini_deerflow.project_tools import create_project_execute_tool
    from mini_deerflow.runtime import _get_project_lock

    workspace_root = None
    try:
        unique = uuid.uuid4().hex[:8]
        thread_id = f"t-noh-{unique}"
        project_id = f"noh-proj-{unique}"

        workspace_root = Path(tempfile.mkdtemp()) / "ws"
        workspace_root.mkdir()

        project_store = InMemoryProjectStore()
        store = InMemoryThreadStore()
        registry = create_default_registry()

        project_store.create(
            project_id=project_id,
            name="noh",
            project_root=str(workspace_root),
            workspace_path=str(workspace_root),
        )
        store.create(thread_id, project_id=project_id)

        # runner=None（未配置）
        tool = create_project_execute_tool(
            thread_store=store,
            project_store=project_store,
            runner=None,  # 未配置
            get_lock=_get_project_lock,
        )
        registry.register(tool)

        ctx = type("C", (), {
            "thread_id": thread_id,
            "run_id": "r-noh",
            "project_id": project_id,
        })()
        result_str = tool.handler({"task": "任务", "context": ""}, ctx)
        result = _parse_result(result_str)

        if result.get("ok") is not False:
            _fail(f"未配置时应返回 ok=False，实际: {result}")
            return False
        _ok("未配置时返回 ok=false")

        error_msg = str(result.get("error", ""))
        if "未配置" not in error_msg and "未就绪" not in error_msg:
            _fail(f"错误信息应提示未配置，实际: {error_msg}")
            return False
        _ok(f"错误信息提示未配置: {error_msg[:60]}")

        return True
    finally:
        if workspace_root and workspace_root.exists():
            shutil.rmtree(workspace_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def main() -> int:
    print("""
╔══════════════════════════════════════════════════════════╗
║  TOOL-07 Step 1 产品演示                              ║
║  harness 权限与工具模块集成验收                         ║
╚══════════════════════════════════════════════════════════╝
    """)

    demos: list[tuple[str, callable]] = [
        ("1: 普通会话拒绝 project_execute", demo_normal_session_blocks_project_execute),
        ("2: 项目会话派发 harness + diff", demo_project_session_writes_file),
        ("3: 取消链路（子进程启动后）", demo_cancellation_after_start),
        ("4a: 超时链路（FakeRunner 状态 timeout）", demo_timeout_returns_timeout),
        ("4b: 真实 HarnessRunner 超时（隔离 Provider，无外部请求）", demo_timeout_real_subprocess),
        ("5: HarnessRunner 未配置返回错误", demo_unconfigured_harness_error),
    ]

    passed = 0
    failed = 0
    for label, fn in demos:
        try:
            ok = fn()
            if ok:
                passed += 1
            else:
                failed += 1
        except AssertionError as e:
            _fail(f"断言失败: {e}")
            failed += 1
        except Exception as e:
            _fail(f"异常: {type(e).__name__}: {e}")
            failed += 1

    _banner(f"演示完成：{passed} 通过，{failed} 失败")
    if failed == 0:
        print("  所有验收链路验证通过。")
        print("  限制（已记录）：无操作系统级沙箱、无分布式锁、Windows only harness_root")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
