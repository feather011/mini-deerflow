"""CLI Skill 装配与命令测试（15F 阶段）。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mini_deerflow.cli import (
    CLIState,
    create_skill_runtime,
    format_skill_list,
    format_skill_metadata,
    handle_command,
    main,
)
from mini_deerflow.skill_catalog import SkillCatalog
from mini_deerflow.skill_metadata import MarkdownSkillMetadataParser, SkillMetadata
from mini_deerflow.thread_store import InMemoryThreadStore


def _captured_prints(mock_print) -> str:
    """把所有 print 调用拼成一个字符串便于断言。"""
    return "\n".join(str(c) for c in mock_print.call_args_list)


def _skill_metadata(
    name: str = "test-skill",
    description: str = "Test Skill",
    source_path: str | Path = "/test/SKILL.md",
) -> SkillMetadata:
    return SkillMetadata(
        name=name,
        description=description,
        source_path=Path(source_path),
    )


class TestFormatSkillList(unittest.TestCase):
    """format_skill_list 纯函数测试。"""

    def test_empty_list(self):
        """空列表返回固定文本。"""
        result = format_skill_list([])
        self.assertEqual(result, "暂无可用 Skill")

    def test_single_metadata(self):
        """单个 Metadata。"""
        metadata = [_skill_metadata("python-testing", "Python 测试工作流程")]
        result = format_skill_list(metadata)
        self.assertIn("可用 Skills", result)
        self.assertIn("python-testing", result)
        self.assertIn("Python 测试工作流程", result)

    def test_multiple_metadata(self):
        """多个 Metadata。"""
        metadata = [
            _skill_metadata("code-review", "代码审查流程"),
            _skill_metadata("python-testing", "测试工作流程"),
        ]
        result = format_skill_list(metadata)
        self.assertIn("可用 Skills", result)
        self.assertIn("code-review", result)
        self.assertIn("python-testing", result)

    def test_chinese_content(self):
        """中文内容。"""
        metadata = [_skill_metadata("test", "中文描述")]
        result = format_skill_list(metadata)
        self.assertIn("中文描述", result)

    def test_order_stable(self):
        """顺序稳定（按 name 升序）。"""
        metadata = [
            _skill_metadata("zebra", "Z"),
            _skill_metadata("alpha", "A"),
        ]
        result = format_skill_list(metadata)
        z_pos = result.find("zebra")
        a_pos = result.find("alpha")
        self.assertLess(a_pos, z_pos)

    def test_does_not_output_source_path(self):
        """不输出 source_path。"""
        metadata = [_skill_metadata("test", "Test", "/path/to/SKILL.md")]
        result = format_skill_list(metadata)
        self.assertNotIn("/path/to", result)

    def test_usage_hint(self):
        """包含使用提示。"""
        metadata = [_skill_metadata("test", "Test")]
        result = format_skill_list(metadata)
        self.assertIn("@skill-name", result)


class TestFormatSkillMetadata(unittest.TestCase):
    """format_skill_metadata 纯函数测试。"""

    def test_normal(self):
        """正常格式化。"""
        metadata = _skill_metadata("python-testing", "Python 测试")
        result = format_skill_metadata(metadata)
        self.assertIn("python-testing", result)
        self.assertIn("Python 测试", result)
        self.assertIn("@python-testing", result)

    def test_chinese(self):
        """中文内容。"""
        metadata = _skill_metadata("test", "中文描述")
        result = format_skill_metadata(metadata)
        self.assertIn("中文描述", result)

    def test_does_not_show_instructions(self):
        """不显示 instructions。"""
        metadata = _skill_metadata("test", "Test", "/test/SKILL.md")
        result = format_skill_metadata(metadata)
        self.assertNotIn("instructions", result.lower())
        self.assertNotIn("SKILL.md", result)

    def test_does_not_show_source_path(self):
        """不显示 source_path。"""
        metadata = _skill_metadata("test", "Test", "/path/to/SKILL.md")
        result = format_skill_metadata(metadata)
        self.assertNotIn("/path/to", result)


class TestHandleCommandSkills(unittest.TestCase):
    """Skill 命令处理测试。"""

    def _make_agent_and_store(self):
        """创建测试用的 Agent 和 Store。"""
        from mini_deerflow.agent import Agent
        from mini_deerflow.models import FakeModel
        from mini_deerflow.tools import create_default_registry

        store = InMemoryThreadStore()
        store.create("default")
        agent = Agent(
            model=FakeModel(),
            registry=create_default_registry(),
            store=store,
        )
        state = CLIState(current_thread_id="default")
        return store, agent, state

    def test_skills_not_enabled(self):
        """Skill 功能未启用时。"""
        store, agent, state = self._make_agent_and_store()

        with patch("builtins.print") as mock_print:
            handle_command("/skills", state, store, agent, None, None)

        output = _captured_prints(mock_print)
        self.assertIn("未启用", output)

    def test_skills_empty_catalog(self):
        """空 Catalog。"""
        store, agent, state = self._make_agent_and_store()
        catalog = SkillCatalog(MarkdownSkillMetadataParser())

        with patch("builtins.print") as mock_print:
            handle_command("/skills", state, store, agent, None, catalog)

        output = _captured_prints(mock_print)
        self.assertIn("暂无可用", output)

    def test_skills_multiple(self):
        """多个 Skill。"""
        store, agent, state = self._make_agent_and_store()

        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = Path(tmpdir) / "python-testing"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                "---\nname: python-testing\ndescription: 测试工作流程\n---\n# Test",
                encoding="utf-8",
            )

            catalog = SkillCatalog(MarkdownSkillMetadataParser())
            catalog.load_directory(tmpdir)

            with patch("builtins.print") as mock_print:
                handle_command("/skills", state, store, agent, None, catalog)

            output = _captured_prints(mock_print)
            self.assertIn("python-testing", output)
            self.assertIn("测试工作流程", output)

    def test_skill_missing_arg(self):
        """缺少参数。"""
        store, agent, state = self._make_agent_and_store()
        catalog = SkillCatalog(MarkdownSkillMetadataParser())

        with patch("builtins.print") as mock_print:
            handle_command("/skill", state, store, agent, None, catalog)

        output = _captured_prints(mock_print)
        self.assertIn("用法", output)

    def test_skill_not_enabled(self):
        """Skill 功能未启用时。"""
        store, agent, state = self._make_agent_and_store()

        with patch("builtins.print") as mock_print:
            handle_command("/skill python-testing", state, store, agent, None, None)

        output = _captured_prints(mock_print)
        self.assertIn("未启用", output)

    def test_skill_not_found(self):
        """Skill 不存在。"""
        store, agent, state = self._make_agent_and_store()
        catalog = SkillCatalog(MarkdownSkillMetadataParser())

        with patch("builtins.print") as mock_print:
            handle_command("/skill python-testing", state, store, agent, None, catalog)

        output = _captured_prints(mock_print)
        self.assertIn("未找到", output)

    def test_skill_exists(self):
        """Skill 存在。"""
        store, agent, state = self._make_agent_and_store()

        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = Path(tmpdir) / "python-testing"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                "---\nname: python-testing\ndescription: 测试工作流程\n---\n# Test",
                encoding="utf-8",
            )

            catalog = SkillCatalog(MarkdownSkillMetadataParser())
            catalog.load_directory(tmpdir)

            with patch("builtins.print") as mock_print:
                handle_command("/skill python-testing", state, store, agent, None, catalog)

            output = _captured_prints(mock_print)
            self.assertIn("python-testing", output)
            self.assertIn("测试工作流程", output)
            self.assertIn("@python-testing", output)

    def test_skill_case_sensitive(self):
        """大小写严格。"""
        store, agent, state = self._make_agent_and_store()

        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = Path(tmpdir) / "python-testing"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                "---\nname: python-testing\ndescription: Test\n---\n# Test",
                encoding="utf-8",
            )

            catalog = SkillCatalog(MarkdownSkillMetadataParser())
            catalog.load_directory(tmpdir)

            with patch("builtins.print") as mock_print:
                handle_command("/skill Python-Testing", state, store, agent, None, catalog)

            output = _captured_prints(mock_print)
            self.assertIn("未找到", output)

    def test_skill_command_does_not_call_agent(self):
        """Skill 命令不调用 Agent。"""
        from mini_deerflow.agent import Agent
        from mini_deerflow.models import FakeModel
        from mini_deerflow.tools import create_default_registry

        store = InMemoryThreadStore()
        store.create("default")

        call_count = 0

        class CountingModel:
            def decide(self, messages):
                nonlocal call_count
                call_count += 1
                return FakeModel().decide(messages)

        agent = Agent(
            model=CountingModel(),
            registry=create_default_registry(),
            store=store,
        )
        state = CLIState(current_thread_id="default")
        catalog = SkillCatalog(MarkdownSkillMetadataParser())

        handle_command("/skills", state, store, agent, None, catalog)

        self.assertEqual(call_count, 0)

    def test_skill_command_does_not_change_thread(self):
        """Skill 命令不改变 Thread。"""
        from mini_deerflow.agent import Agent
        from mini_deerflow.models import FakeModel
        from mini_deerflow.tools import create_default_registry

        store = InMemoryThreadStore()
        store.create("default")
        thread = store.get("default")
        initial_count = thread.message_count

        agent = Agent(
            model=FakeModel(),
            registry=create_default_registry(),
            store=store,
        )
        state = CLIState(current_thread_id="default")
        catalog = SkillCatalog(MarkdownSkillMetadataParser())

        handle_command("/skills", state, store, agent, None, catalog)

        thread = store.get("default")
        self.assertEqual(thread.message_count, initial_count)


class TestCreateSkillRuntime(unittest.TestCase):
    """create_skill_runtime 装配测试。"""

    def test_empty_directory(self):
        """空目录返回空 Catalog。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            catalog, provider = create_skill_runtime(Path(tmpdir))
            self.assertEqual(len(catalog.list_metadata()), 0)

    def test_nonexistent_directory(self):
        """不存在的目录返回空 Catalog。"""
        catalog, provider = create_skill_runtime(Path("/nonexistent/path"))
        self.assertEqual(len(catalog.list_metadata()), 0)

    def test_loads_metadata(self):
        """加载 Metadata。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = Path(tmpdir) / "python-testing"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                "---\nname: python-testing\ndescription: 测试工作流程\n---\n# Test",
                encoding="utf-8",
            )

            catalog, provider = create_skill_runtime(Path(tmpdir))
            metadata = catalog.list_metadata()

            self.assertEqual(len(metadata), 1)
            self.assertEqual(metadata[0].name, "python-testing")

    def test_duplicate_names_fails(self):
        """重名时失败。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir1 = Path(tmpdir) / "python-testing"
            skill_dir2 = Path(tmpdir) / "code-review"
            skill_dir1.mkdir()
            skill_dir2.mkdir()
            (skill_dir1 / "SKILL.md").write_text(
                "---\nname: python-testing\ndescription: Test\n---\n# Test",
                encoding="utf-8",
            )
            (skill_dir2 / "SKILL.md").write_text(
                "---\nname: python-testing\ndescription: Duplicate\n---\n# Test",
                encoding="utf-8",
            )

            with self.assertRaises(Exception):
                create_skill_runtime(Path(tmpdir))

    def test_invalid_skills_md_fails(self):
        """损坏的 SKILL.md 失败。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = Path(tmpdir) / "test"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text("这不是有效的 Front Matter", encoding="utf-8")

            with self.assertRaises(Exception):
                create_skill_runtime(Path(tmpdir))


class _TempDBTestCase(unittest.TestCase):
    """基类：每个测试使用独立的临时数据库，不写项目目录。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = str(Path(self._tmp.name) / "test.db")

    def run_main(self, inputs: list[str], extra_argv: list[str] | None = None) -> str:
        """用给定输入序列运行 main()，返回捕获的输出。"""
        argv = ["mini-deerflow", "--db", self.db_path] + (extra_argv or [])
        with patch("builtins.input", side_effect=inputs), \
             patch("builtins.print") as mock_print, \
             patch("sys.argv", argv):
            main()
        return _captured_prints(mock_print)


class TestCLIWithSkills(_TempDBTestCase):
    """CLI 与 Skill 集成测试。"""

    def test_skills_command(self):
        """运行 /skills 命令。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = Path(tmpdir) / "python-testing"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                "---\nname: python-testing\ndescription: 测试工作流程\n---\n# Test",
                encoding="utf-8",
            )

            out = self.run_main(["/skills", "exit"], extra_argv=["--skills-dir", tmpdir])
            self.assertIn("python-testing", out)
            self.assertIn("测试工作流程", out)

    def test_skill_command(self):
        """运行 /skill 命令。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = Path(tmpdir) / "python-testing"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                "---\nname: python-testing\ndescription: 测试工作流程\n---\n# Test",
                encoding="utf-8",
            )

            out = self.run_main(["/skill python-testing", "exit"], extra_argv=["--skills-dir", tmpdir])
            self.assertIn("python-testing", out)
            self.assertIn("@python-testing", out)

    def test_help_includes_skill_commands(self):
        """/help 包含 Skill 命令。"""
        out = self.run_main(["/help", "exit"])
        self.assertIn("/skills", out)
        self.assertIn("/skill", out)


if __name__ == "__main__":
    unittest.main()
