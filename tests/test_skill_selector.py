"""SkillSelector 测试。"""

from __future__ import annotations

import unittest
from pathlib import Path

from mini_deerflow.skill_metadata import SkillMetadata
from mini_deerflow.skill_selector import (
    RuleBasedSkillSelector,
    SkillSelector,
)


def _meta(
    name: str = "test-skill",
    description: str = "测试 Skill",
    source_path: str | Path = "/test/SKILL.md",
) -> SkillMetadata:
    return SkillMetadata(
        name=name,
        description=description,
        source_path=Path(source_path),
    )


class TestRuleBasedSkillSelectorInit(unittest.TestCase):
    """初始化测试。"""

    def test_normal_init(self):
        """正常初始化。"""
        selector = RuleBasedSkillSelector(
            {"python-testing": ("测试", "pytest")},
            max_skills=5,
        )
        self.assertEqual(selector._max_skills, 5)

    def test_empty_rules(self):
        """空 rules。"""
        selector = RuleBasedSkillSelector({})
        self.assertEqual(selector._rules, {})

    def test_default_max_skills(self):
        """默认 max_skills。"""
        selector = RuleBasedSkillSelector({})
        self.assertEqual(selector._max_skills, 3)

    def test_rules_key_not_string_raises(self):
        """rules key 不是字符串。"""
        with self.assertRaises(TypeError):
            RuleBasedSkillSelector({123: ("test",)})

    def test_rules_key_empty_raises(self):
        """rules key 为空字符串。"""
        with self.assertRaises(TypeError):
            RuleBasedSkillSelector({"": ("test",)})

    def test_rules_key_whitespace_raises(self):
        """rules key 为空白。"""
        with self.assertRaises(TypeError):
            RuleBasedSkillSelector({"   ": ("test",)})

    def test_rules_value_string_raises(self):
        """rules value 是字符串而非 Sequence。"""
        with self.assertRaises(TypeError):
            RuleBasedSkillSelector({"test": "pytest"})

    def test_rules_value_not_sequence_raises(self):
        """rules value 不是 Sequence。"""
        with self.assertRaises(TypeError):
            RuleBasedSkillSelector({"test": 123})

    def test_rules_trigger_not_string_raises(self):
        """触发词不是字符串。"""
        with self.assertRaises(TypeError):
            RuleBasedSkillSelector({"test": (123,)})

    def test_rules_trigger_empty_raises(self):
        """触发词为空字符串。"""
        with self.assertRaises(ValueError):
            RuleBasedSkillSelector({"test": ("",)})

    def test_rules_trigger_whitespace_raises(self):
        """触发词为空白。"""
        with self.assertRaises(ValueError):
            RuleBasedSkillSelector({"test": ("   ",)})

    def test_rules_trigger_dedup(self):
        """重复触发词去重。"""
        selector = RuleBasedSkillSelector(
            {"test": ("pytest", "PYTEST", "  PyTest  ")}
        )
        # 应该只有一个（标准化后相同：pytest）
        self.assertEqual(len(selector._rules["test"]), 1)

    def test_rules_immutable_copy(self):
        """rules 不可变副本。"""
        original = {"test": ("pytest",)}
        selector = RuleBasedSkillSelector(original)
        original["test"] = ("changed",)
        self.assertEqual(len(selector._rules["test"]), 1)

    def test_max_skills_bool_raises(self):
        """max_skills 是 bool。"""
        with self.assertRaises(TypeError):
            RuleBasedSkillSelector({}, max_skills=True)

    def test_max_skills_float_raises(self):
        """max_skills 是 float。"""
        with self.assertRaises(TypeError):
            RuleBasedSkillSelector({}, max_skills=3.0)

    def test_max_skills_string_raises(self):
        """max_skills 是字符串。"""
        with self.assertRaises(TypeError):
            RuleBasedSkillSelector({}, max_skills="3")

    def test_max_skills_zero_raises(self):
        """max_skills 为零。"""
        with self.assertRaises(ValueError):
            RuleBasedSkillSelector({}, max_skills=0)

    def test_max_skills_negative_raises(self):
        """max_skills 为负数。"""
        with self.assertRaises(ValueError):
            RuleBasedSkillSelector({}, max_skills=-1)


class TestRuleBasedSkillSelectorSelect(unittest.TestCase):
    """select 方法测试。"""

    def setUp(self):
        self.skills = [
            _meta("python-testing", "Python 测试流程", "/python/SKILL.md"),
            _meta("code-review", "代码审查", "/review/SKILL.md"),
            _meta("api-design", "API 设计", "/api/SKILL.md"),
        ]
        self.rules = {
            "python-testing": ("测试", "pytest", "单元测试"),
            "code-review": ("审查", "review"),
            "api-design": ("api", "rest", "接口"),
        }
        self.selector = RuleBasedSkillSelector(self.rules)

    def test_user_input_not_string_raises(self):
        """user_input 不是字符串。"""
        with self.assertRaises(TypeError):
            self.selector.select(123, self.skills)

    def test_skills_is_string_raises(self):
        """skills 是字符串。"""
        with self.assertRaises(TypeError):
            self.selector.select("test", "not a list")

    def test_skills_not_sequence_raises(self):
        """skills 不是 Sequence。"""
        with self.assertRaises(TypeError):
            self.selector.select("test", 123)

    def test_empty_user_input(self):
        """空输入。"""
        result = self.selector.select("", self.skills)
        self.assertEqual(result, [])

    def test_whitespace_user_input(self):
        """空白输入。"""
        result = self.selector.select("   ", self.skills)
        self.assertEqual(result, [])

    def test_no_match(self):
        """没有匹配。"""
        result = self.selector.select("完全不相关的输入", self.skills)
        self.assertEqual(result, [])

    def test_single_keyword_match(self):
        """单个关键词匹配。"""
        result = self.selector.select("运行 pytest", self.skills)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].name, "python-testing")

    def test_multiple_keyword_match(self):
        """多个关键词匹配。"""
        result = self.selector.select("进行代码审查", self.skills)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].name, "code-review")

    def test_chinese_keyword(self):
        """中文关键词。"""
        result = self.selector.select("写单元测试", self.skills)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].name, "python-testing")

    def test_english_keyword(self):
        """英文关键词。"""
        result = self.selector.select("需要写 api", self.skills)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].name, "api-design")

    def test_case_insensitive(self):
        """大小写不敏感。"""
        result = self.selector.select("PYTEST", self.skills)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].name, "python-testing")

    def test_partial_match(self):
        """部分匹配。"""
        result = self.selector.select("使用 pytest", self.skills)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].name, "python-testing")

    def test_nfkc_normalization(self):
        """NFKC 标准化。"""
        result = self.selector.select("使用 pytest", self.skills)
        self.assertEqual(len(result), 1)

    def test_whitespace_normalization(self):
        """空白标准化。"""
        result = self.selector.select("使用   pytest  测试", self.skills)
        self.assertEqual(len(result), 1)

    def test_multiple_skills_match(self):
        """多个 Skill 匹配。"""
        # api-design 匹配 "api"
        result = self.selector.select("rest api", self.skills)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].name, "api-design")

    def test_explicit_selection(self):
        """显式选择。"""
        result = self.selector.select("@python-testing", self.skills)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].name, "python-testing")

    def test_explicit_selection_with_context(self):
        """显式选择带上下文。"""
        result = self.selector.select("@python-testing 需要写测试", self.skills)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].name, "python-testing")

    def test_explicit_selection_nonexistent(self):
        """显式选择不存在的 Skill。"""
        result = self.selector.select("@nonexistent", self.skills)
        self.assertEqual(result, [])

    def test_multiple_explicit_selections(self):
        """多个显式选择。"""
        result = self.selector.select("@python-testing @api-design", self.skills)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].name, "python-testing")
        self.assertEqual(result[1].name, "api-design")

    def test_explicit_selection_dedup(self):
        """显式选择去重。"""
        result = self.selector.select("@python-testing @python-testing", self.skills)
        self.assertEqual(len(result), 1)

    def test_explicit_and_keyword_combined(self):
        """显式和关键词组合。"""
        result = self.selector.select("@python-testing 需要 api", self.skills)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].name, "python-testing")
        self.assertEqual(result[1].name, "api-design")

    def test_max_skills_limit(self):
        """max_skills 限制。"""
        selector = RuleBasedSkillSelector(
            self.rules, max_skills=2
        )
        result = selector.select("@python-testing @api-design @code-review", self.skills)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].name, "python-testing")
        self.assertEqual(result[1].name, "api-design")

    def test_sorted_by_match_count(self):
        """按匹配数量排序。"""
        rules = {
            "skill-a": ("test",),
            "skill-b": ("test", "more", "extra"),
        }
        skills = [
            _meta("skill-a", "A", "/a.md"),
            _meta("skill-b", "B", "/b.md"),
        ]
        selector = RuleBasedSkillSelector(rules)
        result = selector.select("test more extra", skills)
        # skill-b 匹配更多关键词，应该排在前面
        self.assertEqual(result[0].name, "skill-b")

    def test_sorted_by_longest_match(self):
        """按最长匹配排序。"""
        rules = {
            "skill-a": ("ab",),
            "skill-b": ("abcd",),
        }
        skills = [
            _meta("skill-a", "A", "/a.md"),
            _meta("skill-b", "B", "/b.md"),
        ]
        selector = RuleBasedSkillSelector(rules)
        result = selector.select("test abcd test", skills)
        # 都匹配一个，skill-b 的匹配更长，应该排在前面
        self.assertEqual(result[0].name, "skill-b")

    def test_sorted_by_name_tiebreaker(self):
        """按 name 升序作为最终排序。"""
        rules = {
            "zebra": ("test",),
            "alpha": ("test",),
        }
        skills = [
            _meta("zebra", "Z", "/z.md"),
            _meta("alpha", "A", "/a.md"),
        ]
        selector = RuleBasedSkillSelector(rules)
        result = selector.select("test", skills)
        # 匹配数相同，longest 相同，按 name 升序
        self.assertEqual(result[0].name, "alpha")
        self.assertEqual(result[1].name, "zebra")

    def test_returns_new_list(self):
        """返回新 list。"""
        result = self.selector.select("pytest", self.skills)
        self.assertIsNot(result, self.skills)

    def test_does_not_modify_candidates(self):
        """不修改候选列表。"""
        skills_copy = list(self.skills)
        self.selector.select("pytest", self.skills)
        self.assertEqual(self.skills, skills_copy)

    def test_returns_original_metadata(self):
        """返回原始元数据对象。"""
        result = self.selector.select("pytest", self.skills)
        self.assertIs(result[0], self.skills[0])

    def test_duplicate_names_in_candidates_raises(self):
        """候选中存在重复 name。"""
        dup = _meta("python-testing", "重复", "/other.md")
        with self.assertRaises(ValueError):
            self.selector.select("pytest", [dup, self.skills[0]])

    def test_skill_not_in_rules(self):
        """没有 rule 的 Skill 不被匹配。"""
        no_rule_skill = _meta("other-skill", "其他", "/other.md")
        skills = self.skills + [no_rule_skill]
        result = self.selector.select("test", skills)
        self.assertEqual(len(result), 0)

    def test_rule_not_in_candidates(self):
        """rule 存在但候选中不存在。"""
        rules = {
            "python-testing": ("test",),
            "nonexistent": ("test",),  # 不在候选中
        }
        selector = RuleBasedSkillSelector(rules)
        result = selector.select("test", self.skills)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].name, "python-testing")

    def test_empty_candidates(self):
        """空候选列表。"""
        result = self.selector.select("test", [])
        self.assertEqual(result, [])


class TestSkillSelectorProtocol(unittest.TestCase):
    """SkillSelector Protocol 测试。"""

    def test_protocol_implementation(self):
        """实现 Protocol。"""
        selector = RuleBasedSkillSelector({})
        self.assertIsInstance(selector, SkillSelector)

    def test_select_returns_list_of_metadata(self):
        """select 返回 SkillMetadata 列表。"""
        selector = RuleBasedSkillSelector({"test": ("x",)})
        meta = _meta("test", "Test", "/test.md")
        result = selector.select("x", [meta])
        self.assertIsInstance(result, list)
        self.assertTrue(all(isinstance(m, SkillMetadata) for m in result))


class TestSkillSelectorIntegration(unittest.TestCase):
    """集成测试。"""

    def test_full_workflow(self):
        """完整工作流。"""
        from mini_deerflow.skill_catalog import SkillCatalog
        from mini_deerflow.skill_metadata import MarkdownSkillMetadataParser

        catalog = SkillCatalog(MarkdownSkillMetadataParser())
        fixture_dir = Path(__file__).parent / "fixtures" / "skill_registry" / "valid"
        catalog.load_directory(fixture_dir)

        rules = {
            "valid-skill": ("测试", "有效"),
            "another-valid": ("另一个",),
        }
        selector = RuleBasedSkillSelector(rules)

        result = selector.select("运行测试", catalog.list_metadata())

        # 结果仍然是 SkillMetadata，不是完整 Skill
        self.assertTrue(all(isinstance(m, SkillMetadata) for m in result))
        self.assertTrue(all(not hasattr(m, "instructions") for m in result))


if __name__ == "__main__":
    unittest.main()
