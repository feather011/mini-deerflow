"""Skill 上下文提供者协议与默认实现。"""

from __future__ import annotations

from typing import Protocol

from mini_deerflow.skill_catalog import SkillCatalog
from mini_deerflow.skill_context import SkillContextBuilder
from mini_deerflow.skill_loader import SkillLoader
from mini_deerflow.skill_metadata import SkillMetadata
from mini_deerflow.skill_selector import SkillSelector


class SkillContextProvider(Protocol):
    """可以提供 Skill 上下文的对象。

    职责：
    - 输入本轮用户原始输入
    - 返回本轮 Skill 上下文文本
    - 没有选中 Skill 时返回空字符串
    - 不返回 None
    - 不直接修改 Agent
    - 不持久化状态到 ThreadState
    """

    def build(self, user_input: str, thread_id: str = "") -> str:
        """根据用户输入构建并返回 Skill 上下文文本。

        Args:
            user_input: 本轮用户原始输入
            thread_id: 当前线程 ID（可选，供需要区分项目会话的 Provider 使用）

        Returns:
            Skill 上下文文本，无选中 Skill 时返回空字符串
        """
        ...


class DefaultSkillContextProvider:
    """默认 Skill 上下文提供者。

    组合 SkillCatalog、SkillSelector、SkillLoader、SkillContextBuilder
    实现渐进式 Skill 选择和上下文构建。

    执行顺序：
    1. catalog.list_metadata() 调用一次
    2. selector.select() 调用一次
    3. 无选择结果时返回 ""
    4. 有选择结果时 loader.load_selected() 调用一次
    5. builder.build() 调用一次并返回
    """

    def __init__(
        self,
        catalog: SkillCatalog,
        selector: SkillSelector,
        loader: SkillLoader,
        builder: SkillContextBuilder,
    ) -> None:
        self._catalog = catalog
        self._selector = selector
        self._loader = loader
        self._builder = builder

    def build(self, user_input: str, thread_id: str = "") -> str:
        """根据用户输入构建 Skill 上下文。

        执行顺序：
        1. 调用 catalog.list_metadata() 获取候选列表
        2. 调用 selector.select() 选择匹配的 Skill
        3. 无选择时返回 ""
        4. 有选择时调用 loader.load_selected()
        5. 调用 builder.build() 构建上下文并返回

        Args:
            user_input: 本轮用户原始输入
            thread_id: 当前线程 ID（可选）
        """
        if not isinstance(user_input, str):
            raise TypeError(
                f"user_input 必须是 str，收到: {type(user_input).__name__}"
            )

        # 步骤 1：获取所有元数据
        metadata = self._catalog.list_metadata()

        # 步骤 2：选择匹配的 Skill
        selected = self._selector.select(user_input, metadata)

        # 步骤 3：无选择结果时返回空字符串
        if not selected:
            return ""

        # 步骤 4：加载选中的 Skill
        loaded = self._loader.load_selected(selected)

        # 步骤 5：构建上下文并验证返回类型
        context = self._builder.build(loaded)

        if not isinstance(context, str):
            raise TypeError(
                f"SkillContextBuilder.build() 必须返回 str，"
                f"收到: {type(context).__name__}"
            )

        return context
