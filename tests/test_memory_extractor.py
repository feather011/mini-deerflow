"""MemoryExtractor 接口与 RuleBasedMemoryExtractor 实现测试。"""

from __future__ import annotations

import copy
import unittest
from typing import Iterator

from mini_deerflow.memory import Memory
from mini_deerflow.memory_extractor import MemoryExtractor, RuleBasedMemoryExtractor
from mini_deerflow.models import Message


def _msg(role: str, content: str) -> Message:
    return Message(role=role, content=content)


class TestMemoryExtractorInterface(unittest.TestCase):
    """验证 RuleBasedMemoryExtractor 实现了 MemoryExtractor Protocol。"""

    def test_implements_protocol(self):
        """RuleBasedMemoryExtractor 必须实现 MemoryExtractor 协议。"""
        extractor: MemoryExtractor = RuleBasedMemoryExtractor(lambda: "id")
        self.assertTrue(hasattr(extractor, "extract"))


class TestRuleBasedMemoryExtractorExtract(unittest.TestCase):
    """extract 方法基本功能测试。"""

    def test_extract_fact(self):
        """提取 fact 类型记忆。"""
        extractor = RuleBasedMemoryExtractor(lambda: "mem-1")
        messages = [_msg("user", "记住事实：用户住在上海")]
        result = extractor.extract("thread-1", messages)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].kind, "fact")
        self.assertEqual(result[0].content, "用户住在上海")

    def test_extract_preference(self):
        """提取 preference 类型记忆。"""
        extractor = RuleBasedMemoryExtractor(lambda: "mem-1")
        messages = [_msg("user", "记住偏好：用户喜欢使用 uv 管理项目")]
        result = extractor.extract("thread-1", messages)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].kind, "preference")
        self.assertEqual(result[0].content, "用户喜欢使用 uv 管理项目")

    def test_extract_instruction(self):
        """提取 instruction 类型记忆。"""
        extractor = RuleBasedMemoryExtractor(lambda: "mem-1")
        messages = [_msg("user", "记住指令：每次回复前先说您好")]
        result = extractor.extract("thread-1", messages)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].kind, "instruction")
        self.assertEqual(result[0].content, "每次回复前先说您好")


class TestRuleBasedMemoryExtractorColon(unittest.TestCase):
    """中英文冒号支持测试。"""

    def test_chinese_fullwidth_colon(self):
        """中文全角冒号（：）正常工作。"""
        extractor = RuleBasedMemoryExtractor(lambda: "mem-1")
        messages = [_msg("user", "记住事实：用户来自北京")]
        result = extractor.extract("thread-1", messages)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].content, "用户来自北京")

    def test_english_halfwidth_colon(self):
        """英文半角冒号（:）正常工作。"""
        extractor = RuleBasedMemoryExtractor(lambda: "mem-1")
        messages = [_msg("user", "记住偏好:用户偏好深色主题")]
        result = extractor.extract("thread-1", messages)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].content, "用户偏好深色主题")


class TestRuleBasedMemoryExtractorWhitespace(unittest.TestCase):
    """空白字符处理测试。"""

    def test_content_whitespace_trimmed(self):
        """内容首尾空白被清理。"""
        extractor = RuleBasedMemoryExtractor(lambda: "mem-1")
        messages = [_msg("user", "记住事实：   用户住在深圳   ")]
        result = extractor.extract("thread-1", messages)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].content, "用户住在深圳")

    def test_prefix_with_leading_whitespace(self):
        """前缀前有空白时，strip 后能匹配。"""
        extractor = RuleBasedMemoryExtractor(lambda: "mem-1")
        messages = [_msg("user", "  记住事实：用户在北京")]
        result = extractor.extract("thread-1", messages)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].content, "用户在北京")

    def test_both_leading_and_trailing_whitespace(self):
        """前后都有空白时，正常提取并清理。"""
        extractor = RuleBasedMemoryExtractor(lambda: "mem-1")
        messages = [_msg("user", "   记住偏好：用户喜欢 uv   ")]
        result = extractor.extract("thread-1", messages)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].kind, "preference")
        self.assertEqual(result[0].content, "用户喜欢 uv")


class TestRuleBasedMemoryExtractorIgnoredMessages(unittest.TestCase):
    """消息过滤测试。"""

    def test_assistant_message_ignored(self):
        """assistant 消息被忽略。"""
        extractor = RuleBasedMemoryExtractor(lambda: "mem-1")
        messages = [_msg("assistant", "记住事实：用户住在上海")]
        result = extractor.extract("thread-1", messages)
        self.assertEqual(len(result), 0)

    def test_tool_message_ignored(self):
        """tool 消息被忽略。"""
        extractor = RuleBasedMemoryExtractor(lambda: "mem-1")
        messages = [_msg("tool", "记住偏好：用户喜欢深色主题")]
        result = extractor.extract("thread-1", messages)
        self.assertEqual(len(result), 0)

    def test_normal_user_message_ignored(self):
        """没有前缀的普通用户消息被忽略。"""
        extractor = RuleBasedMemoryExtractor(lambda: "mem-1")
        messages = [_msg("user", "你好，请帮我计算 1+1")]
        result = extractor.extract("thread-1", messages)
        self.assertEqual(len(result), 0)

    def test_empty_content_ignored(self):
        """前缀后内容为空时被忽略。"""
        extractor = RuleBasedMemoryExtractor(lambda: "mem-1")
        messages = [_msg("user", "记住事实：")]
        result = extractor.extract("thread-1", messages)
        self.assertEqual(len(result), 0)

    def test_empty_content_with_whitespace_ignored(self):
        """前缀后只有空白时被忽略。"""
        extractor = RuleBasedMemoryExtractor(lambda: "mem-1")
        messages = [_msg("user", "记住偏好：   ")]
        result = extractor.extract("thread-1", messages)
        self.assertEqual(len(result), 0)


class TestRuleBasedMemoryExtractorMultiple(unittest.TestCase):
    """多条记忆测试。"""

    def test_multiple_memories_preserves_order(self):
        """多条记忆保持原始顺序。"""
        id_counter = iter(["m1", "m2", "m3"])
        extractor = RuleBasedMemoryExtractor(lambda: next(id_counter))
        messages = [
            _msg("user", "记住事实：用户住在深圳"),
            _msg("user", "记住偏好：用户喜欢喝咖啡"),
            _msg("user", "记住指令：每次回复结尾加署名"),
        ]
        result = extractor.extract("thread-1", messages)
        self.assertEqual(len(result), 3)
        self.assertEqual([m.kind for m in result], ["fact", "preference", "instruction"])
        self.assertEqual(
            [m.content for m in result],
            ["用户住在深圳", "用户喜欢喝咖啡", "每次回复结尾加署名"],
        )

    def test_id_factory_called_per_memory(self):
        """每条记忆调用一次 id_factory。"""
        call_count = 0

        def counter() -> str:
            nonlocal call_count
            call_count += 1
            return f"id-{call_count}"

        extractor = RuleBasedMemoryExtractor(counter)
        messages = [
            _msg("user", "记住事实：第一条"),
            _msg("user", "记住事实：第二条"),
            _msg("user", "记住事实：第三条"),
        ]
        result = extractor.extract("thread-1", messages)
        self.assertEqual(call_count, 3)
        self.assertEqual([m.memory_id for m in result], ["id-1", "id-2", "id-3"])

    def test_source_thread_id_correct(self):
        """source_thread_id 正确传递。"""
        extractor = RuleBasedMemoryExtractor(lambda: "mem-1")
        messages = [_msg("user", "记住事实：用户住在杭州")]
        result = extractor.extract("thread-xyz", messages)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].source_thread_id, "thread-xyz")


class TestRuleBasedMemoryExtractorValidation(unittest.TestCase):
    """参数校验测试。"""

    def test_empty_source_thread_id(self):
        """空字符串 source_thread_id 抛出 ValueError。"""
        extractor = RuleBasedMemoryExtractor(lambda: "mem-1")
        with self.assertRaises(ValueError) as ctx:
            extractor.extract("", [_msg("user", "记住事实：内容")])
        self.assertIn("source_thread_id", str(ctx.exception))

    def test_whitespace_only_source_thread_id(self):
        """只有空白字符的 source_thread_id 抛出 ValueError。"""
        extractor = RuleBasedMemoryExtractor(lambda: "mem-1")
        with self.assertRaises(ValueError):
            extractor.extract("   ", [_msg("user", "记住事实：内容")])

    def test_non_message_in_messages(self):
        """messages 中存在非 Message 元素时抛出 TypeError。"""
        extractor = RuleBasedMemoryExtractor(lambda: "mem-1")
        with self.assertRaises(TypeError) as ctx:
            extractor.extract("thread-1", [_msg("user", "内容"), "不是 Message"])
        self.assertIn("messages[1]", str(ctx.exception))

    def test_id_factory_returns_empty_string(self):
        """id_factory 返回空字符串时抛出 ValueError。"""
        extractor = RuleBasedMemoryExtractor(lambda: "")
        with self.assertRaises(ValueError) as ctx:
            extractor.extract("thread-1", [_msg("user", "记住事实：内容")])
        self.assertIn("id_factory", str(ctx.exception))

    def test_id_factory_returns_whitespace_only(self):
        """id_factory 返回只有空白字符时抛出 ValueError。"""
        extractor = RuleBasedMemoryExtractor(lambda: "   ")
        with self.assertRaises(ValueError):
            extractor.extract("thread-1", [_msg("user", "记住事实：内容")])


class TestRuleBasedMemoryExtractorEdgeCases(unittest.TestCase):
    """边界情况测试。"""

    def test_empty_message_list(self):
        """空消息列表返回空列表。"""
        extractor = RuleBasedMemoryExtractor(lambda: "mem-1")
        result = extractor.extract("thread-1", [])
        self.assertEqual(result, [])

    def test_input_messages_not_modified(self):
        """不修改输入消息列表。"""
        extractor = RuleBasedMemoryExtractor(lambda: "mem-1")
        messages = [_msg("user", "记住事实：用户住在广州")]
        original_content = copy.deepcopy(messages[0].content)
        extractor.extract("thread-1", messages)
        self.assertEqual(messages[0].content, original_content)

    def test_generator_input(self):
        """支持生成器作为 messages 输入。"""
        def msg_generator() -> Iterator[Message]:
            yield _msg("user", "记住事实：第一条")
            yield _msg("user", "记住偏好：第二条")

        extractor = RuleBasedMemoryExtractor(lambda: "id")
        result = extractor.extract("thread-1", msg_generator())
        self.assertEqual(len(result), 2)

    def test_mixed_valid_and_invalid_messages(self):
        """混合有效和无效消息时，正确处理有效的。"""
        id_counter = iter(["m1", "m2"])
        extractor = RuleBasedMemoryExtractor(lambda: next(id_counter))
        messages = [
            _msg("user", "记住事实：有效内容一"),
            _msg("assistant", "记住偏好：无效消息"),
            _msg("user", "记住偏好：有效内容二"),
        ]
        result = extractor.extract("thread-1", messages)
        self.assertEqual(len(result), 2)
        self.assertEqual([m.content for m in result], ["有效内容一", "有效内容二"])


if __name__ == "__main__":
    unittest.main()
