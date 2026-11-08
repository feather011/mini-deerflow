"""Subagent 首版用户验收环境（人工体验用，浏览器）。

**这是一个测试入口，不是正常聊天入口。** 它把生产模型换成脚本化的
`_AcceptanceModel`，因此只能验证「流程」，不能验证对话质量。
正常聊天请用生产入口 `uvicorn mini_deerflow.api.app:app`。

目的：让验收人能在浏览器里真实体验子 Agent 首版闭环的**三件事**：

1. **派发**：主 Agent 自主决定把独立子任务派给子 Agent
2. **进度**：聊天区出现子任务卡，随 SSE 事件实时更新
3. **结果**：子任务的产出回到主会话，主 Agent 据此整合

外加 **刷新恢复**：刷新页面后子任务卡仍在（记录已落库，G6）。

全程 **不调用任何付费模型、不外网**。

启动：

    python scripts/subagent_acceptance_server.py --port 8021
    cd frontend && npm run dev      # 前端，http://localhost:5173

前端需要指向这个后端：设置 `frontend/.env.local` 里的
`VITE_API_TARGET=http://127.0.0.1:8021`（或用 --api-target 参数自动生成）。

组成：
- **后端**：复用生产的 `create_app()` + 全部生产路由，只替换两项：
  1. **模型** → `_AcceptanceModel`（脚本化）：先派发子任务，再整合
  2. **子任务模型** → `_ChildModel`（脚本化）：给出固定结论
  工具链路、SSE、落库、Trace 全是真的。

注意：这是**验收辅助脚本**，不属于生产入口，也不进运行时依赖。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from mini_deerflow.agent import Agent  # noqa: E402
from mini_deerflow.api.app import create_app  # noqa: E402
from mini_deerflow.api.config import ApiSettings  # noqa: E402
from mini_deerflow.api.services.event_broker import RunEventBroker  # noqa: E402
from mini_deerflow.middleware import (  # noqa: E402
    Budget,
    ExecutionContext,
    MiddlewarePipeline,
    OperationType,
)
from mini_deerflow.models import FinalAnswer, Message, ToolCall  # noqa: E402
from mini_deerflow.runtime import RuntimeConfig, create_runtime  # noqa: E402
from mini_deerflow.subagent_tools import create_subagent_tools  # noqa: E402
from mini_deerflow.thread_store import InMemoryThreadStore  # noqa: E402
from mini_deerflow.tool_authorization import ToolPolicyRegistry  # noqa: E402
from mini_deerflow.tools import Tool, ToolRegistry  # noqa: E402


CHILD_CONCLUSION = (
    "子 Agent 结论：三个方向值得查 —— "
    "①顺着调用链看谁改了状态 ②看错误码对应的分支 ③写个最小用例复现。"
)


class _ChildModel:
    """子 Agent 的脚本模型：给一个固定但像样的结论。"""

    def decide(self, messages: list[Message], tools: list[Any] | None = None):
        return FinalAnswer(content=CHILD_CONCLUSION)


class _AcceptanceModel:
    """主 Agent 的脚本模型：先派发子任务，再整合。

    第一轮返回 `subagent_submit` 工具调用 —— 这就是「主 Agent 自主派发」。
    第二轮读工具结果，给出整合后的最终回答。

    判定依据是**当前消息里有没有工具结果**，而不是调用计数 ——
    计数在第二个 run 里会错位（同一个模型实例被复用）。
    """

    def decide(self, messages: list[Message], tools: list[Any] | None = None):
        # 已经有工具结果 → 这一轮是整合，产出最终回答
        if any(m.role == "tool" for m in messages):
            return FinalAnswer(
                content="已根据子任务的结论整理出三个可查的方向，详见上文子任务卡。"
            )
        # 还没有工具结果 → 派发子任务
        return ToolCall(
            name="subagent_submit",
            arguments={
                "instruction": "调研这个报错的三个可能方向",
                "timeout_seconds": 60,
            },
        )


def _build_runtime():
    """构造 runtime：开子 Agent + 注入脚本模型。"""
    store = InMemoryThreadStore()
    registry = ToolRegistry()
    registry.register(
        Tool(
            name="noop",
            description="占位工具",
            parameters={"type": "object", "properties": {}},
            handler=lambda args: "{}",
        )
    )

    config = RuntimeConfig(
        subagent_enabled=True,
        subagent_max_concurrency=2,
        subagent_max_depth=1,
        subagent_default_timeout=120.0,
        # 验收环境不设预算上限，避免验收人连续测试时被额度打断
        disable_budget=True,
    )
    runtime = create_runtime(store=store, registry=registry, config=config)

    # 用脚本模型替换主 Agent 的模型
    runtime.agent._model = _AcceptanceModel()  # type: ignore[attr-defined]

    # 用脚本模型替换子任务的模型（runtime 默认注入的是配置里的模型）
    manager = runtime.manager
    assert manager is not None
    registry.unregister("subagent_submit")
    for tool in create_subagent_tools(
        manager=manager,
        parent_run_id="acceptance",
        parent_thread_id="default",
        thread_store=store,
        model=_ChildModel(),
        registry=registry,
        parent_tool_names=tuple(t.name for t in registry.list_tools()),
    ):
        if tool.name == "subagent_submit":
            registry.register(tool)

    return runtime


def main() -> int:
    parser = argparse.ArgumentParser(description="Subagent 首版验收服务器")
    parser.add_argument("--port", type=int, default=8021)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--api-target",
        default=None,
        help=(
            "写出 frontend/.env.local 指向的前端代理目标；"
            "默认指向本后端自身；传 'none' 则不写文件"
        ),
    )
    args = parser.parse_args()

    runtime = _build_runtime()
    # CORS：前端 dev server 端口可能被占用而顺延（5173 → 5174…），
    # 所以这里放开常见的本地开发端口段，避免验收人撞上跨源拦截。
    settings = ApiSettings(
        cors_origins=[
            f"http://127.0.0.1:{port}"
            for port in range(5173, 5210)
        ] + [
            f"http://localhost:{port}"
            for port in range(5173, 5210)
        ],
    )
    app = create_app(settings, runtime_factory=lambda: runtime)

    # 写前端的环境文件：Vite dev server 默认代理到 5173 后端的写法在
    # 本仓库是 VITE_API_TARGET，验收人不该手工去猜该填哪个端口。
    env_path = REPO_ROOT / "frontend" / ".env.local"
    if args.api_target != "none":
        target = args.api_target or f"http://{args.host}:{args.port}"
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text(f"VITE_API_TARGET={target}\n", encoding="utf-8")
        print(f"已写出 {env_path} → VITE_API_TARGET={target}", flush=True)

    # 打印验收指引
    print("\n" + "=" * 66, flush=True)
    print("Subagent 首版验收服务器（脚本模型，零付费、零外网）", flush=True)
    print("=" * 66, flush=True)
    print(f"后端地址：http://{args.host}:{args.port}", flush=True)
    print(f"API 文档：http://{args.host}:{args.port}/docs", flush=True)
    print("\n建议体验的 4 个动作：", flush=True)
    print("  1. 打开前端，新建会话，发送任意消息", flush=True)
    print("     → 聊天区应出现「子任务卡」，状态从「执行中」变为「已完成」", flush=True)
    print("  2. 卡片上能看到：子任务指令 + 子 Agent 的产出", flush=True)
    print("  3. 刷新浏览器（F5）", flush=True)
    print("     → 子任务卡**仍在**（记录已落库，G6 收口）", flush=True)
    print("  4. 打开「查看任务 → 过程」", flush=True)
    print("     → 时间线里能看到「子任务开始 / 子任务结束」事件", flush=True)
    print("\n提示：左侧会话栏**不应**出现 subagent- 开头的条目", flush=True)
    print("      （子代理的隔离线程不是用户会话）", flush=True)
    print("\n停服：Ctrl+C", flush=True)
    print("=" * 66 + "\n", flush=True)

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
