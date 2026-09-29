"""SkillRegistry 测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mini_deerflow.skill import (
    MarkdownSkillParser,
    Skill,
    SkillParseError,
)
from mini_deerflow.skill_registry import (
    SkillAlreadyExistsError,
    SkillDirectoryError,
    SkillNotFoundError,
    SkillRegistry,
)


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


class _FakeParser:
    """用于测试的假解析器。"""

    def __init__(self) -> None:
        self.paths: list[Path] = []
        self._skills: dict[str, Skill] = {}

    def register_parse_result(self, skill: Skill) -> None:
        """预注册解析结果。"""
        self._skills[str(skill.source_path)] = skill

    def parse(self, path: str | Path) -> Skill:
        p = Path(path)
        self.paths.append(p)
        if str(p) in self._skills:
            return self._skills[str(p)]
        # 默认行为：创建 Skill，name 取自路径（包含父目录确保唯一）
        # 例如: valid/SKILL.md -> valid_skill
        parts = []
        for part in p.parts[-3:-1]:  # 取 grandparent 和 parent
            part_clean = part.lower().replace("-", "_").replace(" ", "_")
            if part_clean:
                parts.append(part_clean)
        if not parts:
            parts = ["skill"]
        name = "_".join(parts)
        return Skill(
            name=name,
            description=f"Parsed from {p}",
            instructions=f"# {p.name}",
            source_path=p.resolve(),
        )


class TestSkillRegistryRegister(unittest.TestCase):
    """register 方法测试。"""

    def test_register_single_skill(self):
        """注册一个 Skill。"""
        registry = SkillRegistry(_FakeParser())
        registry.register(_skill("test", "描述", "# Test", "/a/SKILL.md"))
        self.assertEqual(len(registry), 1)
        self.assertTrue(registry.exists("test"))

    def test_register_multiple_skills(self):
        """注册多个 Skill。"""
        registry = SkillRegistry(_FakeParser())
        registry.register(_skill("skill-a", "A", "# A", "/a.md"))
        registry.register(_skill("skill-b", "B", "# B", "/b.md"))
        self.assertEqual(len(registry), 2)
        self.assertTrue(registry.exists("skill-a"))
        self.assertTrue(registry.exists("skill-b"))

    def test_register_non_skill_raises(self):
        """注册非 Skill 类型。"""
        registry = SkillRegistry(_FakeParser())
        with self.assertRaises(TypeError):
            registry.register("not a skill")  # type: ignore

    def test_duplicate_name_raises(self):
        """重复名称。"""
        registry = SkillRegistry(_FakeParser())
        registry.register(_skill("test", "描述1", "# Test1", "/a.md"))
        with self.assertRaises(SkillAlreadyExistsError) as ctx:
            registry.register(_skill("test", "描述2", "# Test2", "/b.md"))
        self.assertEqual(ctx.exception.name, "test")
        # 错误包含两个 source_path
        self.assertIn("/a.md", str(ctx.exception))
        self.assertIn("/b.md", str(ctx.exception))

    def test_identical_skill_repeated_register_raises(self):
        """完全相同的 Skill 重复注册。"""
        registry = SkillRegistry(_FakeParser())
        skill = _skill("test", "描述", "# Test", "/a.md")
        registry.register(skill)
        with self.assertRaises(SkillAlreadyExistsError):
            registry.register(skill)

    def test_register_failure_preserves_state(self):
        """注册失败后状态不变。"""
        registry = SkillRegistry(_FakeParser())
        registry.register(_skill("first", "First", "# First", "/first.md"))
        try:
            registry.register(_skill("first", "Dupe", "# Dupe", "/dupe.md"))
        except SkillAlreadyExistsError:
            pass
        # 第一个 Skill 仍在
        self.assertEqual(len(registry), 1)
        self.assertTrue(registry.exists("first"))


class TestSkillRegistryGet(unittest.TestCase):
    """get 方法测试。"""

    def test_get_existing(self):
        """获取存在的 Skill。"""
        registry = SkillRegistry(_FakeParser())
        skill = _skill("test", "描述", "# Test", "/a.md")
        registry.register(skill)
        retrieved = registry.get("test")
        self.assertIs(retrieved, skill)

    def test_get_nonexistent_raises(self):
        """获取不存在的 Skill。"""
        registry = SkillRegistry(_FakeParser())
        with self.assertRaises(SkillNotFoundError) as ctx:
            registry.get("nonexistent")
        self.assertEqual(ctx.exception.name, "nonexistent")

    def test_get_non_string_raises(self):
        """非字符串名称。"""
        registry = SkillRegistry(_FakeParser())
        with self.assertRaises(TypeError):
            registry.get(123)  # type: ignore

    def test_get_returns_original_object(self):
        """返回原 Skill 对象。"""
        registry = SkillRegistry(_FakeParser())
        skill = _skill("test", "描述", "# Test", "/a.md")
        registry.register(skill)
        retrieved = registry.get("test")
        self.assertIs(retrieved, skill)


class TestSkillRegistryExists(unittest.TestCase):
    """exists 方法测试。"""

    def test_exists_true(self):
        """存在。"""
        registry = SkillRegistry(_FakeParser())
        registry.register(_skill("test", "描述", "# Test", "/a.md"))
        self.assertTrue(registry.exists("test"))

    def test_exists_false(self):
        """不存在。"""
        registry = SkillRegistry(_FakeParser())
        self.assertFalse(registry.exists("nonexistent"))

    def test_exists_non_string_raises(self):
        """非字符串名称。"""
        registry = SkillRegistry(_FakeParser())
        with self.assertRaises(TypeError):
            registry.exists(123)  # type: ignore

    def test_exists_does_not_change_state(self):
        """不改变 Registry 状态。"""
        registry = SkillRegistry(_FakeParser())
        registry.exists("test")
        self.assertEqual(len(registry), 0)


class TestSkillRegistryList(unittest.TestCase):
    """list_skills 方法测试。"""

    def test_list_empty(self):
        """空 Registry。"""
        registry = SkillRegistry(_FakeParser())
        self.assertEqual(registry.list_skills(), [])

    def test_list_single_skill(self):
        """一个 Skill。"""
        registry = SkillRegistry(_FakeParser())
        registry.register(_skill("b-skill", "B", "# B", "/b.md"))
        skills = registry.list_skills()
        self.assertEqual(len(skills), 1)
        self.assertEqual(skills[0].name, "b-skill")

    def test_list_sorted_by_name(self):
        """多个 Skill 按 name 排序。"""
        registry = SkillRegistry(_FakeParser())
        registry.register(_skill("z-skill", "Z", "# Z", "/z.md"))
        registry.register(_skill("a-skill", "A", "# A", "/a.md"))
        registry.register(_skill("m-skill", "M", "# M", "/m.md"))
        names = [s.name for s in registry.list_skills()]
        self.assertEqual(names, ["a-skill", "m-skill", "z-skill"])

    def test_list_returns_new_list(self):
        """返回新 list。"""
        registry = SkillRegistry(_FakeParser())
        registry.register(_skill("test", "描述", "# Test", "/a.md"))
        result1 = registry.list_skills()
        result2 = registry.list_skills()
        self.assertIsNot(result1, result2)

    def test_modify_returned_list_does_not_affect_registry(self):
        """修改返回值不影响 Registry。"""
        registry = SkillRegistry(_FakeParser())
        registry.register(_skill("test", "描述", "# Test", "/a.md"))
        result = registry.list_skills()
        result.clear()
        self.assertEqual(len(registry), 1)

    def test_list_order_stable(self):
        """多次调用顺序稳定。"""
        registry = SkillRegistry(_FakeParser())
        registry.register(_skill("c", "C", "# C", "/c.md"))
        registry.register(_skill("a", "A", "# A", "/a.md"))
        for _ in range(3):
            names = [s.name for s in registry.list_skills()]
            self.assertEqual(names, ["a", "c"])


class TestSkillRegistryLoadDirectory(unittest.TestCase):
    """load_directory 方法测试。"""

    FIXTURE_DIR = Path(__file__).parent / "fixtures" / "skill_registry"

    def test_load_directory_str_path(self):
        """root 使用 str。"""
        registry = SkillRegistry(MarkdownSkillParser())
        root = str(self.FIXTURE_DIR / "valid")
        skills = registry.load_directory(root)
        self.assertGreaterEqual(len(skills), 1)

    def test_load_directory_path_object(self):
        """root 使用 Path。"""
        registry = SkillRegistry(MarkdownSkillParser())
        skills = registry.load_directory(self.FIXTURE_DIR / "valid")
        self.assertGreaterEqual(len(skills), 1)

    def test_load_directory_nonexistent_raises(self):
        """目录不存在。"""
        registry = SkillRegistry(MarkdownSkillParser())
        with self.assertRaises(SkillDirectoryError) as ctx:
            registry.load_directory("/nonexistent/dir")
        self.assertIn("不存在", str(ctx.exception))

    def test_load_directory_is_file_raises(self):
        """root 是文件。"""
        registry = SkillRegistry(MarkdownSkillParser())
        with self.assertRaises(SkillDirectoryError) as ctx:
            registry.load_directory(__file__)
        self.assertIn("不是目录", str(ctx.exception))

    def test_load_directory_empty(self):
        """空目录。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = SkillRegistry(MarkdownSkillParser())
            skills = registry.load_directory(tmpdir)
            self.assertEqual(skills, [])

    def test_load_directory_single_file(self):
        """单层 SKILL.md。"""
        registry = SkillRegistry(MarkdownSkillParser())
        skills = registry.load_directory(self.FIXTURE_DIR / "valid")
        self.assertGreaterEqual(len(skills), 1)
        self.assertTrue(registry.exists("valid-skill"))

    def test_load_directory_nested(self):
        """嵌套目录中的 SKILL.md。"""
        registry = SkillRegistry(MarkdownSkillParser())
        skills = registry.load_directory(self.FIXTURE_DIR / "valid")
        # 包含子目录中的 Skill
        self.assertTrue(registry.exists("another-valid"))

    def test_load_directory_multiple_files(self):
        """多个 SKILL.md。"""
        registry = SkillRegistry(MarkdownSkillParser())
        skills = registry.load_directory(self.FIXTURE_DIR / "valid")
        self.assertEqual(len(skills), 2)

    def test_load_directory_ignores_normal_markdown(self):
        """忽略普通 Markdown 文件。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            # 创建普通 markdown
            readme = Path(tmpdir) / "README.md"
            readme.write_text("# README", encoding="utf-8")
            # 创建 SKILL.md
            skill_file = Path(tmpdir) / "SKILL.md"
            skill_file.write_text("---\nname: test\ndescription: Test\n---\n# Test", encoding="utf-8")

            registry = SkillRegistry(MarkdownSkillParser())
            skills = registry.load_directory(tmpdir)
            self.assertEqual(len(skills), 1)
            self.assertEqual(skills[0].name, "test")

    def test_load_directory_ignores_lowercase_skill(self):
        """忽略小写 skill.md。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            # 创建小写 skill.md
            skill_file = Path(tmpdir) / "skill.md"
            skill_file.write_text("---\nname: test\ndescription: Test\n---\n# Test", encoding="utf-8")

            registry = SkillRegistry(MarkdownSkillParser())
            skills = registry.load_directory(tmpdir)
            self.assertEqual(len(skills), 0)

    def test_load_directory_parse_order_stable(self):
        """解析顺序稳定。"""
        registry = SkillRegistry(MarkdownSkillParser())
        skills = registry.load_directory(self.FIXTURE_DIR / "valid")
        names1 = [s.name for s in skills]
        # 再次加载
        registry2 = SkillRegistry(MarkdownSkillParser())
        skills2 = registry2.load_directory(self.FIXTURE_DIR / "valid")
        names2 = [s.name for s in skills2]
        self.assertEqual(names1, names2)

    def test_load_directory_each_file_parsed_once(self):
        """每个文件只解析一次。"""
        parser = _FakeParser()
        registry = SkillRegistry(parser)
        registry.load_directory(self.FIXTURE_DIR / "valid")
        # 两个文件
        self.assertEqual(len(parser.paths), 2)

    def test_source_path_correct(self):
        """source_path 正确。"""
        registry = SkillRegistry(MarkdownSkillParser())
        registry.load_directory(self.FIXTURE_DIR / "valid")
        skill = registry.get("valid-skill")
        self.assertTrue(skill.source_path.name, "SKILL.md")


class TestSkillRegistryAtomicLoad(unittest.TestCase):
    """原子化加载测试。"""

    FIXTURE_DIR = Path(__file__).parent / "fixtures" / "skill_registry"

    def test_parse_failure_preserves_state(self):
        """解析失败，Registry 不变。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            # 创建第一个 SKILL.md（会成功）
            skill1 = Path(tmpdir) / "SKILL.md"
            skill1.write_text(
                "---\nname: good\ndescription: Good\n---\n# Good",
                encoding="utf-8",
            )
            # 创建第二个 SKILL.md（会失败：缺少 name）
            subdir = Path(tmpdir) / "sub"
            subdir.mkdir()
            skill2 = subdir / "SKILL.md"
            skill2.write_text(
                "---\ndescription: No name\n---\n# Bad",
                encoding="utf-8",
            )

            registry = SkillRegistry(MarkdownSkillParser())
            # 第二个文件解析失败，整体应回滚
            with self.assertRaises(SkillParseError):
                registry.load_directory(tmpdir)
            # Registry 应该是空的（原子化失败）
            self.assertEqual(len(registry), 0)

    def test_batch_duplicate_raises(self):
        """批次内两个 Skill 重名。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            # 创建两个同名的 SKILL.md
            for i in range(2):
                p = Path(tmpdir) / f"skill{i}"
                p.mkdir()
                (p / "SKILL.md").write_text(
                    "---\nname: same-name\ndescription: Same\n---\n# Same",
                    encoding="utf-8",
                )

            registry = SkillRegistry(MarkdownSkillParser())
            with self.assertRaises(SkillAlreadyExistsError) as ctx:
                registry.load_directory(tmpdir)
            self.assertEqual(ctx.exception.name, "same-name")
            # Registry 应该为空
            self.assertEqual(len(registry), 0)

    def test_conflict_with_existing_raises(self):
        """与已有 Skill 重名。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_file = Path(tmpdir) / "SKILL.md"
            skill_file.write_text(
                "---\nname: existing\ndescription: Existing\n---\n# Existing",
                encoding="utf-8",
            )

            registry = SkillRegistry(MarkdownSkillParser())
            registry.register(_skill("existing", "Old", "# Old", "/old.md"))

            with self.assertRaises(SkillAlreadyExistsError):
                registry.load_directory(tmpdir)
            # 原有 Skill 仍在
            self.assertEqual(len(registry), 1)
            self.assertTrue(registry.exists("existing"))

    def test_second_directory_failure_keeps_first(self):
        """第二个目录加载失败，第一个目录结果保留。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            dir1 = Path(tmpdir) / "dir1"
            dir2 = Path(tmpdir) / "dir2"
            dir1.mkdir()
            dir2.mkdir()

            (dir1 / "SKILL.md").write_text(
                "---\nname: first\ndescription: First\n---\n# First",
                encoding="utf-8",
            )
            # dir2 有无效文件
            (dir2 / "SKILL.md").write_text(
                "---\ndescription: No name\n---\n# Bad",
                encoding="utf-8",
            )

            registry = SkillRegistry(MarkdownSkillParser())
            registry.load_directory(dir1)
            self.assertTrue(registry.exists("first"))

            # 第二个目录失败
            with self.assertRaises(SkillParseError):
                registry.load_directory(dir2)

            # 第一个目录的结果保留
            self.assertTrue(registry.exists("first"))
            self.assertEqual(len(registry), 1)

    def test_parser_exception_not_swallowed(self):
        """parser 异常不被静默吞掉。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_file = Path(tmpdir) / "SKILL.md"
            skill_file.write_text(
                "---\nname: test\ndescription: Test\n---\n# Test",
                encoding="utf-8",
            )

            # 使用假 parser 抛出自定义异常
            class CustomError(Exception):
                pass

            class CustomParser:
                def parse(self, path):
                    raise CustomError("custom error")

            registry = SkillRegistry(CustomParser())  # type: ignore
            with self.assertRaises(SkillDirectoryError) as ctx:
                registry.load_directory(tmpdir)
            self.assertIn("custom error", str(ctx.exception))

    def test_empty_batch_no_change(self):
        """空批次不改变 Registry。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = SkillRegistry(MarkdownSkillParser())
            registry.register(_skill("pre-existing", "Pre", "# Pre", "/pre.md"))
            skills = registry.load_directory(tmpdir)
            self.assertEqual(skills, [])
            self.assertTrue(registry.exists("pre-existing"))


class TestSkillRegistryIntegration(unittest.TestCase):
    """真实 MarkdownSkillParser 集成测试。"""

    FIXTURE_DIR = Path(__file__).parent / "fixtures" / "skill_registry"

    def test_load_valid_directory(self):
        """加载有效目录。"""
        registry = SkillRegistry(MarkdownSkillParser())
        skills = registry.load_directory(self.FIXTURE_DIR / "valid")
        self.assertEqual(len(skills), 2)

    def test_chinese_content(self):
        """中文内容。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_file = Path(tmpdir) / "SKILL.md"
            skill_file.write_text(
                "---\nname: chinese-test\ndescription: 中文描述测试\n---\n# 中文标题\n\n中文内容",
                encoding="utf-8",
            )

            registry = SkillRegistry(MarkdownSkillParser())
            skills = registry.load_directory(tmpdir)
            self.assertEqual(len(skills), 1)
            skill = registry.get("chinese-test")
            self.assertIn("中文", skill.description)
            self.assertIn("中文", skill.instructions)

    def test_list_get_exists_combined(self):
        """list/get/exists 组合使用。"""
        registry = SkillRegistry(MarkdownSkillParser())
        registry.load_directory(self.FIXTURE_DIR / "valid")

        self.assertTrue(registry.exists("valid-skill"))
        self.assertTrue(registry.exists("another-valid"))

        skill = registry.get("valid-skill")
        self.assertIn("有效", skill.description)

        skills = registry.list_skills()
        self.assertEqual(len(skills), 2)

    def test_skill_parse_error_preserves_path(self):
        """SkillParseError 保留文件路径。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            bad_file = Path(tmpdir) / "SKILL.md"
            bad_file.write_text("---\ndescription: No name\n---\n# Bad", encoding="utf-8")

            registry = SkillRegistry(MarkdownSkillParser())
            with self.assertRaises(SkillParseError) as ctx:
                registry.load_directory(tmpdir)
            self.assertIn("SKILL.md", str(ctx.exception))


class TestSkillRegistryLen(unittest.TestCase):
    """__len__ 测试。"""

    def test_len_empty(self):
        registry = SkillRegistry(_FakeParser())
        self.assertEqual(len(registry), 0)

    def test_len_multiple(self):
        registry = SkillRegistry(_FakeParser())
        registry.register(_skill("a", "A", "# A", "/a.md"))
        registry.register(_skill("b", "B", "# B", "/b.md"))
        self.assertEqual(len(registry), 2)


if __name__ == "__main__":
    unittest.main()
