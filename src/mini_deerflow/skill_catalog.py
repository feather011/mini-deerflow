"""Skill 目录：轻量级元数据索引。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Sequence

from mini_deerflow.skill_metadata import SkillMetadata, SkillMetadataParser


_SKILL_FILENAME = "SKILL.md"


def _walk_skill_files(root: Path) -> list[Path]:
    """递归枚举 root 下所有精确名为 SKILL.md 的文件（大小写敏感）。

    使用 os.walk 而不是 rglob，因为：
    - rglob 在 Windows 上对模式的大小写匹配不敏感，且返回的路径会带模式的大小写
    - os.walk 返回文件名时保留文件系统上的实际大小写
    - 通过对 entry.name 做精确字符串比较，可保证只在确实大写为 SKILL.md 时才接受
    """
    results: list[Path] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if name == _SKILL_FILENAME:
                results.append(Path(dirpath) / name)
    results.sort(key=str)
    return results


class SkillCatalogError(Exception):
    """Skill 目录相关错误的基类。"""


class SkillAlreadyExistsError(SkillCatalogError):
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


class SkillNotFoundError(SkillCatalogError):
    """查找未知 Skill 时抛出。"""

    def __init__(self, name: str):
        self.name = name
        super().__init__(f"未知 Skill: {name}")


class SkillDirectoryError(SkillCatalogError):
    """目录访问或解析失败时抛出。"""

    def __init__(self, path: Path, reason: str, cause: Exception | None = None):
        self.path = path
        self.reason = reason
        if cause:
            super().__init__(f"目录 {path}: {reason}: {cause}")
        else:
            super().__init__(f"目录 {path}: {reason}")


class SkillCatalog:
    """Skill 轻量目录：管理 SkillMetadata 的发现和索引。

    与 SkillRegistry 的区别：
    - SkillCatalog 保存 SkillMetadata（不含 instructions）
    - SkillRegistry 保存完整 Skill
    - SkillCatalog 用于发现和选择阶段
    - SkillRegistry 用于保存已加载的完整 Skill
    """

    def __init__(self, parser: SkillMetadataParser) -> None:
        self._parser = parser
        self._metadata: dict[str, SkillMetadata] = {}

    def register(self, metadata: SkillMetadata) -> None:
        """注册元数据；重名时抛出 SkillAlreadyExistsError。"""
        if not isinstance(metadata, SkillMetadata):
            raise TypeError(
                f"期望 SkillMetadata 类型，收到: {type(metadata).__name__}"
            )

        if metadata.name in self._metadata:
            existing = self._metadata[metadata.name].source_path
            raise SkillAlreadyExistsError(
                metadata.name, existing, metadata.source_path
            )

        self._metadata[metadata.name] = metadata

    def get(self, name: str) -> SkillMetadata:
        """按名称获取元数据；未知时抛出 SkillNotFoundError。"""
        if not isinstance(name, str):
            raise TypeError(f"name 必须是 str，收到: {type(name).__name__}")

        if name not in self._metadata:
            raise SkillNotFoundError(name)

        return self._metadata[name]

    def exists(self, name: str) -> bool:
        """检查元数据是否存在。"""
        if not isinstance(name, str):
            raise TypeError(f"name 必须是 str，收到: {type(name).__name__}")

        return name in self._metadata

    def list_metadata(self) -> list[SkillMetadata]:
        """返回所有元数据，按 name 升序排列。"""
        return [self._metadata[name] for name in sorted(self._metadata)]

    def load_directory(self, root: str | Path) -> list[SkillMetadata]:
        """从目录递归发现并注册所有 SKILL.md 的元数据。

        原子化加载：所有检查通过后才提交，中途失败不影响 Catalog 状态。
        """
        root_path = Path(root)

        # 验证目录存在
        if not root_path.exists():
            raise SkillDirectoryError(root_path, "目录不存在")

        if not root_path.is_dir():
            raise SkillDirectoryError(root_path, "路径不是目录")

        # 发现并排序所有 SKILL.md
        # 用 os.walk 枚举，避免 rglob 在 Windows 上大小写不敏感匹配
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
        candidates: list[SkillMetadata] = []
        for path in paths:
            try:
                metadata = self._parser.parse(path)
                candidates.append(metadata)
            except Exception:
                # 解析失败时原样抛出
                raise

        # 阶段 2：检查批次内部重名
        seen_names: dict[str, Path] = {}
        for meta in candidates:
            if meta.name in seen_names:
                raise SkillAlreadyExistsError(
                    meta.name,
                    seen_names[meta.name],
                    meta.source_path,
                )
            seen_names[meta.name] = meta.source_path

        # 阶段 3：检查与已有元数据重名
        for meta in candidates:
            if meta.name in self._metadata:
                existing = self._metadata[meta.name].source_path
                raise SkillAlreadyExistsError(meta.name, existing, meta.source_path)

        # 阶段 4：全部检查通过，一次性提交
        for meta in candidates:
            self._metadata[meta.name] = meta

        return list(candidates)

    def __len__(self) -> int:
        """返回注册的元数据数量。"""
        return len(self._metadata)
