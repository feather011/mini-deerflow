"""Message 和 ThreadState 序列化测试。"""

import copy
import unittest

from mini_deerflow.models import Message
from mini_deerflow.thread_state import SCHEMA_VERSION, ThreadState


class TestMessageSerialization(unittest.TestCase):
    """Message.to_dict / Message.from_dict。"""

    def test_to_dict_shape(self):
        m = Message(role="user", content="你好")
        self.assertEqual(m.to_dict(), {"role": "user", "content": "你好"})

    def test_roundtrip(self):
        m = Message(role="assistant", content="ok")
        self.assertEqual(Message.from_dict(m.to_dict()), m)

    def test_from_dict_does_not_mutate_input(self):
        data = {"role": "user", "content": "hi"}
        snapshot = copy.deepcopy(data)
        Message.from_dict(data)
        self.assertEqual(data, snapshot)

    def test_non_dict_rejected(self):
        for bad in ("not a dict", 42, None, []):
            with self.assertRaises(ValueError):
                Message.from_dict(bad)

    def test_missing_role_rejected(self):
        with self.assertRaises(ValueError):
            Message.from_dict({"content": "x"})

    def test_missing_content_rejected(self):
        with self.assertRaises(ValueError):
            Message.from_dict({"role": "user"})

    def test_role_not_string_rejected(self):
        with self.assertRaises(ValueError):
            Message.from_dict({"role": 42, "content": "x"})

    def test_content_not_string_rejected(self):
        with self.assertRaises(ValueError):
            Message.from_dict({"role": "user", "content": [1, 2]})

    def test_unknown_role_rejected(self):
        for bad in ("admin", "god", ""):
            with self.assertRaises(ValueError):
                Message.from_dict({"role": bad, "content": "x"})


class TestThreadStateSerialization(unittest.TestCase):
    """ThreadState.to_dict / ThreadState.from_dict。"""

    def _make_thread(self) -> ThreadState:
        t = ThreadState(thread_id="default")
        t.extend([
            Message(role="user", content="计算 1 + 2"),
            Message(role="tool", content="3"),
            Message(role="assistant", content="计算结果: 3"),
        ])
        return t

    def test_to_dict_shape(self):
        d = self._make_thread().to_dict()
        self.assertEqual(d["schema_version"], 2)
        self.assertEqual(d["thread_id"], "default")
        self.assertIsInstance(d["messages"], list)
        self.assertEqual(len(d["messages"]), 3)

    def test_roundtrip(self):
        original = self._make_thread()
        restored = ThreadState.from_dict(original.to_dict())
        self.assertEqual(restored.thread_id, original.thread_id)
        self.assertEqual(restored.snapshot(), original.snapshot())

    def test_roundtrip_empty_messages(self):
        original = ThreadState(thread_id="empty")
        restored = ThreadState.from_dict(original.to_dict())
        self.assertEqual(restored.thread_id, "empty")
        self.assertEqual(restored.message_count, 0)

    def test_message_order_preserved(self):
        original = self._make_thread()
        restored = ThreadState.from_dict(original.to_dict())
        contents = [m.content for m in restored.snapshot()]
        self.assertEqual(
            contents,
            ["计算 1 + 2", "3", "计算结果: 3"],
        )

    def test_to_dict_does_not_expose_internal_list(self):
        """to_dict 应返回新的 messages 列表，不暴露内部列表"""
        thread = self._make_thread()
        d = thread.to_dict()
        self.assertIsNot(d["messages"], thread.messages)
        # 修改 d["messages"] 不影响 thread
        d["messages"].append({"role": "user", "content": "x"})
        self.assertEqual(thread.message_count, 3)

    def test_from_dict_does_not_mutate_input(self):
        original = self._make_thread()
        data = original.to_dict()
        snapshot = copy.deepcopy(data)
        ThreadState.from_dict(data)
        self.assertEqual(data, snapshot)

    def test_missing_schema_version_rejected(self):
        with self.assertRaises(ValueError):
            ThreadState.from_dict({"thread_id": "t", "messages": []})

    def test_unknown_schema_version_rejected(self):
        with self.assertRaises(ValueError):
            ThreadState.from_dict({
                "schema_version": 999, "thread_id": "t", "messages": []
            })

    def test_bool_schema_version_rejected(self):
        with self.assertRaises(ValueError):
            ThreadState.from_dict({
                "schema_version": True, "thread_id": "t", "messages": []
            })

    def test_missing_thread_id_rejected(self):
        with self.assertRaises(ValueError):
            ThreadState.from_dict({"schema_version": 1, "messages": []})

    def test_empty_thread_id_rejected(self):
        with self.assertRaises(ValueError):
            ThreadState.from_dict({"schema_version": 1, "thread_id": "", "messages": []})

    def test_messages_not_list_rejected(self):
        with self.assertRaises(ValueError):
            ThreadState.from_dict({
                "schema_version": 1, "thread_id": "t", "messages": "oops"
            })

    def test_invalid_message_in_messages_rejected(self):
        """messages 中存在非法 message 应抛 ValueError"""
        with self.assertRaises(ValueError):
            ThreadState.from_dict({
                "schema_version": 1, "thread_id": "t",
                "messages": [
                    {"role": "user", "content": "ok"},
                    {"role": "admin", "content": "bad role"},  # 未知 role
                ],
            })

    def test_non_dict_rejected(self):
        for bad in ("str", 42, None, []):
            with self.assertRaises(ValueError):
                ThreadState.from_dict(bad)


class TestSchemaVersionConstant(unittest.TestCase):
    """SCHEMA_VERSION 常量。"""

    def test_schema_version_is_two(self):
        self.assertEqual(SCHEMA_VERSION, 2)


if __name__ == "__main__":
    unittest.main()