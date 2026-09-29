"""SkillContextBuilder 测试。"""

from __future__ import annotations

import unittest
from pathlib import Path

from mini_deerflow.skill import Skill, MarkdownSkillParser
from mini_deerflow.skill_context import SkillContextBuilder


def _skill(
    name: str = "test-skill",
    description: str = "测试 Skill",
    instructions: str = "# Test",
    source_path: str | Path = "/test/SKILL.md",
) -> Skill:
    return Skill(
        name=name,
        description=description,
        instructions=instructions,
        source_path=Path(source_path),
    )


class TestSkillContextBuilder(unittest.TestCase):
    """SkillContextBuilder 测试。"""

    def test_empty_skills(self):
        """空 Skill 列表。"""
        builder = SkillContextBuilder()
        result = builder.build([])
        self.assertEqual(result, "")

    def test_single_skill(self):
        """单个 Skill。"""
        builder = SkillContextBuilder()
        skill = _skill("test", "Test desc", "# Test\n\nInstructions")
        result = builder.build([skill])
        self.assertIn("# Test", result)
        self.assertIn("Instructions", result)

    def test_multiple_skills(self):
        """多个 Skill。"""
        builder = SkillContextBuilder()
        skills = [
            _skill("first", "First desc", "# First"),
            _skill("second", "Second desc", "# Second"),
        ]
        result = builder.build(skills)
        self.assertIn("first", result)
        self.assertIn("second", result)
        self.assertIn("# First", result)
        self.assertIn("# Second", result)

    def test_preserves_input_order(self):
        """保持输入顺序。"""
        builder = SkillContextBuilder()
        skills = [
            _skill("zebra", "Z", "# Z"),
            _skill("alpha", "A", "# A"),
            _skill("middle", "M", "# M"),
        ]
        result = builder.build(skills)
        # 顺序应该保持
        z_pos = result.find("zebra")
        a_pos = result.find("alpha")
        m_pos = result.find("middle")
        self.assertLess(z_pos, a_pos)
        self.assertLess(a_pos, m_pos)

    def test_chinese_content(self):
        """中文内容。"""
        builder = SkillContextBuilder()
        skill = _skill("test-skill", "中文描述", "# 中文标题\n\n中文内容")
        result = builder.build([skill])
        self.assertIn("中文", result)

    def test_multiline_instructions(self):
        """多行 instructions。"""
        builder = SkillContextBuilder()
        instructions = "Line 1\n\nLine 2\n\nLine 3"
        skill = _skill("test", "Test", instructions)
        result = builder.build([skill])
        self.assertIn("Line 1", result)
        self.assertIn("Line 2", result)
        self.assertIn("Line 3", result)

    def test_instructions_with_markdown(self):
        """instructions 中包含 Markdown。"""
        builder = SkillContextBuilder()
        instructions = """# Title

## Subtitle

- Item 1
- Item 2

```python
def hello():
    print("hello")
```"""
        skill = _skill("test", "Test", instructions)
        result = builder.build([skill])
        self.assertIn("## Subtitle", result)
        self.assertIn("Item 1", result)
        self.assertIn("```python", result)

    def test_instructions_with_hyphen(self):
        """instructions 中包含 ---。"""
        builder = SkillContextBuilder()
        instructions = """# Test

```yaml
---
key: value
---
```

结束
"""
        skill = _skill("test", "Test", instructions)
        result = builder.build([skill])
        self.assertIn("---", result)
        self.assertIn("结束", result)

    def test_does_not_include_source_path(self):
        """不包含 source_path。"""
        builder = SkillContextBuilder()
        skill = _skill("test", "Test", "# Test", "/path/to/SKILL.md")
        result = builder.build([skill])
        self.assertNotIn("/path/to", result)

    def test_non_sequence_raises(self):
        """非 Sequence。"""
        builder = SkillContextBuilder()
        with self.assertRaises(TypeError):
            builder.build(123)

    def test_string_raises(self):
        """str 非法。"""
        builder = SkillContextBuilder()
        with self.assertRaises(TypeError):
            builder.build("not a list")

    def test_contains_non_skill_raises(self):
        """包含非 Skill 元素。"""
        builder = SkillContextBuilder()
        with self.assertRaises(ValueError):
            builder.build([_skill(), "not a skill"])

    def test_duplicate_names_raises(self):
        """重复名称。"""
        builder = SkillContextBuilder()
        with self.assertRaises(ValueError):
            builder.build([_skill("test", "A", "# A"), _skill("test", "B", "# B")])

    def test_does_not_modify_input(self):
        """不修改输入。"""
        builder = SkillContextBuilder()
        skills = [_skill("test", "Test", "# Test")]
        original = skills[0].instructions
        builder.build(skills)
        self.assertEqual(skills[0].instructions, original)

    def test_output_stable(self):
        """输出稳定。"""
        builder = SkillContextBuilder()
        skills = [_skill("test", "Test", "# Test")]
        result1 = builder.build(skills)
        result2 = builder.build(skills)
        self.assertEqual(result1, result2)

    def test_only_includes_passed_skills(self):
        """只包含传入的 Skill。"""
        builder = SkillContextBuilder()
        skills = [_skill("included", "Included", "# Included")]
        result = builder.build(skills)
        self.assertIn("included", result)
        self.assertNotIn("excluded", result)

    def test_header_format(self):
        """头部格式。"""
        builder = SkillContextBuilder()
        skill = _skill("test", "Test", "# Test")
        result = builder.build([skill])
        self.assertIn("以下技能指令", result)
        self.assertIn("完成当前任务", result)


class TestSkillContextBuilderIntegration(unittest.TestCase):
    """集成测试。"""

    def test_full_pipeline(self):
        """完整流程。"""
        from mini_deerflow.skill_catalog import SkillCatalog
        from mini_deerflow.skill_metadata import MarkdownSkillMetadataParser
        from mini_deerflow.skill_selector import RuleBasedSkillSelector
        from mini_deerflow.skill_loader import ProgressiveSkillLoader
        from mini_deerflow.skill_registry import SkillRegistry

        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = Path(tmpdir) / "test"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                "---\nname: test-skill\ndescription: Test Skill\n---\n# Test Skill\n\nStep 1\nStep 2",
                encoding="utf-8",
            )

            # 1. 扫描
            catalog = SkillCatalog(MarkdownSkillMetadataParser())
            catalog.load_directory(tmpdir)

            # 2. 选择
            selector = RuleBasedSkillSelector({"test-skill": ("test",)})
            selected = selector.select("test", catalog.list_metadata())

            # 3. 加载
            parser = MarkdownSkillParser()
            registry = SkillRegistry(parser)
            loader = ProgressiveSkillLoader(parser, registry)
            skills = loader.load_selected(selected)

            # 4. 构建上下文
            builder = SkillContextBuilder()
            context = builder.build(skills)

            # 验证
            self.assertIn("test-skill", context)
            self.assertIn("Test Skill", context)
            self.assertIn("Step 1", context)
            self.assertIn("Step 2", context)


if __name__ == "__main__":
    unittest.main()
