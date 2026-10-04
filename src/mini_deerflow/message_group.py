"""Phase 24 安全消息分组：压缩边界只能落在完整语义组之间。

必须捆在一起的三种形态（规格 4.7）：

```
assistant(tool_call)  →  tool(tool_result)  →  assistant(继续推理或 FinalAnswer)
```

为什么不能拆：

- 只发 `tool_call` 不发 `tool_result`：供应商直接拒收（缺配对的 tool_result）。
- 只发 `tool_result` 不发 `tool_call`：孤儿 tool_result，同样拒收。
- 在 `tool_result` 和收尾 `assistant` 之间切开：模型看到半截工具调用，
  上下文语义断裂，压缩后行为不可预测。

因此**一次工具链 = 一个不可分割的组**，链内可能有多次 tool_call 与多次
tool_result（并行工具、多步推理都算同一条链），直到一个不带 tool_call 的
assistant 才闭合。
"""

from __future__ import annotations

from dataclasses import dataclass

from mini_deerflow.context_errors import MalformedMessageSequenceError
from mini_deerflow.models import Message

__all__ = ["MessageGroup", "MessageGroupBuilder"]


def is_tool_call_message(message: Message) -> bool:
    """是否是「发起工具调用」的 assistant 消息。

    只有同时是 assistant 且带 tool 标识才算——`role=tool` 的是结果，
    两者方向相反，混用会让分组把结果当成新链的开头。
    """
    return (
        message.role == "assistant"
        and (message.tool_call_id is not None or message.tool_name is not None)
    )


@dataclass(frozen=True)
class MessageGroup:
    """一组不可再分的消息，携带其在原序列中的位置。"""

    start: int
    """起始序号（含），与 ContextState 的 watermark 同一坐标系。"""
    end: int
    """结束序号（不含）。"""
    messages: tuple[Message, ...]
    is_tool_cycle: bool
    """是否包含工具往返（assistant tool_call → tool result → 收尾）。"""
    is_complete: bool
    """结构是否闭合。strict 模式下恒为 True。"""


class MessageGroupBuilder:
    """把线性消息序列切成安全组。"""

    def build(
        self,
        messages: list[Message] | tuple[Message, ...],
        *,
        strict: bool = True,
    ) -> list[MessageGroup]:
        """切组。

        Args:
            messages: 待切分的消息序列。
            strict: True 时结构非法直接抛 `MalformedMessageSequenceError`；
                False 时把未闭合的工具链收成 `is_complete=False` 的组，
                交由调用方决定是排除还是降级（容错路径用）。

        Returns:
            按原顺序排列、位置覆盖完整、互不重叠的组列表。
        """
        if not isinstance(messages, (list, tuple)):
            raise TypeError(f"messages 必须是序列，收到 {type(messages).__name__}")

        groups: list[MessageGroup] = []
        index = 0
        total = len(messages)

        while index < total:
            current = messages[index]
            if not isinstance(current, Message):
                raise TypeError(
                    f"messages[{index}] 必须是 Message，收到 {type(current).__name__}"
                )

            # 孤立 tool_result：前面没有任何配对的 tool_call
            if current.role == "tool":
                if strict:
                    raise MalformedMessageSequenceError(
                        "孤立 tool_result，缺少配对的 tool_call", index=index
                    )
                groups.append(
                    MessageGroup(
                        start=index,
                        end=index + 1,
                        messages=(current,),
                        is_tool_cycle=False,
                        is_complete=False,
                    )
                )
                index += 1
                continue

            # 普通消息自成一组
            if not is_tool_call_message(current):
                groups.append(
                    MessageGroup(
                        start=index,
                        end=index + 1,
                        messages=(current,),
                        is_tool_cycle=False,
                        is_complete=True,
                    )
                )
                index += 1
                continue

            # ---- 工具链：一路吃到不带 tool_call 的 assistant 为止 ----
            chain: list[Message] = [current]
            cursor = index + 1
            closed = False

            while cursor < total:
                following = messages[cursor]
                if not isinstance(following, Message):
                    raise TypeError(
                        f"messages[{cursor}] 必须是 Message，"
                        f"收到 {type(following).__name__}"
                    )
                if following.role == "tool":
                    chain.append(following)
                    cursor += 1
                    continue
                if is_tool_call_message(following):
                    chain.append(following)
                    cursor += 1
                    continue
                if following.role == "assistant":
                    # 不带 tool 标识的 assistant = 本轮收尾，链闭合
                    chain.append(following)
                    cursor += 1
                    closed = True
                    break
                # user / system 插进工具链中间 = 结构损坏
                if strict:
                    raise MalformedMessageSequenceError(
                        f"{following.role} 消息插入工具链中间", index=cursor
                    )
                closed = False
                break

            if not closed:
                if strict:
                    raise MalformedMessageSequenceError(
                        "悬空工具链：存在 tool_call 却无收尾 assistant", index=index
                    )
                groups.append(
                    MessageGroup(
                        start=index,
                        end=cursor,
                        messages=tuple(chain),
                        is_tool_cycle=True,
                        is_complete=False,
                    )
                )
                index = cursor
                continue

            groups.append(
                MessageGroup(
                    start=index,
                    end=cursor,
                    messages=tuple(chain),
                    is_tool_cycle=True,
                    is_complete=True,
                )
            )
            index = cursor

        return groups

    def split_at_safe_boundary(
        self,
        messages: list[Message] | tuple[Message, ...],
        *,
        keep_last: int,
    ) -> tuple[tuple[Message, ...], tuple[Message, ...]]:
        """按「保留最近 `keep_last` 条」切，但边界吸附到组边界。

        返回 `(可压缩的前缀, 必须保留的后缀)`。若吸附后前缀为空，
        说明所有消息都在必须保留的组里——调用方据此判断「无处可压」。
        """
        if keep_last < 0:
            raise ValueError("keep_last 不能为负")
        groups = self.build(messages)
        if not groups:
            return (), ()
        if keep_last >= len(messages):
            return (), tuple(messages)

        # 目标切点：从末尾数 keep_last 条
        target = len(messages) - keep_last
        boundary = None
        for group in groups:
            if group.start <= target <= group.end:
                # 切点落在组内 → 吸附到组起点，整组保留
                boundary = group.start
                break
        if boundary is None:
            boundary = len(messages)
        if boundary <= 0:
            return (), tuple(messages)
        return tuple(messages[:boundary]), tuple(messages[boundary:])
