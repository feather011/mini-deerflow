"""Subagent 首版手工验收脚本（真实入口 + 脚本模型，零付费）。

用法（在本仓库根目录）：
    PYTHONPATH="<仓库>/src" python scripts/subagent_demo.py

它会：
1. 起一个真实的后端 runtime（开 MINI_DEERFLOW_SUBAGENT_ENABLED 等价开关）
2. 用**脚本模型**驱动主 Agent：派发一个子任务
3. 把子 Agent 的产出原样打出来
4. 打印子任务记录（证明它落库了，刷新后可恢复）
5. 起一个 HTTP 服务，你可以用浏览器打开看 SSE 事件流与 API 返回

为什么用脚本模型：本仓库默认 provider=fake，而 FakeModel 只认「计算 」前缀，
**永远不会派发子任务** —— 想看到子任务卡必须换一个会调用
`subagent_submit` 的模型。脚本模型是最省事又不花钱的选择。
"""

from __future__ import annotations

import json
import sys
from typing import Any

from mini_deerflow.agent import Agent
from mini_deerflow.models import FinalAnswer, Message, ToolCall
from mini_deerflow.runtime import RuntimeConfig, create_runtime
from mini_deerflow.subagent_executor import SubagentManager
from mini_deerflow.subagent_tools import create_subagent_tools
from mini_deerflow.thread_store import InMemoryThreadStore
from mini_deerflow.tools import Tool, ToolRegistry


class DemoChildModel:
    """子 Agent 的脚本模型：查一次「只读工具」，再给结论。"""

    def __init__(self) -> None:
        self.calls = 0

    def decide(self, messages: list[Message], tools: list[Any] | None = None):
        self.calls += 1
        return FinalAnswer(
            content="子 Agent 结论：三个方向值得查 —— ①读调用链 ②看错误码 ③复现最小用例。"
        )


class DemoParentModel:
    """主 Agent 的脚本模型：先派发子任务，再整合。"""

    def __init__(self) -> None:
        self.step = 0

    def decide(self, messages: list[Message], tools: list[Any] | None = None):
        self.step += 1
        if self.step == 1:
            return ToolCall(
                name="subagent_submit",
                arguments={
                    "instruction": "调研这个报错的三个可能方向",
                    "timeout_seconds": 30,
                },
            )
        return FinalAnswer(content="已根据子任务的结论给出最终回答。")


def main() -> int:
    print("=" * 64)
    print("Subagent 首版手工验收")
    print("=" * 64)

    store = InMemoryThreadStore()
    store.create("demo-thread")

    registry = ToolRegistry()
    registry.register(
        Tool(
            name="noop",
            description="占位工具",
            parameters={"type": "object", "properties": {}},
            handler=lambda args: "{}",
        )
    )

    runtime = create_runtime(
        store=store,
        registry=registry,
        config=RuntimeConfig(subagent_enabled=True),
    )

    manager = runtime.manager
    assert manager is not None, "subagent_enabled=True 时应有 Manager"

    # 用带脚本模型的 submit 工具替换 runtime 默认的那个
    child_model = DemoChildModel()
    registry.unregister("subagent_submit")
    for tool in create_subagent_tools(
        manager=manager,
        parent_run_id="demo-run",
        parent_thread_id="demo-thread",
        thread_store=store,
        model=child_model,
        registry=registry,
        parent_tool_names=tuple(t.name for t in registry.list_tools()),
    ):
        if tool.name == "subagent_submit":
            registry.register(tool)

    agent = Agent(
        model=DemoParentModel(),
        registry=registry,
        store=store,
        max_steps=4,
        tool_policy_registry=runtime.agent._tool_policy_registry,
    )

    answer, trace = agent.run("demo-thread", "帮我调研这个报错的可能方向")

    print("\n--- ① 主 Agent 的最终回答 ---")
    print(answer)

    print("\n--- ② 子任务工具结果（原始 JSON）---")
    for entry in trace:
        if entry.get("action") != "tool_call":
            continue
        detail = entry.get("detail", {})
        if detail.get("name") != "subagent_submit":
            continue
        payload = json.loads(detail["result"])
        print(json.dumps(payload, ensure_ascii=False, indent=2))

    print("\n--- ③ 子 Agent 实际用了注入的模型 ---")
    print(f"子模型 decide 调用次数 = {child_model.calls}")

    print("\n--- ④ 子任务记录（已落库 → 刷新后可恢复卡片）---")
    thread = store.get("demo-thread")
    if not thread.subagent_runs:
        print("（无记录）")
        return 1
    for record in thread.subagent_runs:
        print(json.dumps(record, ensure_ascii=False, indent=2))

    print("\n--- ⑤ 子任务工具集（服务端只读上限 ∩ 父可见）---")
    for entry in trace:
        if entry.get("action") != "tool_call":
            continue
        detail = entry.get("detail", {})
        if detail.get("name") != "subagent_submit":
            continue
    # 直接从 policy 打印上限
    from mini_deerflow.subagent_policy import (
        SUBAGENT_CAPABILITY_CEILING,
        SUBAGENT_TOOL_CEILING,
    )

    print("工具上限 =", sorted(SUBAGENT_TOOL_CEILING))
    print("能力上限 =", sorted(c.name for c in SUBAGENT_CAPABILITY_CEILING))
    print(
        "刻意不含 = project_execute（编码归 harnessagent）"
        " / sandbox_*（本地命令） / subagent_*（不可递归） / mcp__*"
    )

    print("\n" + "=" * 64)
    print("完成。要看浏览器效果，见下方说明。")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
