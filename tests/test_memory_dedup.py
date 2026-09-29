"""记忆去重模块测试（14E1 阶段）。"""

from __future__ import annotations

import copy
import unittest

from mini_deerflow.memory import Memory
from mini_deerflow.memory_dedup import (
    ExactMemoryDeduplicator,
    MemoryDeduplicator,
    memory_dedup_key,
    normalize_memory_content,
)


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


class TestNormalizeMemoryContent(unittest.TestCase):
    """normalize_memory_content 函数测试。"""

    def test_trim_leading_trailing_whitespace(self):
        """去除首尾空格。"""
        result = normalize_memory_content("  内容  ")
        self.assertEqual(result, "内容")

    def test_compress_multiple_spaces(self):
        """压缩多个空格。"""
        result = normalize_memory_content("用户  喜欢  uv")
        self.assertEqual(result, "用户 喜欢 uv")

    def test_compress_tabs(self):
        """压缩 Tab。"""
        result = normalize_memory_content("用户\t喜欢\tuv")
        self.assertEqual(result, "用户 喜欢 uv")

    def test_compress_newlines(self):
        """压缩换行。"""
        result = normalize_memory_content("用户\n喜欢\nuv")
        self.assertEqual(result, "用户 喜欢 uv")

    def test_english_casefold(self):
        """英文大小写 casefold。"""
        result = normalize_memory_content("USER PREFERENCES")
        self.assertEqual(result, "user preferences")

    def test_chinese_content_preserved(self):
        """中文内容保持正常。"""
        result = normalize_memory_content("用户喜欢使用 uv")
        self.assertEqual(result, "用户喜欢使用 uv")

    def test_punctuation_preserved(self):
        """标点不被删除。"""
        result = normalize_memory_content("用户喜欢: uv, python!")
        self.assertEqual(result, "用户喜欢: uv, python!")

    def test_original_string_not_modified(self):
        """原始字符串不被修改。"""
        original = "  用户  "
        normalize_memory_content(original)
        self.assertEqual(original, "  用户  ")


class TestMemoryDedupKey(unittest.TestCase):
    """memory_dedup_key 函数测试。"""

    def test_same_kind_and_content_same_key(self):
        """相同 kind 和标准化 content 产生相同 key。"""
        key1 = memory_dedup_key(_memory("m1", kind="fact", content="用户在北京"))
        key2 = memory_dedup_key(_memory("m2", kind="fact", content="用户在北京"))
        self.assertEqual(key1, key2)

    def test_different_kind_different_key(self):
        """不同 kind 产生不同 key。"""
        key1 = memory_dedup_key(_memory("m1", kind="fact", content="用户喜欢 uv"))
        key2 = memory_dedup_key(_memory("m2", kind="preference", content="用户喜欢 uv"))
        self.assertNotEqual(key1, key2)

    def test_source_thread_id_not_affect_key(self):
        """source_thread_id 不影响 key。"""
        key1 = memory_dedup_key(_memory("m1", source_thread_id="thread-1", content="内容"))
        key2 = memory_dedup_key(_memory("m2", source_thread_id="thread-2", content="内容"))
        self.assertEqual(key1, key2)

    def test_memory_id_not_affect_key(self):
        """memory_id 不影响 key。"""
        key1 = memory_dedup_key(_memory("m1", content="内容"))
        key2 = memory_dedup_key(_memory("m2", content="内容"))
        self.assertEqual(key1, key2)


class TestExactMemoryDeduplicator(unittest.TestCase):
    """ExactMemoryDeduplicator 测试。"""

    def test_implements_protocol(self):
        """实现了 MemoryDeduplicator Protocol。"""
        dedup: MemoryDeduplicator = ExactMemoryDeduplicator()
        self.assertTrue(hasattr(dedup, "filter_new"))

    def test_no_duplicates_returns_all(self):
        """无重复时全部返回。"""
        candidates = [
            _memory("m1", content="内容一"),
            _memory("m2", content="内容二"),
        ]
        existing = []
        dedup = ExactMemoryDeduplicator()
        result = dedup.filter_new(candidates, existing)
        self.assertEqual(len(result), 2)

    def test_duplicate_keeps_first(self):
        """重复时保留第一条。"""
        candidates = [
            _memory("m1", kind="fact", content="用户在北京"),
            _memory("m2", kind="fact", content="用户在北京"),
        ]
        existing = []
        dedup = ExactMemoryDeduplicator()
        result = dedup.filter_new(candidates, existing)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].memory_id, "m1")

    def test_multiple_duplicates_keeps_first_of_each(self):
        """多个重复只保留第一条。"""
        candidates = [
            _memory("m1", content="A"),
            _memory("m2", content="B"),
            _memory("m3", content="A"),
            _memory("m4", content="B"),
            _memory("m5", content="C"),
        ]
        existing = []
        dedup = ExactMemoryDeduplicator()
        result = dedup.filter_new(candidates, existing)
        self.assertEqual(len(result), 3)
        self.assertEqual([m.memory_id for m in result], ["m1", "m2", "m5"])

    def test_preserves_original_order(self):
        """保持非重复记忆原始顺序。"""
        candidates = [
            _memory("m1", content="第一"),
            _memory("m3", content="第三"),
            _memory("m2", content="第二"),
        ]
        existing = []
        dedup = ExactMemoryDeduplicator()
        result = dedup.filter_new(candidates, existing)
        self.assertEqual([m.content for m in result], ["第一", "第三", "第二"])

    def test_candidates_not_modified(self):
        """不修改候选列表。"""
        candidates = [_memory("m1")]
        existing = []
        dedup = ExactMemoryDeduplicator()
        original = copy.deepcopy(candidates)
        dedup.filter_new(candidates, existing)
        self.assertEqual(candidates, original)

    def test_non_memory_in_candidates_raises_type_error(self):
        """candidates 中有非 Memory 对象时抛出 TypeError。"""
        candidates = [_memory("m1"), "not a memory"]  # type: ignore
        existing = []
        dedup = ExactMemoryDeduplicator()
        with self.assertRaises(TypeError) as ctx:
            dedup.filter_new(candidates, existing)
        self.assertIn("candidates[1]", str(ctx.exception))

    def test_non_memory_in_existing_raises_type_error(self):
        """existing 中有非 Memory 对象时抛出 TypeError。"""
        candidates = [_memory("m1")]
        existing = [_memory("m2"), "not a memory"]  # type: ignore
        dedup = ExactMemoryDeduplicator()
        with self.assertRaises(TypeError) as ctx:
            dedup.filter_new(candidates, existing)
        self.assertIn("existing[1]", str(ctx.exception))


class TestExactMemoryDeduplicatorWithExisting(unittest.TestCase):
    """与已有 Store 内容去重测试。"""

    def test_skip_existing_content(self):
        """Store 已有相同内容时跳过。"""
        candidates = [_memory("m2", kind="fact", content="用户在北京")]
        existing = [_memory("m1", kind="fact", content="用户在北京")]
        dedup = ExactMemoryDeduplicator()
        result = dedup.filter_new(candidates, existing)
        self.assertEqual(len(result), 0)

    def test_different_thread_same_content_skipped(self):
        """不同 source_thread_id 仍视为重复。"""
        candidates = [_memory("m2", source_thread_id="thread-b", content="内容")]
        existing = [_memory("m1", source_thread_id="thread-a", content="内容")]
        dedup = ExactMemoryDeduplicator()
        result = dedup.filter_new(candidates, existing)
        self.assertEqual(len(result), 0)

    def test_different_kind_not_skipped(self):
        """不同 kind 不视为重复。"""
        candidates = [_memory("m2", kind="fact", content="内容")]
        existing = [_memory("m1", kind="preference", content="内容")]
        dedup = ExactMemoryDeduplicator()
        result = dedup.filter_new(candidates, existing)
        self.assertEqual(len(result), 1)

    def test_different_content_not_skipped(self):
        """不同 content 不视为重复。"""
        candidates = [_memory("m2", content="内容A")]
        existing = [_memory("m1", content="内容B")]
        dedup = ExactMemoryDeduplicator()
        result = dedup.filter_new(candidates, existing)
        self.assertEqual(len(result), 1)

    def test_existing_not_modified(self):
        """不修改 existing 列表。"""
        candidates = [_memory("m2")]
        existing = [_memory("m1")]
        dedup = ExactMemoryDeduplicator()
        original = copy.deepcopy(existing)
        dedup.filter_new(candidates, existing)
        self.assertEqual(existing, original)


class TestNormalizeContentDedup(unittest.TestCase):
    """标准化内容去重测试。"""

    def test_whitespace_variation_deduped(self):
        """空白变体视为重复。"""
        # 注意：user likes uv 和 user likes uv（有额外空格）是重复的
        # 但 user likesuv 和 user likes uv（一个没空格）不是重复
        candidates = [_memory("m2", kind="fact", content="  user likes uv  ")]
        existing = [_memory("m1", kind="fact", content="user likes uv")]
        dedup = ExactMemoryDeduplicator()
        result = dedup.filter_new(candidates, existing)
        self.assertEqual(len(result), 0)

    def test_case_variation_deduped(self):
        """大小写变体视为重复。"""
        candidates = [_memory("m2", kind="fact", content="USER LIKE UV")]
        existing = [_memory("m1", kind="fact", content="user like uv")]
        dedup = ExactMemoryDeduplicator()
        result = dedup.filter_new(candidates, existing)
        self.assertEqual(len(result), 0)

    def test_mixed_variation_deduped(self):
        """混合变体视为重复。"""
        candidates = [_memory("m2", kind="fact", content="  USER  Like  uv  ")]
        existing = [_memory("m1", kind="fact", content="user  LIKE  UV")]
        dedup = ExactMemoryDeduplicator()
        result = dedup.filter_new(candidates, existing)
        self.assertEqual(len(result), 0)


if __name__ == "__main__":
    unittest.main()
