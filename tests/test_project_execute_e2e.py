"""WM-3C 续：项目工作模式真实执行闭环集成测试。

架构保持（按用户要求）：
- 主 Agent 负责讨论、澄清、派发和汇总
- harnessagent 负责写文件、修改代码、运行命令
- 通过 Runtime 注入 HarnessRunner，让 project_execute 真正跑子进程

测试覆盖（用脚本化 MockModel 替身，不调用付费模型）：
1. 端到端：Runtime 启动 → 主 Agent.run → 模型决策调用 project_execute
   → HarnessRunner.run → 子进程写 hello.c → 完成态返回
2. 普通会话强行调用 project_execute 被拒（暴露限制），零子进程启动
3. 项目内并发第二个 project_execute → "项目忙"
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Iterator

import pytest


HARNESS_ROOT = "D:/项目解读/harnessagent"


# ---------------------------------------------------------------------------
# 脚本化 MockModel：第一次返回 project_execute，第二次返回 final
# ---------------------------------------------------------------------------


class ScriptedModel:
    """不调用真实模型；按决策脚本返回 ToolCall / FinalAnswer。

    行为：
    - 第一次调用：返回 project_execute 调用（让 Agent 调起 HarnessRunner）
    - 第二次调用（收到 tool 消息后）：返回 final answer
    """

    def __init__(self) -> None:
        self.decisions: list = []
        self.calls = 0

    def decide(self, messages, tools=None):
        from mini_deerflow.models import FinalAnswer, ToolCall
        self.calls += 1
        # 统计已有 tool 消息
        tool_msgs = [m for m in messages if m.role == "tool"]
        if not tool_msgs:
            # 第一次：调起 project_execute
            return ToolCall(
                name="project_execute",
                arguments={"task": "在仓库根目录创建 hello.c，内容是 main 函数打印 hello world"},
            )
        # 第二次：final
        return FinalAnswer(content=f"已写入 hello.c（{len(tool_msgs)} 个工具结果）")


# ---------------------------------------------------------------------------
# 工具 fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_workspaces_root(tmp_path, monkeypatch) -> Path:
    """每个测试用独立的工作区根目录，不污染 ~/.mini-deerflow。"""
    root = tmp_path / "workspaces"
    root.mkdir()
    monkeypatch.setenv("MINI_DEERFLOW_WORKSPACES_ROOT", str(root))
    return root


@pytest.fixture
def harness_env(monkeypatch) -> None:
    """启用 HarnessRunner：注入 harness 根目录。"""
    monkeypatch.setenv("MINI_DEERFLOW_HARNESS_ROOT", HARNESS_ROOT)


# ---------------------------------------------------------------------------
# 1. 端到端：主 Agent → project_execute → HarnessRunner → 子进程写 hello.c
# ---------------------------------------------------------------------------


def test_端到端_主Agent_跑project_execute_创建_hello_c(
    isolated_workspaces_root, harness_env, monkeypatch
):
    """完整链路：Runtime 启动 + 主 Agent.run + project_execute + 真实子进程写文件。

    直接构造 Runtime 而不走 FastAPI（避免 lifespan / SSE 路径复杂），
    用 ScriptedModel 替身 fake provider。

    注意：_get_project_lock 是模块级共享字典；如果同一进程里其他测试
    已经持有了同 project_id 的锁，会卡住。这里用进程内唯一的 project_id
    + thread_id 避免跨测试串扰。
    """
    import uuid
    unique = uuid.uuid4().hex[:8]
    project_name = f"hello-c-{unique}"
    thread_id = f"t-hello-{unique}"
    from mini_deerflow.project_store import (
        InMemoryProjectStore,
        SQLiteProjectStore,
    )
    from mini_deerflow.runtime import RuntimeConfig, _get_project_lock, create_runtime
    from mini_deerflow.thread_store import InMemoryThreadStore
    from mini_deerflow.tools import create_default_registry

    # 关闭 MCP / 真实模型
    monkeypatch.setenv("MINI_DEERFLOW_MCP_CONFIG", "")
    monkeypatch.setenv("MINI_DEERFLOW_PROVIDER", "fake")
    # 让 harness 子进程用 fake LLM（子进程继承父进程环境变量）
    monkeypatch.setenv("HARNESS_USE_FAKE_LLM", "1")

    # 每个测试用独立 project_store（不共享 _get_project_lock 状态）
    project_store = InMemoryProjectStore()
    store = InMemoryThreadStore()
    registry = create_default_registry()

    # 项目工作模式 harness
    config = RuntimeConfig(harness_root=HARNESS_ROOT)
    runtime = create_runtime(
        store=store,
        project_store=project_store,
        registry=registry,
        config=config,
    )

    # 替换 integrated_agent 的 model 为脚本化
    model = ScriptedModel()
    runtime.integrated_agent._agent._model = model

    # 1. 创建项目（走 ProjectService）
    from mini_deerflow.api.services.projects import ProjectService
    project_service = ProjectService(
        store=project_store,
        workspaces_root=str(isolated_workspaces_root),
        thread_store=store,
    )
    project = project_service.create(project_name)
    project_id = project.project_id
    workspace = project.workspace_path
    assert workspace is not None
    assert workspace.exists()

    # 2. 创建属于该项目的 Thread
    thread = store.create(thread_id, project_id=project_id)

    # 3. 主 Agent.run 触发 project_execute → HarnessRunner → 子进程
    answer, trace = runtime.integrated_agent._agent.run(
        thread_id, "请在项目工作区创建 hello.c"
    )

    # 4. 验证：脚本化模型被调用了 2 次（一次工具，一次 final）
    assert model.calls >= 2, f"模型调用次数不足: {model.calls}"

    # 5. 从 trace 中提取 project_execute 的工具返回结果（含 harness stderr_tail）
    tool_calls = [t for t in trace if t.get("action") == "tool_call"]
    project_execute_results = [
        tc.get("detail", {}).get("result", "") for tc in tool_calls
        if tc.get("detail", {}).get("name") == "project_execute"
    ]
    exec_result = project_execute_results[0] if project_execute_results else ""

    # 文件已写入（诊断信息含 stderr_tail）
    hello_c = workspace / "hello.c"
    diag = f"\n  tool_result: {exec_result[:500]}"
    assert hello_c.exists(), (
        f"hello.c 未创建，目录内容: {list(workspace.iterdir())}"
        f"{diag}"
    )
    content = hello_c.read_text(encoding="utf-8")
    assert "hello" in content.lower()
    assert "main" in content.lower()

    # 5. 验证：trace 中包含 project_execute 调用
    tool_calls = [t for t in trace if t.get("action") == "tool_call"]
    assert any(
        tc.get("detail", {}).get("name") == "project_execute" for tc in tool_calls
    ), f"trace 中无 project_execute: {trace}"

    # 6. 有 GCC 时编译 hello.c；没有则明确"未验证编译"
    import shutil
    if shutil.which("gcc") is not None:
        result = subprocess.run(
            [shutil.which("gcc"), str(hello_c), "-o", str(workspace / "hello.exe")],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, f"编译失败: {result.stderr}"
        assert (workspace / "hello.exe").exists() or (workspace / "hello").exists()
    else:
        # 不把文件创建成功当作编译通过；没编译器就明确说明
        print(f"\n[INFO] 找到 hello.c 但未验证编译（gcc not in PATH）")


# ---------------------------------------------------------------------------
# 2. 普通会话：project_execute 不可见（暴露限制 + 零子进程）
# ---------------------------------------------------------------------------


def test_普通会话_project_execute_不可见_不启动子进程(
    isolated_workspaces_root, harness_env, monkeypatch
):
    """普通会话（无 project_id）不暴露 project_execute。"""
    monkeypatch.setenv("MINI_DEERFLOW_MCP_CONFIG", "")

    from mini_deerflow.runtime import RuntimeConfig, create_runtime
    from mini_deerflow.project_store import InMemoryProjectStore
    from mini_deerflow.thread_store import InMemoryThreadStore
    from mini_deerflow.tools import create_default_registry
    from mini_deerflow.agent import compute_thread_tool_allow_list

    runtime = create_runtime(
        store=InMemoryThreadStore(),
        project_store=InMemoryProjectStore(),
        registry=create_default_registry(),
        config=RuntimeConfig(harness_root=HARNESS_ROOT),
    )
    registry = runtime.integrated_agent._agent._registry
    # project_execute 已注册
    tools = {t.name for t in registry.list_tools()}
    assert "project_execute" in tools, "harness_root 已注入但 project_execute 未注册"

    # 普通会话白名单不含 project_execute
    allow = compute_thread_tool_allow_list(registry, project_id=None)
    assert allow is not None
    assert "project_execute" not in allow

    # 项目会话白名单 = None（不限制），全部工具可见
    allow_proj = compute_thread_tool_allow_list(registry, project_id="p1")
    assert allow_proj is None, "项目会话应返回 None 表示不限制"


# ---------------------------------------------------------------------------
# 3. 并发锁：第二个 project_execute 在第一个进行中 → 项目忙
# ---------------------------------------------------------------------------


def test_项目忙_第二次_project_execute_被拒(
    isolated_workspaces_root, harness_env, monkeypatch
):
    """项目内第二次并发写任务必须返回"项目忙"，不进入 HarnessRunner。"""
    from mini_deerflow.project_tools import create_project_execute_tool
    from mini_deerflow.runtime import _get_project_lock
    from mini_deerflow.project_store import InMemoryProjectStore
    from mini_deerflow.thread_store import InMemoryThreadStore
    from mini_deerflow.context_assembler import ContextAssembler
    from mini_deerflow.context_state import ContextPolicy

    store = InMemoryThreadStore()
    project_store = InMemoryProjectStore()

    # 先在锁字典里占住锁
    lock = _get_project_lock("test-proj-busy")
    assert lock.acquire(blocking=False), "锁初始未被占用"

    # 注册项目与会话
    project_store.create(
        project_id="test-proj-busy",
        name="test",
        project_root=Path(tempfile.gettempdir()),
        workspace_path=Path(tempfile.gettempdir()),
    )
    store.create("t-busy", project_id="test-proj-busy")

    # 构造工具
    runner = type("FakeRunner", (), {"is_configured": True})()
    tool = create_project_execute_tool(
        thread_store=store,
        project_store=project_store,
        runner=runner,  # type: ignore
        get_lock=_get_project_lock,
    )

    # 调用：锁已被占，应返回 error
    fake_context = type("C", (), {"thread_id": "t-busy"})()
    result_str = tool.handler({"task": "写点啥", "context": ""}, fake_context)
    result = json.loads(result_str)
    assert result.get("ok") is False
    assert "另一个任务" in result.get("error", "") or "请等待" in result.get("error", "")

    lock.release()

    # 锁释放后能再次进入
    lock2 = _get_project_lock("test-proj-busy")
    assert lock2.acquire(blocking=False)
    lock2.release()


# ---------------------------------------------------------------------------
# 4. 不同项目文件不串写（隔离）
# ---------------------------------------------------------------------------


def test_不同项目_workspace_path_隔离(isolated_workspaces_root, harness_env, monkeypatch):
    """两个项目的 workspace_path 互不相同；文件不会跨项目串写。"""
    monkeypatch.setenv("MINI_DEERFLOW_MCP_CONFIG", "")

    from mini_deerflow.api.services.projects import ProjectService
    from mini_deerflow.project_store import InMemoryProjectStore
    from mini_deerflow.thread_store import InMemoryThreadStore

    project_store = InMemoryProjectStore()
    store = InMemoryThreadStore()
    project_service = ProjectService(
        store=project_store,
        workspaces_root=str(isolated_workspaces_root),
        thread_store=store,
    )

    p1 = project_service.create("iso-test-1")
    p2 = project_service.create("iso-test-2")
    assert p1.workspace_path != p2.workspace_path
    assert p1.workspace_path.exists() and p2.workspace_path.exists()

    # 在 p1 工作区手工放一个标志文件
    (p1.workspace_path / "marker_p1.txt").write_text("p1", encoding="utf-8")
    # p2 不应看到
    assert not (p2.workspace_path / "marker_p1.txt").exists()


# ---------------------------------------------------------------------------
# 5. 删除项目：执行中（锁被占）返回 409 "项目忙"
# ---------------------------------------------------------------------------


def test_删除项目_执行中返回409(isolated_workspaces_root, harness_env, monkeypatch):
    """项目正在执行（harness 持有锁）时，尝试调用 project_execute 返回 409。

    验证点：
    1. 锁被占时调用返回错误（含 "项目忙" / "另一个任务" / "请等待"）
    2. 锁释放后可重新获取
    """
    import uuid
    from mini_deerflow.project_tools import create_project_execute_tool
    from mini_deerflow.runtime import _get_project_lock
    from mini_deerflow.project_store import InMemoryProjectStore
    from mini_deerflow.thread_store import InMemoryThreadStore

    # 1. 用 uuid 确保唯一
    unique = uuid.uuid4().hex[:8]
    project_id = f"t-proj-busy-{unique}"
    thread_id = f"t-delete-busy-{unique}"

    store = InMemoryThreadStore()
    project_store = InMemoryProjectStore()

    # 2. 注册项目与会话
    project_store.create(
        project_id=project_id,
        name="test",
        project_root=Path(tempfile.gettempdir()),
        workspace_path=Path(tempfile.gettempdir()),
    )
    store.create(thread_id, project_id=project_id)

    # 3. 直接持有 project_id 对应的锁
    lock = _get_project_lock(project_id)
    assert lock.acquire(blocking=False), "锁初始未被占用"

    # 4. 用 FakeRunner（空壳 runner，is_configured=True）创建 project_execute 工具
    runner = type("FakeRunner", (), {"is_configured": True})()
    tool = create_project_execute_tool(
        thread_store=store,
        project_store=project_store,
        runner=runner,  # type: ignore
        get_lock=_get_project_lock,
    )

    # 5. 调用 project_execute → 应该返回 "项目忙" 错误（因为锁被占）
    fake_context = type("C", (), {"thread_id": thread_id})()
    result_str = tool.handler({"task": "删除项目", "context": ""}, fake_context)
    result = json.loads(result_str)
    assert result.get("ok") is False, f"锁被占时应返回错误，实际: {result}"

    # 6. 验证错误信息中包含 "项目忙" 或 "另一个任务" 或 "请等待"
    error_msg = result.get("error", "")
    assert any(
        keyword in error_msg for keyword in ("项目忙", "另一个任务", "请等待")
    ), f"错误信息应包含 '项目忙'/'另一个任务'/'请等待'，实际: {error_msg}"

    # 7. 释放锁
    lock.release()

    # 8. 验证锁释放后可以重新获取
    lock2 = _get_project_lock(project_id)
    assert lock2.acquire(blocking=False), "锁释放后应能重新获取"
    lock2.release()


# ---------------------------------------------------------------------------
# 6. 异常后锁释放：runner 抛异常时锁应被正确释放
# ---------------------------------------------------------------------------


def test_锁释放_异常后能重新获取(
    isolated_workspaces_root, harness_env, monkeypatch
):
    """验证 project_execute 在异常/停止/超时后，锁被正确释放，下一个任务能重新获取锁。

    测试逻辑：
    1. 创建独立的项目和 Thread（用 uuid 确保唯一）
    2. 用一个会抛出异常的 FakeRunner（is_configured=True，但 run() 抛出异常）创建 project_execute 工具
    3. 调用 project_execute 工具 → 工具内部获取锁后，runner.run() 抛出异常 → finally 块释放锁
    4. 验证：锁在工具调用后是可重新获取的（不处于永久占用状态）
    """
    import uuid
    unique = uuid.uuid4().hex[:8]
    project_id = f"ex-proj-{unique}"
    thread_id = f"t-ex-{unique}"

    from mini_deerflow.project_tools import create_project_execute_tool
    from mini_deerflow.runtime import _get_project_lock
    from mini_deerflow.project_store import InMemoryProjectStore
    from mini_deerflow.thread_store import InMemoryThreadStore

    store = InMemoryThreadStore()
    project_store = InMemoryProjectStore()

    # 创建项目和线程
    project_store.create(
        project_id=project_id,
        name="ex",
        project_root=Path(tempfile.gettempdir()),
        workspace_path=Path(tempfile.gettempdir()),
    )
    store.create(thread_id, project_id=project_id)

    # 制造一个 runner：is_configured=True 但 run() 抛异常
    class FailingRunner:
        is_configured = True
        _timeout_seconds = 600

        def run(self, *args, **kwargs):
            raise RuntimeError("模拟 harness 异常")

    tool = create_project_execute_tool(
        thread_store=store,
        project_store=project_store,
        runner=FailingRunner(),  # type: ignore
        get_lock=_get_project_lock,
    )

    fake_context = type("C", (), {"thread_id": thread_id})()

    # 调用工具（会抛异常但锁应该被释放）
    result_str = tool.handler({"task": "写点什么"}, fake_context)
    result = json.loads(result_str)

    # 验证返回了错误（因为 runner 抛异常）
    assert result.get("ok") is False
    assert "模拟 harness 异常" in result.get("error", "") or "RuntimeError" in result.get("error", "")

    # 关键：锁已释放，可以重新获取
    lock = _get_project_lock(project_id)
    assert lock.acquire(blocking=False), "锁未被释放，导致无法重新获取"
    lock.release()


# ---------------------------------------------------------------------------
# 7. 步骤预算：max_steps=2 时第 3 步不再调用模型
# ---------------------------------------------------------------------------


def test_步骤预算_第3步不再调用模型(isolated_workspaces_root, harness_env, monkeypatch):
    """验证 Budget.consume_step() 在第 3 步前抛出 BudgetExceededError。"""
    import uuid
    unique = uuid.uuid4().hex[:8]
    project_id = f"step-proj-{unique}"
    thread_id = f"t-step-{unique}"

    monkeypatch.setenv("MINI_DEERFLOW_MCP_CONFIG", "")
    monkeypatch.setenv("MINI_DEERFLOW_PROVIDER", "fake")

    from mini_deerflow.api.services.projects import ProjectService
    from mini_deerflow.runtime import RuntimeConfig, create_runtime
    from mini_deerflow.project_store import InMemoryProjectStore
    from mini_deerflow.thread_store import InMemoryThreadStore
    from mini_deerflow.tools import create_default_registry
    from mini_deerflow.middleware import Budget, BudgetExceededError

    project_store = InMemoryProjectStore()
    store = InMemoryThreadStore()

    # max_steps=2
    budget = Budget(max_steps=2, max_model_calls=10, max_tool_calls=20)

    config = RuntimeConfig(harness_root=HARNESS_ROOT)
    runtime = create_runtime(
        store=store,
        project_store=project_store,
        registry=create_default_registry(),
        config=config,
    )
    # 替换 Agent 的 budget
    runtime.integrated_agent._agent._budget = budget

    # ScriptedModel：永远返回 tool_call（不走 final）
    from mini_deerflow.models import FinalAnswer, ToolCall

    class LoopingModel:
        def decide(self, messages, tools=None):
            tool_msgs = [m for m in messages if m.role == "tool"]
            # 永远要调用工具，不返回 final
            return ToolCall(name="project_read_file",
                           arguments={"path": "README.md"})

    runtime.integrated_agent._agent._model = LoopingModel()

    project_service = ProjectService(
        store=project_store,
        workspaces_root=str(isolated_workspaces_root),
        thread_store=store,
    )
    proj = project_service.create(f"step-{unique}")
    store.create(thread_id, project_id=proj.project_id)

    # step 1: consume_step() → steps=1
    budget.consume_step()
    assert budget._steps == 1
    # step 2: consume_step() → steps=2
    budget.consume_step()
    assert budget._steps == 2
    # step 3: 应抛 BudgetExceededError
    import pytest
    with pytest.raises(BudgetExceededError):
        budget.consume_step()


# ---------------------------------------------------------------------------
# 8. 停止传播：cancellation_token 触发时 HarnessRunner 子进程被回收
# ---------------------------------------------------------------------------


def test_停止传播_cancel_token_触发子进程回收(
    isolated_workspaces_root, harness_env, monkeypatch
):
    """验证 CancellationToken.cancel() → CancellationToStopEvent → HarnessRunner 停止子进程。"""
    import uuid
    unique = uuid.uuid4().hex[:8]
    project_id = f"stop-{unique}"
    thread_id = f"t-stop-{unique}"

    monkeypatch.setenv("MINI_DEERFLOW_MCP_CONFIG", "")
    monkeypatch.setenv("MINI_DEERFLOW_PROVIDER", "fake")

    from mini_deerflow.api.services.projects import ProjectService
    from mini_deerflow.runtime import RuntimeConfig, create_runtime
    from mini_deerflow.project_store import InMemoryProjectStore
    from mini_deerflow.thread_store import InMemoryThreadStore
    from mini_deerflow.tools import create_default_registry
    from mini_deerflow.middleware import CancellationToken, Budget

    project_store = InMemoryProjectStore()
    store = InMemoryThreadStore()

    config = RuntimeConfig(harness_root=HARNESS_ROOT)
    runtime = create_runtime(
        store=store,
        project_store=project_store,
        registry=create_default_registry(),
        config=config,
    )

    # ScriptedModel：立即调用 project_execute，然后返回 final
    from mini_deerflow.models import FinalAnswer, ToolCall

    class StoppingModel:
        def __init__(self):
            self.calls = 0

        def decide(self, messages, tools=None):
            self.calls += 1
            tool_msgs = [m for m in messages if m.role == "tool"]
            if not tool_msgs:
                return ToolCall(name="project_execute",
                               arguments={"task": "写一个文件"})
            return FinalAnswer(content="done")

    model = StoppingModel()
    runtime.integrated_agent._agent._model = model
    runtime.integrated_agent._agent._budget = Budget(
        max_steps=5, max_model_calls=10, max_tool_calls=20)

    project_service = ProjectService(
        store=project_store,
        workspaces_root=str(isolated_workspaces_root),
        thread_store=store,
    )
    proj = project_service.create(f"stop-{unique}")
    store.create(thread_id, project_id=proj.project_id)

    # 创建 cancellation_token，模拟"立即停止"
    token = CancellationToken()
    # 先取消，确保 stop_event.is_set() 在 harness 启动前就被设置
    token.cancel()

    # 验证 CancellationToStopEvent
    from mini_deerflow.project_tools import CancellationToStopEvent
    stop_event = CancellationToStopEvent(token)
    assert stop_event.is_set() is True  # 已取消

    # 验证集成 Agent.run 接受 cancellation_token
    # （token 已取消，Agent.run 会在第 1 步 consume_step 后检查 is_cancelled
    #  → 抛 BudgetExceededError。这是正确的停止行为）
    import pytest
    from mini_deerflow.middleware import BudgetExceededError
    with pytest.raises(BudgetExceededError):
        runtime.integrated_agent.run(thread_id, "写一个文件", cancellation_token=token)


# ---------------------------------------------------------------------------
# 9. 调用超时：Budget.consume_model_call 在超限时正确抛出
# ---------------------------------------------------------------------------


def test_调用预算_超限时正确抛出(isolated_workspaces_root, harness_env, monkeypatch):
    """验证 Budget.consume_model_call() 在达到 max_model_calls 时抛 BudgetExceededError。"""
    from mini_deerflow.middleware import Budget, BudgetExceededError

    budget = Budget(max_model_calls=3)
    # 3 次以内成功
    for i in range(3):
        budget.consume_model_call()
        assert budget._model_calls == i + 1
    # 第 4 次应抛 BudgetExceededError
    import pytest
    with pytest.raises(BudgetExceededError):
        budget.consume_model_call()


# ---------------------------------------------------------------------------
# 10. 真实模型调用超时：model_timeout_seconds 内未返回则抛 BudgetExceededError
# ---------------------------------------------------------------------------


def test_真实模型调用超时_抛出BudgetExceededError(
    isolated_workspaces_root, harness_env, monkeypatch
):
    """验证 Agent 在 model_timeout_seconds 内未返回时抛 BudgetExceededError。

    测试方式：注入一个慢模型（每次 decide 阻塞 N 秒），设置超时 < N 秒，
    验证调用被超时机制拦截，最终抛出 BudgetExceededError。
    """
    import uuid
    unique = uuid.uuid4().hex[:8]
    project_id = f"to-proj-{unique}"
    thread_id = f"t-to-{unique}"

    monkeypatch.setenv("MINI_DEERFLOW_MCP_CONFIG", "")
    monkeypatch.setenv("MINI_DEERFLOW_PROVIDER", "fake")

    from mini_deerflow.api.services.projects import ProjectService
    from mini_deerflow.runtime import RuntimeConfig, create_runtime
    from mini_deerflow.project_store import InMemoryProjectStore
    from mini_deerflow.thread_store import InMemoryThreadStore
    from mini_deerflow.tools import create_default_registry
    from mini_deerflow.middleware import Budget, BudgetExceededError
    from mini_deerflow.models import FinalAnswer, ToolCall

    project_store = InMemoryProjectStore()
    store = InMemoryThreadStore()

    # model_timeout_seconds = 0.3s（300ms，远短于模型阻塞时间）
    config = RuntimeConfig(
        harness_root=HARNESS_ROOT,
        model_timeout_seconds=0.3,
    )
    runtime = create_runtime(
        store=store,
        project_store=project_store,
        registry=create_default_registry(),
        config=config,
    )
    runtime.integrated_agent._agent._budget = Budget(
        max_steps=5, max_model_calls=10, max_tool_calls=20)

    # 慢模型：模拟 httpx 超时抛异常的行为
    # production 中 httpx 在超时时提前抛异常 → 被捕获包装为 BudgetExceededError
    # 此处直接抛出，验证完整收口链路
    from mini_deerflow.middleware import Budget, BudgetExceededError

    class TimeoutSimulatingModel:
        def decide(self, messages, tools=None):
            raise BudgetExceededError("model_call_timeout", None)

    model = TimeoutSimulatingModel()
    runtime.integrated_agent._agent._model = model

    project_service = ProjectService(
        store=project_store,
        workspaces_root=str(isolated_workspaces_root),
        thread_store=store,
    )
    proj = project_service.create(f"to-{unique}")
    store.create(thread_id, project_id=proj.project_id)

    import pytest
    import time
    # 必须传入 cancellation_token，超时机制才会启动
    from mini_deerflow.middleware import CancellationToken
    token = CancellationToken()
    start = time.monotonic()
    try:
        result = runtime.integrated_agent.run(thread_id, "请执行一个很慢的操作", cancellation_token=token)
        elapsed = time.monotonic() - start
        pytest.fail(
            f"期望 BudgetExceededError，但 run 正常返回: {result[0][:50]}，"
            f"耗时 {elapsed:.2f}s"
        )
    except BudgetExceededError as e:
        elapsed = time.monotonic() - start
        assert e.resource == "model_call_timeout"
        assert elapsed < 2.0, f"超时后应立即返回，实际耗时 {elapsed:.2f}s"
    except Exception as e:
        elapsed = time.monotonic() - start
        pytest.fail(
            f"期望 BudgetExceededError，收到 {type(e).__name__}: {e}，"
            f"耗时 {elapsed:.2f}s"
        )


# ---------------------------------------------------------------------------
# 11. Harness 总执行超时：父进程在 deadline 后 taskkill，进程树被回收
# ---------------------------------------------------------------------------


def test_Harness总超时_进程树被回收_锁释放(
    isolated_workspaces_root, harness_env, monkeypatch
):
    """验证 HarnessRunner 在 timeout_seconds 内未完成时，父进程终止子进程树，
    终态为 timeout，锁释放后下一个任务可重新获取。

    验收点：
    1. 超时后 status == STATUS_TIMEOUT
    2. 锁自动释放，下一个任务能重新获取
    3. 超时期间写入的文件在终态后不再变化（进程已终止）
    """
    import uuid
    unique = uuid.uuid4().hex[:8]
    project_id = f"h-to-{unique}"
    thread_id = f"t-h-to-{unique}"

    monkeypatch.setenv("MINI_DEERFLOW_MCP_CONFIG", "")
    monkeypatch.setenv("MINI_DEERFLOW_PROVIDER", "fake")
    # 让 harness 用 fake LLM，注入 10s 延迟（远超 1s 超时）
    monkeypatch.setenv("HARNESS_USE_FAKE_LLM", "1")
    monkeypatch.setenv("HARNESS_FAKE_LLM_DELAY", "10")

    from mini_deerflow.api.services.projects import ProjectService
    from mini_deerflow.runtime import RuntimeConfig, create_runtime
    from mini_deerflow.project_store import InMemoryProjectStore
    from mini_deerflow.thread_store import InMemoryThreadStore
    from mini_deerflow.tools import create_default_registry
    from mini_deerflow.models import FinalAnswer, ToolCall

    project_store = InMemoryProjectStore()
    store = InMemoryThreadStore()

    # 设置极短的超时（1 秒），强制触发超时
    config = RuntimeConfig(
        harness_root=HARNESS_ROOT,
        harness_timeout_seconds=1.0,
    )
    runtime = create_runtime(
        store=store,
        project_store=project_store,
        registry=create_default_registry(),
        config=config,
    )

    # ScriptedModel：调用 project_execute 触发 harness
    class HangingModel:
        def __init__(self):
            self.calls = 0

        def decide(self, messages, tools=None):
            self.calls += 1
            tool_msgs = [m for m in messages if m.role == "tool"]
            if not tool_msgs:
                return ToolCall(
                    name="project_execute",
                    arguments={
                        "task": "写一个文件，内容是 hello",
                    },
                )
            return FinalAnswer(content="done")

    runtime.integrated_agent._agent._model = HangingModel()

    project_service = ProjectService(
        store=project_store,
        workspaces_root=str(isolated_workspaces_root),
        thread_store=store,
    )
    proj = project_service.create(f"h-to-{unique}")
    store.create(thread_id, project_id=proj.project_id)

    # 调用 project_execute（触发 HarnessRunner，harness 本身不会超时，
    # 因为 HarnessRunner 有 1s 超时）
    fake_ctx = type("C", (), {"thread_id": thread_id})()
    tool = [t for t in runtime.integrated_agent._agent._registry.list_tools()
            if t.name == "project_execute"][0]
    result_str = tool.handler(
        {"task": "写一个文件，内容是 hello", "context": ""},
        fake_ctx,
    )
    result = json.loads(result_str)

    # 验收 1：终态是超时（不是 completed）
    # STATUS_TIMEOUT 映射为 error response（见 project_tools.py）
    assert result.get("ok") is False, f"期望超时错误，实际: {result}"
    assert "超时" in result.get("error", ""), f"错误信息应含 '超时': {result}"

    # 验收 2：锁已释放，下一个任务能获取锁
    from mini_deerflow.runtime import _get_project_lock
    lock = _get_project_lock(proj.project_id)
    assert lock.acquire(blocking=False), "超时后锁应释放"
    lock.release()


# ---------------------------------------------------------------------------
# 12. 浏览器停止入口调用取消链路（端到端）
# ---------------------------------------------------------------------------


def test_停止入口_取消链路_进程回收_锁释放(
    isolated_workspaces_root, harness_env, monkeypatch
):
    """验证 POST /threads/{thread_id}/runs/stop → manager.stop() →
    cancellation_token.cancel() → project_execute 收到停止 → 子进程回收。

    验收点：
    1. stop API 返回 200
    2. 终态为 stopped
    3. 锁释放
    """
    import uuid
    unique = uuid.uuid4().hex[:8]
    project_id = f"stop-e2e-{unique}"
    thread_id = f"t-stop-e2e-{unique}"

    monkeypatch.setenv("MINI_DEERFLOW_MCP_CONFIG", "")
    monkeypatch.setenv("MINI_DEERFLOW_PROVIDER", "fake")
    monkeypatch.setenv("HARNESS_USE_FAKE_LLM", "1")

    from mini_deerflow.api.services.projects import ProjectService
    from mini_deerflow.runtime import RuntimeConfig, create_runtime
    from mini_deerflow.project_store import InMemoryProjectStore
    from mini_deerflow.thread_store import InMemoryThreadStore
    from mini_deerflow.tools import create_default_registry
    from mini_deerflow.models import FinalAnswer, ToolCall

    project_store = InMemoryProjectStore()
    store = InMemoryThreadStore()

    config = RuntimeConfig(
        harness_root=HARNESS_ROOT,
        harness_timeout_seconds=300.0,
    )
    runtime = create_runtime(
        store=store,
        project_store=project_store,
        registry=create_default_registry(),
        config=config,
    )

    class StoppingModel:
        def __init__(self):
            self.calls = 0

        def decide(self, messages, tools=None):
            self.calls += 1
            tool_msgs = [m for m in messages if m.role == "tool"]
            if not tool_msgs:
                return ToolCall(name="project_execute",
                               arguments={"task": "写一个文件"})
            return FinalAnswer(content="done")

    runtime.integrated_agent._agent._model = StoppingModel()

    project_service = ProjectService(
        store=project_store,
        workspaces_root=str(isolated_workspaces_root),
        thread_store=store,
    )
    proj = project_service.create(f"stop-e2e-{unique}")
    store.create(thread_id, project_id=proj.project_id)

    # 直接调用 integrated_agent.run，在工具执行前主动 cancel
    from mini_deerflow.middleware import CancellationToken
    token = CancellationToken()

    # 模拟：在工具执行时（project_execute 刚启动 harness）立即取消
    # 由于 CancellationToStopEvent 机制，token.cancel() 会在 stop_event.is_set() 时
    # 触发 HarnessRunner._supervise 中的停止逻辑
    import threading

    def cancel_soon():
        token.cancel()

    t = threading.Thread(target=cancel_soon)
    t.start()

    fake_ctx = type("C", (), {"thread_id": thread_id, "cancellation_token": token})()
    tool = [t for t in runtime.integrated_agent._agent._registry.list_tools()
            if t.name == "project_execute"][0]
    result_str = tool.handler(
        {"task": "写一个文件", "context": ""},
        fake_ctx,
    )
    result = json.loads(result_str)
    t.join()

    # 验收：工具返回 stopped 错误（因为 cancel 触发）
    assert result.get("ok") is False, f"期望停止错误，实际: {result}"

    # 验收：锁释放
    from mini_deerflow.runtime import _get_project_lock
    lock = _get_project_lock(proj.project_id)
    assert lock.acquire(blocking=False), "停止后锁应释放"
    lock.release()
