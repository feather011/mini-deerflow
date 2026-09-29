"""memory.py 单元测试。"""

import copy
import unittest
from dataclasses import FrozenInstanceError

from mini_deerflow.memory import ALLOWED_KINDS, Memory


def _valid_data() -> dict[str, object]:
    return {
        "memory_id": "memory-001",
        "kind": "preference",
        "content": "用户喜欢使用 uv 管理 Python 项目",
        "source_thread_id": "python-learning",
    }


class TestMemory(unittest.TestCase):
    """Memory 数据模型的创建、校验与序列化。"""

    def test_create_valid(self):
        """正常创建"""
        m = Memory(
            memory_id="memory-001",
            kind="fact",
            content="用户在北京",
            source_thread_id="t1",
        )
        self.assertEqual(m.memory_id, "memory-001")
        self.assertEqual(m.kind, "fact")

    def test_frozen_immutable(self):
        """frozen 不可变"""
        m = Memory(
            memory_id="m", kind="fact", content="c", source_thread_id="t"
        )
        with self.assertRaises(FrozenInstanceError):
            m.content = "changed"  # type: ignore[misc]

    def test_allowed_kinds(self):
        """kind 只允许三种"""
        self.assertEqual(ALLOWED_KINDS, {"fact", "preference", "instruction"})

    def test_to_dict(self):
        """to_dict 输出完整四字段"""
        m = Memory.from_dict(_valid_data())
        self.assertEqual(m.to_dict(), _valid_data())

    def test_from_dict(self):
        """from_dict 恢复正确字段"""
        m = Memory.from_dict(_valid_data())
        self.assertEqual(m.memory_id, "memory-001")
        self.assertEqual(m.kind, "preference")
        self.assertIn("uv", m.content)
        self.assertEqual(m.source_thread_id, "python-learning")

    def test_roundtrip(self):
        """序列化往返保持相等"""
        original = Memory.from_dict(_valid_data())
        restored = Memory.from_dict(original.to_dict())
        self.assertEqual(restored, original)

    def test_chinese_content(self):
        """中文内容正常处理"""
        m = Memory.from_dict(_valid_data())
        self.assertEqual(m.content, "用户喜欢使用 uv 管理 Python 项目")

    def test_missing_field(self):
        """缺少字段被拒绝"""
        for field in ("memory_id", "kind", "content", "source_thread_id"):
            data = _valid_data()
            del data[field]
            with self.assertRaises(ValueError) as ctx:
                Memory.from_dict(data)
            self.assertIn(field, str(ctx.exception))

    def test_wrong_type_field(self):
        """错误字段类型被拒绝"""
        data = _valid_data()
        data["memory_id"] = 42
        with self.assertRaises(ValueError):
            Memory.from_dict(data)

    def test_empty_string_field(self):
        """空字符串被拒绝"""
        for field in ("memory_id", "kind", "content", "source_thread_id"):
            data = _valid_data()
            data[field] = ""
            with self.assertRaises(ValueError):
                Memory.from_dict(data)

    def test_unknown_kind(self):
        """未知 kind 被拒绝"""
        data = _valid_data()
        data["kind"] = "secret"
        with self.assertRaises(ValueError) as ctx:
            Memory.from_dict(data)
        self.assertIn("kind", str(ctx.exception))

    def test_non_dict_input(self):
        """非字典输入被拒绝"""
        for bad in ("str", 42, None, []):
            with self.assertRaises(ValueError):
                Memory.from_dict(bad)  # type: ignore[arg-type]

    def test_input_dict_not_modified(self):
        """from_dict 不得修改输入字典"""
        data = _valid_data()
        snapshot = copy.deepcopy(data)
        Memory.from_dict(data)
        self.assertEqual(data, snapshot)


if __name__ == "__main__":
    unittest.main()
