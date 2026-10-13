"""Phase 25A / 滚动批次校验与资源冲突测试。

覆盖规格必测场景：重复 ID、缺失依赖、自依赖、循环依赖、目标字段非空，
以及读读/写写/读写/unknown 四类资源冲突与资源规范化。
"""

from __future__ import annotations

import pytest

from mini_deerflow.lead_agent import (
    CyclicDependencyError,
    DuplicateWorkIdError,
    EmptyGoalFieldError,
    GoalContract,
    MissingDependencyError,
    ResourceAccessMode,
    ResourceClaim,
    ResourceConflictError,
    SelfDependencyError,
    UnknownResourceError,
    WorkItemStatus,
    check_resource_conflicts,
    conflicts,
    is_path_resource,
    normalize_resource,
    validate_batch,
    validate_parallel_safe,
    WorkItemProposal,
)

T0 = 1_700_000_000.0


def _goal(**overrides) -> GoalContract:
    payload: dict = dict(
        goal_id="goal-1",
        objective="验证滚动批次",
        acceptance_criteria=("批次合法",),
        created_at=T0,
    )
    payload.update(overrides)
    return GoalContract(**payload)


def _p(wid: str, deps: tuple[str, ...] = (), claims: tuple = ()) -> WorkItemProposal:
    return WorkItemProposal(
        work_id=wid,
        objective=f"完成 {wid}",
        depends_on=deps,
        claims=claims,
        created_at=T0,
    )


# ---------------------------------------------------------------------------
# 滚动批次校验
# ---------------------------------------------------------------------------


def test_valid_batch_produces_work_items() -> None:
    items = validate_batch(
        (_p("a"), _p("b", deps=("a",)), _p("c", deps=("a", "b"))),
        verified_work_ids=frozenset(),
        goal=_goal(),
    )
    assert tuple(i.work_id for i in items) == ("a", "b", "c")
    assert all(i.status is WorkItemStatus.READY for i in items)


def test_batch_accepts_dependency_on_verified_history() -> None:
    """依赖可以指向已验证历史——这是滚动批次的核心语义。"""
    items = validate_batch(
        (_p("new-1", deps=("already-verified",)),),
        verified_work_ids=frozenset({"already-verified"}),
        goal=_goal(),
    )
    assert items[0].depends_on == ("already-verified",)


def test_empty_batch_is_accepted() -> None:
    """空批次合法：规划可以得出「本批无可执行项」。"""
    assert validate_batch(
        (), verified_work_ids=frozenset(), goal=_goal()
    ) == ()


# --- 目标字段 ---


def test_empty_objective_rejected_at_contract_construction() -> None:
    """objective 为空的第一道防线在 GoalContract 构造期。

    构造期就拦下，因此无法构造一个「objective 为空」的合法 GoalContract
    再喂给 validate_batch——批次层的复核是防御性的第二道防线。
    """
    with pytest.raises(EmptyGoalFieldError, match="objective"):
        _goal(objective=" ")


def test_empty_acceptance_criteria_rejected_at_contract_construction() -> None:
    """acceptance_criteria 为空同样在构造期即被领域异常拦下。"""
    with pytest.raises(EmptyGoalFieldError, match="acceptance_criteria"):
        _goal(acceptance_criteria=())


# --- ID 唯一 ---


def test_duplicate_id_inside_batch_rejected() -> None:
    with pytest.raises(DuplicateWorkIdError, match="a"):
        validate_batch(
            (_p("a"), _p("a")), verified_work_ids=frozenset(), goal=_goal()
        )


def test_duplicate_id_against_verified_history_rejected() -> None:
    """已验证历史里的 work_id 不可被本批再次认领（否则事实归属混乱）。"""
    with pytest.raises(DuplicateWorkIdError, match="done"):
        validate_batch(
            (_p("done"),),
            verified_work_ids=frozenset({"done"}),
            goal=_goal(),
        )


# --- 依赖规则 ---


def test_self_dependency_rejected() -> None:
    with pytest.raises(SelfDependencyError, match="solo"):
        validate_batch(
            (_p("solo", deps=("solo",)),), verified_work_ids=frozenset(), goal=_goal()
        )


def test_missing_dependency_rejected() -> None:
    """依赖既不在历史也不在本批 = 引用了不存在的未来任务。"""
    with pytest.raises(MissingDependencyError) as excinfo:
        validate_batch(
            (_p("a", deps=("ghost",)),),
            verified_work_ids=frozenset(),
            goal=_goal(),
        )
    assert excinfo.value.missing == ("ghost",)
    assert excinfo.value.work_id == "a"


def test_dependency_may_point_to_later_item_in_same_batch() -> None:
    """同批内可以引用后声明的条目——批次校验看的是集合，不是顺序。"""
    items = validate_batch(
        (_p("b", deps=("a",)), _p("a")),
        verified_work_ids=frozenset(),
        goal=_goal(),
    )
    assert {i.work_id for i in items} == {"a", "b"}


# --- 循环依赖 ---


def test_direct_cycle_rejected() -> None:
    with pytest.raises(CyclicDependencyError) as excinfo:
        validate_batch(
            (_p("x", deps=("y",)), _p("y", deps=("x",))),
            verified_work_ids=frozenset(),
            goal=_goal(),
        )
    assert set(excinfo.value.cycle) == {"x", "y"}


def test_three_node_cycle_rejected() -> None:
    with pytest.raises(CyclicDependencyError) as excinfo:
        validate_batch(
            (_p("a", deps=("c",)), _p("b", deps=("a",)), _p("c", deps=("b",))),
            verified_work_ids=frozenset(),
            goal=_goal(),
        )
    cycle = excinfo.value.cycle
    assert len(cycle) >= 3
    assert cycle[0] == cycle[-1]  # 环闭合


def test_self_loop_reported_as_cycle_or_self_error() -> None:
    """自依赖先被 SelfDependencyError 拦截（在环检测之前）。"""
    with pytest.raises(SelfDependencyError):
        validate_batch(
            (_p("n", deps=("n",)),), verified_work_ids=frozenset(), goal=_goal()
        )


def test_diamond_dependency_is_not_a_cycle() -> None:
    """菱形依赖（a←b, a←c, b/c←d）不是环，必须放行。"""
    items = validate_batch(
        (_p("d"), _p("b", deps=("d",)), _p("c", deps=("d",)), _p("a", deps=("b", "c"))),
        verified_work_ids=frozenset(),
        goal=_goal(),
    )
    assert len(items) == 4


def test_cycle_detection_handles_fan_out_graph() -> None:
    """大批次不因图规模出问题（迭代式 DFS，不递归）。"""
    # n0<-n1<-...<-n99 单链，再加 n99<-n0 闭合成 100 节点的大环
    chain = [_p(f"n{i}", deps=(f"n{i + 1}",)) for i in range(99)]
    chain.append(_p("n99", deps=("n0",)))
    with pytest.raises(CyclicDependencyError):
        validate_batch(tuple(chain), verified_work_ids=frozenset(), goal=_goal())


def test_missing_beats_cycle_ordering() -> None:
    """依赖缺失先于环检测报错（更具体的诊断）。"""
    with pytest.raises(MissingDependencyError):
        validate_batch(
            (_p("a", deps=("ghost", "b")), _p("b", deps=("a",))),
            verified_work_ids=frozenset(),
            goal=_goal(),
        )


# ---------------------------------------------------------------------------
# 资源规范化
# ---------------------------------------------------------------------------


def test_normalize_strips_and_unifies_separators() -> None:
    assert normalize_resource("  data//store.db  ") == "data/store.db"
    assert normalize_resource("data\\sub\\file") == "data/sub/file"
    assert normalize_resource("path/to/") == "path/to"
    assert normalize_resource("/") == "/"


def test_path_resources_fold_case() -> None:
    """路径型资源折叠大小写——Windows 文件系统大小写不敏感，
    `A.txt` 与 `a.txt` 可能是同一个文件；不折叠会判为不同资源而
    **允许并行**，那是漏掉真实冲突。保守方向只能是「多串行」。"""
    assert normalize_resource("A.txt") == normalize_resource("a.txt")
    assert normalize_resource("/db/MyTable.sql") == normalize_resource("/db/mytable.sql")


def test_non_path_resources_keep_their_case_and_exact_match() -> None:
    """非路径型（逻辑标识符）保持大小写语义，不做文件路径式折叠。"""
    for logical in ("cache", "db:Orders", "redis:Key", "queue:jobs"):
        assert is_path_resource(logical) is False, logical
    # 大小写不同的逻辑资源是两个不同资源（不做折叠）
    assert normalize_resource("db:Orders") != normalize_resource("db:orders")
    assert normalize_resource("db:Orders") == "db:Orders"


def test_path_type_detection() -> None:
    assert is_path_resource("file:src") is True
    assert is_path_resource("workspace:/tmp") is True
    assert is_path_resource("/abs/path") is True
    assert is_path_resource("a\\b") is True
    assert is_path_resource("A.txt") is True
    assert is_path_resource("cache") is False
    assert is_path_resource("db:orders") is False


def test_normalize_folds_dot_segments() -> None:
    """`./a` 与 `a` 指向同一资源，不折叠会产生假资源。"""
    assert normalize_resource("./src/./agent.py") == normalize_resource("src/agent.py")
    assert normalize_resource("file:./src/x") == normalize_resource("file:src/x")


def test_normalize_rejects_parent_traversal() -> None:
    """拒绝未解析的 `..`：不静默解析，也不让声明跨出预期边界。"""
    with pytest.raises(UnknownResourceError, match=r"\.\."):
        normalize_resource("src/../etc/passwd")
    with pytest.raises(UnknownResourceError, match=r"\.\."):
        normalize_resource("a/..")


def test_normalize_keeps_scheme_and_root() -> None:
    assert normalize_resource("file:src/agent.py") == "file:src/agent.py"
    assert normalize_resource("/ROOT/dir/") == "/root/dir"


def test_normalize_rejects_empty_and_non_string() -> None:
    with pytest.raises(UnknownResourceError, match="不能为空"):
        normalize_resource("   ")
    with pytest.raises(UnknownResourceError, match="字符串"):
        normalize_resource(None)  # type: ignore[arg-type]


def test_normalize_is_idempotent() -> None:
    once = normalize_resource("a//b/")
    assert normalize_resource(once) == once
    assert normalize_resource(normalize_resource("A.TXT")) == normalize_resource("A.TXT")


# ---------------------------------------------------------------------------
# 父子路径重叠（任务 4）
# ---------------------------------------------------------------------------


def _claim(resource: str, access: ResourceAccessMode) -> ResourceClaim:
    return ResourceClaim(resource=normalize_resource(resource), access=access)


def test_parent_dir_write_and_child_file_read_conflict() -> None:
    """父目录写 + 子文件读 → 冲突：写父目录会波及子文件的内容。"""
    hits = check_resource_conflicts(
        (_claim("file:src", ResourceAccessMode.WRITE),),
        (_claim("file:src/agent.py", ResourceAccessMode.READ),),
    )
    assert hits == [("file:src", "write", "read")]


def test_parent_dir_read_and_child_file_write_conflict() -> None:
    """父目录读 + 子文件写 → 冲突。"""
    hits = check_resource_conflicts(
        (_claim("file:src", ResourceAccessMode.READ),),
        (_claim("file:src/agent.py", ResourceAccessMode.WRITE),),
    )
    assert hits == [("file:src", "read", "write")]


def test_parent_dir_read_and_child_file_read_allowed() -> None:
    """父目录读 + 子文件读 → 读读并行，父子重叠也不例外。"""
    hits = check_resource_conflicts(
        (_claim("file:src", ResourceAccessMode.READ),),
        (_claim("file:src/agent.py", ResourceAccessMode.READ),),
    )
    assert hits == []


def test_write_then_read_overlapping_paths_via_validate_parallel_safe() -> None:
    """走完整并行判定路径，父子冲突必须抛错。"""
    left = (_claim("file:src", ResourceAccessMode.WRITE),)
    right = (_claim("file:src/agent.py", ResourceAccessMode.READ),)
    with pytest.raises(ResourceConflictError) as excinfo:
        validate_parallel_safe((left, right))
    assert excinfo.value.resource == "file:src"


def test_sibling_files_do_not_overlap() -> None:
    """同目录下的兄弟文件互不重叠：`src/a.py` 不覆盖 `src/b.py`。"""
    hits = check_resource_conflicts(
        (_claim("file:src/a.py", ResourceAccessMode.WRITE),),
        (_claim("file:src/b.py", ResourceAccessMode.READ),),
    )
    assert hits == []


def test_non_path_resource_ignores_prefix_semantics() -> None:
    """逻辑资源没有「父目录」：`db` 与 `db:orders` 是两个不同资源。"""
    hits = check_resource_conflicts(
        (_claim("db", ResourceAccessMode.WRITE),),
        (_claim("db:orders", ResourceAccessMode.READ),),
    )
    assert hits == []


def test_case_differing_path_resources_conflict_on_write_read() -> None:
    """任务 4 点名场景：`A.txt` 写 + `a.txt` 读必须判冲突。"""
    hits = check_resource_conflicts(
        (_claim("A.txt", ResourceAccessMode.WRITE),),
        (_claim("a.txt", ResourceAccessMode.READ),),
    )
    assert hits, "大小写不同的路径资源在 Windows 下可能同文件，写读必须冲突"
    assert hits[0][0] == "a.txt"  # 已 casefold


def test_case_differing_path_read_read_still_allowed() -> None:
    """即便折叠了大小写，读读仍然并行。"""
    hits = check_resource_conflicts(
        (_claim("A.txt", ResourceAccessMode.READ),),
        (_claim("a.txt", ResourceAccessMode.READ),),
    )
    assert hits == []


# ---------------------------------------------------------------------------
# 冲突规则
# ---------------------------------------------------------------------------


def test_read_read_allows_parallel() -> None:
    assert conflicts(ResourceAccessMode.READ, ResourceAccessMode.READ) is False


def test_write_write_conflicts() -> None:
    assert conflicts(ResourceAccessMode.WRITE, ResourceAccessMode.WRITE) is True


def test_read_write_conflicts_both_orders() -> None:
    assert conflicts(ResourceAccessMode.READ, ResourceAccessMode.WRITE) is True
    assert conflicts(ResourceAccessMode.WRITE, ResourceAccessMode.READ) is True


def test_unknown_always_conflicts() -> None:
    """副作用不明 → 强制冲突，即便对面是读。"""
    for other in (
        ResourceAccessMode.READ,
        ResourceAccessMode.WRITE,
        ResourceAccessMode.UNKNOWN,
    ):
        assert conflicts(ResourceAccessMode.UNKNOWN, other) is True
        assert conflicts(other, ResourceAccessMode.UNKNOWN) is True


def test_conflicts_rejects_non_route_type() -> None:
    with pytest.raises(TypeError):
        conflicts("read", "write")  # type: ignore[arg-type]


def test_check_resource_conflicts_pairs_same_resource_only() -> None:
    left = (
        ResourceClaim(resource="/db", access=ResourceAccessMode.WRITE),
        ResourceClaim(resource="/only-left", access=ResourceAccessMode.WRITE),
    )
    right = (
        ResourceClaim(resource="/db", access=ResourceAccessMode.READ),
        ResourceClaim(resource="/only-right", access=ResourceAccessMode.WRITE),
    )
    hits = check_resource_conflicts(left, right)
    assert hits == [("/db", "write", "read")]


def test_check_resource_conflicts_empty_for_disjoint_resources() -> None:
    left = (ResourceClaim(resource="/a", access=ResourceAccessMode.WRITE),)
    right = (ResourceClaim(resource="/b", access=ResourceAccessMode.WRITE),)
    assert check_resource_conflicts(left, right) == []


def test_parallel_safe_group_passes_for_disjoint_or_read_only() -> None:
    g1 = (
        ResourceClaim(resource="/db", access=ResourceAccessMode.READ),
        ResourceClaim(resource="/api", access=ResourceAccessMode.READ),
    )
    g2 = (ResourceClaim(resource="/db", access=ResourceAccessMode.READ),)
    g3 = (ResourceClaim(resource="/other", access=ResourceAccessMode.WRITE),)
    validate_parallel_safe((g1, g2, g3))  # 不应抛


def test_parallel_safe_rejects_write_write() -> None:
    g1 = (ResourceClaim(resource="/db", access=ResourceAccessMode.WRITE),)
    g2 = (ResourceClaim(resource="/db", access=ResourceAccessMode.WRITE),)
    with pytest.raises(ResourceConflictError) as excinfo:
        validate_parallel_safe((g1, g2))
    assert excinfo.value.resource == "/db"
    assert excinfo.value.left == "write"
    assert excinfo.value.right == "write"


def test_parallel_safe_rejects_unknown_side_effect() -> None:
    g1 = (ResourceClaim(resource="/cache", access=ResourceAccessMode.UNKNOWN),)
    g2 = (ResourceClaim(resource="/cache", access=ResourceAccessMode.READ),)
    with pytest.raises(ResourceConflictError):
        validate_parallel_safe((g1, g2))


def test_parallel_safe_detects_non_adjacent_conflict() -> None:
    """冲突不具传递性：必须两两比较，只比相邻会漏掉 0 vs 2。"""
    g0 = (ResourceClaim(resource="/x", access=ResourceAccessMode.WRITE),)
    g1 = (ResourceClaim(resource="/y", access=ResourceAccessMode.WRITE),)
    g2 = (ResourceClaim(resource="/x", access=ResourceAccessMode.READ),)
    with pytest.raises(ResourceConflictError) as excinfo:
        validate_parallel_safe((g0, g1, g2))
    assert excinfo.value.resource == "/x"


def test_parallel_safe_accepts_single_group() -> None:
    validate_parallel_safe(((ResourceClaim(resource="/z", access=ResourceAccessMode.WRITE),),))
