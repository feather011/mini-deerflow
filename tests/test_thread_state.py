"""thread_state.py 单元测试。"""

import unittest

from mini_deerflow.models import Message
from mini_deerflow.thread_state import ThreadState


def _msg(content: str) -> Message:
    return Message(role="user", content=content)


class TestThreadState(unittest.TestCase):
    """测试 ThreadState 的创建、写入、读取与验证。"""

    def test_create_empty(self):
        """创建空 ThreadState"""
        thread = ThreadState(thread_id="t1")
        self.assertEqual(thread.thread_id, "t1")
        self.assertEqual(thread.messages, [])
        self.assertEqual(thread.message_count, 0)

    def test_two_threads_do_not_share_messages(self):
        """两个 Thread 的 messages 列表互不共享"""
        t1 = ThreadState(thread_id="t1")
        t2 = ThreadState(thread_id="t2")
        t1.append(_msg("hello"))
        self.assertEqual(t1.message_count, 1)
        self.assertEqual(t2.message_count, 0)
        self.assertIsNot(t1.messages, t2.messages)

    def test_append_count_and_last(self):
        """append 后数量和 last_message 正确"""
        thread = ThreadState(thread_id="t1")
        thread.append(_msg("first"))
        thread.append(_msg("second"))
        self.assertEqual(thread.message_count, 2)
        self.assertEqual(thread.last_message.content, "second")

    def test_extend_preserves_order(self):
        """extend 保持消息原有顺序"""
        thread = ThreadState(thread_id="t1")
        thread.extend([_msg("a"), _msg("b"), _msg("c")])
        contents = [m.content for m in thread.messages]
        self.assertEqual(contents, ["a", "b", "c"])

    def test_snapshot_is_tuple(self):
        """snapshot 返回 tuple"""
        thread = ThreadState(thread_id="t1")
        thread.append(_msg("x"))
        snap = thread.snapshot()
        self.assertIsInstance(snap, tuple)
        self.assertEqual(len(snap), 1)

    def test_external_list_change_does_not_affect_snapshot(self):
        """修改外部列表不会修改 snapshot"""
        thread = ThreadState(thread_id="t1")
        thread.append(_msg("original"))
        snap = thread.snapshot()
        external = list(snap)
        external.append(_msg("mutated"))
        self.assertEqual(len(snap), 1)
        self.assertEqual(snap[0].content, "original")

    def test_last_message_none_when_empty(self):
        """空 Thread 的 last_message 是 None"""
        thread = ThreadState(thread_id="t1")
        self.assertIsNone(thread.last_message)

    def test_blank_thread_id_rejected(self):
        """空白 thread_id 被拒绝"""
        for bad in ("", "   ", "\t\n"):
            with self.assertRaises(ValueError):
                ThreadState(thread_id=bad)

    def test_non_string_thread_id_rejected(self):
        """非字符串 thread_id 被拒绝"""
        for bad in (123, None, ["t1"]):
            with self.assertRaises(ValueError):
                ThreadState(thread_id=bad)

    def test_append_non_message_rejected(self):
        """append 非 Message 被拒绝"""
        thread = ThreadState(thread_id="t1")
        with self.assertRaises(TypeError):
            thread.append("not a message")
        self.assertEqual(thread.message_count, 0)

    def test_extend_invalid_element_no_partial_write(self):
        """extend 包含非法元素时不产生部分写入"""
        thread = ThreadState(thread_id="t1")
        with self.assertRaises(TypeError):
            thread.extend([_msg("a"), "bad", _msg("c")])
        self.assertEqual(thread.message_count, 0)
        self.assertEqual(thread.messages, [])


if __name__ == "__main__":
    unittest.main()
