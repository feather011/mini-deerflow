"""Skill 元数据：渐进式披露的第一层。"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import yaml

# Skill name 验证正则（与 Skill 保持一致）
_SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def validate_skill_name(name: str) -> str:
    """验证并返回规范化的 Skill name。

    - 必须是字符串
    - 去除首尾空白后必须匹配 ^[a-z0-9][a-z0-9_-]*$
    - 不自动修正非法 name
    """
    if not isinstance(name, str) or not name.strip():
        raise TypeError(f"name 必须是字符串，收到: {type(name).__name__}")

    name_stripped = name.strip()
    if not _SKILL_NAME_PATTERN.match(name_stripped):
        raise ValueError(
            f"非法 name: {name!r}，"
            f"必须是小写字母、数字、连字符和下划线，且以字母或数字开头"
        )

    return name_stripped


class SkillMetadataParseError(ValueError):
    """解析 Front Matter 失败时抛出。"""

    def __init__(self, path: Path, message: str):
        self.path = path
        self.message = message
        super().__init__(f"{path}: {message}")


@dataclass(frozen=True)
class SkillMetadata:
    """Skill 轻量元数据：只包含 name、description、source_path。

    不包含 instructions，用于渐进式披露的选择阶段。
    """

    name: str
    description: str
    source_path: Path

    def __post_init__(self) -> None:
        """验证并规范化 SkillMetadata 字段。"""
        # name 验证（复用公共函数）
        validated_name = validate_skill_name(self.name)

        # description 验证
        if not isinstance(self.description, str) or not self.description.strip():
            raise TypeError(
                f"description 必须是字符串，收到: {type(self.description).__name__}"
            )

        # source_path 验证
        if not isinstance(self.source_path, Path):
            raise TypeError(
                f"source_path 必须是 Path，收到: {type(self.source_path).__name__}"
            )

        # 在 frozen 模式下规范化字段
        if self.name != validated_name:
            object.__setattr__(self, "name", validated_name)

        desc_stripped = self.description.strip()
        if self.description != desc_stripped:
            object.__setattr__(self, "description", desc_stripped)


class SkillMetadataParser(Protocol):
    """Skill 元数据解析器接口。"""

    def parse(self, path: str | Path) -> SkillMetadata:
        """解析 SKILL.md 的 Front Matter，返回元数据。"""
        ...


class MarkdownSkillMetadataParser:
    """解析 SKILL.md Front Matter 的 SkillMetadataParser 实现。

    使用二进制流式读取，只解析 Front Matter，不读取 Markdown 正文。
    """

    def parse(self, path: str | Path) -> SkillMetadata:
        """流式读取 Front Matter，不加载完整文件。

        执行顺序：
        1. 验证 path 类型并转换为 Path
        2. 检查文件存在且为普通文件
        3. 二进制流式读取 Front Matter（遇到第二个 --- 立即停止）
        4. 解码 Front Matter
        5. 解析 YAML
        6. 验证字段有效性
        7. 创建并返回 SkillMetadata
        """
        path = Path(path)

        # 1. 检查文件存在且为普通文件
        if not path.exists():
            raise SkillMetadataParseError(path, "文件不存在")
        if not path.is_file():
            raise SkillMetadataParseError(path, "路径不是普通文件")

        # 2. 二进制流式读取 Front Matter
        front_matter_lines: list[bytes] = []

        try:
            with path.open("rb") as file:
                # 检查第一行必须是 ---
                first_line = file.readline()
                if not first_line:
                    raise SkillMetadataParseError(path, "文件为空")

                first_line_stripped = first_line.rstrip(b"\r\n")
                if first_line_stripped != b"---":
                    raise SkillMetadataParseError(path, "缺少起始分隔符 '---'")

                # 读取 Front Matter 直到第二个 ---
                while True:
                    line = file.readline()
                    if not line:
                        raise SkillMetadataParseError(path, "缺少结束分隔符 '---'")

                    if line.rstrip(b"\r\n") == b"---":
                        # 找到结束分隔符，停止读取
                        break

                    front_matter_lines.append(line)

        except SkillMetadataParseError:
            raise
        except OSError as e:
            raise SkillMetadataParseError(path, f"读取文件失败: {e}") from e

        # 3. 解码 Front Matter
        try:
            front_matter_text = b"".join(front_matter_lines).decode("utf-8")
        except UnicodeDecodeError as e:
            raise SkillMetadataParseError(path, f"UTF-8 解码失败: {e}") from e

        if not front_matter_text.strip():
            raise SkillMetadataParseError(path, "Front Matter 为空")

        # 4. 解析 YAML
        try:
            front_matter = yaml.safe_load(front_matter_text)
        except yaml.YAMLError as e:
            raise SkillMetadataParseError(path, f"YAML 解析失败: {e}") from e

        if front_matter is None:
            raise SkillMetadataParseError(path, "Front Matter 为空")

        if not isinstance(front_matter, dict):
            raise SkillMetadataParseError(
                path, f"YAML 必须是 mapping，收到: {type(front_matter).__name__}"
            )

        # 5. 提取并验证字段
        if "name" not in front_matter:
            raise SkillMetadataParseError(path, "缺少必填字段 'name'")

        if "description" not in front_matter:
            raise SkillMetadataParseError(path, "缺少必填字段 'description'")

        name_value = front_matter["name"]
        desc_value = front_matter["description"]

        # 类型检查
        if not isinstance(name_value, str):
            raise SkillMetadataParseError(
                path, f"name 必须是字符串，收到: {type(name_value).__name__}"
            )

        if not isinstance(desc_value, str):
            raise SkillMetadataParseError(
                path,
                f"description 必须是字符串，收到: {type(desc_value).__name__}",
            )

        # 值检查（使用公共函数验证 name）
        try:
            name = validate_skill_name(name_value)
        except (TypeError, ValueError) as e:
            raise SkillMetadataParseError(path, str(e)) from e

        description = desc_value.strip()
        if not description:
            raise SkillMetadataParseError(path, "description 不能为空")

        # 6. 返回 SkillMetadata（不包含 instructions）
        return SkillMetadata(
            name=name,
            description=description,
            source_path=path.resolve(),
        )


def normalize_selection_text(text: str) -> str:
    """标准化选择文本用于关键词匹配。

    规则：
    1. NFKC 标准化
    2. Casefold
    3. 连续空白压缩为一个空格
    4. 去除首尾空白
    """
    # NFKC 标准化
    text = unicodedata.normalize("NFKC", text)
    # Casefold（比 lower 更彻底）
    text = text.casefold()
    # 压缩连续空白为单个空格
    import re as _re

    text = _re.sub(r"\s+", " ", text)
    # 去除首尾空白
    return text.strip()
