"""SkillCatalog 测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mini_deerflow.skill_metadata import MarkdownSkillMetadataParser, SkillMetadata
from mini_deerflow.skill_catalog import (
    SkillAlreadyExistsError,
    SkillCatalog,
    SkillDirectoryError,
    SkillNotFoundError,
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


class _FakeMetadataParser:
    """用于测试的假解析器。"""

    def __init__(self) -> None:
        self.paths: list[Path] = []
        self._metadata: dict[str, SkillMetadata] = {}

    def register_parse_result(self, metadata: SkillMetadata) -> None:
        """预注册解析结果。"""
        self._metadata[str(metadata.source_path)] = metadata

    def parse(self, path: str | Path) -> SkillMetadata:
        p = Path(path)
        self.paths.append(p)
        if str(p) in self._metadata:
            return self._metadata[str(p)]
        # 默认行为：创建 Metadata，name 取自父目录
        parent = p.parent.name.lower().replace("-", "_")
        return SkillMetadata(
            name=parent or "skill",
            description=f"Parsed from {p}",
            source_path=p.resolve(),
        )


class TestSkillCatalogRegister(unittest.TestCase):
    """register 方法测试。"""

    def test_register_single(self):
        """注册单个元数据。"""
        catalog = SkillCatalog(_FakeMetadataParser())
        catalog.register(_meta("test", "描述", "/a/SKILL.md"))
        self.assertEqual(len(catalog), 1)
        self.assertTrue(catalog.exists("test"))

    def test_register_multiple(self):
        """注册多个元数据。"""
        catalog = SkillCatalog(_FakeMetadataParser())
        catalog.register(_meta("skill-a", "A", "/a.md"))
        catalog.register(_meta("skill-b", "B", "/b.md"))
        self.assertEqual(len(catalog), 2)

    def test_register_non_metadata_raises(self):
        """注册非 SkillMetadata 类型。"""
        catalog = SkillCatalog(_FakeMetadataParser())
        with self.assertRaises(TypeError):
            catalog.register("not metadata")  # type: ignore

    def test_duplicate_name_raises(self):
        """重复名称。"""
        catalog = SkillCatalog(_FakeMetadataParser())
        catalog.register(_meta("test", "描述1", "/a.md"))
        with self.assertRaises(SkillAlreadyExistsError) as ctx:
            catalog.register(_meta("test", "描述2", "/b.md"))
        self.assertEqual(ctx.exception.name, "test")

    def test_register_failure_preserves_state(self):
        """注册失败后状态不变。"""
        catalog = SkillCatalog(_FakeMetadataParser())
        catalog.register(_meta("first", "First", "/first.md"))
        try:
            catalog.register(_meta("first", "Dupe", "/dupe.md"))
        except SkillAlreadyExistsError:
            pass
        self.assertEqual(len(catalog), 1)


class TestSkillCatalogGet(unittest.TestCase):
    """get 方法测试。"""

    def test_get_existing(self):
        """获取存在的元数据。"""
        catalog = SkillCatalog(_FakeMetadataParser())
        meta = _meta("test", "描述", "/a.md")
        catalog.register(meta)
        retrieved = catalog.get("test")
        self.assertIs(retrieved, meta)

    def test_get_nonexistent_raises(self):
        """获取不存在的元数据。"""
        catalog = SkillCatalog(_FakeMetadataParser())
        with self.assertRaises(SkillNotFoundError) as ctx:
            catalog.get("nonexistent")
        self.assertEqual(ctx.exception.name, "nonexistent")

    def test_get_non_string_raises(self):
        """非字符串名称。"""
        catalog = SkillCatalog(_FakeMetadataParser())
        with self.assertRaises(TypeError):
            catalog.get(123)  # type: ignore


class TestSkillCatalogExists(unittest.TestCase):
    """exists 方法测试。"""

    def test_exists_true(self):
        """存在。"""
        catalog = SkillCatalog(_FakeMetadataParser())
        catalog.register(_meta("test", "描述", "/a.md"))
        self.assertTrue(catalog.exists("test"))

    def test_exists_false(self):
        """不存在。"""
        catalog = SkillCatalog(_FakeMetadataParser())
        self.assertFalse(catalog.exists("nonexistent"))

    def test_exists_non_string_raises(self):
        """非字符串名称。"""
        catalog = SkillCatalog(_FakeMetadataParser())
        with self.assertRaises(TypeError):
            catalog.exists(123)  # type: ignore


class TestSkillCatalogList(unittest.TestCase):
    """list_metadata 方法测试。"""

    def test_list_empty(self):
        """空目录。"""
        catalog = SkillCatalog(_FakeMetadataParser())
        self.assertEqual(catalog.list_metadata(), [])

    def test_list_single(self):
        """单个元数据。"""
        catalog = SkillCatalog(_FakeMetadataParser())
        catalog.register(_meta("b-skill", "B", "/b.md"))
        skills = catalog.list_metadata()
        self.assertEqual(len(skills), 1)
        self.assertEqual(skills[0].name, "b-skill")

    def test_list_sorted_by_name(self):
        """多个元数据按 name 排序。"""
        catalog = SkillCatalog(_FakeMetadataParser())
        catalog.register(_meta("z-skill", "Z", "/z.md"))
        catalog.register(_meta("a-skill", "A", "/a.md"))
        names = [s.name for s in catalog.list_metadata()]
        self.assertEqual(names, ["a-skill", "z-skill"])

    def test_list_returns_new_list(self):
        """返回新 list。"""
        catalog = SkillCatalog(_FakeMetadataParser())
        catalog.register(_meta("test", "描述", "/a.md"))
        result1 = catalog.list_metadata()
        result2 = catalog.list_metadata()
        self.assertIsNot(result1, result2)


class TestSkillCatalogLoadDirectory(unittest.TestCase):
    """load_directory 方法测试。"""

    FIXTURE_DIR = Path(__file__).parent / "fixtures" / "skill_registry"

    def test_load_directory_path(self):
        """Path 对象。"""
        catalog = SkillCatalog(MarkdownSkillMetadataParser())
        metas = catalog.load_directory(self.FIXTURE_DIR / "valid")
        self.assertGreaterEqual(len(metas), 1)

    def test_load_directory_str_path(self):
        """str 路径。"""
        catalog = SkillCatalog(MarkdownSkillMetadataParser())
        metas = catalog.load_directory(str(self.FIXTURE_DIR / "valid"))
        self.assertGreaterEqual(len(metas), 1)

    def test_load_directory_nonexistent_raises(self):
        """目录不存在。"""
        catalog = SkillCatalog(MarkdownSkillMetadataParser())
        with self.assertRaises(SkillDirectoryError):
            catalog.load_directory("/nonexistent/dir")

    def test_load_directory_is_file_raises(self):
        """root 是文件。"""
        catalog = SkillCatalog(MarkdownSkillMetadataParser())
        with self.assertRaises(SkillDirectoryError):
            catalog.load_directory(__file__)

    def test_load_directory_empty(self):
        """空目录。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            catalog = SkillCatalog(MarkdownSkillMetadataParser())
            metas = catalog.load_directory(tmpdir)
            self.assertEqual(metas, [])

    def test_load_directory_nested(self):
        """嵌套目录。"""
        catalog = SkillCatalog(MarkdownSkillMetadataParser())
        metas = catalog.load_directory(self.FIXTURE_DIR / "valid")
        names = [m.name for m in metas]
        self.assertIn("valid-skill", names)
        self.assertIn("another-valid", names)

    def test_load_directory_multiple(self):
        """多个 SKILL.md。"""
        catalog = SkillCatalog(MarkdownSkillMetadataParser())
        metas = catalog.load_directory(self.FIXTURE_DIR / "valid")
        self.assertEqual(len(metas), 2)

    def test_load_directory_ignores_lowercase(self):
        """忽略小写 skill.md。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_file = Path(tmpdir) / "skill.md"
            skill_file.write_text(
                "---\nname: test\ndescription: Test\n---\n# Test",
                encoding="utf-8",
            )
            catalog = SkillCatalog(MarkdownSkillMetadataParser())
            metas = catalog.load_directory(tmpdir)
            self.assertEqual(len(metas), 0)

    def test_load_directory_parse_order_stable(self):
        """解析顺序稳定。"""
        catalog = SkillCatalog(MarkdownSkillMetadataParser())
        metas1 = catalog.load_directory(self.FIXTURE_DIR / "valid")
        names1 = [m.name for m in metas1]

        catalog2 = SkillCatalog(MarkdownSkillMetadataParser())
        metas2 = catalog2.load_directory(self.FIXTURE_DIR / "valid")
        names2 = [m.name for m in metas2]

        self.assertEqual(names1, names2)

    def test_load_directory_each_file_parsed_once(self):
        """每个文件只解析一次。"""
        parser = _FakeMetadataParser()
        catalog = SkillCatalog(parser)
        catalog.load_directory(self.FIXTURE_DIR / "valid")
        self.assertEqual(len(parser.paths), 2)


class TestSkillCatalogAtomicLoad(unittest.TestCase):
    """原子化加载测试。"""

    def test_parse_failure_preserves_state(self):
        """解析失败，Catalog 不变。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            skill1 = Path(tmpdir) / "SKILL.md"
            skill1.write_text(
                "---\nname: good\ndescription: Good\n---\n# Good",
                encoding="utf-8",
            )
            subdir = Path(tmpdir) / "sub"
            subdir.mkdir()
            skill2 = subdir / "SKILL.md"
            skill2.write_text(
                "---\ndescription: No name\n---\n# Bad",
                encoding="utf-8",
            )

            catalog = SkillCatalog(MarkdownSkillMetadataParser())
            from mini_deerflow.skill_metadata import SkillMetadataParseError

            with self.assertRaises(SkillMetadataParseError):
                catalog.load_directory(tmpdir)

            self.assertEqual(len(catalog), 0)

    def test_batch_duplicate_raises(self):
        """批次内两个元数据重名。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            for i in range(2):
                p = Path(tmpdir) / f"skill{i}"
                p.mkdir()
                (p / "SKILL.md").write_text(
                    "---\nname: same-name\ndescription: Same\n---\n# Same",
                    encoding="utf-8",
                )

            catalog = SkillCatalog(MarkdownSkillMetadataParser())
            with self.assertRaises(SkillAlreadyExistsError) as ctx:
                catalog.load_directory(tmpdir)
            self.assertEqual(ctx.exception.name, "same-name")
            self.assertEqual(len(catalog), 0)

    def test_conflict_with_existing_raises(self):
        """与已有元数据重名。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_file = Path(tmpdir) / "SKILL.md"
            skill_file.write_text(
                "---\nname: existing\ndescription: Existing\n---\n# Existing",
                encoding="utf-8",
            )

            catalog = SkillCatalog(MarkdownSkillMetadataParser())
            catalog.register(_meta("existing", "Old", "/old.md"))

            with self.assertRaises(SkillAlreadyExistsError):
                catalog.load_directory(tmpdir)

            self.assertEqual(len(catalog), 1)
            self.assertTrue(catalog.exists("existing"))


class TestSkillCatalogIntegration(unittest.TestCase):
    """集成测试。"""

    FIXTURE_DIR = Path(__file__).parent / "fixtures" / "skill_registry"

    def test_load_and_query(self):
        """加载后查询。"""
        catalog = SkillCatalog(MarkdownSkillMetadataParser())
        catalog.load_directory(self.FIXTURE_DIR / "valid")

        self.assertTrue(catalog.exists("valid-skill"))
        self.assertTrue(catalog.exists("another-valid"))

        meta = catalog.get("valid-skill")
        self.assertIn("有效", meta.description)

        metas = catalog.list_metadata()
        self.assertEqual(len(metas), 2)

    def test_metadata_has_no_instructions(self):
        """元数据没有 instructions 字段。"""
        catalog = SkillCatalog(MarkdownSkillMetadataParser())
        catalog.load_directory(self.FIXTURE_DIR / "valid")

        for meta in catalog.list_metadata():
            self.assertFalse(hasattr(meta, "instructions"))


if __name__ == "__main__":
    unittest.main()
