"""Skill 注册表：目录发现、注册和查询。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol, Sequence

from mini_deerflow.skill import Skill, SkillParseError


_SKILL_FILENAME = "SKILL.md"


def _walk_skill_files(root: Path) -> list[Path]:
    """递归枚举 root 下所有精确名为 SKILL.md 的文件（大小写敏感）。

    使用 os.walk 而不是 rglob，原因同 skill_catalog._walk_skill_files。
    """
    results: list[Path] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if name == _SKILL_FILENAME:
                results.append(Path(dirpath) / name)
    results.sort(key=str)
    return results


class SkillRegistryError(Exception):
    """Skill 注册表相关错误的基类。"""


class SkillAlreadyExistsError(SkillRegistryError):
    """注册已存在的 Skill 名称时抛出。"""

    def __init__(self, name: str, existing: Path, new: Path | None = None):
        self.name = name
        self.existing = existing
        self.new = new
        existing_str = str(existing).replace("\\", "/")
        msg = f"Skill 已存在: {name}（来源: {existing_str}）"
        if new:
            new_str = str(new).replace("\\", "/")
            msg += f"，与 {new_str} 冲突"
        super().__init__(msg)


class SkillNotFoundError(SkillRegistryError):
    """查找未知 Skill 时抛出。"""

    def __init__(self, name: str):
        self.name = name
        super().__init__(f"未知 Skill: {name}")


class SkillDirectoryError(SkillRegistryError):
    """目录访问或解析失败时抛出。"""

    def __init__(self, path: Path, reason: str, cause: Exception | None = None):
        self.path = path
        self.reason = reason
        if cause:
            super().__init__(f"目录 {path}: {reason}: {cause}")
        else:
            super().__init__(f"目录 {path}: {reason}")


class SkillParser(Protocol):
    """Skill 解析器接口。"""

    def parse(self, path: str | Path) -> Skill:
        """解析 SKILL.md 文件返回 Skill。"""
        ...


class SkillRegistry:
    """Skill 注册表：管理 Skill 的注册、查找与目录发现。"""

    def __init__(self, parser: SkillParser) -> None:
        self._parser = parser
        self._skills: dict[str, Skill] = {}

    def register(self, skill: Skill) -> None:
        """注册 Skill；重名时抛出 SkillAlreadyExistsError。"""
        if not isinstance(skill, Skill):
            raise TypeError(f"期望 Skill 类型，收到: {type(skill).__name__}")

        if skill.name in self._skills:
            existing = self._skills[skill.name].source_path
            raise SkillAlreadyExistsError(skill.name, existing, skill.source_path)

        self._skills[skill.name] = skill

    def register_many(self, skills: Sequence[Skill]) -> None:
        """批量注册多个 Skill；原子化操作。

        - 先验证整个批次
        - 检查批次内部重名
        - 检查与已有 Skill 重名
        - 所有验证通过后一次性更新
        - 任何错误时 Registry 完全不变
        """
        if isinstance(skills, str):
            raise TypeError("skills 不能是 str")

        if not isinstance(skills, Sequence):
            raise TypeError(
                f"skills 必须是 Sequence，收到: {type(skills).__name__}"
            )

        # 阶段 1：类型校验
        for skill in skills:
            if not isinstance(skill, Skill):
                raise TypeError(
                    f"skills 中的元素必须是 Skill，收到: {type(skill).__name__}"
                )

        # 阶段 2：检查批次内部重名
        seen_names: dict[str, Path] = {}
        for skill in skills:
            if skill.name in seen_names:
                raise SkillAlreadyExistsError(
                    skill.name,
                    seen_names[skill.name],
                    skill.source_path,
                )
            seen_names[skill.name] = skill.source_path

        # 阶段 3：检查与已有 Skill 重名
        for skill in skills:
            if skill.name in self._skills:
                existing = self._skills[skill.name].source_path
                raise SkillAlreadyExistsError(skill.name, existing, skill.source_path)

        # 阶段 4：全部验证通过，一次性提交
        for skill in skills:
            self._skills[skill.name] = skill

    def get(self, name: str) -> Skill:
        """按名称获取 Skill；未知时抛出 SkillNotFoundError。"""
        if not isinstance(name, str):
            raise TypeError(f"name 必须是 str，收到: {type(name).__name__}")

        if name not in self._skills:
            raise SkillNotFoundError(name)

        return self._skills[name]

    def exists(self, name: str) -> bool:
        """检查 Skill 是否存在。"""
        if not isinstance(name, str):
            raise TypeError(f"name 必须是 str，收到: {type(name).__name__}")

        return name in self._skills

    def list_skills(self) -> list[Skill]:
        """返回所有 Skill，按 name 升序排列。"""
        return [self._skills[name] for name in sorted(self._skills)]

    def load_directory(self, root: str | Path) -> list[Skill]:
        """从目录递归发现并注册所有 SKILL.md。

        原子化加载：所有检查通过后才提交，中途失败不影响 Registry 状态。
        """
        root_path = Path(root)

        # 验证目录存在
        if not root_path.exists():
            raise SkillDirectoryError(root_path, "目录不存在")

        if not root_path.is_dir():
            raise SkillDirectoryError(root_path, "路径不是目录")

        # 发现并排序所有 SKILL.md
        # 用 _walk_skill_files 枚举，避免 rglob 在 Windows 上大小写不敏感匹配
        try:
            paths = _walk_skill_files(root_path)
        except OSError as e:
            raise SkillDirectoryError(root_path, "读取目录失败", e) from e

        if not paths:
            return []

        # 过滤只保留普通文件
        paths = [p for p in paths if p.is_file()]
        if not paths:
            return []

        # 阶段 1：解析所有文件
        candidates: list[Skill] = []
        for path in paths:
            try:
                skill = self._parser.parse(path)
                candidates.append(skill)
            except SkillParseError:
                # 解析失败时原样抛出，让调用者知道哪个文件出错
                raise
            except Exception as e:
                raise SkillDirectoryError(path, f"解析失败: {e}", e) from e

        # 阶段 2：检查批次内部重名
        seen_names: dict[str, Path] = {}
        for skill in candidates:
            if skill.name in seen_names:
                raise SkillAlreadyExistsError(
                    skill.name,
                    seen_names[skill.name],
                    skill.source_path,
                )
            seen_names[skill.name] = skill.source_path

        # 阶段 3：检查与已有 Skill 重名
        for skill in candidates:
            if skill.name in self._skills:
                existing = self._skills[skill.name].source_path
                raise SkillAlreadyExistsError(skill.name, existing, skill.source_path)

        # 阶段 4：全部检查通过，一次性提交
        for skill in candidates:
            self._skills[skill.name] = skill

        return list(candidates)

    def __len__(self) -> int:
        """返回注册的 Skill 数量。"""
        return len(self._skills)
