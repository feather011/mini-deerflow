"""MemoryContextBuilder 测试。"""

from __future__ import annotations

import unittest

from mini_deerflow.memory import InMemoryMemoryStore, Memory, MemoryStore
from mini_deerflow.memory_context import MemoryContextBuilder, MemoryContextProvider


def _memory(
    memory_id: str,
    kind: str = "fact",
    content: str = "测试内容",
    source_thread_id: str = "thread-1",
) -> Memory:
    return Memory(
        memory_id=memory_id,
        kind=kind,
        content=content,
        source_thread_id=source_thread_id,
    )


class StubMemoryStore:
    """桩 MemoryStore，用于验证调用次数和异常传播。"""

    def __init__(
        self,
        memories: list[Memory] | None = None,
        exception: Exception | None = None,
    ) -> None:
        self._memories = memories or []
        self._exception = exception
        self.list_memories_call_count = 0

    def list_memories(self) -> list[Memory]:
        self.list_memories_call_count += 1
        if self._exception is not None:
            raise self._exception
        return list(self._memories)

    def exists(self, memory_id: str) -> bool:
        return any(m.memory_id == memory_id for m in self._memories)

    def add(self, memory: Memory) -> None:
        self._memories.append(memory)


class TestMemoryContextProviderProtocol(unittest.TestCase):
    """验证 MemoryContextBuilder 实现了 MemoryContextProvider Protocol。"""

    def test_implements_protocol(self):
        """MemoryContextBuilder 必须实现 MemoryContextProvider 协议。"""
        store = InMemoryMemoryStore()
        builder: MemoryContextProvider = MemoryContextBuilder(store)
        self.assertTrue(hasattr(builder, "build"))


class TestMemoryContextBuilderEmpty(unittest.TestCase):
    """空 Store 测试。"""

    def test_empty_store_returns_empty_string(self):
        """空 Store 返回空字符串。"""
        store = InMemoryMemoryStore()
        builder = MemoryContextBuilder(store)
        result = builder.build()
        self.assertEqual(result, "")

    def test_empty_after_deletion(self):
        """删除所有记忆后返回空字符串。"""
        store = InMemoryMemoryStore()
        store.add(_memory("m1"))
        store.delete("m1")
        builder = MemoryContextBuilder(store)
        self.assertEqual(builder.build(), "")


class TestMemoryContextBuilderSingleKind(unittest.TestCase):
    """单类型记忆测试。"""

    def test_only_instruction(self):
        """只有一条 instruction 时正确输出。"""
        store = InMemoryMemoryStore()
        store.add(_memory("m1", kind="instruction", content="回答前先说您好"))
        builder = MemoryContextBuilder(store)
        result = builder.build()
        self.assertIn("长期指令", result)
        self.assertIn("回答前先说您好", result)
        self.assertNotIn("用户偏好", result)
        self.assertNotIn("相关事实", result)

    def test_only_preference(self):
        """只有一条 preference 时正确输出。"""
        store = InMemoryMemoryStore()
        store.add(_memory("m1", kind="preference", content="用户喜欢 uv"))
        builder = MemoryContextBuilder(store)
        result = builder.build()
        self.assertIn("用户偏好", result)
        self.assertIn("用户喜欢 uv", result)
        self.assertNotIn("长期指令", result)
        self.assertNotIn("相关事实", result)

    def test_only_fact(self):
        """只有一条 fact 时正确输出。"""
        store = InMemoryMemoryStore()
        store.add(_memory("m1", kind="fact", content="用户住在上海"))
        builder = MemoryContextBuilder(store)
        result = builder.build()
        self.assertIn("相关事实", result)
        self.assertIn("用户住在上海", result)
        self.assertNotIn("长期指令", result)
        self.assertNotIn("用户偏好", result)


class TestMemoryContextBuilderMultipleKinds(unittest.TestCase):
    """多类型记忆测试。"""

    def test_all_three_kinds_present(self):
        """三种类型同时存在时都正确输出。"""
        store = InMemoryMemoryStore()
        store.add(_memory("m1", kind="instruction", content="指令一"))
        store.add(_memory("m2", kind="preference", content="偏好一"))
        store.add(_memory("m3", kind="fact", content="事实一"))
        builder = MemoryContextBuilder(store)
        result = builder.build()

        self.assertIn("长期指令", result)
        self.assertIn("用户偏好", result)
        self.assertIn("相关事实", result)
        self.assertIn("指令一", result)
        self.assertIn("偏好一", result)
        self.assertIn("事实一", result)


class TestMemoryContextBuilderSectionOrder(unittest.TestCase):
    """分类顺序测试。"""

    def test_section_order_fixed(self):
        """分类顺序固定为 instruction、preference、fact。"""
        store = InMemoryMemoryStore()
        # 按反序添加，验证输出顺序是固定的
        store.add(_memory("m3", kind="fact", content="事实"))
        store.add(_memory("m2", kind="preference", content="偏好"))
        store.add(_memory("m1", kind="instruction", content="指令"))
        builder = MemoryContextBuilder(store)
        result = builder.build()

        inst_pos = result.find("长期指令")
        pref_pos = result.find("用户偏好")
        fact_pos = result.find("相关事实")

        self.assertLess(inst_pos, pref_pos)
        self.assertLess(pref_pos, fact_pos)


class TestMemoryContextBuilderSorting(unittest.TestCase):
    """排序测试。"""

    def test_same_kind_sorted_by_memory_id(self):
        """同一分类按 memory_id 升序排列。"""
        store = InMemoryMemoryStore()
        store.add(_memory("m3", kind="preference", content="第三个"))
        store.add(_memory("m1", kind="preference", content="第一个"))
        store.add(_memory("m2", kind="preference", content="第二个"))
        builder = MemoryContextBuilder(store)
        result = builder.build()

        first_pos = result.find("第一个")
        second_pos = result.find("第二个")
        third_pos = result.find("第三个")

        self.assertLess(first_pos, second_pos)
        self.assertLess(second_pos, third_pos)


class TestMemoryContextBuilderFormat(unittest.TestCase):
    """格式测试。"""

    def test_header_present(self):
        """第一行固定为"长期记忆"。"""
        store = InMemoryMemoryStore()
        store.add(_memory("m1", kind="fact", content="事实"))
        builder = MemoryContextBuilder(store)
        result = builder.build()
        self.assertTrue(result.startswith("长期记忆"))

    def test_no_empty_section_headers(self):
        """不输出空分类标题。"""
        store = InMemoryMemoryStore()
        store.add(_memory("m1", kind="fact", content="事实"))
        builder = MemoryContextBuilder(store)
        result = builder.build()

        self.assertNotIn("长期指令", result)
        self.assertNotIn("用户偏好", result)

    def test_item_prefix(self):
        """每条记忆使用"- "开头。"""
        store = InMemoryMemoryStore()
        store.add(_memory("m1", kind="fact", content="测试内容"))
        builder = MemoryContextBuilder(store)
        result = builder.build()
        self.assertIn("- 测试内容", result)

    def test_chinese_content(self):
        """中文内容正确处理。"""
        store = InMemoryMemoryStore()
        store.add(_memory("m1", kind="fact", content="用户住在北京市朝阳区"))
        builder = MemoryContextBuilder(store)
        result = builder.build()
        self.assertIn("用户住在北京市朝阳区", result)

    def test_no_memory_id(self):
        """不展示 memory_id。"""
        store = InMemoryMemoryStore()
        store.add(_memory("m1", kind="fact", content="内容"))
        builder = MemoryContextBuilder(store)
        result = builder.build()
        self.assertNotIn("m1", result)
        self.assertNotIn("memory_id", result.lower())

    def test_no_source_thread_id(self):
        """不展示 source_thread_id。"""
        store = InMemoryMemoryStore()
        store.add(_memory("m1", kind="fact", content="内容", source_thread_id="thread-xyz"))
        builder = MemoryContextBuilder(store)
        result = builder.build()
        self.assertNotIn("thread-xyz", result)
        self.assertNotIn("source_thread_id", result.lower())

    def test_no_trailing_newline(self):
        """输出结尾没有额外换行。"""
        store = InMemoryMemoryStore()
        store.add(_memory("m1", kind="fact", content="内容"))
        builder = MemoryContextBuilder(store)
        result = builder.build()
        self.assertFalse(result.endswith("\n"))


class TestMemoryContextBuilderConsistency(unittest.TestCase):
    """一致性和稳定性测试。"""

    def test_multiple_builds_same_result(self):
        """相同输入多次构建结果相同。"""
        store = InMemoryMemoryStore()
        store.add(_memory("m1", kind="instruction", content="指令"))
        store.add(_memory("m2", kind="preference", content="偏好"))
        store.add(_memory("m3", kind="fact", content="事实"))
        builder = MemoryContextBuilder(store)

        result1 = builder.build()
        result2 = builder.build()
        result3 = builder.build()

        self.assertEqual(result1, result2)
        self.assertEqual(result2, result3)


class TestMemoryContextBuilderCallCount(unittest.TestCase):
    """调用次数测试。"""

    def test_list_memories_called_once(self):
        """每次 build 只调用一次 list_memories。"""
        stub = StubMemoryStore([_memory("m1")])
        builder = MemoryContextBuilder(stub)

        builder.build()

        self.assertEqual(stub.list_memories_call_count, 1)

    def test_multiple_builds_multiple_calls(self):
        """多次 build 调用多次 list_memories。"""
        stub = StubMemoryStore([])
        builder = MemoryContextBuilder(stub)

        builder.build()
        builder.build()
        builder.build()

        self.assertEqual(stub.list_memories_call_count, 3)


class TestMemoryContextBuilderExceptionPropagation(unittest.TestCase):
    """异常传播测试。"""

    def test_store_exception_propagates(self):
        """Store 异常原样传播。"""
        error = RuntimeError("Store 错误")
        stub = StubMemoryStore(exception=error)
        builder = MemoryContextBuilder(stub)

        with self.assertRaises(RuntimeError) as ctx:
            builder.build()
        self.assertEqual(str(ctx.exception), "Store 错误")


class TestMemoryContextBuilderImmutability(unittest.TestCase):
    """不可变性测试。"""

    def test_store_list_not_modified(self):
        """不修改 Store 返回的列表。"""
        memories = [_memory("m1", kind="fact", content="内容")]
        stub = StubMemoryStore(memories)
        builder = MemoryContextBuilder(stub)

        original_len = len(stub._memories)
        builder.build()
        self.assertEqual(len(stub._memories), original_len)

    def test_memory_objects_not_modified(self):
        """不修改 Memory 对象。"""
        memory = _memory("m1", kind="fact", content="原始内容")
        store = InMemoryMemoryStore()
        store.add(memory)
        builder = MemoryContextBuilder(store)

        builder.build()

        # 再次获取验证内容未变
        saved = store.get("m1")
        self.assertEqual(saved.content, "原始内容")


class TestMemoryContextBuilderCompleteOutput(unittest.TestCase):
    """完整输出示例测试。"""

    def test_complete_output_format(self):
        """验证完整输出格式。"""
        store = InMemoryMemoryStore()
        store.add(_memory("m1", kind="instruction", content="回答问题前先解释架构"))
        store.add(_memory("m1a", kind="preference", content="用户喜欢使用 uv 管理 Python 项目"))
        store.add(_memory("m1b", kind="fact", content="用户正在学习 DeerFlow"))
        builder = MemoryContextBuilder(store)
        result = builder.build()

        expected = (
            "长期记忆\n\n"
            "长期指令\n"
            "- 回答问题前先解释架构\n\n"
            "用户偏好\n"
            "- 用户喜欢使用 uv 管理 Python 项目\n\n"
            "相关事实\n"
            "- 用户正在学习 DeerFlow"
        )
        self.assertEqual(result, expected)


if __name__ == "__main__":
    unittest.main()
