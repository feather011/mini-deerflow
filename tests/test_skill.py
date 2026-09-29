"""Skill 模型与 MarkdownSkillParser 测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mini_deerflow.skill import (
    MarkdownSkillParser,
    Skill,
    SkillParseError,
)


class TestSkillModel(unittest.TestCase):
    """Skill 模型测试。"""

    def test_normal_creation(self):
        """正常创建。"""
        skill = Skill(
            name="test-skill",
            description="测试技能",
            instructions="# 测试",
            source_path=Path("test/SKILL.md"),
        )
        self.assertEqual(skill.name, "test-skill")
        self.assertEqual(skill.description, "测试技能")
        self.assertEqual(skill.instructions, "# 测试")
        self.assertEqual(skill.source_path, Path("test/SKILL.md"))

    def test_frozen_immutable(self):
        """frozen 不可变。"""
        skill = Skill(
            name="test",
            description="描述",
            instructions="# 指令",
            source_path=Path("test.md"),
        )
        with self.assertRaises(AttributeError):
            skill.name = "new"  # type: ignore

    def test_chinese_description(self):
        """中文 description。"""
        skill = Skill(
            name="test",
            description="中文描述",
            instructions="指令",
            source_path=Path("test.md"),
        )
        self.assertEqual(skill.description, "中文描述")

    def test_multiline_instructions(self):
        """多行 instructions。"""
        instructions = "第一行\n第二行\n第三行"
        skill = Skill(
            name="test",
            description="描述",
            instructions=instructions,
            source_path=Path("test.md"),
        )
        self.assertEqual(skill.instructions, instructions)

    def test_empty_name(self):
        """空 name。"""
        with self.assertRaises(TypeError):
            Skill(
                name="",
                description="描述",
                instructions="指令",
                source_path=Path("test.md"),
            )

    def test_whitespace_name(self):
        """空白 name。"""
        with self.assertRaises(TypeError):
            Skill(
                name="   ",
                description="描述",
                instructions="指令",
                source_path=Path("test.md"),
            )

    def test_empty_description(self):
        """空 description。"""
        with self.assertRaises(TypeError):
            Skill(
                name="test",
                description="",
                instructions="指令",
                source_path=Path("test.md"),
            )

    def test_empty_instructions(self):
        """空 instructions。"""
        with self.assertRaises(TypeError):
            Skill(
                name="test",
                description="描述",
                instructions="",
                source_path=Path("test.md"),
            )

    def test_non_string_name(self):
        """非字符串 name。"""
        with self.assertRaises(TypeError):
            Skill(
                name=123,  # type: ignore
                description="描述",
                instructions="指令",
                source_path=Path("test.md"),
            )

    def test_non_string_description(self):
        """非字符串 description。"""
        with self.assertRaises(TypeError):
            Skill(
                name="test",
                description=123,  # type: ignore
                instructions="指令",
                source_path=Path("test.md"),
            )

    def test_non_path_source(self):
        """非 Path source_path。"""
        with self.assertRaises(TypeError):
            Skill(
                name="test",
                description="描述",
                instructions="指令",
                source_path="/path/to/file",  # type: ignore
            )

    def test_name_with_space(self):
        """name 包含空格。"""
        with self.assertRaises(ValueError):
            Skill(
                name="test skill",
                description="描述",
                instructions="指令",
                source_path=Path("test.md"),
            )

    def test_name_with_uppercase(self):
        """name 包含大写。"""
        with self.assertRaises(ValueError):
            Skill(
                name="TestSkill",
                description="描述",
                instructions="指令",
                source_path=Path("test.md"),
            )

    def test_name_with_chinese(self):
        """name 包含中文。"""
        with self.assertRaises(ValueError):
            Skill(
                name="测试技能",
                description="描述",
                instructions="指令",
                source_path=Path("test.md"),
            )

    def test_name_with_path_separator(self):
        """name 包含路径分隔符。"""
        with self.assertRaises(ValueError):
            Skill(
                name="test/skill",
                description="描述",
                instructions="指令",
                source_path=Path("test.md"),
            )

    def test_legal_hyphen_and_underscore(self):
        """合法的连字符和下划线。"""
        skill = Skill(
            name="test-skill_v2",
            description="描述",
            instructions="指令",
            source_path=Path("test.md"),
        )
        self.assertEqual(skill.name, "test-skill_v2")

    def test_name_stripped(self):
        """name 去除首尾空白。"""
        skill = Skill(
            name="  test-skill  ",
            description="描述",
            instructions="指令",
            source_path=Path("test.md"),
        )
        self.assertEqual(skill.name, "test-skill")

    def test_description_stripped(self):
        """description 去除首尾空白。"""
        skill = Skill(
            name="test",
            description="  描述  ",
            instructions="指令",
            source_path=Path("test.md"),
        )
        self.assertEqual(skill.description, "描述")


class TestMarkdownSkillParser(unittest.TestCase):
    """MarkdownSkillParser 测试。"""

    FIXTURE_PATH = Path(__file__).parent / "fixtures" / "skills" / "python-testing" / "SKILL.md"

    def test_parse_fixture(self):
        """解析测试夹具。"""
        parser = MarkdownSkillParser()
        skill = parser.parse(self.FIXTURE_PATH)
        self.assertEqual(skill.name, "python-testing")
        self.assertEqual(skill.description, "Python 项目的测试工作流程")
        self.assertIn("Python Testing", skill.instructions)
        self.assertTrue(skill.instructions.startswith("# Python Testing"))

    def test_parse_str_path(self):
        """str 路径。"""
        parser = MarkdownSkillParser()
        skill = parser.parse(str(self.FIXTURE_PATH))
        self.assertEqual(skill.name, "python-testing")

    def test_parse_path_object(self):
        """Path 路径。"""
        parser = MarkdownSkillParser()
        skill = parser.parse(self.FIXTURE_PATH)
        self.assertEqual(skill.name, "python-testing")

    def test_windows_newline(self):
        """Windows 换行。"""
        content = "---\r\nname: test\r\ndescription: Test\r\n---\r\n# Test"
        with tempfile.NamedTemporaryFile(mode="wb", suffix=".md", delete=False) as f:
            f.write(content.encode("utf-8"))
            path = Path(f.name)

        try:
            parser = MarkdownSkillParser()
            skill = parser.parse(path)
            self.assertEqual(skill.name, "test")
        finally:
            path.unlink()

    def test_unix_newline(self):
        """Unix 换行。"""
        content = "---\nname: test\ndescription: Test\n---\n# Test"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write(content)
            path = Path(f.name)

        try:
            parser = MarkdownSkillParser()
            skill = parser.parse(path)
            self.assertEqual(skill.name, "test")
        finally:
            path.unlink()

    def test_chinese_content(self):
        """中文内容。"""
        content = "---\nname: test\ndescription: 中文描述\n---\n# 中文标题\n\n中文内容"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write(content)
            path = Path(f.name)

        try:
            parser = MarkdownSkillParser()
            skill = parser.parse(path)
            self.assertIn("中文", skill.description)
            self.assertIn("中文", skill.instructions)
        finally:
            path.unlink()

    def test_multiline_markdown(self):
        """多行 Markdown。"""
        content = "---\nname: test\ndescription: Test\n---\n# Title\n\nLine 1\n\nLine 2\n\nLine 3"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write(content)
            path = Path(f.name)

        try:
            parser = MarkdownSkillParser()
            skill = parser.parse(path)
            self.assertIn("Line 1", skill.instructions)
            self.assertIn("Line 2", skill.instructions)
            self.assertIn("Line 3", skill.instructions)
        finally:
            path.unlink()

    def test_markdown_contains_hyphen(self):
        """Markdown 中包含 ---。"""
        content = "---\nname: test\ndescription: Test\n---\n# Title\n\n代码块:\n```yaml\n---\nkey: value\n---\n```"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write(content)
            path = Path(f.name)

        try:
            parser = MarkdownSkillParser()
            skill = parser.parse(path)
            self.assertIn("---", skill.instructions)
        finally:
            path.unlink()

    def test_description_with_colon(self):
        """description 中包含冒号。"""
        content = '---\nname: test\ndescription: "描述: 带冒号"\n---\n# Test'
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write(content)
            path = Path(f.name)

        try:
            parser = MarkdownSkillParser()
            skill = parser.parse(path)
            self.assertIn(":", skill.description)
        finally:
            path.unlink()

    def test_extra_metadata_fields(self):
        """允许未知 metadata。"""
        content = "---\nname: test\ndescription: Test\nversion: '1.0'\nauthor: demo\n---\n# Test"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write(content)
            path = Path(f.name)

        try:
            parser = MarkdownSkillParser()
            skill = parser.parse(path)
            self.assertEqual(skill.name, "test")
            self.assertEqual(skill.description, "Test")
        finally:
            path.unlink()

    def test_repeated_parse_same_result(self):
        """重复解析结果相同。"""
        parser = MarkdownSkillParser()
        skill1 = parser.parse(self.FIXTURE_PATH)
        skill2 = parser.parse(self.FIXTURE_PATH)
        self.assertEqual(skill1, skill2)

    def test_source_file_not_modified(self):
        """源文件不被修改。"""
        original = self.FIXTURE_PATH.read_text(encoding="utf-8")
        parser = MarkdownSkillParser()
        parser.parse(self.FIXTURE_PATH)
        after = self.FIXTURE_PATH.read_text(encoding="utf-8")
        self.assertEqual(original, after)


class TestMarkdownSkillParserErrors(unittest.TestCase):
    """解析失败场景测试。"""

    def test_file_not_exists(self):
        """文件不存在。"""
        parser = MarkdownSkillParser()
        with self.assertRaises(SkillParseError) as ctx:
            parser.parse("/nonexistent/path.md")
        self.assertIn("文件不存在", str(ctx.exception))

    def test_path_is_directory(self):
        """路径是目录。"""
        parser = MarkdownSkillParser()
        with self.assertRaises(SkillParseError) as ctx:
            parser.parse(Path(__file__).parent)
        self.assertIn("不是普通文件", str(ctx.exception))

    def test_non_utf8_file(self):
        """非 UTF-8 文件。"""
        with tempfile.NamedTemporaryFile(mode="wb", suffix=".md", delete=False) as f:
            f.write(b"\xff\xfe")
            path = Path(f.name)

        try:
            parser = MarkdownSkillParser()
            with self.assertRaises(SkillParseError) as ctx:
                parser.parse(path)
            self.assertIn("UTF-8", str(ctx.exception))
        finally:
            path.unlink()

    def test_empty_file(self):
        """空文件。"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write("")
            path = Path(f.name)

        try:
            parser = MarkdownSkillParser()
            with self.assertRaises(SkillParseError) as ctx:
                parser.parse(path)
            self.assertIn("为空", str(ctx.exception))
        finally:
            path.unlink()

    def test_missing_start_delimiter(self):
        """缺少起始 ---。"""
        content = "name: test\ndescription: Test\n---\n# Test"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write(content)
            path = Path(f.name)

        try:
            parser = MarkdownSkillParser()
            with self.assertRaises(SkillParseError) as ctx:
                parser.parse(path)
            self.assertIn("缺少起始", str(ctx.exception))
        finally:
            path.unlink()

    def test_missing_end_delimiter(self):
        """缺少结束 ---。"""
        content = "---\nname: test\ndescription: Test\n# Test"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write(content)
            path = Path(f.name)

        try:
            parser = MarkdownSkillParser()
            with self.assertRaises(SkillParseError) as ctx:
                parser.parse(path)
            self.assertIn("缺少结束", str(ctx.exception))
        finally:
            path.unlink()

    def test_empty_front_matter(self):
        """空 Front Matter。"""
        content = "---\n---\n# Test"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write(content)
            path = Path(f.name)

        try:
            parser = MarkdownSkillParser()
            with self.assertRaises(SkillParseError) as ctx:
                parser.parse(path)
            self.assertIn("Front Matter", str(ctx.exception))
        finally:
            path.unlink()

    def test_yaml_syntax_error(self):
        """YAML 语法错误。"""
        content = "---\nname: [unclosed\n---\n# Test"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write(content)
            path = Path(f.name)

        try:
            parser = MarkdownSkillParser()
            with self.assertRaises(SkillParseError) as ctx:
                parser.parse(path)
            self.assertIn("YAML", str(ctx.exception))
        finally:
            path.unlink()

    def test_yaml_is_list(self):
        """YAML 是 list。"""
        content = "---\n- item1\n- item2\n---\n# Test"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write(content)
            path = Path(f.name)

        try:
            parser = MarkdownSkillParser()
            with self.assertRaises(SkillParseError) as ctx:
                parser.parse(path)
            self.assertIn("mapping", str(ctx.exception))
        finally:
            path.unlink()

    def test_yaml_is_string(self):
        """YAML 是字符串。"""
        content = "---\njust a string\n---\n# Test"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write(content)
            path = Path(f.name)

        try:
            parser = MarkdownSkillParser()
            with self.assertRaises(SkillParseError) as ctx:
                parser.parse(path)
            self.assertIn("mapping", str(ctx.exception))
        finally:
            path.unlink()

    def test_missing_name(self):
        """缺少 name。"""
        content = "---\ndescription: Test\n---\n# Test"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write(content)
            path = Path(f.name)

        try:
            parser = MarkdownSkillParser()
            with self.assertRaises(SkillParseError) as ctx:
                parser.parse(path)
            self.assertIn("name", str(ctx.exception))
        finally:
            path.unlink()

    def test_missing_description(self):
        """缺少 description。"""
        content = "---\nname: test\n---\n# Test"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write(content)
            path = Path(f.name)

        try:
            parser = MarkdownSkillParser()
            with self.assertRaises(SkillParseError) as ctx:
                parser.parse(path)
            self.assertIn("description", str(ctx.exception))
        finally:
            path.unlink()

    def test_name_type_error(self):
        """name 类型错误。"""
        content = "---\nname: 123\ndescription: Test\n---\n# Test"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write(content)
            path = Path(f.name)

        try:
            parser = MarkdownSkillParser()
            with self.assertRaises(SkillParseError) as ctx:
                parser.parse(path)
            self.assertIn("字符串", str(ctx.exception))
        finally:
            path.unlink()

    def test_description_type_error(self):
        """description 类型错误。"""
        content = "---\nname: test\ndescription: 123\n---\n# Test"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write(content)
            path = Path(f.name)

        try:
            parser = MarkdownSkillParser()
            with self.assertRaises(SkillParseError) as ctx:
                parser.parse(path)
            self.assertIn("字符串", str(ctx.exception))
        finally:
            path.unlink()

    def test_empty_markdown_body(self):
        """空正文。"""
        content = "---\nname: test\ndescription: Test\n---\n   \n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write(content)
            path = Path(f.name)

        try:
            parser = MarkdownSkillParser()
            with self.assertRaises(SkillParseError) as ctx:
                parser.parse(path)
            self.assertIn("正文为空", str(ctx.exception))
        finally:
            path.unlink()

    def test_only_whitespace_body(self):
        """只有空白的正文。"""
        content = "---\nname: test\ndescription: Test\n---\n   \n   \n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write(content)
            path = Path(f.name)

        try:
            parser = MarkdownSkillParser()
            with self.assertRaises(SkillParseError) as ctx:
                parser.parse(path)
            self.assertIn("正文为空", str(ctx.exception))
        finally:
            path.unlink()

    def test_illegal_name(self):
        """非法 name。"""
        content = "---\nname: Test Skill!\ndescription: Test\n---\n# Test"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write(content)
            path = Path(f.name)

        try:
            parser = MarkdownSkillParser()
            with self.assertRaises(SkillParseError) as ctx:
                parser.parse(path)
            self.assertIn("非法 name", str(ctx.exception))
        finally:
            path.unlink()

    def test_yaml_dangerous_tag_rejected(self):
        """YAML 危险标签被拒绝。"""
        content = "---\nname: !!python/object:__main__.Test obj\ndescription: Test\n---\n# Test"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write(content)
            path = Path(f.name)

        try:
            parser = MarkdownSkillParser()
            with self.assertRaises(SkillParseError) as ctx:
                parser.parse(path)
            self.assertIn("YAML", str(ctx.exception))
        finally:
            path.unlink()

    def test_error_contains_path_and_reason(self):
        """错误信息包含文件路径和原因。"""
        parser = MarkdownSkillParser()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write("---\ndescription: Test\n---")
            path = Path(f.name)

        try:
            with self.assertRaises(SkillParseError) as ctx:
                parser.parse(path)
            self.assertIn(str(path), str(ctx.exception))
            self.assertIn("name", str(ctx.exception))
        finally:
            path.unlink()


if __name__ == "__main__":
    unittest.main()
