"""Phase 25A 滚动批次校验与资源冲突检查。

两块职责：

1. **滚动批次校验**：一批 `WorkItemProposal` 要变成可执行的 `WorkItem`，
   必须通过 work_id 唯一性、依赖闭合（已验证历史 ∪ 当前批次）、
   禁止自依赖、批次内无环，以及目标字段非空。
2. **资源冲突**：并行调度按资源读写声明判定——读读并行、写写/读写冲突、
   副作用不明强制冲突（保守串行）。

本模块纯函数、无 I/O、import 无副作用。
"""

from __future__ import annotations

import re

from .errors import (
    CyclicDependencyError,
    DuplicateWorkIdError,
    EmptyGoalFieldError,
    MissingDependencyError,
    ResourceConflictError,
    SelfDependencyError,
    UnknownResourceError,
)
from .models import (
    GoalContract,
    WorkItem,
    WorkItemProposal,
)
from .state_machine import ResourceAccessMode

__all__ = [
    "check_resource_conflicts",
    "conflicts",
    "is_path_resource",
    "normalize_resource",
    "validate_batch",
    "validate_parallel_safe",
]


# ---------------------------------------------------------------------------
# 资源规范化
# ---------------------------------------------------------------------------

_MULTI_SLASH = re.compile(r"/{2,}")

# 显式声明为路径型资源的 scheme 前缀
_PATH_SCHEMES = ("file:", "workspace:", "path:")
# 形如 `name.ext` 的裸文件名也算路径（例如 `A.txt`）
_BARE_FILENAME = re.compile(r"^[^/\\:]+\.[A-Za-z0-9]+$")


def is_path_resource(resource: str) -> bool:
    """该资源是否是**路径型**（文件 / workspace 等）。

    路径型资源适用文件系统语义：分隔符归一、折叠 `.`、拒绝 `..`、
    **大小写折叠**。非路径型（如 `cache`、`db:orders`、`redis:key`）
    是逻辑标识符，保持**精确匹配 + 原样大小写**。

    判定顺序：
    1. 带 `file:` / `workspace:` / `path:` scheme → 路径型
    2. 含路径分隔符（正斜杠或反斜杠）→ 路径型
    3. 形如 `name.ext` 的裸文件名 → 路径型
    4. 其余 → 逻辑资源
    """
    if not isinstance(resource, str):
        raise TypeError(f"资源必须是字符串，收到 {type(resource).__name__}")
    if resource.startswith(_PATH_SCHEMES):
        return True
    if "/" in resource or "\\" in resource:
        return True
    return bool(_BARE_FILENAME.match(resource))


def normalize_resource(resource: str) -> str:
    """把资源声明规范化到同一坐标系，冲突比较才有效。

    **路径型资源**（见 `is_path_resource`）：
    - 统一 `\\` → `/`，折叠重复分隔符
    - 折叠 `.` 段（`./a` 与 `a` 同一资源）
    - **拒绝未解析的 `..`**（宁可报错也不静默解析成另一个路径）
    - **`casefold()` 折叠大小写**——Windows 文件系统大小写不敏感，
      `A.txt` 与 `a.txt` 可能是同一个文件；不折叠会把它们判为不同资源
      而**允许并行**，那是漏掉真实冲突。代价是 Linux 上可能多出一些
      串行，但保守方向只能是「多串行」，不能是「漏冲突」。
    - 保留 scheme 前缀与根前导 `/`

    **非路径型资源**：只做去空白与基础去重，**不做**大小写折叠、
    **不套用**文件路径规则——逻辑标识符的大小写通常是有意义的区分。

    Raises:
        UnknownResourceError: 空、非字符串、或路径含 `..`。
    """
    if not isinstance(resource, str):
        raise UnknownResourceError(f"资源必须是字符串，收到 {type(resource).__name__}")
    cleaned = resource.strip()
    if not cleaned:
        raise UnknownResourceError("资源不能为空")

    if not is_path_resource(cleaned):
        # 逻辑资源：只折叠多余空白与重复分隔符的机械形式，保留大小写
        return cleaned

    # 拆出 scheme（`file:` / `workspace:` / `path:`）
    scheme = ""
    body = cleaned
    for prefix in _PATH_SCHEMES:
        if body.startswith(prefix):
            scheme = prefix
            body = body[len(prefix) :]
            break

    unified = body.replace("\\", "/")
    unified = _MULTI_SLASH.sub("/", unified)

    is_root = unified.startswith("/")
    segments: list[str] = []
    for segment in unified.split("/"):
        if segment == "..":
            # 不做路径穿越解析：`a/../b` 可能解析到预期之外的位置，
            # 也可能跨出 workspace 边界——直接拒绝，让声明方写清楚
            raise UnknownResourceError(f"资源不能包含未解析的 '..': {resource!r}")
        if segment in ("", "."):
            continue  # 折叠 `.` 段与空段
        segments.append(segment)

    body_normalized = "/".join(segments)
    if is_root:
        # 即使 segments 为空也要保留根：`/` 本身是一个合法资源
        body_normalized = "/" + body_normalized

    result = scheme + body_normalized
    if not result:
        raise UnknownResourceError(f"资源规范化后为空: {resource!r}")
    # 路径型统一折叠大小写（见 docstring：漏冲突比多串行危险）
    return result.casefold()


def conflicts(
    left: ResourceAccessMode, right: ResourceAccessMode
) -> bool:
    """两条访问方式对**同一资源**是否冲突。

    冻结规则：
    - 读–读 → **允许并行**（False）
    - 任一侧为 `UNKNOWN`（副作用不明）→ **强制冲突**（True）
    - 写–写、读–写 → 冲突（True）

    `UNKNOWN` 的优先级最高：它是「我不知道」，不能因为另一侧是读就放过——
    分不清时选择串行的代价只是慢，错误并行的代价是数据竞争。

    注意本函数只判断「同资源」的读写冲突；**父目录与子文件是否算同一
    资源**由 `check_resource_conflicts` 的路径重叠判定负责。
    """
    if not isinstance(left, ResourceAccessMode) or not isinstance(
        right, ResourceAccessMode
    ):
        raise TypeError("访问方式必须是 ResourceAccessMode")
    if left is ResourceAccessMode.UNKNOWN or right is ResourceAccessMode.UNKNOWN:
        return True
    if left is ResourceAccessMode.READ and right is ResourceAccessMode.READ:
        return False
    return True


def _paths_overlap(left: str, right: str) -> bool:
    """两条**已规范化、已 casefold** 的路径型资源是否指向同一片区域。

    相等，或一方是另一方的父目录（`file:src` ⊃ `file:src/agent.py`）。
    只对路径型资源调用——逻辑标识符没有「父目录」概念。
    """
    if left == right:
        return True
    return left.startswith(right + "/") or right.startswith(left + "/")


def _same_resource(left: str, right: str) -> bool:
    """两条规范化资源是否指向同一目标（含父子重叠）。"""
    if left == right:
        return True
    # 只有双方都是路径型时才谈父子重叠；逻辑资源保持精确匹配
    if is_path_resource(left) and is_path_resource(right):
        return _paths_overlap(left, right)
    return False


def check_resource_conflicts(
    claims_left: tuple, claims_right: tuple
) -> list[tuple[str, str, str]]:
    """比较两组资源声明，返回全部冲突（资源, 左访问, 右访问）。

    同一资源的配对用 `_same_resource`——路径型资源还会检查**父子重叠**
    （父目录写 + 子文件读也判冲突，只要双方不是读读）。
    """
    hits: list[tuple[str, str, str]] = []
    for left in claims_left:
        for right in claims_right:
            if not _same_resource(left.resource, right.resource):
                continue
            # 只读与只读允许并行；其余（含任一 UNKNOWN）冲突
            if conflicts(left.access, right.access):
                hits.append(
                    (left.resource, left.access.value, right.access.value)
                )
    return hits


def validate_parallel_safe(groups: tuple[tuple, ...]) -> None:
    """判定一组执行路线能否全部并行；存在任意冲突即抛错。

    任意两两比较——资源冲突不具传递性，只比较相邻对会漏掉跨对冲突。
    """
    for i in range(len(groups)):
        for j in range(i + 1, len(groups)):
            hits = check_resource_conflicts(groups[i], groups[j])
            if hits:
                resource, left, right = hits[0]
                raise ResourceConflictError(resource, left, right)


# ---------------------------------------------------------------------------
# 滚动批次校验
# ---------------------------------------------------------------------------


def _detect_cycle(graph: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    """在依赖图里找一个环，返回环路径（首尾同 id）；无环返回空元组。

    用迭代式 DFS + 三色标记（白/灰/黑），避免深批次下的递归深度问题。
    灰色节点再次被访问即发现环，沿父指针回溯出完整环路。
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {node: WHITE for node in graph}
    parent: dict[str, str | None] = {node: None for node in graph}

    for start in graph:
        if color[start] != WHITE:
            continue
        stack: list[str] = [start]
        color[start] = GRAY
        while stack:
            node = stack[-1]
            advanced = False
            for neighbor in graph.get(node, ()):
                if neighbor not in color:
                    continue  # 依赖指向已验证历史，不在本批图中
                state = color[neighbor]
                if state == GRAY:
                    # 发现环：从 node 沿 parent 回溯到 neighbor，再闭合
                    cycle = [neighbor]
                    cursor: str | None = node
                    while cursor is not None and cursor != neighbor:
                        cycle.append(cursor)
                        cursor = parent[cursor]
                    cycle.append(neighbor)
                    cycle.reverse()
                    return tuple(cycle)
                if state == WHITE:
                    color[neighbor] = GRAY
                    parent[neighbor] = node
                    stack.append(neighbor)
                    advanced = True
                    break
            if not advanced:
                color[node] = BLACK
                stack.pop()
    return ()


def validate_batch(
    proposals: tuple[WorkItemProposal, ...] | list[WorkItemProposal],
    *,
    verified_work_ids: frozenset[str] | set[str],
    goal: GoalContract,
) -> tuple[WorkItem, ...]:
    """校验一个滚动批次，返回可执行的 `WorkItem` 序列。

    校验项（顺序即优先级，失败即抛）：
    1. `goal.objective` 与 `goal.acceptance_criteria` 非空——空目标没有
       成功判据，滚动批次会永远规划下去（构造期已拦，这里防御性复核）。
    2. 批次内 work_id 唯一，且不与**已验证历史**重名。
    3. 每个条目的 objective 非空（同 GoalContract 理由）。
    4. 无自依赖。
    5. 每条依赖 ∈ 已验证历史 ∪ 当前批次（滚动批次不接受「未来任务」依赖）。
    6. 当前批次无循环依赖。

    Raises:
        EmptyGoalFieldError / DuplicateWorkIdError / SelfDependencyError:
            对应校验失败。
        MissingDependencyError: 依赖指向批次外且未经验证的 work_id。
        CyclicDependencyError: 批次内有环。
    """
    # 1. 目标非空
    if not str(goal.objective).strip():
        raise EmptyGoalFieldError("objective")
    if not goal.acceptance_criteria:
        raise EmptyGoalFieldError("acceptance_criteria")

    proposals = tuple(proposals)
    known = frozenset(verified_work_ids)

    # 2. 批次内唯一 + 不与已验证历史重名
    seen: set[str] = set()
    for proposal in proposals:
        if proposal.work_id in seen:
            raise DuplicateWorkIdError(proposal.work_id)
        if proposal.work_id in known:
            raise DuplicateWorkIdError(proposal.work_id)
        seen.add(proposal.work_id)

    graph: dict[str, tuple[str, ...]] = {}
    for proposal in proposals:
        # 3. 单条 objective 非空（WorkItemProposal 构造期已拦，防御性复核）
        if not proposal.objective.strip():
            raise EmptyGoalFieldError("objective")

        # 4. 禁止自依赖
        if proposal.work_id in proposal.depends_on:
            raise SelfDependencyError(proposal.work_id)

        # 5. 依赖必须存在于已验证历史或当前批次
        missing = tuple(
            dep
            for dep in proposal.depends_on
            if dep not in known and dep not in seen
        )
        if missing:
            raise MissingDependencyError(proposal.work_id, missing)

        in_batch = tuple(dep for dep in proposal.depends_on if dep in seen)
        graph[proposal.work_id] = in_batch

    # 6. 批次内循环依赖
    cycle = _detect_cycle(graph)
    if cycle:
        raise CyclicDependencyError(cycle)

    return tuple(
        WorkItem.from_proposal(proposal, status=None) for proposal in proposals
    )
