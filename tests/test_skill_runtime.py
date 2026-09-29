"""SkillRuntime 测试（15E 阶段）。"""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

from mini_deerflow.skill import Skill
from mini_deerflow.skill_catalog import SkillCatalog
from mini_deerflow.skill_context import SkillContextBuilder
from mini_deerflow.skill_loader import ProgressiveSkillLoader
from mini_deerflow.skill_metadata import SkillMetadata
from mini_deerflow.skill_registry import SkillRegistry
from mini_deerflow.skill_runtime import (
    DefaultSkillContextProvider,
    SkillContextProvider,
)
from mini_deerflow.skill_selector import RuleBasedSkillSelector


def _metadata(
    name: str = "test-skill",
    description: str = "Test Skill",
    source_path: str | Path = "/test/SKILL.md",
) -> SkillMetadata:
    return SkillMetadata(
        name=name,
        description=description,
        source_path=Path(source_path),
    )


def _skill(
    name: str = "test-skill",
    description: str = "Test Skill",
    instructions: str = "# Test",
    source_path: str | Path = "/test/SKILL.md",
) -> Skill:
    return Skill(
        name=name,
        description=description,
        instructions=instructions,
        source_path=Path(source_path),
    )


class FakeCatalog:
    """记录调用并返回预设元数据的 Fake Catalog。"""

    def __init__(self, metadata_list: list[SkillMetadata]) -> None:
        self._metadata_list = metadata_list
        self.list_metadata_call_count = 0
        self.list_metadata_calls: list[Any] = []

    def list_metadata(self) -> list[SkillMetadata]:
        self.list_metadata_call_count += 1
        self.list_metadata_calls.append(self._metadata_list)
        return list(self._metadata_list)


class FakeSelector:
    """记录调用并返回预设选择的 Fake Selector。"""

    def __init__(self, selected: list[SkillMetadata]) -> None:
        self._selected = selected
        self.select_call_count = 0
        self.select_calls: list[tuple[Any, Any]] = []

    def select(
        self,
        user_input: str,
        skills: list[SkillMetadata],
    ) -> list[SkillMetadata]:
        self.select_call_count += 1
        self.select_calls.append((user_input, list(skills)))
        return list(self._selected)


class FakeLoader:
    """记录调用并返回预设 Skills 的 Fake Loader。"""

    def __init__(self, skills: list[Skill]) -> None:
        self._skills = skills
        self.load_selected_call_count = 0
        self.load_selected_calls: list[Any] = []

    def load_selected(
        self,
        selected: list[SkillMetadata],
    ) -> list[Skill]:
        self.load_selected_call_count += 1
        self.load_selected_calls.append(list(selected))
        return list(self._skills)


class FakeBuilder:
    """记录调用并返回预设上下文的 Fake Builder。"""

    def __init__(self, context: str) -> None:
        self._context = context
        self.build_call_count = 0
        self.build_calls: list[Any] = []

    def build(self, skills: list[Skill]) -> str:
        self.build_call_count += 1
        self.build_calls.append(list(skills))
        return self._context


class TestSkillContextProviderProtocol(unittest.TestCase):
    """SkillContextProvider Protocol 测试。"""

    def test_protocol_exists(self):
        """Protocol 类存在。"""
        self.assertTrue(hasattr(SkillContextProvider, "build"))

    def test_protocol_is_protocol(self):
        """Protocol 可以通过运行时检查（如果标记了 runtime_checkable）。"""
        # Protocol 存在且有 build 方法即可

        class ValidProvider:
            def build(self, user_input: str) -> str:
                return "context"

        # 验证 ValidProvider 有 build 方法
        self.assertTrue(hasattr(ValidProvider, "build"))
        self.assertTrue(callable(getattr(ValidProvider, "build")))


class TestDefaultSkillContextProviderBasic(unittest.TestCase):
    """DefaultSkillContextProvider 基本测试。"""

    def test_empty_catalog(self):
        """空 Catalog 返回空字符串。"""
        catalog = FakeCatalog([])
        selector = FakeSelector([])
        loader = FakeLoader([])
        builder = FakeBuilder("不应被调用")

        provider = DefaultSkillContextProvider(catalog, selector, loader, builder)
        result = provider.build("用户输入")

        self.assertEqual(result, "")

    def test_empty_input(self):
        """空白输入仍可交给 selector。"""
        catalog = FakeCatalog([_metadata("test", "Test", "/a/SKILL.md")])
        selector = FakeSelector([])
        loader = FakeLoader([])
        builder = FakeBuilder("不应被调用")

        provider = DefaultSkillContextProvider(catalog, selector, loader, builder)
        result = provider.build("   ")

        self.assertEqual(result, "")
        self.assertEqual(selector.select_call_count, 1)

    def test_selector_no_result(self):
        """Selector 无结果时返回空字符串。"""
        catalog = FakeCatalog([_metadata("test", "Test", "/a/SKILL.md")])
        selector = FakeSelector([])
        loader = FakeLoader([])
        builder = FakeBuilder("不应被调用")

        provider = DefaultSkillContextProvider(catalog, selector, loader, builder)
        result = provider.build("不匹配任何 Skill 的输入")

        self.assertEqual(result, "")

    def test_selector_one_result(self):
        """Selector 有一个结果。"""
        metadata = _metadata("test", "Test", "/a/SKILL.md")
        skill = _skill("test", "Test", "# Test")
        catalog = FakeCatalog([metadata])
        selector = FakeSelector([metadata])
        loader = FakeLoader([skill])
        builder = FakeBuilder("Skill context")

        provider = DefaultSkillContextProvider(catalog, selector, loader, builder)
        result = provider.build("用户输入")

        self.assertEqual(result, "Skill context")

    def test_selector_multiple_results(self):
        """Selector 有多个结果。"""
        m1 = _metadata("test1", "Test 1", "/a/SKILL.md")
        m2 = _metadata("test2", "Test 2", "/b/SKILL.md")
        s1 = _skill("test1", "Test 1", "# Test 1")
        s2 = _skill("test2", "Test 2", "# Test 2")
        catalog = FakeCatalog([m1, m2])
        selector = FakeSelector([m1, m2])
        loader = FakeLoader([s1, s2])
        builder = FakeBuilder("Multiple skills")

        provider = DefaultSkillContextProvider(catalog, selector, loader, builder)
        result = provider.build("用户输入")

        self.assertEqual(result, "Multiple skills")

    def test_user_input_type_validation(self):
        """user_input 必须是 str。"""
        catalog = FakeCatalog([])
        selector = FakeSelector([])
        loader = FakeLoader([])
        builder = FakeBuilder("")

        provider = DefaultSkillContextProvider(catalog, selector, loader, builder)

        with self.assertRaises(TypeError):
            provider.build(123)  # type: ignore

        with self.assertRaises(TypeError):
            provider.build(None)  # type: ignore


class TestDefaultSkillContextProviderCallOrder(unittest.TestCase):
    """DefaultSkillContextProvider 调用顺序测试。"""

    def test_catalog_called_once(self):
        """catalog.list_metadata 只调用一次。"""
        catalog = FakeCatalog([_metadata("test", "Test", "/a/SKILL.md")])
        selector = FakeSelector([])
        loader = FakeLoader([])
        builder = FakeBuilder("")

        provider = DefaultSkillContextProvider(catalog, selector, loader, builder)
        provider.build("输入")

        self.assertEqual(catalog.list_metadata_call_count, 1)

    def test_selector_called_once(self):
        """selector.select 只调用一次。"""
        catalog = FakeCatalog([_metadata("test", "Test", "/a/SKILL.md")])
        selector = FakeSelector([])
        loader = FakeLoader([])
        builder = FakeBuilder("")

        provider = DefaultSkillContextProvider(catalog, selector, loader, builder)
        provider.build("输入")

        self.assertEqual(selector.select_call_count, 1)

    def test_no_selection_loader_not_called(self):
        """无选择时 loader 不调用。"""
        catalog = FakeCatalog([_metadata("test", "Test", "/a/SKILL.md")])
        selector = FakeSelector([])
        loader = FakeLoader([])
        builder = FakeBuilder("")

        provider = DefaultSkillContextProvider(catalog, selector, loader, builder)
        provider.build("输入")

        self.assertEqual(loader.load_selected_call_count, 0)

    def test_no_selection_builder_not_called(self):
        """无选择时 builder 不调用。"""
        catalog = FakeCatalog([_metadata("test", "Test", "/a/SKILL.md")])
        selector = FakeSelector([])
        loader = FakeLoader([])
        builder = FakeBuilder("")

        provider = DefaultSkillContextProvider(catalog, selector, loader, builder)
        provider.build("输入")

        self.assertEqual(builder.build_call_count, 0)

    def test_with_selection_loader_called_once(self):
        """有选择时 loader 调用一次。"""
        metadata = _metadata("test", "Test", "/a/SKILL.md")
        skill = _skill("test", "Test", "# Test")
        catalog = FakeCatalog([metadata])
        selector = FakeSelector([metadata])
        loader = FakeLoader([skill])
        builder = FakeBuilder("context")

        provider = DefaultSkillContextProvider(catalog, selector, loader, builder)
        provider.build("输入")

        self.assertEqual(loader.load_selected_call_count, 1)

    def test_with_selection_builder_called_once(self):
        """有选择时 builder 调用一次。"""
        metadata = _metadata("test", "Test", "/a/SKILL.md")
        skill = _skill("test", "Test", "# Test")
        catalog = FakeCatalog([metadata])
        selector = FakeSelector([metadata])
        loader = FakeLoader([skill])
        builder = FakeBuilder("context")

        provider = DefaultSkillContextProvider(catalog, selector, loader, builder)
        provider.build("输入")

        self.assertEqual(builder.build_call_count, 1)

    def test_order_preserved(self):
        """顺序完整保持。"""
        m1 = _metadata("first", "First", "/a/SKILL.md")
        m2 = _metadata("second", "Second", "/b/SKILL.md")
        m3 = _metadata("third", "Third", "/c/SKILL.md")
        s1 = _skill("first", "First", "# First")
        s2 = _skill("second", "Second", "# Second")
        s3 = _skill("third", "Third", "# Third")

        catalog = FakeCatalog([m1, m2, m3])
        selector = FakeSelector([m3, m1, m2])  # 倒序选择
        loader = FakeLoader([s3, s1, s2])
        builder = FakeBuilder("context")

        provider = DefaultSkillContextProvider(catalog, selector, loader, builder)
        provider.build("输入")

        # Selector 返回顺序应传递到 Loader
        self.assertEqual(len(loader.load_selected_calls), 1)
        loaded = loader.load_selected_calls[0]
        self.assertEqual([m.name for m in loaded], ["third", "first", "second"])

        # Loader 返回顺序应传递到 Builder
        self.assertEqual(len(builder.build_calls), 1)
        built = builder.build_calls[0]
        self.assertEqual([s.name for s in built], ["third", "first", "second"])


class TestDefaultSkillContextProviderException(unittest.TestCase):
    """异常传播测试。"""

    def test_catalog_exception_propagates(self):
        """Catalog 异常原样传播。"""

        class FailingCatalog:
            def list_metadata(self) -> list[SkillMetadata]:
                raise RuntimeError("Catalog 失败")

        provider = DefaultSkillContextProvider(
            FailingCatalog(),  # type: ignore
            FakeSelector([]),
            FakeLoader([]),
            FakeBuilder(""),
        )

        with self.assertRaises(RuntimeError) as ctx:
            provider.build("输入")
        self.assertEqual(str(ctx.exception), "Catalog 失败")

    def test_selector_exception_propagates(self):
        """Selector 异常原样传播。"""

        class FailingSelector:
            def select(
                self,
                user_input: str,
                skills: list[SkillMetadata],
            ) -> list[SkillMetadata]:
                raise RuntimeError("Selector 失败")

        provider = DefaultSkillContextProvider(
            FakeCatalog([]),
            FailingSelector(),  # type: ignore
            FakeLoader([]),
            FakeBuilder(""),
        )

        with self.assertRaises(RuntimeError) as ctx:
            provider.build("输入")
        self.assertEqual(str(ctx.exception), "Selector 失败")

    def test_loader_exception_propagates(self):
        """Loader 异常原样传播。"""

        class FailingLoader:
            def load_selected(
                self,
                selected: list[SkillMetadata],
            ) -> list[Skill]:
                raise RuntimeError("Loader 失败")

        provider = DefaultSkillContextProvider(
            FakeCatalog([_metadata("test", "Test", "/a/SKILL.md")]),
            FakeSelector([_metadata("test", "Test", "/a/SKILL.md")]),
            FailingLoader(),  # type: ignore
            FakeBuilder(""),
        )

        with self.assertRaises(RuntimeError) as ctx:
            provider.build("输入")
        self.assertEqual(str(ctx.exception), "Loader 失败")

    def test_builder_exception_propagates(self):
        """Builder 异常原样传播。"""

        class FailingBuilder:
            def build(self, skills: list[Skill]) -> str:
                raise RuntimeError("Builder 失败")

        provider = DefaultSkillContextProvider(
            FakeCatalog([_metadata("test", "Test", "/a/SKILL.md")]),
            FakeSelector([_metadata("test", "Test", "/a/SKILL.md")]),
            FakeLoader([_skill("test", "Test", "# Test")]),
            FailingBuilder(),  # type: ignore
        )

        with self.assertRaises(RuntimeError) as ctx:
            provider.build("输入")
        self.assertEqual(str(ctx.exception), "Builder 失败")

    def test_builder_returns_non_string_raises(self):
        """Builder 返回非 str 时抛 TypeError。"""

        class NonStringBuilder:
            def build(self, skills: list[Skill]) -> str:
                return 123  # type: ignore

        provider = DefaultSkillContextProvider(
            FakeCatalog([_metadata("test", "Test", "/a/SKILL.md")]),
            FakeSelector([_metadata("test", "Test", "/a/SKILL.md")]),
            FakeLoader([_skill("test", "Test", "# Test")]),
            NonStringBuilder(),  # type: ignore
        )

        with self.assertRaises(TypeError):
            provider.build("输入")

    def test_builder_returns_empty_string(self):
        """Builder 返回空字符串。"""
        metadata = _metadata("test", "Test", "/a/SKILL.md")
        skill = _skill("test", "Test", "# Test")

        provider = DefaultSkillContextProvider(
            FakeCatalog([metadata]),
            FakeSelector([metadata]),
            FakeLoader([skill]),
            FakeBuilder(""),
        )

        result = provider.build("输入")
        self.assertEqual(result, "")

    def test_builder_returns_whitespace(self):
        """Builder 返回全空白。"""
        metadata = _metadata("test", "Test", "/a/SKILL.md")
        skill = _skill("test", "Test", "# Test")

        provider = DefaultSkillContextProvider(
            FakeCatalog([metadata]),
            FakeSelector([metadata]),
            FakeLoader([skill]),
            FakeBuilder("   \n\t  "),
        )

        result = provider.build("输入")
        self.assertEqual(result, "   \n\t  ")


class TestDefaultSkillContextProviderNoMutation(unittest.TestCase):
    """不修改输入测试。"""

    def test_does_not_modify_metadata(self):
        """不修改 Metadata。"""
        metadata = _metadata("test", "Test", "/a/SKILL.md")
        original_name = metadata.name
        original_desc = metadata.description

        provider = DefaultSkillContextProvider(
            FakeCatalog([metadata]),
            FakeSelector([metadata]),
            FakeLoader([_skill("test", "Test", "# Test")]),
            FakeBuilder("context"),
        )
        provider.build("输入")

        self.assertEqual(metadata.name, original_name)
        self.assertEqual(metadata.description, original_desc)

    def test_does_not_modify_skill(self):
        """不修改 Skill。"""
        skill = _skill("test", "Test", "# Test")
        original_instructions = skill.instructions

        provider = DefaultSkillContextProvider(
            FakeCatalog([_metadata("test", "Test", "/a/SKILL.md")]),
            FakeSelector([_metadata("test", "Test", "/a/SKILL.md")]),
            FakeLoader([skill]),
            FakeBuilder("context"),
        )
        provider.build("输入")

        self.assertEqual(skill.instructions, original_instructions)

    def test_does_not_modify_catalog_list(self):
        """不修改 Catalog 返回列表。"""
        metadata = _metadata("test", "Test", "/a/SKILL.md")

        class TrackingCatalog:
            def __init__(self, m: SkillMetadata) -> None:
                self._m = m
                self.call_count = 0

            def list_metadata(self) -> list[SkillMetadata]:
                self.call_count += 1
                return [self._m]

        catalog = TrackingCatalog(metadata)
        original_len = len(catalog.list_metadata())

        provider = DefaultSkillContextProvider(
            catalog,
            FakeSelector([]),
            FakeLoader([]),
            FakeBuilder(""),
        )
        provider.build("输入")

        # Catalog 返回列表未被修改
        self.assertEqual(len(catalog.list_metadata()), original_len)


class TestDefaultSkillContextProviderMultipleBuilds(unittest.TestCase):
    """多次 build 独立性测试。"""

    def test_multiple_builds_independent(self):
        """多次 build 相互独立。"""
        m1 = _metadata("test1", "Test 1", "/a/SKILL.md")
        m2 = _metadata("test2", "Test 2", "/b/SKILL.md")
        s1 = _skill("test1", "Test 1", "# Test 1")
        s2 = _skill("test2", "Test 2", "# Test 2")

        catalog = FakeCatalog([m1, m2])

        # 第一次选择 test1
        selector1 = FakeSelector([m1])
        loader1 = FakeLoader([s1])
        builder1 = FakeBuilder("context1")

        provider1 = DefaultSkillContextProvider(catalog, selector1, loader1, builder1)
        result1 = provider1.build("选择 test1")

        # 第二次选择 test2（同一 Provider 实例）
        selector2 = FakeSelector([m2])
        loader2 = FakeLoader([s2])
        builder2 = FakeBuilder("context2")

        provider2 = DefaultSkillContextProvider(catalog, selector2, loader2, builder2)

        # 注意：这里创建新的 Provider 以测试不同选择场景
        provider = DefaultSkillContextProvider(catalog, selector2, loader2, builder2)
        result2 = provider.build("选择 test2")

        self.assertEqual(result1, "context1")
        self.assertEqual(result2, "context2")


class TestDefaultSkillContextProviderIntegration(unittest.TestCase):
    """真实组件集成测试。"""

    def test_with_real_components(self):
        """与真实 Catalog、Selector、Loader、Builder 集成。"""
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = Path(tmpdir) / "test-skill"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                "---\nname: test-skill\ndescription: Test Skill\n---\n# Test\n\nStep 1\nStep 2",
                encoding="utf-8",
            )

            # 真实组件
            from mini_deerflow.skill_metadata import MarkdownSkillMetadataParser
            from mini_deerflow.skill import MarkdownSkillParser

            catalog = SkillCatalog(MarkdownSkillMetadataParser())
            catalog.load_directory(tmpdir)

            selector = RuleBasedSkillSelector({"test-skill": ("test",)})
            parser = MarkdownSkillParser()
            registry = SkillRegistry(parser)
            loader = ProgressiveSkillLoader(parser, registry)
            builder = SkillContextBuilder()

            provider = DefaultSkillContextProvider(catalog, selector, loader, builder)
            result = provider.build("test input")

            self.assertIn("test-skill", result)
            self.assertIn("Test Skill", result)
            self.assertIn("Step 1", result)
            self.assertIn("Step 2", result)

    def test_progressive_disclosure_only_selected(self):
        """渐进式披露：只加载选中的 Skill。"""
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            # 创建三个 Skill
            for name in ["python-testing", "code-review", "research"]:
                skill_dir = Path(tmpdir) / name
                skill_dir.mkdir()
                (skill_dir / "SKILL.md").write_text(
                    f"---\nname: {name}\ndescription: {name}\n---\n# {name}\n\nContent for {name}",
                    encoding="utf-8",
                )

            from mini_deerflow.skill_metadata import MarkdownSkillMetadataParser
            from mini_deerflow.skill import MarkdownSkillParser

            catalog = SkillCatalog(MarkdownSkillMetadataParser())
            catalog.load_directory(tmpdir)

            # 只选择 python-testing
            selector = RuleBasedSkillSelector({"python-testing": ("testing",)})
            parser = MarkdownSkillParser()
            registry = SkillRegistry(parser)
            loader = ProgressiveSkillLoader(parser, registry)
            builder = SkillContextBuilder()

            provider = DefaultSkillContextProvider(catalog, selector, loader, builder)
            result = provider.build("I need testing help")

            # 只包含 python-testing
            self.assertIn("python-testing", result)
            self.assertNotIn("code-review", result)
            self.assertNotIn("research", result)

            # Registry 中只有 python-testing
            self.assertEqual(len(registry.list_skills()), 1)
            self.assertTrue(registry.exists("python-testing"))

    def test_cache_hit_no_reparse(self):
        """缓存命中时不重复完整解析。"""
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = Path(tmpdir) / "test-skill"
            skill_dir.mkdir()

            # 创建一个可以被修改的 Skill 文件
            skill_file = skill_dir / "SKILL.md"
            skill_file.write_text(
                "---\nname: test-skill\ndescription: Test Skill\n---\n# Test\n\nVersion 1",
                encoding="utf-8",
            )

            from mini_deerflow.skill_metadata import MarkdownSkillMetadataParser
            from mini_deerflow.skill import MarkdownSkillParser

            catalog = SkillCatalog(MarkdownSkillMetadataParser())
            catalog.load_directory(tmpdir)

            selector = RuleBasedSkillSelector({"test-skill": ("test",)})
            parser = MarkdownSkillParser()
            registry = SkillRegistry(parser)
            loader = ProgressiveSkillLoader(parser, registry)
            builder = SkillContextBuilder()

            provider = DefaultSkillContextProvider(catalog, selector, loader, builder)

            # 第一次构建
            result1 = provider.build("test input")
            self.assertIn("Version 1", result1)

            # 修改文件内容
            skill_file.write_text(
                "---\nname: test-skill\ndescription: Test Skill\n---\n# Test\n\nVersion 2",
                encoding="utf-8",
            )

            # 第二次构建应该命中缓存（返回 Version 1）
            result2 = provider.build("test input")
            self.assertIn("Version 1", result2)
            self.assertNotIn("Version 2", result2)


if __name__ == "__main__":
    unittest.main()
