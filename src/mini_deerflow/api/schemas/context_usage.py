"""上下文用量快照 API Schema。

GET /api/threads/{thread_id}/context/usage

返回结构：
- `snapshot` 为 null：当前 Thread 还没有发过消息（或 ContextAssembler 未启用）。
- `snapshot` 不为 null：最近一次「实际送给模型的输入」的用量统计。

字段命名与 `ContextUsageSnapshot.to_dict()` 一致，便于后端直接
`model_validate(snapshot.to_dict())`，前端也能用同一份类型。
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from mini_deerflow.context_usage import ContextUsageSnapshot


class ContextUsageResponse(BaseModel):
    """最近一次上下文用量的 REST 表示。

    `snapshot=None` 表示该 Thread 暂无统计（首次进入、未发送消息、
    或未启用上下文工程）——前端据此显示「暂无统计」。
    """

    snapshot: ContextUsageSnapshot | None = Field(
        default=None,
        description="最近一次组装并压缩后、实际送给模型的 Token 用量。",
    )


__all__ = ["ContextUsageResponse"]