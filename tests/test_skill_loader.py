"""SkillLoader 和 ProgressiveSkillLoader 测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mini_deerflow.skill import Skill
from mini_deerflow.skill_metadata import SkillMetadata
from mini_deerflow.skill_registry import SkillRegistry
from mini_deerflow.skill_loader import (
    ProgressiveSkillLoader,
    SkillLoader,
    SkillMetadataMismatchError,
)
from mini_deerflow.skill import MarkdownSkillParser


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


class _FakeParser:
    """假解析器，记录调用。"""

    def __init__(self) -> None:
        self.parse_count = 0
        self.parsed_paths: list[Path] = []
        self._results: dict[str, Skill] = {}

    def set_result(self, path: str | Path, skill: Skill) -> None:
        """预设解析结果。"""
        self._results[str(Path(path).resolve())] = skill

    def parse(self, path: str | Path) -> Skill:
        p = Path(path).resolve()
        self.parse_count += 1
        self.parsed_paths.append(p)
        if str(p) in self._results:
            return self._results[str(p)]
        # 没有预设结果时抛出错误（测试应该配置 set_result）
        raise ValueError(f"No result configured for {p}")


class TestSkillLoaderProtocol(unittest.TestCase):
    """SkillLoader Protocol 测试。"""

    def test_progressive_loader_implements_interface(self):
        """ProgressiveSkillLoader 实现所需接口。"""
        from mini_deerflow.skill_loader import SkillLoader
        from mini_deerflow.skill import MarkdownSkillParser
        from mini_deerflow.skill_registry import SkillRegistry

        parser = MarkdownSkillParser()
        registry = SkillRegistry(parser)
        loader = ProgressiveSkillLoader(parser, registry)
        # 验证 load_selected 方法存在
        self.assertTrue(hasattr(loader, "load_selected"))
        self.assertTrue(callable(getattr(loader, "load_selected")))


class TestSkillRegistryRegisterMany(unittest.TestCase):
    """register_many 方法测试。"""

    def test_empty_batch(self):
        """空批次。"""
        registry = SkillRegistry(_FakeParser())
        registry.register(_skill("existing", "E", "# E", "/existing.md"))
        registry.register_many([])
        self.assertEqual(len(registry), 1)
        self.assertTrue(registry.exists("existing"))

    def test_single_skill(self):
        """单个 Skill。"""
        registry = SkillRegistry(_FakeParser())
        registry.register_many([_skill("test", "Test", "# Test", "/test.md")])
        self.assertEqual(len(registry), 1)
        self.assertTrue(registry.exists("test"))

    def test_multiple_skills(self):
        """多个 Skill。"""
        registry = SkillRegistry(_FakeParser())
        registry.register_many([
            _skill("a", "A", "# A", "/a.md"),
            _skill("b", "B", "# B", "/b.md"),
        ])
        self.assertEqual(len(registry), 2)

    def test_non_sequence_raises(self):
        """非 Sequence。"""
        registry = SkillRegistry(_FakeParser())
        with self.assertRaises(TypeError):
            registry.register_many(123)

    def test_string_raises(self):
        """str 非法。"""
        registry = SkillRegistry(_FakeParser())
        with self.assertRaises(TypeError):
            registry.register_many("not valid")

    def test_contains_non_skill_raises(self):
        """包含非 Skill 元素。"""
        registry = SkillRegistry(_FakeParser())
        with self.assertRaises(TypeError):
            registry.register_many([_skill(), "not a skill"])

    def test_duplicate_in_batch_raises(self):
        """批次内重复名称。"""
        registry = SkillRegistry(_FakeParser())
        with self.assertRaises(Exception) as ctx:
            registry.register_many([
                _skill("test", "A", "# A", "/a.md"),
                _skill("test", "B", "# B", "/b.md"),
            ])
        self.assertEqual(ctx.exception.name, "test")

    def test_conflict_with_existing_raises(self):
        """与已有 Skill 冲突。"""
        registry = SkillRegistry(_FakeParser())
        registry.register(_skill("existing", "E", "# E", "/e.md"))
        with self.assertRaises(Exception):
            registry.register_many([_skill("existing", "N", "# N", "/n.md")])

    def test_failure_preserves_state(self):
        """失败后状态不变。"""
        registry = SkillRegistry(_FakeParser())
        registry.register(_skill("first", "F", "# F", "/f.md"))
        try:
            registry.register_many([
                _skill("second", "S", "# S", "/s.md"),
                _skill("first", "D", "# D", "/d.md"),  # 冲突
            ])
        except Exception:
            pass
        self.assertEqual(len(registry), 1)
        self.assertTrue(registry.exists("first"))

    def test_success_order_stable(self):
        """成功后顺序稳定。"""
        registry = SkillRegistry(_FakeParser())
        registry.register_many([
            _skill("z", "Z", "# Z", "/z.md"),
            _skill("a", "A", "# A", "/a.md"),
        ])
        names = [s.name for s in registry.list_skills()]
        self.assertEqual(names, ["a", "z"])

    def test_does_not_modify_input(self):
        """不修改输入。"""
        skills = [_skill("test", "Test", "# Test", "/test.md")]
        registry = SkillRegistry(_FakeParser())
        registry.register_many(skills)
        self.assertEqual(len(skills), 1)


class TestProgressiveSkillLoader(unittest.TestCase):
    """ProgressiveSkillLoader 测试。"""

    def test_empty_selection(self):
        """空选择。"""
        parser = _FakeParser()
        registry = SkillRegistry(parser)
        loader = ProgressiveSkillLoader(parser, registry)
        result = loader.load_selected([])
        self.assertEqual(result, [])

    def test_single_metadata(self):
        """单个 Metadata。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_file = Path(tmpdir) / "SKILL.md"
            skill_file.write_text(
                "---\nname: test\ndescription: Test\n---\n# Test",
                encoding="utf-8",
            )

            parser = MarkdownSkillParser()
            registry = SkillRegistry(parser)
            loader = ProgressiveSkillLoader(parser, registry)

            meta = _meta("test", "Test", skill_file)
            result = loader.load_selected([meta])

            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].name, "test")
            self.assertEqual(result[0].instructions, "# Test")

    def test_multiple_metadata(self):
        """多个 Metadata。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            skill1 = Path(tmpdir) / "SKILL.md"
            skill1.write_text(
                "---\nname: first\ndescription: First\n---\n# First",
                encoding="utf-8",
            )
            subdir = Path(tmpdir) / "sub"
            subdir.mkdir()
            skill2 = subdir / "SKILL.md"
            skill2.write_text(
                "---\nname: second\ndescription: Second\n---\n# Second",
                encoding="utf-8",
            )

            parser = MarkdownSkillParser()
            registry = SkillRegistry(parser)
            loader = ProgressiveSkillLoader(parser, registry)

            meta1 = _meta("first", "First", skill1)
            meta2 = _meta("second", "Second", skill2)
            result = loader.load_selected([meta1, meta2])

            self.assertEqual(len(result), 2)
            self.assertEqual(result[0].name, "first")
            self.assertEqual(result[1].name, "second")

    def test_preserves_selection_order(self):
        """保持选择顺序。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            skill1 = Path(tmpdir) / "SKILL.md"
            skill1.write_text(
                "---\nname: first\ndescription: First\n---\n# First",
                encoding="utf-8",
            )
            skill2 = Path(tmpdir) / "second.md"
            skill2.write_text(
                "---\nname: second\ndescription: Second\n---\n# Second",
                encoding="utf-8",
            )

            parser = MarkdownSkillParser()
            registry = SkillRegistry(parser)
            loader = ProgressiveSkillLoader(parser, registry)

            # 反序传入
            meta2 = _meta("second", "Second", skill2)
            meta1 = _meta("first", "First", skill1)
            result = loader.load_selected([meta2, meta1])

            # 应该保持传入顺序
            self.assertEqual(result[0].name, "second")
            self.assertEqual(result[1].name, "first")

    def test_non_sequence_raises(self):
        """非 Sequence。"""
        parser = _FakeParser()
        registry = SkillRegistry(parser)
        loader = ProgressiveSkillLoader(parser, registry)
        with self.assertRaises(TypeError):
            loader.load_selected(123)

    def test_string_raises(self):
        """str 非法。"""
        parser = _FakeParser()
        registry = SkillRegistry(parser)
        loader = ProgressiveSkillLoader(parser, registry)
        with self.assertRaises(TypeError):
            loader.load_selected("not valid")

    def test_non_metadata_element_raises(self):
        """非 Metadata 元素。"""
        parser = _FakeParser()
        registry = SkillRegistry(parser)
        loader = ProgressiveSkillLoader(parser, registry)
        with self.assertRaises(ValueError):
            loader.load_selected([_skill()])

    def test_duplicate_metadata_name_raises(self):
        """Metadata 名称重复。"""
        parser = _FakeParser()
        registry = SkillRegistry(parser)
        loader = ProgressiveSkillLoader(parser, registry)
        meta = _meta("test", "Test", "/test.md")
        with self.assertRaises(ValueError):
            loader.load_selected([meta, meta])

    def test_loads_only_selected_paths(self):
        """只加载选中的路径。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            selected = Path(tmpdir) / "selected" / "SKILL.md"
            selected.parent.mkdir()
            selected.write_text(
                "---\nname: selected\ndescription: Selected\n---\n# Selected",
                encoding="utf-8",
            )

            unselected = Path(tmpdir) / "unselected" / "SKILL.md"
            unselected.parent.mkdir()
            unselected.write_text(
                "---\nname: unselected\ndescription: Unselected\n---\n# Unselected",
                encoding="utf-8",
            )

            parser = _FakeParser()
            parser.set_result(selected, _skill("selected", "Selected", "# Selected", selected))
            registry = SkillRegistry(parser)
            loader = ProgressiveSkillLoader(parser, registry)

            # 只选择 selected
            meta = _meta("selected", "Selected", selected)
            loader.load_selected([meta])

            # 只解析了一个文件
            self.assertEqual(parser.parse_count, 1)
            self.assertEqual(str(selected), str(parser.parsed_paths[0]))

    def test_each_path_parsed_once(self):
        """每个路径只解析一次。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_file = Path(tmpdir) / "SKILL.md"
            skill_file.write_text(
                "---\nname: test\ndescription: Test\n---\n# Test",
                encoding="utf-8",
            )

            parser = _FakeParser()
            parser.set_result(skill_file, _skill("test", "Test", "# Test", skill_file))
            registry = SkillRegistry(parser)
            loader = ProgressiveSkillLoader(parser, registry)

            meta = _meta("test", "Test", skill_file)

            # 第一次加载
            loader.load_selected([meta])
            self.assertEqual(parser.parse_count, 1)

            # 第二次加载同一 Metadata
            loader.load_selected([meta])
            self.assertEqual(parser.parse_count, 1)  # 不应增加

    def test_cache_hit(self):
        """缓存命中。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_file = Path(tmpdir) / "SKILL.md"
            skill_file.write_text(
                "---\nname: test\ndescription: Test\n---\n# Test",
                encoding="utf-8",
            )

            parser = MarkdownSkillParser()
            registry = SkillRegistry(parser)
            loader = ProgressiveSkillLoader(parser, registry)

            meta = _meta("test", "Test", skill_file)

            # 第一次加载
            result1 = loader.load_selected([meta])
            self.assertEqual(len(registry), 1)

            # 第二次加载应命中缓存
            result2 = loader.load_selected([meta])
            self.assertEqual(result1[0], result2[0])

    def test_cache_hit_does_not_call_parser(self):
        """缓存命中不调用 parser。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_file = Path(tmpdir) / "SKILL.md"
            skill_file.write_text(
                "---\nname: test\ndescription: Test\n---\n# Test",
                encoding="utf-8",
            )

            parser = _FakeParser()
            parser.set_result(skill_file, _skill("test", "Test", "# Test", skill_file))
            registry = SkillRegistry(parser)
            loader = ProgressiveSkillLoader(parser, registry)

            meta = _meta("test", "Test", skill_file)

            # 第一次加载
            loader.load_selected([meta])
            parse_count_after_first = parser.parse_count

            # 第二次加载
            loader.load_selected([meta])

            # parser 调用次数不应增加
            self.assertEqual(parser.parse_count, parse_count_after_first)

    def test_partial_cache_partial_load(self):
        """部分缓存、部分新加载。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            skill1 = Path(tmpdir) / "first.md"
            skill1.write_text(
                "---\nname: first\ndescription: First\n---\n# First",
                encoding="utf-8",
            )
            skill2 = Path(tmpdir) / "second.md"
            skill2.write_text(
                "---\nname: second\ndescription: Second\n---\n# Second",
                encoding="utf-8",
            )

            parser = _FakeParser()
            parser.set_result(skill1, _skill("first", "First", "# First", skill1))
            parser.set_result(skill2, _skill("second", "Second", "# Second", skill2))
            registry = SkillRegistry(parser)
            loader = ProgressiveSkillLoader(parser, registry)

            meta1 = _meta("first", "First", skill1)
            meta2 = _meta("second", "Second", skill2)

            # 只加载第一个
            loader.load_selected([meta1])
            self.assertEqual(parser.parse_count, 1)

            # 再加载两个（第一个命中缓存，第二个新加载）
            loader.load_selected([meta1, meta2])
            self.assertEqual(parser.parse_count, 2)

    def test_returns_new_list(self):
        """返回新 list。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_file = Path(tmpdir) / "SKILL.md"
            skill_file.write_text(
                "---\nname: test\ndescription: Test\n---\n# Test",
                encoding="utf-8",
            )

            parser = _FakeParser()
            parser.set_result(skill_file, _skill("test", "Test", "# Test", skill_file))
            registry = SkillRegistry(parser)
            loader = ProgressiveSkillLoader(parser, registry)

            meta = _meta("test", "Test", skill_file)
            result = loader.load_selected([meta])

            self.assertIsNot(result, [meta])

    def test_does_not_modify_selected(self):
        """不修改 selected。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_file = Path(tmpdir) / "SKILL.md"
            skill_file.write_text(
                "---\nname: test\ndescription: Test\n---\n# Test",
                encoding="utf-8",
            )

            parser = MarkdownSkillParser()
            registry = SkillRegistry(parser)
            loader = ProgressiveSkillLoader(parser, registry)

            meta = _meta("test", "Test", skill_file)
            meta_copy = _meta("test", "Test", skill_file)

            loader.load_selected([meta])

            # Metadata 应该没有被修改
            self.assertEqual(meta.name, meta_copy.name)

    def test_name_mismatch_raises(self):
        """name 不一致。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_file = Path(tmpdir) / "SKILL.md"
            skill_file.write_text(
                "---\nname: actual-name\ndescription: Test\n---\n# Test",
                encoding="utf-8",
            )

            parser = MarkdownSkillParser()
            registry = SkillRegistry(parser)
            loader = ProgressiveSkillLoader(parser, registry)

            # Metadata 说 name 是 "wrong-name"
            meta = _meta("wrong-name", "Test", skill_file)

            with self.assertRaises(SkillMetadataMismatchError) as ctx:
                loader.load_selected([meta])
            self.assertEqual(ctx.exception.name, "wrong-name")
            self.assertEqual(ctx.exception.field, "name")

    def test_description_mismatch_raises(self):
        """description 不一致。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_file = Path(tmpdir) / "SKILL.md"
            skill_file.write_text(
                "---\nname: test\ndescription: Actual desc\n---\n# Test",
                encoding="utf-8",
            )

            parser = MarkdownSkillParser()
            registry = SkillRegistry(parser)
            loader = ProgressiveSkillLoader(parser, registry)

            # Metadata 说 description 是 "Wrong desc"
            meta = _meta("test", "Wrong desc", skill_file)

            with self.assertRaises(SkillMetadataMismatchError) as ctx:
                loader.load_selected([meta])
            self.assertEqual(ctx.exception.field, "description")

    def test_source_path_mismatch_raises(self):
        """source_path 不一致。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            # 创建两个不同的路径
            actual_file = Path(tmpdir) / "actual.md"
            actual_file.write_text(
                "---\nname: test\ndescription: Test\n---\n# Test",
                encoding="utf-8",
            )
            wrong_path = Path(tmpdir) / "wrong.md"
            wrong_path.write_text(
                "---\nname: test\ndescription: Test\n---\n# Test",
                encoding="utf-8",
            )

            parser = _FakeParser()
            # parser 返回的 Skill 的 source_path 是 actual_file，但被调用时传的是 wrong_path
            parser.set_result(
                wrong_path,
                _skill("test", "Test", "# Test", actual_file),  # 返回的路径与调用参数不同
            )

            registry = SkillRegistry(parser)
            loader = ProgressiveSkillLoader(parser, registry)

            # Metadata 指向 wrong_path
            meta = _meta("test", "Test", wrong_path)

            with self.assertRaises(SkillMetadataMismatchError) as ctx:
                loader.load_selected([meta])
            self.assertEqual(ctx.exception.field, "source_path")

    def test_batch_atomicity(self):
        """批量原子性。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            skill1 = Path(tmpdir) / "first.md"
            skill1.write_text(
                "---\nname: first\ndescription: First\n---\n# First",
                encoding="utf-8",
            )
            skill2 = Path(tmpdir) / "second.md"
            skill2.write_text(
                "---\nname: second\ndescription: Second\n---\n# Second",
                encoding="utf-8",
            )

            parser = _FakeParser()
            # 让第二个解析失败
            def bad_parse(path):
                if "second" in str(path):
                    raise ValueError("Simulated parse error")
                return Skill(
                    name="first",
                    description="First",
                    instructions="# First",
                    source_path=Path(path),
                )
            parser.parse = bad_parse

            registry = SkillRegistry(parser)
            loader = ProgressiveSkillLoader(parser, registry)

            meta1 = _meta("first", "First", skill1)
            meta2 = _meta("second", "Second", skill2)

            with self.assertRaises(ValueError):
                loader.load_selected([meta1, meta2])

            # Registry 应该为空
            self.assertEqual(len(registry), 0)


class TestProgressiveSkillLoaderEndToEnd(unittest.TestCase):
    """端到端测试。"""

    def test_full_pipeline(self):
        """完整流程。"""
        from mini_deerflow.skill_catalog import SkillCatalog
        from mini_deerflow.skill_metadata import MarkdownSkillMetadataParser
        from mini_deerflow.skill_selector import RuleBasedSkillSelector

        with tempfile.TemporaryDirectory() as tmpdir:
            # 创建测试 Skill
            selected_dir = Path(tmpdir) / "selected"
            selected_dir.mkdir()
            (selected_dir / "SKILL.md").write_text(
                "---\nname: test-skill\ndescription: Test Skill\n---\n# Test Skill\n\nThis is test.",
                encoding="utf-8",
            )

            unselected_dir = Path(tmpdir) / "unselected"
            unselected_dir.mkdir()
            (unselected_dir / "SKILL.md").write_text(
                "---\nname: unselected\ndescription: Unselected\n---\n# Unselected\n\nShould not load.",
                encoding="utf-8",
            )

            # 1. 扫描元数据
            catalog = SkillCatalog(MarkdownSkillMetadataParser())
            catalog.load_directory(tmpdir)

            # 2. 选择
            selector = RuleBasedSkillSelector(
                {"test-skill": ("test",)}, max_skills=3
            )
            selected = selector.select("test", catalog.list_metadata())

            self.assertEqual(len(selected), 1)
            self.assertEqual(selected[0].name, "test-skill")

            # 3. 加载
            parser = _FakeParser()
            real_parser = MarkdownSkillParser()
            registry = SkillRegistry(real_parser)
            loader = ProgressiveSkillLoader(real_parser, registry)

            skills = loader.load_selected(selected)

            self.assertEqual(len(skills), 1)
            self.assertEqual(skills[0].name, "test-skill")
            self.assertIn("This is test", skills[0].instructions)

            # 4. 验证未选中项未被加载
            self.assertFalse(registry.exists("unselected"))

    def test_multiple_skills_order_preserved(self):
        """多个 Skill 顺序保持。"""
        from mini_deerflow.skill_catalog import SkillCatalog
        from mini_deerflow.skill_metadata import MarkdownSkillMetadataParser
        from mini_deerflow.skill_selector import RuleBasedSkillSelector

        with tempfile.TemporaryDirectory() as tmpdir:
            # 创建多个 Skill
            for i, name in enumerate(["alpha", "beta", "gamma"]):
                skill_dir = Path(tmpdir) / name
                skill_dir.mkdir()
                (skill_dir / "SKILL.md").write_text(
                    f"---\nname: {name}\ndescription: {name}\n---\n# {name}",
                    encoding="utf-8",
                )

            # 扫描并选择多个
            catalog = SkillCatalog(MarkdownSkillMetadataParser())
            catalog.load_directory(tmpdir)

            selector = RuleBasedSkillSelector(
                {"alpha": ("a",), "beta": ("b",), "gamma": ("g",)},
                max_skills=3,
            )
            selected = selector.select("a b g", catalog.list_metadata())

            parser = MarkdownSkillParser()
            registry = SkillRegistry(parser)
            loader = ProgressiveSkillLoader(parser, registry)

            skills = loader.load_selected(selected)

            # 顺序保持
            self.assertEqual([s.name for s in skills], ["alpha", "beta", "gamma"])


if __name__ == "__main__":
    unittest.main()
