"""工具调用事件的载荷约束（SSE 通道）。

背景：`api/schemas/streaming.py` 的模块约定写着"绝不暴露 …… 完整工具参数"。
本轮为了让会话里能折叠展示工具调用，给 `tool.started` 事件补了入参信息 ——
但只能是**摘要**（键名 + 值的形状），不能是原值。

这些测试就是那条约束的回归护栏：一旦有人改成直接放原值，这里立刻红。
"""
from __future__ import annotations

import json

from mini_deerflow.api.schemas.streaming import (
    RunEvent,
    RunEventType,
    tool_args_summary,
    tool_output_summary,
)


class TestToolArgsSummary:
    """入参摘要：给形状，不给值。"""

    def test_摘要含键名与形状(self) -> None:
        summary = tool_args_summary({"expression": "1+1", "timeout": 30})
        assert summary is not None
        assert "expression=" in summary
        assert "timeout=" in summary
        assert "str·3字" in summary
        assert "int" in summary

    def test_摘要不含任何原始值(self) -> None:
        # 值里带一个非常好认的哨兵串；摘要里绝不能出现
        summary = tool_args_summary(
            {"path": "/secret/SENTINEL_DIR/file.txt", "expr": "SENTINEL_VALUE"}
        )
        assert summary is not None
        assert "SENTINEL" not in summary
        assert "secret" not in summary

    def test_各类值的形状(self) -> None:
        summary = tool_args_summary(
            {
                "s": "abc",
                "i": 1,
                "f": 1.5,
                "b": True,
                "n": None,
                "lst": [1, 2],
                "obj": {"a": 1},
            }
        )
        assert summary is not None
        assert "s=str·3字" in summary
        assert "i=int" in summary
        assert "f=num" in summary
        assert "b=bool" in summary
        assert "n=null" in summary
        assert "lst=list·2项" in summary
        assert "obj=obj·1键" in summary

    def test_空入参返回None(self) -> None:
        assert tool_args_summary({}) is None
        assert tool_args_summary(None) is None

    def test_超长摘要被截断(self) -> None:
        summary = tool_args_summary({f"k{i}": "x" * 100 for i in range(50)})
        assert summary is not None
        assert len(summary) <= 601  # 600 + 省略号
        assert summary.endswith("…")


class TestToolOutputSummary:
    """工具结果：可展示但有上限。"""

    def test_字符串原样返回(self) -> None:
        assert tool_output_summary("hello") == "hello"

    def test_超长结果被截断(self) -> None:
        out = tool_output_summary("y" * 5000)
        assert out is not None
        assert len(out) <= 2001
        assert out.endswith("…")

    def test_非字符串结果序列化为JSON(self) -> None:
        out = tool_output_summary({"ok": True, "n": 1})
        assert out is not None
        assert json.loads(out) == {"ok": True, "n": 1}

    def test_None返回None(self) -> None:
        assert tool_output_summary(None) is None


class TestRunEventToolFields:
    """RunEvent 的新字段：默认 None，不影响既有事件形状。"""

    def test_默认没有工具字段(self) -> None:
        event = RunEvent(
            run_id="r1",
            thread_id="t1",
            event_type=RunEventType.TOOL_STARTED,
            sequence=1,
        )
        assert event.tool_args is None
        assert event.tool_output is None

    def test_工具字段可携带(self) -> None:
        event = RunEvent(
            run_id="r1",
            thread_id="t1",
            event_type=RunEventType.TOOL_COMPLETED,
            sequence=2,
            tool_name="calculator",
            tool_args="expression=str·3字",
            tool_output="42",
        )
        assert event.tool_args == "expression=str·3字"
        assert event.tool_output == "42"
