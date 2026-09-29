"""Skill 选择器：基于规则的渐进式选择。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable, Sequence

from mini_deerflow.skill_metadata import SkillMetadata, normalize_selection_text


@runtime_checkable
class SkillSelector(Protocol):
    """Skill 选择器接口。"""

    def select(
        self,
        user_input: str,
        skills: Sequence[SkillMetadata],
    ) -> list[SkillMetadata]:
        """根据用户输入从候选列表中选择匹配的 SkillMetadata。"""
        ...


class RuleBasedSkillSelector:
    """基于规则的 Skill 选择器。

    支持：
    - 显式选择：@skill-name
    - 关键词选择：基于 rules 配置的触发词匹配
    """

    def __init__(
        self,
        rules: dict[str, Sequence[str]],
        max_skills: int = 3,
    ) -> None:
        """初始化选择器。

        Args:
            rules: 映射 Skill name 到触发词列表
            max_skills: 最大返回数量
        """
        self._rules = self._validate_rules(rules)
        self._max_skills = self._validate_max_skills(max_skills)

    def _validate_rules(self, rules: dict[str, Sequence[str]]) -> dict[str, frozenset[str]]:
        """验证并标准化 rules 配置。"""
        if not isinstance(rules, dict):
            raise TypeError(f"rules 必须是 dict，收到: {type(rules).__name__}")

        validated: dict[str, frozenset[str]] = {}
        for name, triggers in rules.items():
            # name 验证
            if not isinstance(name, str) or not name.strip():
                raise TypeError(f"rules 的 key 必须是非空字符串，收到: {type(name).__name__}")

            # triggers 验证
            if isinstance(triggers, str):
                raise TypeError(
                    f"rules[{name!r}] 的 value 不能是 str（必须是 Sequence[str]）"
                )

            if not isinstance(triggers, Sequence):
                raise TypeError(
                    f"rules[{name!r}] 必须是 Sequence，收到: {type(triggers).__name__}"
                )

            # 标准化触发词
            normalized_triggers: set[str] = set()
            for trigger in triggers:
                if not isinstance(trigger, str):
                    raise TypeError(
                        f"rules[{name!r}] 中的触发词必须是 str，"
                        f"收到: {type(trigger).__name__}"
                    )

                normalized = trigger.strip()
                if not normalized:
                    raise ValueError(
                        f"rules[{name!r}] 中的触发词不能为空"
                    )

                # 标准化后去重
                normalized_triggers.add(normalize_selection_text(normalized))

            validated[name] = frozenset(normalized_triggers)

        return validated

    def _validate_max_skills(self, max_skills: int) -> int:
        """验证 max_skills 参数。"""
        if isinstance(max_skills, bool):
            raise TypeError("max_skills 不能是 bool 类型")

        if not isinstance(max_skills, int):
            raise TypeError(
                f"max_skills 必须是 int，收到: {type(max_skills).__name__}"
            )

        if max_skills <= 0:
            raise ValueError(f"max_skills 必须大于 0，收到: {max_skills}")

        return max_skills

    def select(
        self,
        user_input: str,
        skills: Sequence[SkillMetadata],
    ) -> list[SkillMetadata]:
        """根据用户输入选择匹配的 SkillMetadata。

        优先级：
        1. 显式选择（@skill-name）
        2. 关键词匹配（基于 rules）

        排序：
        1. 显式选择
        2. 关键词匹配（matched_count 降序，longest_match 降序，name 升序）
        3. 截断到 max_skills
        """
        # 输入校验
        if not isinstance(user_input, str):
            raise TypeError(f"user_input 必须是 str，收到: {type(user_input).__name__}")

        if isinstance(skills, str):
            raise TypeError(f"skills 不能是 str，必须是 Sequence[SkillMetadata]")

        if not isinstance(skills, Sequence):
            raise TypeError(
                f"skills 必须是 Sequence，收到: {type(skills).__name__}"
            )

        # 空白输入返回空
        normalized_input = user_input.strip()
        if not normalized_input:
            return []

        # 验证 skills 元素类型
        name_to_metadata: dict[str, SkillMetadata] = {}
        for skill in skills:
            if not isinstance(skill, SkillMetadata):
                raise ValueError(
                    f"skills 中的元素必须是 SkillMetadata，收到: {type(skill).__name__}"
                )
            if skill.name in name_to_metadata:
                raise ValueError(f"skills 中存在重复的 name: {skill.name}")
            name_to_metadata[skill.name] = skill

        # 解析显式选择
        explicit_names: list[str] = []
        remaining_input = normalized_input

        while True:
            # 查找 @ 开头的位置
            at_pos = remaining_input.find("@")
            if at_pos == -1:
                break

            # 提取 @ 后面的内容
            after_at = remaining_input[at_pos + 1 :]
            # 找到下一个空白或字符串结尾
            space_pos = after_at.find(" ")
            if space_pos == -1:
                name_candidate = after_at
                remaining_input = ""
            else:
                name_candidate = after_at[:space_pos]
                remaining_input = after_at[space_pos:].lstrip()

            # 验证并添加显式选择的 name
            if name_candidate and name_candidate in name_to_metadata:
                if name_candidate not in explicit_names:
                    explicit_names.append(name_candidate)

            if not remaining_input:
                break

        # 关键词匹配
        matched_skills: list[tuple[SkillMetadata, int, int]] = []  # (metadata, count, longest)

        if remaining_input.strip():
            normalized_remaining = normalize_selection_text(remaining_input)

            for skill in skills:
                if skill.name in self._rules:
                    triggers = self._rules[skill.name]
                    matched_count = 0
                    longest_match = 0

                    for trigger in triggers:
                        if trigger in normalized_remaining:
                            matched_count += 1
                            longest_match = max(longest_match, len(trigger))

                    if matched_count > 0:
                        # 不重复添加显式选中的
                        if skill.name not in explicit_names:
                            matched_skills.append((skill, matched_count, longest_match))

        # 排序关键词匹配结果
        matched_skills.sort(
            key=lambda x: (-x[1], -x[2], x[0].name)  # count 降序，longest 降序，name 升序
        )

        # 构建最终结果
        result: list[SkillMetadata] = []

        # 1. 显式选择（按出现顺序）
        for name in explicit_names:
            result.append(name_to_metadata[name])

        # 2. 关键词匹配
        for skill, _, _ in matched_skills:
            result.append(skill)

        # 3. 截断
        return result[: self._max_skills]
