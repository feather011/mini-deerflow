"""Phase 24 历史向量检索：找回被摘要压缩掉的原始细节。

## 本地基线的真实限制（必须如实看待）

默认的 `LocalHashEmbeddingProvider` 是**确定性词袋哈希**，不是语义 Embedding：

- 相同文本 → 相同向量（跨进程、跨机器稳定）
- 只在**词面重合**时才相似，同义词、换说法一律检索不到
- 不下载模型、不联网、零依赖，适合作为离线基线与测试替身

它**不等价于**任何真实的语义 Embedding，也不要把它当成「有检索质量」的实现。
`EmbeddingProvider` 协议就是给未来接入真实模型（远程 API 或本地小模型）留的口子。

## 水位与一致性

索引是**投影**，不是事实源：

1. Thread 消息先落库，再更新索引；
2. 索引失败**不回滚**已经保存的真实消息；
3. `ContextState.indexed_through` 记录已索引到的消息序号；
4. `history_search` 前发现水位落后 → 先补齐缺失消息；
5. 补齐失败 → 抛 `HistoryIndexLagError` 并记 Trace，
   **绝不返回伪造的「完整」搜索结果**。
"""

from __future__ import annotations

import hashlib
import math
import re
import time
import uuid
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from mini_deerflow.context_errors import HistoryIndexLagError, HistoryReferenceError
from mini_deerflow.context_state import ContextState
from mini_deerflow.context_store import ContextStore, cas_update
from mini_deerflow.middleware import TraceEvent, TraceSink
from mini_deerflow.models import Message
from mini_deerflow.thread_store import ThreadStore

__all__ = [
    "DEFAULT_EMBEDDING_DIMENSIONS",
    "EmbeddingProvider",
    "HistoryHit",
    "HistoryVectorIndex",
    "HistoryIndexService",
    "LocalHashEmbeddingProvider",
    "HISTORY_SEARCH_LIMITS",
]

DEFAULT_EMBEDDING_DIMENSIONS = 128

# 参数安全上限：模型给的 query/top_k/before/after 都可能离谱
HISTORY_SEARCH_LIMITS: dict[str, int] = {
    "top_k": 20,
    "before": 10,
    "after": 10,
    "limit": 50,
    "query_chars": 500,
}

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[一-鿿]")


@runtime_checkable
class EmbeddingProvider(Protocol):
    """向量化协议。可替换为远程或本地语义模型。"""

    def embed(self, text: str) -> tuple[float, ...]:
        """把文本转成定长向量。"""
        ...

    @property
    def dimensions(self) -> int:
        """向量维度。"""
        ...


class LocalHashEmbeddingProvider:
    """离线确定性词袋哈希基线（见模块 docstring 的限制说明）。"""

    __slots__ = ("_dimensions",)

    def __init__(self, dimensions: int = DEFAULT_EMBEDDING_DIMENSIONS) -> None:
        if dimensions <= 0:
            raise ValueError("dimensions 必须为正")
        self._dimensions = dimensions

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        return [t.lower() for t in _TOKEN_RE.findall(text)]

    @staticmethod
    def _bucket(token: str) -> int:
        # 用 md5 而非内置 hash()：后者受 PYTHONHASHSEED 影响，
        # 跨进程不稳定，会导致同一份历史两次索引结果不同。
        digest = hashlib.md5(token.encode("utf-8")).digest()
        return int.from_bytes(digest[:4], "big")

    def embed(self, text: str) -> tuple[float, ...]:
        if not isinstance(text, str):
            raise TypeError(f"embed 需要 str，收到 {type(text).__name__}")
        vector = [0.0] * self._dimensions
        for token in self._tokenize(text):
            vector[self._bucket(token) % self._dimensions] += 1.0
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            return tuple(vector)
        return tuple(value / norm for value in vector)


@dataclass(frozen=True)
class HistoryHit:
    """一次检索命中。`reference` 是消息序号，与 watermark 同坐标系。"""

    reference: int
    role: str
    score: float
    snippet: str

    def to_dict(self) -> dict[str, object]:
        return {
            "reference": self.reference,
            "role": self.role,
            "score": round(self.score, 4),
            "snippet": self.snippet,
        }


def _cosine(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    if len(a) != len(b):
        raise ValueError("向量维度不一致")
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


class HistoryVectorIndex:
    """按 thread_id 隔离的内存向量索引。"""

    def __init__(self, provider: EmbeddingProvider | None = None) -> None:
        self._provider = provider or LocalHashEmbeddingProvider()
        # {thread_id: {reference: (vector, role, text)}}
        self._store: dict[str, dict[int, tuple[tuple[float, ...], str, str]]] = {}

    @property
    def provider(self) -> EmbeddingProvider:
        return self._provider

    def upsert(
        self,
        thread_id: str,
        messages: tuple[Message, ...] | list[Message],
        *,
        start_index: int = 0,
    ) -> int:
        """写入（或覆盖）一批消息。**幂等**：同 reference 覆盖同内容。

        Returns:
            该 thread 当前索引条目数。
        """
        if not thread_id:
            raise ValueError("thread_id 不能为空")
        bucket = self._store.setdefault(thread_id, {})
        for offset, message in enumerate(messages):
            if not isinstance(message, Message):
                raise TypeError(
                    f"messages[{offset}] 必须是 Message，收到 {type(message).__name__}"
                )
            reference = start_index + offset
            text = message.content
            bucket[reference] = (
                self._provider.embed(text),
                message.role,
                text,
            )
        return len(bucket)

    def indexed_through(self, thread_id: str) -> int | None:
        """该线程已索引的最大序号；无条目返回 None。"""
        bucket = self._store.get(thread_id)
        if not bucket:
            return None
        return max(bucket)

    def size(self, thread_id: str) -> int:
        return len(self._store.get(thread_id, {}))

    def drop(self, thread_id: str) -> None:
        """删除线程时同步清理（幂等）。"""
        self._store.pop(thread_id, None)

    def clear(self) -> None:
        """清空全部索引。Runtime shutdown 时调用，幂等。

        向量只在内存里，正常会随对象一起被回收；显式清空是为了给
        「运行时已关闭」一个可观测的终点——工具闭包仍持有 service 引用时，
        索引会一直活着，从外部看分不清是关干净了还是漏了。
        """
        self._store.clear()

    def search(
        self, thread_id: str, query: str, *, top_k: int
    ) -> list[HistoryHit]:
        """线程内余弦相似度检索。**跨线程内容不会出现在结果里。**"""
        if top_k <= 0:
            raise HistoryReferenceError(f"top_k 必须为正: {top_k}")
        bucket = self._store.get(thread_id)
        if not bucket:
            return []
        vector = self._provider.embed(query)
        scored: list[HistoryHit] = []
        for reference, (stored, role, text) in bucket.items():
            score = _cosine(vector, stored)
            if score <= 0.0:
                continue
            scored.append(
                HistoryHit(
                    reference=reference,
                    role=role,
                    score=score,
                    snippet=_clip(text, 160),
                )
            )
        scored.sort(key=lambda hit: (-hit.score, hit.reference))
        return scored[:top_k]

    def read_window(
        self, thread_id: str, reference: int, *, before: int, after: int
    ) -> list[HistoryHit]:
        """按序号取邻近窗口（用于 history_read）。"""
        if before < 0 or after < 0:
            raise HistoryReferenceError("before / after 不能为负")
        bucket = self._store.get(thread_id, {})
        if not bucket:
            return []
        hits: list[HistoryHit] = []
        for ref in range(reference - before, reference + after + 1):
            entry = bucket.get(ref)
            if entry is None:
                continue
            _, role, text = entry
            hits.append(
                HistoryHit(reference=ref, role=role, score=1.0, snippet=_clip(text, 400))
            )
        return hits


def _clip(text: str, max_chars: int) -> str:
    """安全截断：只保留前 N 字符，不携带任何指令语义标记。"""
    cleaned = text.strip()
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[:max_chars] + "…"


class HistoryIndexService:
    """把向量索引与两份持久化状态（Thread / Context）协调起来。

    只做三件事：按水位补齐、执行检索、维护 `indexed_through`。
    不修改 ThreadState —— 消息是事实源，索引永远跟在后面。
    """

    def __init__(
        self,
        *,
        index: HistoryVectorIndex,
        thread_store: ThreadStore,
        context_store: ContextStore,
        trace_sink: TraceSink | None = None,
    ) -> None:
        self._index = index
        self._thread_store = thread_store
        self._context_store = context_store
        self._trace_sink = trace_sink

    @property
    def index(self) -> HistoryVectorIndex:
        return self._index

    def _emit(
        self,
        *,
        name: str,
        run_id: str,
        thread_id: str,
        status: str,
        metadata: dict[str, object],
        error_type: str | None = None,
    ) -> None:
        if self._trace_sink is None:
            return
        self._trace_sink.emit(
            TraceEvent(
                event_id=uuid.uuid4().hex,
                parent_id=None,
                run_id=run_id,
                thread_id=thread_id,
                task_id=None,
                operation="TOOL_CALL",
                name=name,
                start=time.monotonic(),
                end=time.monotonic(),
                status=status,
                error_type=error_type,
                metadata=metadata,
            )
        )

    def _state_for(self, thread_id: str) -> ContextState:
        if not self._context_store.exists(thread_id):
            try:
                self._context_store.create(thread_id)
            except Exception:  # 并发创建冲突 → 改走读取
                pass
        return self._context_store.get(thread_id)

    def sync(self, thread_id: str, *, run_id: str = "") -> ContextState:
        """把索引水位补齐到 Thread 当前末尾；返回更新后的 ContextState。

        仅索引**索引实例尚未覆盖的消息**，因此重复调用是幂等的。
        补齐过程中任何失败都会记录 Trace 并抛出，不会返回半成品水位。

        **起点取自索引实例的实际状态，而不是 `ContextState.indexed_through`**：
        后者是持久化记录，前者才是「向量到底还在不在」的事实。
        `HistoryVectorIndex` 是内存实现，进程重启后为空，而 `indexed_through`
        已随 SQLite 落库。若拿持久化水位当起点，`start >= total` 会直接返回，
        索引永远重建不起来，`history_search` 就会**静默返回空结果**——
        这比抛错危险得多：模型会以为「历史里确实没有这件事」。
        """
        thread = self._thread_store.get(thread_id)
        state = self._state_for(thread_id)
        snapshot = thread.snapshot()
        total = len(snapshot)
        if total == 0:
            return state

        persisted_watermark = state.indexed_through
        # 索引实例的真实覆盖范围；None 表示一条都没有（新实例 / 已重启）
        actual_watermark = self._index.indexed_through(thread_id)
        start = 0 if actual_watermark is None else actual_watermark + 1
        if start >= total:
            return state

        missing = snapshot[start:]
        started = time.monotonic()
        try:
            self._index.upsert(thread_id, missing, start_index=start)
        except Exception as exc:
            self._emit(
                name="context.index.repair_failed",
                run_id=run_id,
                thread_id=thread_id,
                status="error",
                error_type=type(exc).__name__,
                metadata={
                    "from": start,
                    "through": total - 1,
                    "missing_count": len(missing),
                    "error_type": type(exc).__name__,
                },
            )
            raise HistoryIndexLagError(
                thread_id,
                indexed_through=persisted_watermark if persisted_watermark is not None else -1,
                available=total - 1,
            ) from exc

        new_through = total - 1
        # 首次建立索引（实际水位为空）不算「修复」，只有补齐既有缺口才 emit。
        # 注意「持久化水位非空但索引为空」也属于首次建立——那正是重启后的场景。
        if actual_watermark is not None:
            self._emit(
                name="context.index.repaired",
                run_id=run_id,
                thread_id=thread_id,
                status="success",
                metadata={
                    "from": start,
                    "through": new_through,
                    "missing_count": len(missing),
                    "repaired": True,
                    "watermark_before": actual_watermark,
                    "watermark_after": new_through,
                    "persisted_watermark": persisted_watermark,
                    "indexed_for": round(time.monotonic() - started, 6),
                },
            )

        def _advance(current: ContextState) -> ContextState:
            from dataclasses import replace as _replace

            return _replace(current, indexed_through=new_through)

        return cas_update(self._context_store, thread_id, _advance)

    def search(
        self, thread_id: str, query: str, *, top_k: int, run_id: str = ""
    ) -> list[HistoryHit]:
        """先补齐水位再检索；补齐失败抛 HistoryIndexLagError。"""
        limit = HISTORY_SEARCH_LIMITS["top_k"]
        if top_k <= 0 or top_k > limit:
            raise HistoryReferenceError(f"top_k 必须在 1..{limit}: {top_k}")
        max_chars = HISTORY_SEARCH_LIMITS["query_chars"]
        if len(query) > max_chars:
            raise HistoryReferenceError(f"query 超长: {len(query)} > {max_chars}")
        self.sync(thread_id, run_id=run_id)
        return self._index.search(thread_id, query, top_k=top_k)

    def read(
        self,
        thread_id: str,
        reference: int,
        *,
        before: int,
        after: int,
        run_id: str = "",
    ) -> list[HistoryHit]:
        """按 reference 返回邻近原始消息；越界参数直接拒绝。"""
        if before < 0 or before > HISTORY_SEARCH_LIMITS["before"]:
            raise HistoryReferenceError(
                f"before 必须在 0..{HISTORY_SEARCH_LIMITS['before']}: {before}"
            )
        if after < 0 or after > HISTORY_SEARCH_LIMITS["after"]:
            raise HistoryReferenceError(
                f"after 必须在 0..{HISTORY_SEARCH_LIMITS['after']}: {after}"
            )
        self.sync(thread_id, run_id=run_id)
        hits = self._index.read_window(
            thread_id, reference, before=before, after=after
        )
        if not hits:
            raise HistoryReferenceError(
                f"reference 不存在或超出索引范围: {reference}"
            )
        return hits
