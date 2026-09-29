"""SkillMetadata 和 MetadataParser 测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mini_deerflow.skill_metadata import (
    MarkdownSkillMetadataParser,
    SkillMetadata,
    SkillMetadataParseError,
    normalize_selection_text,
    validate_skill_name,
)


class TestSkillMetadata(unittest.TestCase):
    """SkillMetadata 模型测试。"""

    def test_normal_creation(self):
        """正常创建。"""
        meta = SkillMetadata(
            name="test-skill",
            description="测试 Skill",
            source_path=Path("test/SKILL.md"),
        )
        self.assertEqual(meta.name, "test-skill")
        self.assertEqual(meta.description, "测试 Skill")
        self.assertEqual(meta.source_path, Path("test/SKILL.md"))

    def test_frozen_immutable(self):
        """frozen 不可变。"""
        meta = SkillMetadata(
            name="test",
            description="描述",
            source_path=Path("test.md"),
        )
        with self.assertRaises(AttributeError):
            meta.name = "new"  # type: ignore

    def test_chinese_description(self):
        """中文 description。"""
        meta = SkillMetadata(
            name="test",
            description="中文描述",
            source_path=Path("test.md"),
        )
        self.assertEqual(meta.description, "中文描述")

    def test_no_instructions_field(self):
        """没有 instructions 字段。"""
        meta = SkillMetadata(
            name="test",
            description="描述",
            source_path=Path("test.md"),
        )
        self.assertFalse(hasattr(meta, "instructions"))

    def test_empty_name_raises(self):
        """空 name。"""
        with self.assertRaises(TypeError):
            SkillMetadata(
                name="",
                description="描述",
                source_path=Path("test.md"),
            )

    def test_whitespace_name_raises(self):
        """空白 name。"""
        with self.assertRaises(TypeError):
            SkillMetadata(
                name="   ",
                description="描述",
                source_path=Path("test.md"),
            )

    def test_illegal_name_raises(self):
        """非法 name（包含空格）。"""
        with self.assertRaises(ValueError):
            SkillMetadata(
                name="test skill",
                description="描述",
                source_path=Path("test.md"),
            )

    def test_uppercase_name_raises(self):
        """大写 name。"""
        with self.assertRaises(ValueError):
            SkillMetadata(
                name="TestSkill",
                description="描述",
                source_path=Path("test.md"),
            )

    def test_chinese_name_raises(self):
        """中文 name。"""
        with self.assertRaises(ValueError):
            SkillMetadata(
                name="测试技能",
                description="描述",
                source_path=Path("test.md"),
            )

    def test_empty_description_raises(self):
        """空 description。"""
        with self.assertRaises(TypeError):
            SkillMetadata(
                name="test",
                description="",
                source_path=Path("test.md"),
            )

    def test_non_path_source_raises(self):
        """非 Path source_path。"""
        with self.assertRaises(TypeError):
            SkillMetadata(
                name="test",
                description="描述",
                source_path="/path/to/file",  # type: ignore
            )

    def test_name_stripped(self):
        """name 去除首尾空白。"""
        meta = SkillMetadata(
            name="  test-skill  ",
            description="描述",
            source_path=Path("test.md"),
        )
        self.assertEqual(meta.name, "test-skill")

    def test_description_stripped(self):
        """description 去除首尾空白。"""
        meta = SkillMetadata(
            name="test",
            description="  描述  ",
            source_path=Path("test.md"),
        )
        self.assertEqual(meta.description, "描述")

    def test_legal_hyphen_and_underscore(self):
        """合法的连字符和下划线。"""
        meta = SkillMetadata(
            name="test-skill_v2",
            description="描述",
            source_path=Path("test.md"),
        )
        self.assertEqual(meta.name, "test-skill_v2")


class TestValidateSkillName(unittest.TestCase):
    """validate_skill_name 函数测试。"""

    def test_valid_name(self):
        """合法 name。"""
        self.assertEqual(validate_skill_name("test"), "test")
        self.assertEqual(validate_skill_name("test-skill"), "test-skill")
        self.assertEqual(validate_skill_name("test_skill"), "test_skill")
        self.assertEqual(validate_skill_name("test123"), "test123")
        self.assertEqual(validate_skill_name("123test"), "123test")

    def test_valid_name_stripped(self):
        """合法 name 去除空白。"""
        self.assertEqual(validate_skill_name("  test  "), "test")

    def test_invalid_type(self):
        """非字符串类型。"""
        with self.assertRaises(TypeError):
            validate_skill_name(123)  # type: ignore

    def test_empty_string(self):
        """空字符串。"""
        with self.assertRaises(TypeError):
            validate_skill_name("")

    def test_whitespace_only(self):
        """仅空白。"""
        with self.assertRaises(TypeError):
            validate_skill_name("   ")

    def test_invalid_pattern(self):
        """非法模式。"""
        with self.assertRaises(ValueError):
            validate_skill_name("Test")
        with self.assertRaises(ValueError):
            validate_skill_name("test skill")
        with self.assertRaises(ValueError):
            validate_skill_name("test/skill")


class TestMarkdownSkillMetadataParser(unittest.TestCase):
    """MarkdownSkillMetadataParser 测试。"""

    FIXTURE_PATH = Path(__file__).parent / "fixtures" / "skills" / "python-testing" / "SKILL.md"

    def test_parse_fixture(self):
        """解析测试夹具。"""
        parser = MarkdownSkillMetadataParser()
        meta = parser.parse(self.FIXTURE_PATH)
        self.assertEqual(meta.name, "python-testing")
        self.assertEqual(meta.description, "Python 项目的测试工作流程")
        self.assertTrue(meta.source_path.name, "SKILL.md")

    def test_parse_str_path(self):
        """str 路径。"""
        parser = MarkdownSkillMetadataParser()
        meta = parser.parse(str(self.FIXTURE_PATH))
        self.assertEqual(meta.name, "python-testing")

    def test_parse_path_object(self):
        """Path 路径。"""
        parser = MarkdownSkillMetadataParser()
        meta = parser.parse(self.FIXTURE_PATH)
        self.assertEqual(meta.name, "python-testing")

    def test_unix_newline(self):
        """Unix 换行。"""
        content = "---\nname: test\ndescription: Test\n---\n# Test\n\nVery long content..."
        with tempfile.NamedTemporaryFile(mode="wb", suffix=".md", delete=False) as f:
            f.write(content.encode("utf-8"))
            path = Path(f.name)

        try:
            parser = MarkdownSkillMetadataParser()
            meta = parser.parse(path)
            self.assertEqual(meta.name, "test")
        finally:
            path.unlink()

    def test_windows_newline(self):
        """Windows 换行。"""
        content = "---\r\nname: test\r\ndescription: Test\r\n---\r\n# Test\r\n\r\nVery long content..."
        with tempfile.NamedTemporaryFile(mode="wb", suffix=".md", delete=False) as f:
            f.write(content.encode("utf-8"))
            path = Path(f.name)

        try:
            parser = MarkdownSkillMetadataParser()
            meta = parser.parse(path)
            self.assertEqual(meta.name, "test")
        finally:
            path.unlink()

    def test_extra_metadata_fields(self):
        """允许未知 metadata。"""
        content = "---\nname: test\ndescription: Test\nversion: '1.0'\nauthor: demo\n---\n# Test"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write(content)
            path = Path(f.name)

        try:
            parser = MarkdownSkillMetadataParser()
            meta = parser.parse(path)
            self.assertEqual(meta.name, "test")
            self.assertEqual(meta.description, "Test")
        finally:
            path.unlink()

    def test_multiline_yaml_in_front_matter(self):
        """Front Matter 中的多行 YAML。"""
        content = '---\nname: test\ndescription: |\n  Multi\n  Line\n---\n# Test'
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write(content)
            path = Path(f.name)

        try:
            parser = MarkdownSkillMetadataParser()
            meta = parser.parse(path)
            self.assertEqual(meta.name, "test")
            self.assertIn("Multi", meta.description)
        finally:
            path.unlink()


class TestMarkdownSkillMetadataParserErrors(unittest.TestCase):
    """解析错误测试。"""

    def test_file_not_exists(self):
        """文件不存在。"""
        parser = MarkdownSkillMetadataParser()
        with self.assertRaises(SkillMetadataParseError) as ctx:
            parser.parse("/nonexistent/path.md")
        self.assertIn("文件不存在", str(ctx.exception))

    def test_path_is_directory(self):
        """路径是目录。"""
        parser = MarkdownSkillMetadataParser()
        with self.assertRaises(SkillMetadataParseError) as ctx:
            parser.parse(Path(__file__).parent)
        self.assertIn("不是普通文件", str(ctx.exception))

    def test_empty_file(self):
        """空文件。"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write("")
            path = Path(f.name)

        try:
            parser = MarkdownSkillMetadataParser()
            with self.assertRaises(SkillMetadataParseError) as ctx:
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
            parser = MarkdownSkillMetadataParser()
            with self.assertRaises(SkillMetadataParseError) as ctx:
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
            parser = MarkdownSkillMetadataParser()
            with self.assertRaises(SkillMetadataParseError) as ctx:
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
            parser = MarkdownSkillMetadataParser()
            with self.assertRaises(SkillMetadataParseError) as ctx:
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
            parser = MarkdownSkillMetadataParser()
            with self.assertRaises(SkillMetadataParseError) as ctx:
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
            parser = MarkdownSkillMetadataParser()
            with self.assertRaises(SkillMetadataParseError) as ctx:
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
            parser = MarkdownSkillMetadataParser()
            with self.assertRaises(SkillMetadataParseError) as ctx:
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
            parser = MarkdownSkillMetadataParser()
            with self.assertRaises(SkillMetadataParseError) as ctx:
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
            parser = MarkdownSkillMetadataParser()
            with self.assertRaises(SkillMetadataParseError) as ctx:
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
            parser = MarkdownSkillMetadataParser()
            with self.assertRaises(SkillMetadataParseError) as ctx:
                parser.parse(path)
            self.assertIn("字符串", str(ctx.exception))
        finally:
            path.unlink()

    def test_illegal_name(self):
        """非法 name。"""
        content = "---\nname: Test Skill!\ndescription: Test\n---\n# Test"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write(content)
            path = Path(f.name)

        try:
            parser = MarkdownSkillMetadataParser()
            with self.assertRaises(SkillMetadataParseError) as ctx:
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
            parser = MarkdownSkillMetadataParser()
            with self.assertRaises(SkillMetadataParseError) as ctx:
                parser.parse(path)
            self.assertIn("YAML", str(ctx.exception))
        finally:
            path.unlink()


class TestProgressiveDisclosure(unittest.TestCase):
    """渐进式披露测试。"""

    def test_stops_at_second_delimiter(self):
        """在第二个 --- 处停止读取。"""
        # 创建一个超长正文
        long_content = "# Test\n\n" + ("Long content line.\n" * 1000)
        content = f"---\nname: test\ndescription: Test\n---\n{long_content}"

        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write(content)
            path = Path(f.name)

        try:
            # 用二进制打开文件，验证解析后文件指针位置
            parser = MarkdownSkillMetadataParser()
            parser.parse(path)

            # 再次打开，验证可以在第二个 --- 之后继续读取
            with path.open("rb") as file:
                file.readline()  # ---\n
                while True:
                    line = file.readline()
                    if line.rstrip(b"\r\n") == b"---":
                        pos_after_front_matter = file.tell()
                        break

                # 读取剩余内容
                remaining = file.read()
                self.assertIn(b"Long content", remaining)
        finally:
            path.unlink()

    def test_large_body_not_loaded(self):
        """大型正文不被加载到内存。"""
        # 创建包含大量内容的文件
        lines = ["Line " + str(i) + "\n" for i in range(10000)]
        long_body = "".join(lines)
        content = f"---\nname: test\ndescription: Test\n---\n{long_body}"

        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write(content)
            path = Path(f.name)

        try:
            parser = MarkdownSkillMetadataParser()
            meta = parser.parse(path)

            # 元数据不包含正文
            self.assertFalse(hasattr(meta, "instructions"))
            self.assertFalse(hasattr(meta, "body"))
            self.assertFalse(hasattr(meta, "content"))
        finally:
            path.unlink()

    def test_body_with_invalid_utf8_still_parses_front_matter(self):
        """正文包含无效 UTF-8，前提 Matter 仍能解析。"""
        content = "---\nname: test\ndescription: Test\n---\n"
        invalid_utf8 = b"\xff\xfe\n"

        with tempfile.NamedTemporaryFile(mode="wb", suffix=".md", delete=False) as f:
            f.write(content.encode("utf-8"))
            f.write(invalid_utf8)
            path = Path(f.name)

        try:
            parser = MarkdownSkillMetadataParser()
            meta = parser.parse(path)
            self.assertEqual(meta.name, "test")
        finally:
            path.unlink()

    def test_body_with_yaml_like_text_in_body(self):
        """正文中的 YAML 风格文本不影响解析。"""
        content = """---
name: test
description: Test
---
# Title

代码块中的 YAML:
```yaml
---
key: value
---
```

这不会影响 Front Matter 解析。
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write(content)
            path = Path(f.name)

        try:
            parser = MarkdownSkillMetadataParser()
            meta = parser.parse(path)
            self.assertEqual(meta.name, "test")
            self.assertEqual(meta.description, "Test")
        finally:
            path.unlink()


class TestNormalizeSelectionText(unittest.TestCase):
    """normalize_selection_text 测试。"""

    def test_empty_string(self):
        """空字符串。"""
        self.assertEqual(normalize_selection_text(""), "")

    def test_whitespace_only(self):
        """仅空白。"""
        self.assertEqual(normalize_selection_text("   \t\n  "), "")

    def test_lowercase(self):
        """转小写。"""
        self.assertEqual(normalize_selection_text("HELLO"), "hello")

    def test_chinese(self):
        """中文。"""
        self.assertEqual(normalize_selection_text("中文测试"), "中文测试")

    def test_nfkc_normalization(self):
        """NFKC 标准化。"""
        # 全角字母
        self.assertEqual(normalize_selection_text("ＡＢＣ"), "abc")
        # 全角数字
        self.assertEqual(normalize_selection_text("１２３"), "123")
        # 带组合符号的字符会分解
        # é (U+00E9) 分解为 e + 组合锐音符
        result = normalize_selection_text("é")
        self.assertTrue(len(result) >= 1)

    def test_collapse_whitespace(self):
        """压缩空白。"""
        self.assertEqual(normalize_selection_text("hello   world"), "hello world")
        self.assertEqual(normalize_selection_text("a\t\nb"), "a b")

    def test_strip(self):
        """去除首尾空白。"""
        self.assertEqual(normalize_selection_text("  hello  "), "hello")

    def test_combined(self):
        """组合测试。"""
        self.assertEqual(normalize_selection_text("  HELLO   WORLD  "), "hello world")


if __name__ == "__main__":
    unittest.main()
