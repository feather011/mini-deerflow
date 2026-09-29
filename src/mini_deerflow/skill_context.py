"""Skill 上下文构建器。"""

from __future__ import annotations

from typing import Sequence

from mini_deerflow.skill import Skill

# 固定头部常量
_SKILL_CONTEXT_HEADER = "以下技能指令仅用于完成当前任务。\n\n"
_SKILL_CONTEXT_SINGLE_HEADER = "技能指令\n\n## {name}\n\n用途：{description}\n\n"
_SKILL_CONTEXT_MULTIPLE_HEADER = "技能指令\n\n## {name}\n\n用途：{description}\n\n"


class SkillContextBuilder:
    """Skill 上下文构建器。

    职责：
    - 将 Skill 列表格式化为上下文文本
    - 不读取文件
    - 不修改 Skill
    - 保持选择顺序
    """

    def build(self, skills: Sequence[Skill]) -> str:
        """将 Skill 列表构建为上下文文本。

        格式：
        - 空列表返回 ""
        - 单个 Skill：
          技能指令

          ## {name}

          用途：{description}

          {instructions}
        - 多个 Skill：
          以下技能指令仅用于完成当前任务。

          ## {name1}

          用途：{description1}

          {instructions1}
          ## {name2}

          用途：{description2}

          {instructions2}
        """
        # 输入校验
        if isinstance(skills, str):
            raise TypeError("skills 不能是 str")

        if not isinstance(skills, Sequence):
            raise TypeError(
                f"skills 必须是 Sequence，收到: {type(skills).__name__}"
            )

        # 检查元素类型和重复名称
        seen_names: set[str] = set()
        for skill in skills:
            if not isinstance(skill, Skill):
                raise ValueError(
                    f"skills 中的元素必须是 Skill，收到: {type(skill).__name__}"
                )
            if skill.name in seen_names:
                raise ValueError(f"skills 中存在重复的 name: {skill.name}")
            seen_names.add(skill.name)

        # 空列表返回空字符串
        if not skills:
            return ""

        # 单个 Skill
        if len(skills) == 1:
            skill = skills[0]
            return (
                f"{_SKILL_CONTEXT_HEADER}"
                f"## {skill.name}\n\n"
                f"用途：{skill.description}\n\n"
                f"{skill.instructions}\n"
            )

        # 多个 Skill
        parts = [_SKILL_CONTEXT_HEADER]
        for skill in skills:
            parts.append(f"## {skill.name}\n\n")
            parts.append(f"用途：{skill.description}\n\n")
            parts.append(f"{skill.instructions}\n\n")

        return "".join(parts)

    def build_single(self, skill: Skill) -> str:
        """构建单个 Skill 的上下文。"""
        return (
            f"{_SKILL_CONTEXT_HEADER}"
            f"{skill.instructions}\n"
        )
