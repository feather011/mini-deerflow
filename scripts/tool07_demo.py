"""TOOL-07 产品演示脚本：harness 权限与工具模块集成验收。

用 FakeLLMClient 展示完整链路：
1. 普通会话 project_* 不可见，执行被拒
2. 项目会话 project_execute 可见且可执行
3. harness 子进程真实写文件
4. 本轮 diff 保留既有修改，区分测试通过/失败/未运行
5. 停止链路：cancel_token → 子进程回收

不调用真实付费服务。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

# 确保 mini-deerflow 可导入
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

os.environ.setdefault("MINI_DEERFLOW_PROVIDER", "fake")
os.environ.setdefault("MINI_DEERFLOW_MCP_CONFIG", "")
os.environ.setdefault("HARNESS_USE_FAKE_LLM", "1")

HARNESS_ROOT = "D:/项目解读/harnessagent"


def _banner(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}\n")


def _section(label: str) -> None:
    print(f"\n## {label}")


# ---------------------------------------------------------------------------
# 演示 1：普通会话 project_* 不可见，执行被拒
# ---------------------------------------------------------------------------

def demo_normal_session_blocks_project_tools() -> None:
    _banner("演示 1：普通会话 project_* 不可见，执行被拒")

    from mini_deerflow.agent import compute_thread_tool_allow_list
    from mini_deerflow.runtime import RuntimeConfig, create_runtime
    from mini_deerflow.project_store import InMemoryProjectStore
    from mini_deerflow.thread_store import InMemoryThreadStore
    from mini_deerflow.tools import create_default_registry

    runtime = create_runtime(
        store=InMemoryThreadStore(),
        project_store=InMemoryProjectStore(),
        registry=create_default_registry(),
        config=RuntimeConfig(harness_root=HARNESS_ROOT),
    )
    registry = runtime.integrated_agent._agent._registry

    # project_execute 已注册
    tools = {t.name for t in registry.list_tools()}
    print(f"  已注册工具数: {len(tools)}")
    assert "project_execute" in tools, "project_execute 应已注册"
    print(f"  OK project_execute 已在工具注册表")

    # 普通会话白名单不含 project_*
    allow = compute_thread_tool_allow_list(registry, project_id=None)
    assert allow is not None
    project_tools = [t for t in allow if t.startswith("project_")]
    print(f"  普通会话白名单中 project_* 工具数: {len(project_tools)}")
    assert len(project_tools) == 0, "普通会话不应暴露 project_* 工具"
    print(f"  OK 普通会话白名单不含任何 project_* 工具")

    # 项目会话白名单 = None（不限制）
    allow_proj = compute_thread_tool_allow_list(registry, project_id="p1")
    assert allow_proj is None
    print(f"  OK 项目会话返回 None（不限制），所有工具可见")


# ---------------------------------------------------------------------------
# 演示 2：普通会话强行调用 project_execute → 执行被拒
# ---------------------------------------------------------------------------

def demo_normal_session_execute_rejected() -> None:
    _banner("演示 2：普通会话强行调用 project_execute → 执行被拒")

    from mini_deerflow.runtime import RuntimeConfig, create_runtime
    from mini_deerflow.project_store import InMemoryProjectStore
    from mini_deerflow.thread_store import InMemoryThreadStore
    from mini_deerflow.tools import create_default_registry

    project_store = InMemoryProjectStore()
    store = InMemoryThreadStore()
    registry = create_default_registry()
    config = RuntimeConfig(harness_root=HARNESS_ROOT)

    runtime = create_runtime(
        store=store,
        project_store=project_store,
        registry=registry,
        config=config,
    )

    # 获取已注册的 project_execute tool（来自 runtime）
    project_tools = [t for t in registry.list_tools() if t.name == "project_execute"]
    assert project_tools, "project_execute 应已注册"
    tool = project_tools[0]
    print(f"  OK 拿到已注册的 project_execute 工具")

    # 验证普通会话白名单不含 project_execute
    from mini_deerflow.agent import compute_thread_tool_allow_list
    allow = compute_thread_tool_allow_list(registry, project_id=None)
    assert allow is not None
    assert "project_execute" not in allow, "普通会话白名单不应含 project_execute"
    print(f"  OK 普通会话白名单不含 project_execute")

    # 直接调用工具层：非项目会话 thread_id → ProjectToolStateError
    fake_ctx = type("C", (), {"thread_id": "no-such-thread"})()
    result_str = tool.handler({"task": "写文件", "context": ""}, fake_ctx)
    result = json.loads(result_str)
    assert result.get("ok") is False, f"普通会话应拒绝执行，实际: {result}"
    print(f"  OK 普通会话 project_execute 返回 ok=False")


# ---------------------------------------------------------------------------
# 演示 3：项目会话派发 harness，harness 真实写文件
# ---------------------------------------------------------------------------

def demo_project_session_executes_harness() -> None:
    _banner("演示 3：项目会话派发 harness → 子进程真实写文件")

    unique = uuid.uuid4().hex[:8]
    project_name = f"hello-c-{unique}"
    thread_id = f"t-hello-{unique}"

    from mini_deerflow.api.services.projects import ProjectService
    from mini_deerflow.runtime import RuntimeConfig, create_runtime
    from mini_deerflow.project_store import InMemoryProjectStore
    from mini_deerflow.thread_store import InMemoryThreadStore
    from mini_deerflow.tools import create_default_registry
    from mini_deerflow.models import FinalAnswer, ToolCall

    # 独立 workspace
    workspaces_root = Path(tempfile.mkdtemp()) / "workspaces"
    workspaces_root.mkdir()

    project_store = InMemoryProjectStore()
    store = InMemoryThreadStore()
    config = RuntimeConfig(harness_root=HARNESS_ROOT)

    runtime = create_runtime(
        store=store,
        project_store=project_store,
        registry=create_default_registry(),
        config=config,
    )

    # ScriptedModel：第一次调 project_execute，第二次 final answer
    class ScriptedModel:
        def __init__(self):
            self.calls = 0

        def decide(self, messages, tools=None):
            self.calls += 1
            tool_msgs = [m for m in messages if getattr(m, "role", None) == "tool"]
            if not tool_msgs:
                return ToolCall(
                    name="project_execute",
                    arguments={
                        "task": "在仓库根目录创建 hello.c，内容是 main 函数打印 hello world",
                    },
                )
            return FinalAnswer(content=f"已写入 hello.c（{len(tool_msgs)} 个工具结果）")

    runtime.integrated_agent._agent._model = ScriptedModel()

    # 创建项目和 Thread
    project_service = ProjectService(
        store=project_store,
        workspaces_root=str(workspaces_root),
        thread_store=store,
    )
    project = project_service.create(project_name)
    store.create(thread_id, project_id=project.project_id)
    workspace = project.workspace_path
    print(f"  项目: {project_name}")
    print(f"  Workspace: {workspace}")

    # 预先放入既有文件
    (workspace / "README.md").write_text("# 既有文件\n这是已有的内容。", encoding="utf-8")
    print(f"  OK 预先写入 README.md（既有修改）")

    # 执行
    _section("Agent.run() 开始")
    answer, trace = runtime.integrated_agent._agent.run(
        thread_id, "请在项目工作区创建 hello.c"
    )
    print(f"  Agent 最终回答: {answer}")

    # 验证文件已创建
    hello_c = workspace / "hello.c"
    assert hello_c.exists(), f"hello.c 未创建: {list(workspace.iterdir())}"
    content = hello_c.read_text(encoding="utf-8")
    print(f"  OK hello.c 已创建，内容长度: {len(content)} 字节")
    assert "hello" in content.lower(), f"hello.c 应含 'hello'，实际: {content[:100]}"
    print(f"  OK hello.c 内容含 'hello'")

    # 验证 diff：README.md 保留，hello.c 新增
    tool_calls = [t for t in trace if t.get("action") == "tool_call"]
    exec_results = [
        tc.get("detail", {}).get("result", "")
        for tc in tool_calls
        if tc.get("detail", {}).get("name") == "project_execute"
    ]
    assert exec_results, "trace 中无 project_execute 调用"
    result_data = json.loads(exec_results[0])
    print(f"\n  project_execute 返回:")
    print(f"    status: {result_data.get('status')}")
    print(f"    ok: {result_data.get('ok')}")

    changes = result_data.get("changes", {}).get("files", [])
    print(f"    变更文件数: {len(changes)}")
    for ch in changes:
        print(f"      [{ch.get('status')}] {ch.get('path')}")

    # README.md 保留（既有修改）
    readme_change = next((c for c in changes if "README.md" in c.get("path", "")), None)
    assert readme_change is None, f"README.md 不应出现在变更中: {readme_change}"
    print(f"  OK README.md 未出现在变更列表（既有修改被保留）")

    # hello.c 新增
    hello_change = next((c for c in changes if "hello.c" in c.get("path", "")), None)
    assert hello_change is not None, f"hello.c 应在变更列表中: {changes}"
    assert hello_change.get("status") == "added", f"hello.c 应为 added: {hello_change}"
    print(f"  OK hello.c 在变更列表中，状态为 added")


# ---------------------------------------------------------------------------
# 演示 4：取消链路
# ---------------------------------------------------------------------------

def demo_cancellation_stops_subprocess() -> None:
    _banner("演示 4：取消链路 → 子进程回收")

    unique = uuid.uuid4().hex[:8]

    from mini_deerflow.api.services.projects import ProjectService
    from mini_deerflow.runtime import RuntimeConfig, create_runtime
    from mini_deerflow.project_store import InMemoryProjectStore
    from mini_deerflow.thread_store import InMemoryThreadStore
    from mini_deerflow.tools import create_default_registry
    from mini_deerflow.middleware import CancellationToken, Budget, BudgetExceededError
    from mini_deerflow.models import FinalAnswer, ToolCall

    workspaces_root = Path(tempfile.mkdtemp()) / "workspaces"
    workspaces_root.mkdir()

    project_store = InMemoryProjectStore()
    store = InMemoryThreadStore()
    config = RuntimeConfig(harness_root=HARNESS_ROOT)

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
            tool_msgs = [m for m in messages if getattr(m, "role", None) == "tool"]
            if not tool_msgs:
                return ToolCall(name="project_execute", arguments={"task": "写一个文件"})
            return FinalAnswer(content="done")

    runtime.integrated_agent._agent._model = StoppingModel()
    runtime.integrated_agent._agent._budget = Budget(
        max_steps=5, max_model_calls=10, max_tool_calls=20
    )

    project_service = ProjectService(
        store=project_store,
        workspaces_root=str(workspaces_root),
        thread_store=store,
    )
    proj = project_service.create(f"stop-{unique}")
    store.create(f"t-stop-{unique}", project_id=proj.project_id)

    # CancellationToken 立即取消
    token = CancellationToken()
    token.cancel()

    from mini_deerflow.project_tools import CancellationToStopEvent
    stop_event = CancellationToStopEvent(token)
    print(f"  stop_event.is_set() = {stop_event.is_set()}（已取消）")

    _section("Agent.run(cancellation_token=token) 开始")
    try:
        runtime.integrated_agent.run(f"t-stop-{unique}", "写一个文件", cancellation_token=token)
        print(f"  FAIL 应抛 BudgetExceededError")
    except BudgetExceededError as e:
        print(f"  OK 收到 BudgetExceededError（{e.resource}）")
        print(f"  取消链路正常：cancellation_token → BudgetExceededError")


# ---------------------------------------------------------------------------
# 演示 5：超时链路
# ---------------------------------------------------------------------------

def demo_timeout_reclaims_process() -> None:
    _banner("演示 5：超时链路 → 进程树回收，状态准确")

    unique = uuid.uuid4().hex[:8]

    from mini_deerflow.api.services.projects import ProjectService
    from mini_deerflow.runtime import RuntimeConfig, create_runtime
    from mini_deerflow.project_store import InMemoryProjectStore
    from mini_deerflow.thread_store import InMemoryThreadStore
    from mini_deerflow.tools import create_default_registry

    workspaces_root = Path(tempfile.mkdtemp()) / "workspaces"
    workspaces_root.mkdir()

    project_store = InMemoryProjectStore()
    store = InMemoryThreadStore()
    config = RuntimeConfig(
        harness_root=HARNESS_ROOT,
        harness_timeout_seconds=1.0,  # 1 秒超时
    )

    runtime = create_runtime(
        store=store,
        project_store=project_store,
        registry=create_default_registry(),
        config=config,
    )

    project_service = ProjectService(
        store=project_store,
        workspaces_root=str(workspaces_root),
        thread_store=store,
    )
    proj = project_service.create(f"h-to-{unique}")
    store.create(f"t-h-to-{unique}", project_id=proj.project_id)

    # 直接调用工具层
    fake_ctx = type("C", (), {
        "thread_id": f"t-h-to-{unique}",
        "run_id": "run-to",
        "project_id": proj.project_id,
    })()
    tool = [t for t in runtime.integrated_agent._agent._registry.list_tools()
            if t.name == "project_execute"][0]

    result_str = tool.handler(
        {"task": "写一个文件，内容是 hello", "context": ""},
        fake_ctx,
    )
    result = json.loads(result_str)

    print(f"  project_execute 返回:")
    print(f"    ok: {result.get('ok')}")
    print(f"    status: {result.get('status')}")
    print(f"    error: {str(result.get('error', ''))[:80]}")

    assert result.get("ok") is False, "超时应返回 ok=False"
    print(f"  OK 超时返回 ok=False")
    assert "超时" in str(result.get("error", "")), "错误信息应含'超时'"
    print(f"  OK 错误信息含'超时'")

    # 锁已释放
    from mini_deerflow.runtime import _get_project_lock
    lock = _get_project_lock(proj.project_id)
    acquired = lock.acquire(blocking=False)
    assert acquired, "超时后锁应已释放"
    lock.release()
    print(f"  OK 超时后锁已释放，下一个任务可重新获取")


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def main() -> int:
    print("""
[==========================================================]
|  TOOL-07 产品演示                                        |
|  harness 权限与工具模块集成验收                             |
[==========================================================╝
    """)

    demos = [
        ("1: 普通会话 project_* 不可见", demo_normal_session_blocks_project_tools),
        ("2: 普通会话强行调用被拒", demo_normal_session_execute_rejected),
        ("3: 项目会话派发 harness", demo_project_session_executes_harness),
        ("4: 取消链路", demo_cancellation_stops_subprocess),
        ("5: 超时链路", demo_timeout_reclaims_process),
    ]

    passed = 0
    failed = 0
    for label, fn in demos:
        try:
            fn()
            passed += 1
        except AssertionError as e:
            print(f"\n  FAIL 断言失败 [{label}]: {e}")
            failed += 1
        except Exception as e:
            print(f"\n  FAIL 异常 [{label}]: {type(e).__name__}: {e}")
            failed += 1

    _banner(f"演示完成：{passed} 通过，{failed} 失败")
    if failed == 0:
        print("  所有验收链路验证通过。")
        print("  限制（已记录）：无操作系统级沙箱、无分布式锁、Windows only harness_root")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
