"""GET /api/threads/{thread_id}/context/usage — 最近一次上下文用量。

返回结构（ContextUsageResponse）：
- `snapshot` 为 null：该 Thread 还没有发送过消息，或 ContextAssembler
  未启用（`MINI_DEERFLOW_CONTEXT_ENABLED=0`）——前端显示「暂无统计」。
- `snapshot` 不为 null：见 `ContextUsageSnapshot` 各字段。

## 不做的事

- 不重新跑一次 assemble 来"实时"算；所有数据都来自
  `IntegratedAgent._last_usage_by_thread[thread_id]`（在最近一次
  Agent.run 结束时捕获）。这是「最近一次发送的快照」，**不**包含
  用户当前输入框里的草稿——避免用户在打字时数字来回跳。
- 不为这个接口实现 25G/25H（Lead Agent 上下文工程）的预实现——
  现有 `RuntimeContext.context_assembler` 是 Agent / Subagent / CLI /
  API 共用的唯一组装入口，已经覆盖未来路径；本路由只是读取最近一次
  的快照，不需要新组装逻辑。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from mini_deerflow.api.dependencies import get_runtime
from mini_deerflow.api.schemas.context_usage import ContextUsageResponse
from mini_deerflow.runtime import RuntimeContext


router = APIRouter(tags=["context"])


@router.get(
    "/threads/{thread_id}/context/usage",
    response_model=ContextUsageResponse,
    summary="最近一次上下文用量",
    description=(
        "读取指定 Thread 最近一次 Agent Run 结束时捕获的「实际送给模型的输入」"
        "Token 用量。返回结构：9 分区明细（与 `ContextTokenBreakdown.to_dict()` "
        "同键名）、输入预算、压缩前后对照。\n\n"
        "`snapshot=null` 表示尚未发送过消息；前端显示「暂无统计」。"
    ),
    responses={
        200: {
            "description": "成功返回最近一次快照（可能为 null）",
        },
        503: {"description": "Runtime 未就绪"},
    },
)
def get_context_usage(
    thread_id: str,
    runtime: RuntimeContext = Depends(get_runtime),
) -> ContextUsageResponse:
    """读取最近一次上下文用量快照。

    线程不存在（未创建）或从未发送过消息时返回 `snapshot=null`，
    不是 404：指示器组件始终需要渲染一个空状态，而不是抛错。
    """
    snapshot = runtime.integrated_agent.get_last_usage(thread_id)
    return ContextUsageResponse(snapshot=snapshot)