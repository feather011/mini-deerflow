"""Phase 25B 持久化审计事件。

`LeadDomainEvent` 是**写进数据库的审计记录**，不是对外的 SSE 事件
（那是 25G 的事，本阶段刻意不与其 Schema 耦合）。

两个硬约束：
1. **payload 必须是安全的 JSON 数据**——不存密钥、模型 Prompt、
   Chain-of-Thought。审计记录会被长期保留并可能被导出查看，
   把 CoT 塞进去等于把推理过程和凭据一起落盘。
2. **sequence 每个 run 单调递增且不可重复**，由 Store 在事务内分配
   （见 `store.py`），事件对象本身不自增。
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "LeadDomainEvent",
    "event_types",
]

# 允许的事件类型（新增类型时在此登记，避免拼写漂移产生孤儿事件）
_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "run.created",
        "run.state_changed",
        "run.batch_planned",
        "run.batch_started",
        "run.batch_finished",
        "run.work_item_started",
        "run.work_item_finished",
        "run.lease_opened",
        "run.lease_closed",
        "run.budget_exhausted",
        "run.failed",
        "run.finished",
    }
)


def event_types() -> frozenset[str]:
    """当前登记的事件类型（供校验与测试枚举）。"""
    return _EVENT_TYPES


# payload 中禁止出现的键名：凭据、模型输入、推理过程
_FORBIDDEN_PAYLOAD_KEYS: tuple[str, ...] = (
    "api_key",
    "apikey",
    "secret",
    "token",
    "password",
    "authorization",
    "credential",
    "prompt",
    "system_prompt",
    "chain_of_thought",
    "chain-of-thought",
    "reasoning",
    "thought",
    "cot",
    "completion",
)


def _normalize_key(key: object) -> str:
    """把键名折成可比对的形式：小写 + 去掉连字符/下划线/空白。

    `api_key`、`API-KEY`、`ApiKey` 说的是同一件事，只做 `.lower()`
    等于给「换个写法」留一条直通路——攻击面全在这类大小写与分隔符差异上。
    """
    return re.sub(r"[-_\s]", "", str(key).lower())


#: 规范化后的禁用键集合（只比对**整个键名**，不做子串匹配）
_FORBIDDEN_KEY_SET = frozenset(
    _normalize_key(key) for key in _FORBIDDEN_PAYLOAD_KEYS
)


def _contains_forbidden_key(keys: object) -> str | None:
    """返回命中的**原始**键名（供错误消息显示）；都没命中则返回 None。"""
    for key in keys:  # type: ignore[union-attr]
        if _normalize_key(key) in _FORBIDDEN_KEY_SET:
            return str(key)
    return None


def _assert_safe_payload(payload: dict[str, Any], *, _path: str = "payload") -> None:
    """校验 payload：可 JSON 序列化、且**递归**不含凭据/Prompt/CoT 键。

    递归是必须的：只查顶层等于给攻击者留一层嵌套就能夹带
    `{"context": {"api_key": "sk-..."}}`。

    **这不是完整的秘密扫描。** 键名过滤只能拦住「按惯例命名」的敏感值，
    拦不住 `"note": "这里有个 sk-ABCDEFGHIJKLMNOPQRSTUVWX"` 这种把密钥
    写进普通文本的形态。因此调用方**仍然只允许提交自己构造的结构化元数据**
    （计数、ID、状态、短标签），绝不能把用户输入、模型输出整段塞进来。
    这个函数是防呆的第二道闸，不是安全边界本身。
    """
    if not isinstance(payload, dict):
        raise ValueError(f"{_path} 必须是字典")

    hit = _contains_forbidden_key(payload.keys())
    if hit is not None:
        raise ValueError(
            f"{_path} 不得包含键 {hit!r}（凭据/模型输入/推理过程）"
        )

    for key, value in payload.items():
        if not isinstance(key, str):
            raise ValueError(f"{_path} 的键必须是字符串")
        child_path = f"{_path}.{key}"
        if isinstance(value, dict):
            _assert_safe_payload(value, _path=child_path)
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                item_path = f"{child_path}[{index}]"
                if isinstance(item, dict):
                    _assert_safe_payload(item, _path=item_path)
                elif isinstance(item, (list, tuple)):
                    _assert_safe_payload(
                        {str(i): v for i, v in enumerate(item)}, _path=item_path
                    )
                elif item is not None and not isinstance(item, (str, int, float, bool)):
                    raise ValueError(
                        f"{item_path} 类型 {type(item).__name__} 不可 JSON 序列化"
                    )

    try:
        json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{_path} 无法 JSON 序列化: {exc}") from exc


@dataclass(frozen=True)
class LeadDomainEvent:
    """一条持久化审计事件。frozen：已落库的事件不可改写。"""

    run_id: str
    sequence: int
    event_type: str
    payload: dict[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    timestamp: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise ValueError("run_id 不能为空")
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int):
            raise ValueError("sequence 必须是整数")
        if self.sequence < 0:
            raise ValueError(f"sequence 不能为负: {self.sequence}")
        if self.event_type not in _EVENT_TYPES:
            raise ValueError(
                f"未登记的 event_type: {self.event_type!r}"
                f"（允许: {', '.join(sorted(_EVENT_TYPES))}）"
            )
        _assert_safe_payload(self.payload)

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        sequence: int,
        event_type: str,
        payload: dict[str, Any] | None = None,
        timestamp: float | None = None,
    ) -> "LeadDomainEvent":
        return cls(
            run_id=run_id,
            sequence=sequence,
            event_type=event_type,
            payload=payload or {},
            timestamp=timestamp if timestamp is not None else time.time(),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "run_id": self.run_id,
            "sequence": self.sequence,
            "event_type": self.event_type,
            "timestamp": self.timestamp,
            "payload": dict(self.payload),
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "LeadDomainEvent":
        if not isinstance(data, dict):
            raise ValueError("LeadDomainEvent 必须是字典")
        payload = data.get("payload", {})
        if not isinstance(payload, dict):
            raise ValueError("payload 必须是字典")
        return cls(
            event_id=str(data.get("event_id") or uuid.uuid4().hex),
            run_id=str(data.get("run_id", "")),
            sequence=int(data.get("sequence", 0)),
            event_type=str(data.get("event_type", "")),
            payload=dict(payload),
            timestamp=float(data.get("timestamp", 0.0)),
        )
