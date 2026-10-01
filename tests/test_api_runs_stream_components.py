"""19D SSE 子组件并发安全审计测试。

覆盖：
- publish_terminal 在 queue_size=1 时替换旧事件（终态不丢失）
- Consumer 最终收到终态并正常结束
- publish_terminal 不永久阻塞
- 同一 run 重复终态被拒绝
- 终态后普通事件被拒绝
- unsubscribe 后终态快速返回 False
- run.failed/run.timed_out 同理
- 慢消费者下 SSE 仍以终态结束
- Runtime shutdown 不被满队列阻塞

不依赖 FastAPI；只测试底层服务组件。
"""
from __future__ import annotations

import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from mini_deerflow.api.middleware.run_event import RunEventMiddleware
from mini_deerflow.api.schemas.streaming import (
    RunEvent,
    RunEventType,
    safe_failure_message,
    terminal_status_from_exception,
)
from mini_deerflow.api.services.agent_runs import (
    AgentRunCancelledError,
    AgentRunService,
    AgentRunTimeoutError,
)
from mini_deerflow.api.services.event_broker import (
    RunEventBroker,
    iter_run_events,
)
from mini_deerflow.api.services.run_stream import (
    RunStreamManager,
    StreamHandle,
    StreamThreadNotFoundError,
    StreamValidationError,
)
from mini_deerflow.middleware import ExecutionContext, OperationType
from mini_deerflow.runtime import RuntimeConfig, create_runtime
from mini_deerflow.thread_store import InMemoryThreadStore
from mini_deerflow.tools import ToolRegistry


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def fresh_broker():
    """每个测试用独立 broker，避免跨测试污染。"""
    b = RunEventBroker()
    yield b
    b.clear()


@pytest.fixture
def fresh_broker_small():
    """queue_size=1 的独立 broker（严苛边界测试）。"""
    b = RunEventBroker(queue_maxsize=1)
    yield b
    b.clear()


# ============================================================================
# 1. queue_size=1 且已装有中间事件时，publish_terminal 替换旧事件
# ============================================================================


def test_publish_terminal_replaces_old_events_when_queue_full(fresh_broker_small):
    """queue_size=1 时，publish_terminal 丢弃旧中间事件并入队终态（终态不丢失）。"""
    b = fresh_broker_small
    q = b.subscribe("r1")

    # 填满队列
    b.publish("r1", RunEvent(run_id="r1", thread_id="t1", event_type=RunEventType.MODEL_STARTED, sequence=0))

    # publish_terminal 必须成功（不丢失）
    terminal = RunEvent(run_id="r1", thread_id="t1", event_type=RunEventType.RUN_COMPLETED, sequence=1)
    assert b.publish_terminal("r1", terminal) is True

    # 队列中有且仅有终态
    items = []
    while True:
        try:
            items.append(q.get_nowait())
        except queue.Empty:
            break
    assert len(items) == 1
    assert items[0].event_type == RunEventType.RUN_COMPLETED


def test_publish_terminal_multiple_old_events_discarded(fresh_broker_small):
    """queue_size=1 时多个旧事件全被逐出。"""
    b = fresh_broker_small
    q = b.subscribe("r1")

    b.publish("r1", RunEvent(run_id="r1", thread_id="t1", event_type=RunEventType.MODEL_STARTED, sequence=0))
    b.publish("r1", RunEvent(run_id="r1", thread_id="t1", event_type=RunEventType.MODEL_COMPLETED, sequence=1))

    terminal = RunEvent(run_id="r1", thread_id="t1", event_type=RunEventType.RUN_COMPLETED, sequence=2)
    assert b.publish_terminal("r1", terminal) is True

    items = []
    while True:
        try:
            items.append(q.get_nowait())
        except queue.Empty:
            break
    assert len(items) == 1
    assert items[0].event_type == RunEventType.RUN_COMPLETED


# ============================================================================
# 2. Consumer 最终收到 run.completed
# ============================================================================


def test_consumer_receives_terminal_and_stream_ends(fresh_broker):
    """Consumer 通过 iter_run_events 最终收到终态。"""
    b = fresh_broker
    q = b.subscribe("r1")

    # 发中间事件
    b.publish("r1", RunEvent(run_id="r1", thread_id="t1", event_type=RunEventType.MODEL_STARTED, sequence=0))
    b.publish("r1", RunEvent(run_id="r1", thread_id="t1", event_type=RunEventType.MODEL_COMPLETED, sequence=1))
    # 发终态
    b.publish_terminal("r1", RunEvent(run_id="r1", thread_id="t1", event_type=RunEventType.RUN_COMPLETED, sequence=2))

    collected = list(iter_run_events(q, poll_timeout=0.05))
    assert len(collected) == 3
    assert collected[-1].event_type == RunEventType.RUN_COMPLETED


def test_consumer_receives_run_failed(fresh_broker):
    """run.failed 终态能正常传达。"""
    b = fresh_broker
    q = b.subscribe("r1")

    b.publish("r1", RunEvent(run_id="r1", thread_id="t1", event_type=RunEventType.MODEL_STARTED, sequence=0))
    b.publish_terminal("r1", RunEvent(run_id="r1", thread_id="t1", event_type=RunEventType.RUN_FAILED, sequence=1))

    collected = list(iter_run_events(q, poll_timeout=0.05))
    assert any(e.event_type == RunEventType.RUN_FAILED for e in collected)


def test_consumer_receives_run_timed_out(fresh_broker):
    """run.timed_out 终态能正常传达。"""
    b = fresh_broker
    q = b.subscribe("r1")

    b.publish("r1", RunEvent(run_id="r1", thread_id="t1", event_type=RunEventType.MODEL_STARTED, sequence=0))
    b.publish_terminal("r1", RunEvent(run_id="r1", thread_id="t1", event_type=RunEventType.RUN_TIMED_OUT, sequence=1))

    collected = list(iter_run_events(q, poll_timeout=0.05))
    assert any(e.event_type == RunEventType.RUN_TIMED_OUT for e in collected)


# ============================================================================
# 3. Consumer 收到终态后结束，不继续等待
# ============================================================================


def test_iter_run_events_stops_immediately_on_terminal(fresh_broker):
    """iter_run_events 收到终态后立即退出，不等待 poll_timeout。"""
    b = fresh_broker
    q = b.subscribe("r1")

    b.publish_terminal("r1", RunEvent(run_id="r1", thread_id="t1", event_type=RunEventType.RUN_COMPLETED, sequence=0))

    start = time.time()
    collected = list(iter_run_events(q, poll_timeout=0.5))
    elapsed = time.time() - start

    assert len(collected) == 1
    assert collected[0].event_type == RunEventType.RUN_COMPLETED
    assert elapsed < 0.15, f"iter 耗时 {elapsed:.2f}s，应立即退出"


# ============================================================================
# 4. publish_terminal 不会无限阻塞
# ============================================================================


def test_publish_terminal_never_blocks_forever(fresh_broker_small):
    """publish_terminal 永不永久阻塞；使用 get_nowait 循环。"""
    b = fresh_broker_small
    q = b.subscribe("r1")

    # 填满队列
    b.publish("r1", RunEvent(run_id="r1", thread_id="t1", event_type=RunEventType.MODEL_STARTED, sequence=0))

    start = time.time()
    terminal = RunEvent(run_id="r1", thread_id="t1", event_type=RunEventType.RUN_COMPLETED, sequence=1)
    result = b.publish_terminal("r1", terminal)
    elapsed = time.time() - start

    assert result is True
    assert elapsed < 0.05, f"publish_terminal 耗时 {elapsed:.2f}s，不应超过 50ms"


def test_publish_terminal_with_consumer_reading_slowly(fresh_broker_small):
    """慢消费者运行时，publish_terminal 仍能成功（终态不丢）。"""
    b = fresh_broker_small
    q = b.subscribe("r1")

    b.publish("r1", RunEvent(run_id="r1", thread_id="t1", event_type=RunEventType.MODEL_STARTED, sequence=0))

    stop_consumer = threading.Event()

    def slow_consumer():
        try:
            while not stop_consumer.is_set():
                try:
                    q.get(timeout=0.05)
                except queue.Empty:
                    continue
        except Exception:  # noqa: BLE001
            pass

    t = threading.Thread(target=slow_consumer, daemon=True)
    t.start()

    try:
        start = time.time()
        terminal = RunEvent(run_id="r1", thread_id="t1", event_type=RunEventType.RUN_COMPLETED, sequence=1)
        result = b.publish_terminal("r1", terminal)
        elapsed = time.time() - start

        assert result is True
        assert elapsed < 0.2, f"慢消费者下 publish_terminal 耗时 {elapsed:.2f}s"
    finally:
        stop_consumer.set()
        t.join(timeout=1.0)


# ============================================================================
# 5. 同一个 run 第二次发布终态返回 False
# ============================================================================


def test_publish_terminal_idempotent(fresh_broker):
    """同一 run 重复调用 publish_terminal 返回 False。"""
    b = fresh_broker
    q = b.subscribe("r1")

    terminal = RunEvent(run_id="r1", thread_id="t1", event_type=RunEventType.RUN_COMPLETED, sequence=0)
    assert b.publish_terminal("r1", terminal) is True
    assert b.publish_terminal("r1", terminal) is False
    assert b.publish_terminal("r1", terminal) is False

    items = []
    while True:
        try:
            items.append(q.get_nowait())
        except queue.Empty:
            break
    assert len(items) == 1


def test_publish_terminal_no_extra_sentinel(fresh_broker):
    """publish_terminal 不写入额外哨兵；队列只有终态事件。"""
    b = fresh_broker
    q = b.subscribe("r1")

    b.publish_terminal("r1", RunEvent(run_id="r1", thread_id="t1", event_type=RunEventType.RUN_COMPLETED, sequence=0))

    items = []
    while True:
        try:
            items.append(q.get_nowait())
        except queue.Empty:
            break

    assert len(items) == 1
    assert isinstance(items[0], RunEvent)


# ============================================================================
# 6. 终态之后的普通事件被拒绝
# ============================================================================


def test_publish_after_terminal_rejected(fresh_broker):
    """publish_terminal 之后普通 publish 返回 False。"""
    b = fresh_broker
    q = b.subscribe("r1")

    b.publish_terminal("r1", RunEvent(run_id="r1", thread_id="t1", event_type=RunEventType.RUN_COMPLETED, sequence=0))

    late_event = RunEvent(run_id="r1", thread_id="t1", event_type=RunEventType.MODEL_STARTED, sequence=1)
    assert b.publish("r1", late_event) is False

    collected = list(iter_run_events(q, poll_timeout=0.05))
    assert len(collected) == 1


def test_iter_run_events_respects_terminal_marker(fresh_broker):
    """iter_run_events 在终态后不再接收普通事件。"""
    b = fresh_broker
    q = b.subscribe("r1")

    b.publish_terminal("r1", RunEvent(run_id="r1", thread_id="t1", event_type=RunEventType.RUN_COMPLETED, sequence=0))
    b.publish("r1", RunEvent(run_id="r1", thread_id="t1", event_type=RunEventType.MODEL_STARTED, sequence=1))

    collected = list(iter_run_events(q, poll_timeout=0.05))
    assert len(collected) == 1
    assert collected[0].event_type == RunEventType.RUN_COMPLETED


# ============================================================================
# 7. unsubscribe 后发布终态立即返回 False
# ============================================================================


def test_publish_terminal_after_unsubscribe_returns_false(fresh_broker):
    """unsubscribe 后 publish_terminal 立即返回 False（不重新创建队列）。"""
    b = fresh_broker
    b.subscribe("r1")
    b.unsubscribe("r1")

    terminal = RunEvent(run_id="r1", thread_id="t1", event_type=RunEventType.RUN_COMPLETED, sequence=0)
    start = time.time()
    result = b.publish_terminal("r1", terminal)
    elapsed = time.time() - start

    assert result is False
    assert elapsed < 0.05


def test_publish_after_unsubscribe_returns_false(fresh_broker):
    """unsubscribe 后普通 publish 也返回 False。"""
    b = fresh_broker
    b.subscribe("r1")
    b.unsubscribe("r1")

    e = RunEvent(run_id="r1", thread_id="t1", event_type=RunEventType.MODEL_STARTED, sequence=0)
    assert b.publish("r1", e) is False


# ============================================================================
# 8. run.failed 和 run.timed_out 同样能够成为终态
# ============================================================================


def test_publish_terminal_run_failed_blocks_no_more_than_get_nowait_cycle(fresh_broker_small):
    """run.failed 在 queue_size=1 时同样能入队。"""
    b = fresh_broker_small
    q = b.subscribe("r1")

    b.publish("r1", RunEvent(run_id="r1", thread_id="t1", event_type=RunEventType.MODEL_STARTED, sequence=0))

    terminal = RunEvent(run_id="r1", thread_id="t1", event_type=RunEventType.RUN_FAILED, sequence=1)
    assert b.publish_terminal("r1", terminal) is True

    items = []
    while True:
        try:
            items.append(q.get_nowait())
        except queue.Empty:
            break
    assert len(items) == 1
    assert items[0].event_type == RunEventType.RUN_FAILED


def test_publish_terminal_run_timed_out_blocks_no_more_than_get_nowait_cycle(fresh_broker_small):
    """run.timed_out 在 queue_size=1 时同样能入队。"""
    b = fresh_broker_small
    q = b.subscribe("r1")

    b.publish("r1", RunEvent(run_id="r1", thread_id="t1", event_type=RunEventType.MODEL_STARTED, sequence=0))

    terminal = RunEvent(run_id="r1", thread_id="t1", event_type=RunEventType.RUN_TIMED_OUT, sequence=1)
    assert b.publish_terminal("r1", terminal) is True

    items = []
    while True:
        try:
            items.append(q.get_nowait())
        except queue.Empty:
            break
    assert len(items) == 1
    assert items[0].event_type == RunEventType.RUN_TIMED_OUT


# ============================================================================
# 9. 客户端消费很慢时，SSE 仍然以终态结束
# ============================================================================


def test_slow_consumer_eventually_receives_terminal(fresh_broker_small):
    """慢消费者场景：publish_terminal 保证终态最终被消费。"""
    b = fresh_broker_small
    q = b.subscribe("r1")

    b.publish("r1", RunEvent(run_id="r1", thread_id="t1", event_type=RunEventType.MODEL_STARTED, sequence=0))

    # publish_terminal 成功（替换旧事件）
    terminal = RunEvent(run_id="r1", thread_id="t1", event_type=RunEventType.RUN_COMPLETED, sequence=1)
    assert b.publish_terminal("r1", terminal) is True

    # 慢消费者：每次 get 后 sleep
    stop_consumer = threading.Event()
    collected: list[RunEvent] = []

    def slow_consumer():
        while not stop_consumer.is_set():
            try:
                item = q.get(timeout=0.05)
            except queue.Empty:
                continue
            if item is None or not isinstance(item, RunEvent):
                break
            collected.append(item)
            if item.event_type in {
                RunEventType.RUN_COMPLETED,
                RunEventType.RUN_FAILED,
                RunEventType.RUN_TIMED_OUT,
                RunEventType.RUN_CANCELLED,
            }:
                stop_consumer.set()
                break

    t = threading.Thread(target=slow_consumer, daemon=True)
    t.start()
    t.join(timeout=2.0)
    stop_consumer.set()

    assert len(collected) >= 1
    assert collected[-1].event_type == RunEventType.RUN_COMPLETED


# ============================================================================
# 10. Runtime shutdown 不会被满队列阻塞
# ============================================================================


def test_broker_close_returns_even_with_no_consumers(fresh_broker):
    """broker.close() 不等待消费者；立即返回。"""
    b = fresh_broker
    q = b.subscribe("r1")
    b.publish("r1", RunEvent(run_id="r1", thread_id="t1", event_type=RunEventType.MODEL_STARTED, sequence=0))

    start = time.time()
    b.close()
    elapsed = time.time() - start
    assert elapsed < 0.1


def test_broker_close_does_not_wait_for_consumers(fresh_broker):
    """broker.close() 时队列有未消费事件；不等待消费者。"""
    b = fresh_broker
    q = b.subscribe("r1")
    for i in range(10):
        b.publish("r1", RunEvent(run_id="r1", thread_id="t1", event_type=RunEventType.MODEL_STARTED, sequence=i))

    start = time.time()
    b.close()
    elapsed = time.time() - start
    assert elapsed < 0.1


def test_iter_run_events_stops_on_broker_close_sentinel(fresh_broker):
    """broker.close() 发送关闭哨兵，iter_run_events 在 poll_timeout 内退出。"""
    b = fresh_broker
    q = b.subscribe("r1")

    b.close()

    start = time.time()
    collected = list(iter_run_events(q, poll_timeout=0.05))
    elapsed = time.time() - start

    assert len(collected) == 0
    assert elapsed < 0.15


def test_run_stream_manager_shutdown_never_waits(fresh_broker):
    """shutdown() 必须立即返回，不等待 worker 完成。"""
    from mini_deerflow.thread_run_coordinator import ThreadRunCoordinator

    store = InMemoryThreadStore()
    store.create("t1")

    def slow_runner(thread_id, user_input, run_id=None):
        time.sleep(5.0)
        return ("reply", [])

    coord = ThreadRunCoordinator()
    service = AgentRunService(runner=slow_runner, store=store, coordinator=coord)
    executor = ThreadPoolExecutor(max_workers=1)
    manager = RunStreamManager(fresh_broker, service, coord, executor)

    handle = manager.submit("t1", "hi")

    start = time.time()
    manager.shutdown()
    elapsed = time.time() - start
    assert elapsed < 0.2


def test_runtime_shutdown_does_not_block():
    """RuntimeContext.shutdown() 立即返回。"""
    rt = create_runtime(store=InMemoryThreadStore(), registry=ToolRegistry(), config=RuntimeConfig())
    broker = rt.run_event_broker

    start = time.time()
    rt.shutdown()
    elapsed = time.time() - start

    assert elapsed < 0.2
    with pytest.raises(RuntimeError):
        broker.subscribe("new")


# ============================================================================
# 回归：broker 基本操作
# ============================================================================


def test_broker_subscribe_creates_queue(fresh_broker):
    b = fresh_broker
    b._maxsize = 8
    q = b.subscribe("r1")
    assert isinstance(q, queue.Queue)
    assert q.maxsize == 8


def test_broker_subscribe_is_idempotent(fresh_broker):
    b = fresh_broker
    q1 = b.subscribe("r1")
    q2 = b.subscribe("r1")
    assert q1 is q2


def test_broker_unsubscribe_is_idempotent(fresh_broker):
    b = fresh_broker
    b.subscribe("r1")
    b.unsubscribe("r1")
    b.unsubscribe("r1")


def test_broker_unsubscribe_does_not_recreate_queue(fresh_broker):
    """unsubscribe 后同一 run_id 的队列不存在。"""
    b = fresh_broker
    b.subscribe("r1")
    b.unsubscribe("r1")
    # 再次 subscribe 创建新队列
    q = b.subscribe("r1")
    assert isinstance(q, queue.Queue)


def test_broker_publish_returns_false_when_no_subscriber(fresh_broker):
    b = fresh_broker
    e = RunEvent(run_id="r1", thread_id="t1", event_type=RunEventType.MODEL_STARTED, sequence=0)
    assert b.publish("r1", e) is False


def test_broker_publish_ordered(fresh_broker):
    b = fresh_broker
    q = b.subscribe("r1")
    for i in range(5):
        e = RunEvent(run_id="r1", thread_id="t1", event_type=RunEventType.MODEL_STARTED, sequence=i)
        assert b.publish("r1", e) is True
    received = [q.get_nowait() for _ in range(5)]
    assert [e.sequence for e in received] == [0, 1, 2, 3, 4]


def test_broker_next_sequence_monotonic(fresh_broker):
    b = fresh_broker
    seqs = [b.next_sequence("r1") for _ in range(5)]
    assert seqs == [0, 1, 2, 3, 4]


def test_broker_stats_reports_active_runs(fresh_broker):
    b = fresh_broker
    b.subscribe("r1")
    b.subscribe("r2")
    assert b.stats()["active_runs"] == 2


def test_broker_close_is_idempotent(fresh_broker):
    b = fresh_broker
    b.subscribe("r1")
    b.close()
    b.close()


def test_broker_subscribe_after_close_raises(fresh_broker):
    b = fresh_broker
    b.close()
    with pytest.raises(RuntimeError):
        b.subscribe("r1")


# ============================================================================
# publish_overflow（终态语义）
# ============================================================================


def test_publish_overflow_emits_terminal(fresh_broker_small):
    """publish_overflow 使用 publish_terminal 语义。"""
    b = fresh_broker_small
    q = b.subscribe("r1")

    b.publish("r1", RunEvent(run_id="r1", thread_id="t1", event_type=RunEventType.MODEL_STARTED, sequence=0))
    assert b.publish_overflow("r1") is True

    items = []
    while True:
        try:
            items.append(q.get_nowait())
        except queue.Empty:
            break
    assert any(isinstance(it, RunEvent) and it.event_type == RunEventType.RUN_FAILED for it in items)


# ============================================================================
# safe_failure_message / terminal_status_from_exception
# ============================================================================


def test_safe_failure_message_hides_message_and_traceback():
    class WeirdError(Exception):
        pass

    e = WeirdError("包含路径/Token")
    msg = safe_failure_message(e)
    assert "包含路径/Token" not in msg
    assert "WeirdError" in msg


def test_terminal_status_from_exception_timeout():
    assert terminal_status_from_exception(TimeoutError()) == RunEventType.RUN_TIMED_OUT


def test_terminal_status_from_exception_cancelled():
    e = AgentRunCancelledError("t", "r")
    assert terminal_status_from_exception(e) == RunEventType.RUN_CANCELLED


def test_terminal_status_from_exception_default_failed():
    assert terminal_status_from_exception(RuntimeError("x")) == RunEventType.RUN_FAILED


# ============================================================================
# 并发竞态测试
# ============================================================================


def _make_terminal(run_id: str, seq: int) -> RunEvent:
    return RunEvent(run_id=run_id, thread_id="t1", event_type=RunEventType.RUN_COMPLETED, sequence=seq)


def _is_terminal_type(et: RunEventType) -> bool:
    return et in {
        RunEventType.RUN_COMPLETED,
        RunEventType.RUN_FAILED,
        RunEventType.RUN_TIMED_OUT,
        RunEventType.RUN_CANCELLED,
    }


def test_publish_terminal_consumer_drains_between_full_and_get(fresh_broker_small):
    """Full 后 Consumer 恰好清空队列时，Empty 后继续重试，终态仍成功。"""
    b = fresh_broker_small
    q = b.subscribe("r1")

    b.publish("r1", RunEvent(run_id="r1", thread_id="t1", event_type=RunEventType.MODEL_STARTED, sequence=0))

    barrier = threading.Barrier(2)

    def consumer():
        barrier.wait(timeout=1.0)
        try:
            q.get(timeout=1.0)
        except queue.Empty:
            pass

    t = threading.Thread(target=consumer, daemon=True)
    t.start()
    barrier.wait(timeout=1.0)

    try:
        result = b.publish_terminal("r1", _make_terminal("r1", 1))
        assert result is True, "Empty 后继续重试，终态应成功"
    finally:
        t.join(timeout=1.0)


def test_publish_and_publish_terminal_concurrent_no_ordinary_after_terminal(fresh_broker_small):
    """普通 publish 与 publish_terminal 并发时，终态之后不存在普通事件。"""
    b = fresh_broker_small
    q = b.subscribe("r1")

    barrier = threading.Barrier(2)

    def publisher():
        barrier.wait(timeout=1.0)
        for i in range(5):
            b.publish("r1", RunEvent(run_id="r1", thread_id="t1", event_type=RunEventType.MODEL_STARTED, sequence=i))

    def terminal_publisher():
        barrier.wait(timeout=1.0)
        b.publish_terminal("r1", _make_terminal("r1", 99))

    t_pub = threading.Thread(target=publisher, daemon=True)
    t_term = threading.Thread(target=terminal_publisher, daemon=True)

    t_term.start()
    t_pub.start()

    t_pub.join(timeout=2.0)
    t_term.join(timeout=2.0)

    items = list(iter_run_events(q, poll_timeout=0.05))
    types = [it.event_type for it in items if isinstance(it, RunEvent)]

    # 最多只有一个终态
    terminal_count = sum(1 for t in types if _is_terminal_type(t))
    assert terminal_count <= 1

    # 终态之后没有普通事件
    if RunEventType.RUN_COMPLETED in types:
        terminal_idx = types.index(RunEventType.RUN_COMPLETED)
        after = types[terminal_idx + 1:]
        assert all(t not in {RunEventType.MODEL_STARTED, RunEventType.MODEL_COMPLETED} for t in after)


def test_at_most_one_terminal_per_run_under_race(fresh_broker_small):
    """100 轮并发：Barrier 同时触发 N 个线程，验证每轮最多一个终态成功。"""
    ROUNDS = 100
    errors: list[str] = []

    for round_i in range(ROUNDS):
        b = RunEventBroker(queue_maxsize=1)
        q = b.subscribe(f"r{round_i}")
        barrier = threading.Barrier(2)
        results: dict[str, bool] = {}

        def try_terminal(label: str):
            barrier.wait(timeout=2.0)
            results[label] = b.publish_terminal(
                f"r{round_i}",
                RunEvent(run_id=f"r{round_i}", thread_id="t1",
                         event_type=RunEventType.RUN_COMPLETED, sequence=0),
            )

        t1 = threading.Thread(target=try_terminal, args=("t1",), daemon=True)
        t2 = threading.Thread(target=try_terminal, args=("t2",), daemon=True)
        t1.start()
        t2.start()

        t1.join(timeout=2.0)
        t2.join(timeout=2.0)

        true_count = sum(1 for v in results.values() if v is True)
        if true_count != 1:
            errors.append(f"round {round_i}: {true_count} terminals succeeded: {results}")

        items = []
        while True:
            try:
                items.append(q.get_nowait())
            except queue.Empty:
                break
        terminals = [it for it in items if isinstance(it, RunEvent) and _is_terminal_type(it.event_type)]
        if len(terminals) != 1:
            errors.append(f"round {round_i}: queue has {len(terminals)} terminals")

        b.close()

    assert not errors, f"并发竞态失败:\n" + "\n".join(errors[:5])


def test_publish_and_publish_terminal_concurrent_no_ordinary_after_terminal(fresh_broker_small):
    """100 轮并发：普通 publish 与 publish_terminal 同时执行，终态之后无普通事件。"""
    ROUNDS = 100
    errors: list[str] = []

    for round_i in range(ROUNDS):
        b = RunEventBroker(queue_maxsize=1)
        q = b.subscribe(f"r{round_i}")
        barrier = threading.Barrier(2)
        ordinary_results: list[bool] = []
        terminal_ok = [False]

        def publisher():
            barrier.wait(timeout=2.0)
            for i in range(3):
                ordinary_results.append(
                    b.publish(
                        f"r{round_i}",
                        RunEvent(run_id=f"r{round_i}", thread_id="t1",
                                 event_type=RunEventType.MODEL_STARTED, sequence=i),
                    ),
                )

        def terminal_publisher():
            barrier.wait(timeout=2.0)
            r = b.publish_terminal(
                f"r{round_i}",
                RunEvent(run_id=f"r{round_i}", thread_id="t1",
                         event_type=RunEventType.RUN_COMPLETED, sequence=99),
            )
            terminal_ok[0] = r

        t_pub = threading.Thread(target=publisher, daemon=True)
        t_term = threading.Thread(target=terminal_publisher, daemon=True)
        t_pub.start()
        t_term.start()
        t_pub.join(timeout=2.0)
        t_term.join(timeout=2.0)

        items = list(iter_run_events(q, poll_timeout=0.05))
        types = [it.event_type for it in items if isinstance(it, RunEvent)]

        # 终态后无普通事件
        if RunEventType.RUN_COMPLETED in types:
            terminal_idx = types.index(RunEventType.RUN_COMPLETED)
            after = types[terminal_idx + 1:]
            if any(t in {RunEventType.MODEL_STARTED, RunEventType.MODEL_COMPLETED} for t in after):
                errors.append(f"round {round_i}: ordinary events after terminal: {after}")

        b.close()

    assert not errors, f"并发竞态失败:\n" + "\n".join(errors[:5])


# ============================================================================
# RunStreamManager
# ============================================================================


def _build_manager(broker):
    from mini_deerflow.thread_run_coordinator import ThreadRunCoordinator

    store = InMemoryThreadStore()
    store.create("t1")

    def runner(thread_id, user_input, run_id=None):
        return (f"reply:{user_input}", [])

    coord = ThreadRunCoordinator()
    service = AgentRunService(runner=runner, store=store, coordinator=None)  # type: ignore[arg-type]
    service._coordinator = coord
    executor = ThreadPoolExecutor(max_workers=2)
    manager = RunStreamManager(broker, service, coord, executor)
    return manager, executor


def test_stream_manager_submit_returns_handle(fresh_broker):
    manager, executor = _build_manager(fresh_broker)
    try:
        handle = manager.submit("t1", "hi")
        assert isinstance(handle, StreamHandle)
        assert handle.run_id.startswith("run-")
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
        fresh_broker.close()


def test_stream_manager_emits_terminal_event(fresh_broker):
    manager, executor = _build_manager(fresh_broker)
    try:
        handle = manager.submit("t1", "hello")
        events = list(iter_run_events(handle.queue, poll_timeout=0.5))
        terminal = [e for e in events if e.event_type == RunEventType.RUN_COMPLETED]
        assert len(terminal) == 1
        assert terminal[0].answer == "reply:hello"
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
        fresh_broker.close()


def test_stream_manager_thread_not_found(fresh_broker):
    manager, executor = _build_manager(fresh_broker)
    try:
        with pytest.raises(StreamThreadNotFoundError):
            manager.submit("nope", "hi")
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
        fresh_broker.close()


def test_stream_manager_validation_error(fresh_broker):
    manager, executor = _build_manager(fresh_broker)
    try:
        with pytest.raises(StreamValidationError):
            manager.submit("t1", "")
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
        fresh_broker.close()


def test_stream_manager_shutdown_is_idempotent(fresh_broker):
    manager, executor = _build_manager(fresh_broker)
    manager.shutdown()
    manager.shutdown()
    with pytest.raises(RuntimeError):
        fresh_broker.subscribe("new")


# ============================================================================
# RunEventMiddleware
# ============================================================================


def _ctx(op: OperationType, run_id: str = "r1", thread_id: str = "t1") -> ExecutionContext:
    return ExecutionContext(run_id=run_id, thread_id=thread_id, operation=op, operation_name=op.name.lower())


def test_middleware_publishes_run_started(fresh_broker):
    b = fresh_broker
    q = b.subscribe("r1")
    mw = RunEventMiddleware(b)
    mw.before(_ctx(OperationType.AGENT_RUN, "r1", "t1"))
    b.close()
    events = list(iter_run_events(q, poll_timeout=0.05))
    assert len(events) == 1
    assert events[0].event_type == RunEventType.RUN_STARTED


def test_middleware_publishes_model_started_completed(fresh_broker):
    b = fresh_broker
    q = b.subscribe("r1")
    mw = RunEventMiddleware(b)
    mw.before(_ctx(OperationType.AGENT_RUN, "r1", "t1"))
    mw.before(_ctx(OperationType.MODEL_CALL))
    mw.success(_ctx(OperationType.MODEL_CALL), "result")
    mw.finally_(_ctx(OperationType.AGENT_RUN))
    b.close()
    events = list(iter_run_events(q, poll_timeout=0.05))
    types = [e.event_type for e in events]
    assert RunEventType.RUN_STARTED in types
    assert RunEventType.MODEL_STARTED in types
    assert RunEventType.MODEL_COMPLETED in types


def test_middleware_does_not_publish_agent_run_terminal(fresh_broker):
    """AGENT_RUN 终态不由 middleware 发。"""
    b = fresh_broker
    q = b.subscribe("r1")
    mw = RunEventMiddleware(b)
    mw.before(_ctx(OperationType.AGENT_RUN, "r1", "t1"))
    mw.success(_ctx(OperationType.AGENT_RUN), ("answer", []))
    mw.finally_(_ctx(OperationType.AGENT_RUN))
    b.close()
    events = list(iter_run_events(q, poll_timeout=0.05))
    types = [e.event_type for e in events]
    assert RunEventType.RUN_COMPLETED not in types


# ============================================================================
# RuntimeContext
# ============================================================================


def test_runtime_context_has_stream_components():
    rt = create_runtime(store=InMemoryThreadStore(), registry=ToolRegistry(), config=RuntimeConfig())
    try:
        assert rt.run_event_broker is not None
        assert rt.run_stream_manager is not None
        assert rt.run_event_middleware is not None
        assert rt.stream_executor is not None
    finally:
        rt.shutdown()
