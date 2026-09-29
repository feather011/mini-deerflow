"""渐进式 Skill 延迟加载器。"""

from __future__ import annotations

from typing import Protocol, Sequence

from mini_deerflow.skill import Skill, SkillParseError
from mini_deerflow.skill_metadata import SkillMetadata


class SkillLoader(Protocol):
    """Skill 加载器接口。"""

    def load_selected(
        self,
        selected: Sequence[SkillMetadata],
    ) -> list[Skill]:
        """加载选中的 SkillMetadata 对应的完整 Skill。"""
        ...


class SkillParser(Protocol):
    """Skill 解析器接口。"""

    def parse(self, path: str | Path) -> Skill:
        """解析 SKILL.md 文件返回 Skill。"""
        ...


class SkillMetadataMismatchError(ValueError):
    """SkillMetadata 与完整 Skill 不一致时抛出。"""

    def __init__(
        self,
        name: str,
        metadata_path: str,
        field: str,
        metadata_value: str,
        skill_value: str,
    ):
        self.name = name
        self.metadata_path = metadata_path
        self.field = field
        self.metadata_value = metadata_value
        self.skill_value = skill_value
        super().__init__(
            f"Skill {name} 的 {field} 不一致："
            f"Metadata ({metadata_value}) vs Skill ({skill_value})"
        )


class ProgressiveSkillLoader:
    """渐进式 Skill 加载器。

    职责：
    - 只完整解析选中的 SkillMetadata
    - 复用 SkillRegistry 缓存
    - 保持选择顺序
    """

    def __init__(
        self,
        parser: SkillParser,
        registry: SkillRegistry,
    ) -> None:
        self._parser = parser
        self._registry = registry

    def load_selected(
        self,
        selected: Sequence[SkillMetadata],
    ) -> list[Skill]:
        """加载选中的 SkillMetadata。

        执行顺序：
        1. 输入校验
        2. 缓存命中检查
        3. 新文件完整解析
        4. 元数据一致性校验
        5. 批量原子注册
        6. 返回结果
        """
        # 阶段 1：输入校验
        self._validate_selected(selected)

        # 阶段 2 & 3：缓存命中或完整解析
        result_slots: list[Skill] = []
        newly_loaded: list[Skill] = []

        for metadata in selected:
            if self._registry.exists(metadata.name):
                # 缓存命中
                skill = self._registry.get(metadata.name)
                self._validate_match(metadata, skill)
                result_slots.append(skill)
            else:
                # 完整解析
                skill = self._parser.parse(metadata.source_path)
                self._validate_match(metadata, skill)
                newly_loaded.append(skill)
                result_slots.append(skill)

        # 阶段 4：批量原子注册
        if newly_loaded:
            self._registry.register_many(newly_loaded)

        # 阶段 5：返回结果
        return list(result_slots)

    def _validate_selected(self, selected: Sequence[SkillMetadata]) -> None:
        """校验 selected 输入。"""
        if isinstance(selected, str):
            raise TypeError("selected 不能是 str")

        if not isinstance(selected, Sequence):
            raise TypeError(
                f"selected 必须是 Sequence，收到: {type(selected).__name__}"
            )

        seen_names: set[str] = set()
        for metadata in selected:
            if not isinstance(metadata, SkillMetadata):
                raise ValueError(
                    f"selected 中的元素必须是 SkillMetadata，"
                    f"收到: {type(metadata).__name__}"
                )
            if metadata.name in seen_names:
                raise ValueError(f"selected 中存在重复的 name: {metadata.name}")
            seen_names.add(metadata.name)

    def _validate_match(
        self,
        metadata: SkillMetadata,
        skill: Skill,
    ) -> None:
        """校验 SkillMetadata 与 Skill 的一致性。"""
        # 校验 name
        if skill.name != metadata.name:
            raise SkillMetadataMismatchError(
                metadata.name,
                str(metadata.source_path),
                "name",
                metadata.name,
                skill.name,
            )

        # 校验 description
        if skill.description != metadata.description:
            raise SkillMetadataMismatchError(
                metadata.name,
                str(metadata.source_path),
                "description",
                metadata.description,
                skill.description,
            )

        # 校验 source_path（使用 resolve 后的 Path 比较）
        skill_path = skill.source_path.resolve()
        metadata_path = metadata.source_path.resolve()
        if skill_path != metadata_path:
            raise SkillMetadataMismatchError(
                metadata.name,
                str(metadata.source_path),
                "source_path",
                str(metadata_path),
                str(skill_path),
            )


# 前向引用避免循环导入
from pathlib import Path
from mini_deerflow.skill_registry import SkillRegistry
