"""Skill 领域模型与 SKILL.md 解析器。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

import yaml


class SkillParseError(ValueError):
    """解析 SKILL.md 失败时抛出。"""

    def __init__(self, path: Path, message: str):
        self.path = path
        self.message = message
        super().__init__(f"{path}: {message}")


# 合法 name 格式的正则
_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


@dataclass(frozen=True)
class Skill:
    """一个可执行的 Skill，包含名称、描述、指令和来源路径。"""

    name: str
    description: str
    instructions: str
    source_path: Path

    def __post_init__(self) -> None:
        """验证并规范化 Skill 字段。"""
        # name 验证
        if not isinstance(self.name, str) or not self.name.strip():
            raise TypeError(f"name 必须是字符串，收到: {type(self.name).__name__}")

        name_stripped = self.name.strip()
        if not _NAME_PATTERN.match(name_stripped):
            raise ValueError(
                f"非法 name: {self.name!r}，"
                f"必须是小写字母、数字、连字符和下划线，且以字母或数字开头"
            )

        # description 验证
        if not isinstance(self.description, str) or not self.description.strip():
            raise TypeError(
                f"description 必须是字符串，收到: {type(self.description).__name__}"
            )

        # instructions 验证
        if not isinstance(self.instructions, str) or not self.instructions.strip():
            raise TypeError(
                f"instructions 必须是字符串，收到: {type(self.instructions).__name__}"
            )

        # source_path 验证
        if not isinstance(self.source_path, Path):
            raise TypeError(
                f"source_path 必须是 Path，收到: {type(self.source_path).__name__}"
            )

        # 在 frozen 模式下使用 object.__setattr__ 规范化字段
        if self.name != name_stripped:
            object.__setattr__(self, "name", name_stripped)

        desc_stripped = self.description.strip()
        if self.description != desc_stripped:
            object.__setattr__(self, "description", desc_stripped)

        instr_stripped = self.instructions.strip()
        if self.instructions != instr_stripped:
            object.__setattr__(self, "instructions", instr_stripped)


@runtime_checkable
class SkillParser(Protocol):
    """Skill 解析器接口。"""

    def parse(self, path: str | Path) -> Skill:
        """解析 SKILL.md 文件返回 Skill。"""
        ...


class MarkdownSkillParser:
    """解析 SKILL.md 格式的 SkillParser 实现。"""

    def parse(self, path: str | Path) -> Skill:
        """解析 SKILL.md 文件。

        执行顺序：
        1. 验证 path 类型并转换为 Path
        2. 检查文件存在且为普通文件
        3. 读取文件内容（UTF-8）
        4. 查找 Front Matter 边界
        5. 解析 YAML Front Matter
        6. 提取 Markdown 正文
        7. 验证字段有效性
        8. 创建并返回 Skill
        """
        path = Path(path)

        # 1. 检查文件存在且为普通文件
        if not path.exists():
            raise SkillParseError(path, "文件不存在")
        if not path.is_file():
            raise SkillParseError(path, "路径不是普通文件")

        # 3. 读取文件内容
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as e:
            raise SkillParseError(path, f"UTF-8 解码失败: {e}") from e
        except OSError as e:
            raise SkillParseError(path, f"读取文件失败: {e}") from e

        # 4. 查找 Front Matter 边界
        lines = content.splitlines()
        if not lines:
            raise SkillParseError(path, "文件为空")

        # 检查第一行必须是 ---
        if lines[0].strip() != "---":
            raise SkillParseError(path, "缺少起始分隔符 '---'")

        # 查找第二个独立的 ---
        end_idx = -1
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                end_idx = i
                break

        if end_idx == -1:
            raise SkillParseError(path, "缺少结束分隔符 '---'")

        # 5. 解析 YAML Front Matter
        front_matter_text = "\n".join(lines[1:end_idx])
        if not front_matter_text.strip():
            raise SkillParseError(path, "Front Matter 为空")

        try:
            front_matter = yaml.safe_load(front_matter_text)
        except yaml.YAMLError as e:
            raise SkillParseError(path, f"YAML 解析失败: {e}") from e

        if front_matter is None:
            raise SkillParseError(path, "Front Matter 为空")

        if not isinstance(front_matter, dict):
            raise SkillParseError(
                path, f"YAML 必须是 mapping，收到: {type(front_matter).__name__}"
            )

        # 6. 提取字段
        if "name" not in front_matter:
            raise SkillParseError(path, "缺少必填字段 'name'")

        if "description" not in front_matter:
            raise SkillParseError(path, "缺少必填字段 'description'")

        name_value = front_matter["name"]
        desc_value = front_matter["description"]

        # 类型检查
        if not isinstance(name_value, str):
            raise SkillParseError(
                path, f"name 必须是字符串，收到: {type(name_value).__name__}"
            )

        if not isinstance(desc_value, str):
            raise SkillParseError(
                path,
                f"description 必须是字符串，收到: {type(desc_value).__name__}",
            )

        name = name_value.strip()
        description = desc_value.strip()

        # 值检查
        if not name:
            raise SkillParseError(path, "name 不能为空")

        if not description:
            raise SkillParseError(path, "description 不能为空")

        # name 格式验证
        if not _NAME_PATTERN.match(name):
            raise SkillParseError(
                path,
                f"非法 name: {name!r}，"
                f"必须是小写字母、数字、连字符和下划线，且以字母或数字开头",
            )

        # 6. 提取 Markdown 正文
        markdown_lines = lines[end_idx + 1 :]
        instructions = "\n".join(markdown_lines).strip()

        if not instructions:
            raise SkillParseError(path, "Markdown 正文为空")

        # 7. 创建 Skill
        return Skill(
            name=name,
            description=description,
            instructions=instructions,
            source_path=path.resolve(),
        )
