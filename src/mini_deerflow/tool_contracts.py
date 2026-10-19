"""TOOL-01：工具契约与授权策略基础（Phase 1）。

提供工具元数据、能力分类、运行权限和访问决策的基础契约。
不实现 Provider、搜索、MCP 管理或真实确认执行。

模块结构：
- ToolMetadata     → 工具的精确身份、能力集合、副作用分类
- ToolSource       → 工具来源（本地 / MCP）
- ToolCapability   → 原子能力枚举
- ToolSideEffect   → 副作用分类
- RunAuthority     → 执行主体的可信运行权限
- ToolAccessDecision → allow / confirm / deny 判定结果
- ToolResult       → 统一结果契约（未来序列化用）
- evaluate_tool_access() → 判定入口
- 内置工具元数据清单与默认策略构造
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any

from mini_deerflow.mcp_errors import MCPError


class MCPConfigError(MCPError):
    """MCP 配置非法（包括工具分类声明未知枚举名）。"""

# ---------------------------------------------------------------------------
# ToolSource
# ---------------------------------------------------------------------------


class ToolSourceKind(Enum):
    """工具来源。"""
    LOCAL = auto()      # 注册到 ToolRegistry 的本地工具
    MCP = auto()        # 通过 MCP 协议接入的远端工具


# ---------------------------------------------------------------------------
# ToolCapability
# ---------------------------------------------------------------------------


class ToolCapability(Enum):
    """原子能力枚举。模型不能提供可信的能力声明；未分类工具默认拒绝。

    能力与副作用分类分开：一个工具可需要多个能力。
    """
    # --- 计算 ---
    COMPUTE = auto()
    """纯计算，不产生外部可见副作用。"""

    # --- 会话读写 ---
    SESSION_HISTORY_READ = auto()
    """读取会话历史消息。"""
    SESSION_STATE_WRITE = auto()
    """写入会话状态（如 task_note），不修改项目文件。"""

    # --- 网络 ---
    NETWORK_READ = auto()
    """发起网络读取请求。"""
    NETWORK_WRITE = auto()
    """发起网络写入请求。"""

    # --- 项目操作 ---
    PROJECT_READ = auto()
    """读取项目绑定仓库内的文件或元信息。"""
    PROJECT_EXEC_DELEGATE = auto()
    """将编码任务委派给 harnessagent 执行（可写文件、运行命令）。"""
    PROJECT_MODIFY = auto()
    """直接修改项目绑定仓库内的文件（不含 project_execute 的 harness 委派路径）。"""

    # --- 命令执行 ---
    COMMAND_EXECUTE = auto()
    """执行本地命令（sandbox_run_command 等）。"""
    COMMAND_EXECUTE_RAW = auto()
    """执行任意本地命令（危险）。"""

    # --- 外部服务 ---
    EXTERNAL_SERVICE_READ = auto()
    """读取外部服务的 API。"""
    EXTERNAL_SERVICE_WRITE = auto()
    """写入外部服务的 API。"""

    # --- 子 Agent ---
    SUBAGENT_DELEGATE = auto()
    """向子 Agent 委派任务。"""


# ---------------------------------------------------------------------------
# ToolSideEffect
# ---------------------------------------------------------------------------


class ToolSideEffect(Enum):
    """副作用分类。与来源、能力分开；不用一个 read/write 字段承载所有语义。"""
    NONE = auto()           # 无外部副作用（纯计算）
    READS_SESSION = auto()  # 读取会话历史
    WRITES_SESSION = auto() # 写入会话状态
    READS_NETWORK = auto()  # 读网络
    WRITES_NETWORK = auto() # 写网络（POST/PUT/DELETE 等）
    READS_PROJECT = auto()  # 读项目文件
    WRITES_PROJECT = auto() # 写项目文件（含 project_execute）
    RUNS_COMMAND = auto()   # 执行本地命令
    DELEGATES_SUBAGENT = auto()  # 委派给子 Agent


# ---------------------------------------------------------------------------
# ToolMetadata
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolMetadata:
    """工具的精确身份与能力要求。

    模型不能提供可信的能力声明；未分类状态（uncategorized=True）
    默认拒绝。
    """

    name: str
    """工具名。必须与 ToolRegistry 中注册的名称一致。"""

    source: ToolSourceKind
    """工具来源。"""

    source_service: str | None = None
    """MCP 服务标识；仅 source=MCP时有意义。"""

    capabilities: frozenset[ToolCapability] = field(default_factory=lambda: frozenset())
    """所需能力集合。空集合表示"未分类"，默认拒绝。"""

    side_effects: frozenset[ToolSideEffect] = field(default_factory=lambda: frozenset())
    """副作用分类。空集合表示"无外部副作用"。"""

    uncategorized: bool = False
    """是否为未分类工具。未分类工具默认 deny。"""

    def is_safe(self) -> bool:
        """是否为无外部副作用工具。"""
        return (
            not self.uncategorized
            and ToolSideEffect.NONE in self.side_effects
        )

    def requires_confirmation(self) -> bool:
        """是否有外部副作用（默认需要本次确认）。"""
        if self.uncategorized:
            return True
        # 会话内部读写不算危险副作用；RUNS_COMMAND 由 evaluate_tool_access 中 project_execute 例外覆盖
        return bool(self.side_effects - {
            ToolSideEffect.NONE,
            ToolSideEffect.READS_SESSION,
            ToolSideEffect.WRITES_SESSION,
            ToolSideEffect.READS_PROJECT,
        })


# ---------------------------------------------------------------------------
# ExecutorPrincipal
# ---------------------------------------------------------------------------


class ExecutorPrincipal(Enum):
    """执行主体。"""
    MAIN_AGENT = auto()
    """主 Agent（管理对话、讨论、任务派发）。"""
    MINI_SUBAGENT = auto()
    """Mini 子 Agent（在主 Agent 委派下运行）。"""
    HARNESS_AGENT = auto()
    """Harness 执行 Agent（接收 project_execute 委派）。"""


# ---------------------------------------------------------------------------
# RunAuthority
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunAuthority:
    """应用构造的可信运行权限。

    工具参数不能覆盖这些值；普通会话缺少有效项目绑定时，
    即使错误传入项目能力也不能放行项目操作。
    """

    principal: ExecutorPrincipal
    """执行主体。"""

    thread_id: str
    """当前线程 ID。"""

    run_id: str
    """当前 Run ID。"""

    project_id: str | None = None
    """绑定项目 ID；普通会话为 None。"""

    granted_capabilities: frozenset[ToolCapability] = field(default_factory=frozenset)
    """本次已授予的能力。"""

    # 派生属性
    @property
    def is_project_session(self) -> bool:
        """是否为项目会话。"""
        return self.project_id is not None

    def has_capability(self, cap: ToolCapability) -> bool:
        """是否已授予指定能力。"""
        return cap in self.granted_capabilities

    def has_all_capabilities(self, caps: frozenset[ToolCapability]) -> bool:
        """是否已授予全部指定能力。"""
        return caps.issubset(self.granted_capabilities)


# ---------------------------------------------------------------------------
# ToolAccessDecision
# ---------------------------------------------------------------------------


class AccessDecisionKind(Enum):
    """访问决策结果。"""
    ALLOW = auto()    # 允许执行
    CONFIRM = auto()  # 需要本次确认（confirm 不代表执行成功，不得执行 handler）
    DENY = auto()     # 拒绝


@dataclass(frozen=True)
class ToolAccessDecision:
    """工具访问判定结果。"""

    kind: AccessDecisionKind
    """决策结果。"""

    reason_code: str
    """稳定的原因码，供日志和前端区分使用。"""

    reason: str
    """简短说明（面向用户或日志）。"""

    metadata: dict = field(default_factory=dict)
    """扩展元数据（如 CONFIRM 时挂载的 pending request_id）。"""

    @classmethod
    def allow(cls, reason: str = "allowed") -> "ToolAccessDecision":
        return cls(AccessDecisionKind.ALLOW, "allowed", reason)

    @classmethod
    def confirm(cls, reason: str) -> "ToolAccessDecision":
        return cls(AccessDecisionKind.CONFIRM, "requires_confirmation", reason)

    @classmethod
    def deny(cls, reason_code: str, reason: str) -> "ToolAccessDecision":
        return cls(AccessDecisionKind.DENY, reason_code, reason)

    # 便捷谓词
    @property
    def is_allowed(self) -> bool:
        return self.kind == AccessDecisionKind.ALLOW

    @property
    def is_confirm(self) -> bool:
        return self.kind == AccessDecisionKind.CONFIRM

    @property
    def is_denied(self) -> bool:
        return self.kind == AccessDecisionKind.DENY


# ---------------------------------------------------------------------------
# ToolResult
# ---------------------------------------------------------------------------


class ToolResultStatus(Enum):
    """工具执行终态。"""
    SUCCEEDED = auto()
    FAILED = auto()
    CANCELLED = auto()
    TIMED_OUT = auto()


@dataclass(frozen=True)
class ToolResult:
    """统一结果契约。

    适合后续序列化；不放入异常实例、函数、连接或密钥。
    待确认属于调用控制状态，不加入工具执行成功状态——
    工具调用成功不等于业务验收通过。
    """

    status: ToolResultStatus
    """执行终态。"""

    content: str
    """供模型读取的内容。"""

    data: dict[str, Any] | None = None
    """可选结构化数据。"""

    sources: list[str] = field(default_factory=list)
    """来源引用（如文件路径、URL）。"""

    artifacts: list[str] = field(default_factory=list)
    """产物引用（如写入的文件路径）。"""

    error: str | None = None
    """错误说明（仅 status=FAILED 时有值）。"""

    truncated: bool = False
    """输出是否被截断。"""

    def __post_init__(self) -> None:
        # 矛盾状态校验
        if self.status == ToolResultStatus.SUCCEEDED and self.error is not None:
            raise ValueError("succeeded status 不能携带 error")
        if self.status != ToolResultStatus.FAILED and self.error is not None:
            raise ValueError("非 failed 状态不应携带 error")

    @classmethod
    def succeeded(
        cls,
        content: str,
        *,
        data: dict[str, Any] | None = None,
        sources: list[str] | None = None,
        artifacts: list[str] | None = None,
        truncated: bool = False,
    ) -> "ToolResult":
        return cls(
            status=ToolResultStatus.SUCCEEDED,
            content=content,
            data=data,
            sources=sources or [],
            artifacts=artifacts or [],
            truncated=truncated,
        )

    @classmethod
    def failed(cls, content: str, error: str) -> "ToolResult":
        return cls(
            status=ToolResultStatus.FAILED,
            content=content,
            error=error,
        )

    @classmethod
    def cancelled(cls, content: str = "") -> "ToolResult":
        return cls(status=ToolResultStatus.CANCELLED, content=content)

    @classmethod
    def timed_out(cls, content: str = "") -> "ToolResult":
        return cls(status=ToolResultStatus.TIMED_OUT, content=content)


# ---------------------------------------------------------------------------
# evaluate_tool_access()
# ---------------------------------------------------------------------------


def evaluate_tool_access(
    metadata: ToolMetadata | None,
    policy: "ToolPolicy | None",
    authority: RunAuthority,
) -> ToolAccessDecision:
    """判定工具访问权限。

    判定顺序（按规格 D 节）：
    1. 元数据缺失、未分类或工具身份不一致 → deny
    2. 策略缺失或工具禁用 → deny
    3. 当前执行主体未被允许 → deny
    4. 资源作用域不符（如普通会话调用项目工具）→ deny
    5. 所需能力不全部满足 → deny
    6. 已授权且需要确认 → confirm
    7. 其余允许的调用 → allow

    授权先于确认。confirm 不代表执行成功，也不得执行 handler。
    """
    # 1. 元数据缺失或未分类
    if metadata is None:
        return ToolAccessDecision.deny(
            "metadata_missing",
            "工具元数据缺失，无法判定",
        )
    if metadata.uncategorized:
        return ToolAccessDecision.deny(
            "uncategorized",
            f"工具 {metadata.name} 未分类，默认拒绝",
        )

    # 2. 策略缺失或工具禁用
    if policy is None:
        return ToolAccessDecision.deny(
            "policy_missing",
            f"工具 {metadata.name} 没有适用策略",
        )
    if not policy.enabled:
        return ToolAccessDecision.deny(
            "tool_disabled",
            f"工具 {metadata.name} 已禁用",
        )

    # 3. 执行主体未被允许
    if not policy.is_principal_allowed(authority.principal):
        return ToolAccessDecision.deny(
            "principal_not_allowed",
            f"执行主体 {authority.principal.name} 不允许调用 {metadata.name}",
        )

    # 4. 资源作用域不符
    # 普通会话（authority.project_id is None）不能调用有项目副作用的工具
    if authority.project_id is None:
        project_side_effects = {
            ToolSideEffect.READS_PROJECT,
            ToolSideEffect.WRITES_PROJECT,
        }
        if metadata.side_effects & project_side_effects:
            return ToolAccessDecision.deny(
                "scope_mismatch",
                "普通会话不允许项目操作",
            )

    # 5. 所需能力不全部满足（合并元数据 + 策略要求）
    all_required = policy.merge_requirements(metadata.capabilities)
    missing = all_required - authority.granted_capabilities
    if missing:
        return ToolAccessDecision.deny(
            "capability_missing",
            f"缺少能力: {', '.join(c.name for c in missing)}",
        )

    # 6. 已授权但需要确认（策略覆盖 > 元数据默认值）
    #
    # - policy.requires_confirmation=True → 强制 CONFIRM（用户/产品要求逐次确认）
    # - policy.requires_confirmation=False 且 metadata.requires_confirmation()=True →
    #   视为「已授权豁免」，不弹确认（仅对 read-only 网络/沙箱操作生效，写操作保持原行为）
    # - project_execute 仍走其受控派发例外（即使 metadata 要求确认，policy=False 也放行）
    if policy.requires_confirmation:
        needs_confirm = True
    elif metadata.name == "project_execute":
        # 受控执行路径：policy.requires_confirmation=False 时直接派发
        needs_confirm = False
    elif metadata.requires_confirmation():
        # R1：策略显式豁免 = 默认已授权的只读网络/沙箱操作不弹确认
        # 只对"显式声明 requires_confirmation=True"的策略生效
        needs_confirm = False
    else:
        needs_confirm = False
    if needs_confirm:
        return ToolAccessDecision.confirm(
            f"工具 {metadata.name} 有外部副作用，需要本次确认",
        )

    # 7. 其余允许
    return ToolAccessDecision.allow(f"工具 {metadata.name} 允许执行")


# ---------------------------------------------------------------------------
# ToolPolicy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolPolicy:
    """工具策略：是否启用、允许的执行主体、所需权限、是否要求确认。"""

    enabled: bool = True
    """工具是否启用。"""

    allowed_principals: frozenset[ExecutorPrincipal] = field(
        default_factory=lambda: frozenset({ExecutorPrincipal.MAIN_AGENT})
    )
    """允许的执行主体集合。"""

    required_capabilities: frozenset[ToolCapability] = field(default_factory=frozenset)
    """额外要求的权限。策略只能增加限制，不能抹除工具原有能力要求。"""

    requires_confirmation: bool = False
    """是否要求本次确认（覆盖元数据的默认判断）。"""

    def is_principal_allowed(self, principal: ExecutorPrincipal) -> bool:
        return principal in self.allowed_principals

    def merge_requirements(
        self, metadata_capabilities: frozenset[ToolCapability]
    ) -> frozenset[ToolCapability]:
        """返回策略对执行主体的最终能力要求。

        元数据中的基础能力不能被策略覆盖掉；策略只能增加限制。
        """
        return metadata_capabilities | self.required_capabilities


# ---------------------------------------------------------------------------
# 内置工具元数据清单
# ---------------------------------------------------------------------------

# calculator
CALCULATOR_METADATA = ToolMetadata(
    name="calculator",
    source=ToolSourceKind.LOCAL,
    capabilities=frozenset({ToolCapability.COMPUTE}),
    side_effects=frozenset({ToolSideEffect.NONE}),
)

# history_search / history_read
HISTORY_SEARCH_METADATA = ToolMetadata(
    name="history_search",
    source=ToolSourceKind.LOCAL,
    capabilities=frozenset({ToolCapability.SESSION_HISTORY_READ}),
    side_effects=frozenset({ToolSideEffect.READS_SESSION}),
)

HISTORY_READ_METADATA = ToolMetadata(
    name="history_read",
    source=ToolSourceKind.LOCAL,
    capabilities=frozenset({ToolCapability.SESSION_HISTORY_READ}),
    side_effects=frozenset({ToolSideEffect.READS_SESSION}),
)

# task_note
TASK_NOTE_METADATA = ToolMetadata(
    name="task_note",
    source=ToolSourceKind.LOCAL,
    capabilities=frozenset({ToolCapability.SESSION_STATE_WRITE}),
    side_effects=frozenset({ToolSideEffect.WRITES_SESSION}),
)

# project_ 只读工具
PROJECT_READ_FILE_METADATA = ToolMetadata(
    name="project_read_file",
    source=ToolSourceKind.LOCAL,
    capabilities=frozenset({ToolCapability.PROJECT_READ}),
    side_effects=frozenset({ToolSideEffect.READS_PROJECT}),
)

PROJECT_GLOB_METADATA = ToolMetadata(
    name="project_glob",
    source=ToolSourceKind.LOCAL,
    capabilities=frozenset({ToolCapability.PROJECT_READ}),
    side_effects=frozenset({ToolSideEffect.READS_PROJECT}),
)

PROJECT_GREP_METADATA = ToolMetadata(
    name="project_grep",
    source=ToolSourceKind.LOCAL,
    capabilities=frozenset({ToolCapability.PROJECT_READ}),
    side_effects=frozenset({ToolSideEffect.READS_PROJECT}),
)

# project_execute（受控委派，RUNS_COMMAND 覆盖执行语义）
PROJECT_EXECUTE_METADATA = ToolMetadata(
    name="project_execute",
    source=ToolSourceKind.LOCAL,
    capabilities=frozenset({ToolCapability.PROJECT_EXEC_DELEGATE}),
    side_effects=frozenset({ToolSideEffect.WRITES_PROJECT, ToolSideEffect.RUNS_COMMAND}),
)

# subagent 工具（主 Agent 可见，Mini Subagent 不应看到自己）
SUBAGENT_METADATA = ToolMetadata(
    name="subagent_submit",
    source=ToolSourceKind.LOCAL,
    capabilities=frozenset({ToolCapability.SUBAGENT_DELEGATE}),
    side_effects=frozenset({ToolSideEffect.DELEGATES_SUBAGENT}),
)

# sandbox 工具（CLI 沙箱权限由 Web 产品独立建模）
SANDBOX_READ_FILE_METADATA = ToolMetadata(
    name="sandbox_read_file",
    source=ToolSourceKind.LOCAL,
    capabilities=frozenset({ToolCapability.COMPUTE}),
    side_effects=frozenset({ToolSideEffect.NONE}),
)

SANDBOX_WRITE_FILE_METADATA = ToolMetadata(
    name="sandbox_write_file",
    source=ToolSourceKind.LOCAL,
    capabilities=frozenset(),
    side_effects=frozenset({ToolSideEffect.WRITES_PROJECT}),
    uncategorized=True,  # sandbox 写入需单独建模策略，首版 deny
)

SANDBOX_RUN_COMMAND_METADATA = ToolMetadata(
    name="sandbox_run_command",
    source=ToolSourceKind.LOCAL,
    capabilities=frozenset({ToolCapability.COMMAND_EXECUTE}),
    side_effects=frozenset({ToolSideEffect.RUNS_COMMAND}),
)


# TOOL-06：网络只读工具
#
# - `web_search` 与 `web_read` 共享 `NETWORK_READ` 能力。
# - side_effects 标 `READS_NETWORK`（非项目操作，普通会话亦可调用）。
# - 不授予 `EXTERNAL_SERVICE_READ`（那是给接入可控外部服务留的——搜索/读取是
#   模型触发的不可信数据源，不能获得"调用外部服务"的更强信任）。
# - requires_confirmation() 默认 True（READS_NETWORK 不在豁免集合），
#   但默认策略（`default_policy_for_web`）会把它显式置 False：已启用且已授权
#   的只读搜索/读取默认无需逐次确认。策略要求显式置位，避免「以为禁用确认」与
#   「实际是元数据豁免」混在一起。
WEB_SEARCH_METADATA = ToolMetadata(
    name="web_search",
    source=ToolSourceKind.LOCAL,
    capabilities=frozenset({ToolCapability.NETWORK_READ}),
    side_effects=frozenset({ToolSideEffect.READS_NETWORK}),
)

WEB_READ_METADATA = ToolMetadata(
    name="web_read",
    source=ToolSourceKind.LOCAL,
    capabilities=frozenset({ToolCapability.NETWORK_READ}),
    side_effects=frozenset({ToolSideEffect.READS_NETWORK}),
)


# ---------------------------------------------------------------------------
# 内置默认策略构造
# ---------------------------------------------------------------------------

def default_policy_for_calculator() -> ToolPolicy:
    return ToolPolicy(enabled=True)


def default_policy_for_history_tools() -> ToolPolicy:
    return ToolPolicy(
        enabled=True,
        allowed_principals=frozenset({
            ExecutorPrincipal.MAIN_AGENT,
            ExecutorPrincipal.MINI_SUBAGENT,
        }),
    )


def default_policy_for_task_note() -> ToolPolicy:
    return ToolPolicy(
        enabled=True,
        allowed_principals=frozenset({
            ExecutorPrincipal.MAIN_AGENT,
            ExecutorPrincipal.MINI_SUBAGENT,
        }),
    )


def default_policy_for_project_read() -> ToolPolicy:
    return ToolPolicy(
        enabled=True,
        allowed_principals=frozenset({
            ExecutorPrincipal.MAIN_AGENT,
            ExecutorPrincipal.MINI_SUBAGENT,
        }),
        # 项目只读需要项目会话（authority.project_id 检查在 evaluate_tool_access 中）
    )


def default_policy_for_project_execute() -> ToolPolicy:
    """project_execute：主 Agent 可见，明确编码请求直接派发（不默认要求确认）。"""
    return ToolPolicy(
        enabled=True,
        allowed_principals=frozenset({
            ExecutorPrincipal.MAIN_AGENT,
        }),
        # 明确编码请求直接派发；requires_confirmation 由用户操作层单独控制
        requires_confirmation=False,
    )


def default_policy_for_subagent() -> ToolPolicy:
    return ToolPolicy(
        enabled=True,
        allowed_principals=frozenset({
            ExecutorPrincipal.MAIN_AGENT,
        }),
    )


def default_policy_for_sandbox() -> ToolPolicy:
    """沙箱工具权限与 Web 产品权限不同；主 Agent 默认 deny。"""
    return ToolPolicy(
        enabled=False,
        allowed_principals=frozenset(),
    )


def default_policy_for_web() -> ToolPolicy:
    """TOOL-06：网络只读工具默认策略。

    - 默认启用（已启用的搜索/读取不需要每次确认）
    - 主 Agent 可见（MINI_SUBAGENT 暂不直接做网络读取，避免子代理扩大外部依赖；
      如需委派，由用户/策略显式授权）
    - `requires_confirmation=False`：已授权的只读调用不弹确认；
      若用户/策略希望强制逐次确认，把 `policy.requires_confirmation` 置 True 即可。
    """
    return ToolPolicy(
        enabled=True,
        allowed_principals=frozenset({ExecutorPrincipal.MAIN_AGENT}),
        requires_confirmation=False,
    )


# TOOL-03 R2：MCP 工具元数据按可信配置分类
# 字符串 → 枚举映射；未知字符串 → MCPToolStateError（避免默认分类导致过度权限）
_TOOL_CAPABILITY_BY_NAME: dict[str, ToolCapability] = {c.name: c for c in ToolCapability}
_TOOL_SIDE_EFFECT_BY_NAME: dict[str, ToolSideEffect] = {s.name: s for s in ToolSideEffect}


def make_mcp_tool_metadata(
    name: str,
    declared_capabilities: frozenset[str] = frozenset(),
    declared_side_effects: frozenset[str] = frozenset(),
) -> ToolMetadata:
    """构造一个 MCP 工具的 ToolMetadata。

    TOOL-03 R2：按可信配置分类。
    - `declared_capabilities` / `declared_side_effects` 均为空 → uncategorized，
      评估时默认拒绝（不要"无副作用 echo 默认通过"）。
    - 任一非空 → 按声明构造元数据。
    """
    if not declared_capabilities and not declared_side_effects:
        # 未声明 → 分类不可信 → uncategorized=True 触发默认拒绝
        return ToolMetadata(
            name=name,
            source=ToolSourceKind.MCP,
            capabilities=frozenset(),
            side_effects=frozenset(),
            uncategorized=True,
        )

    try:
        caps = frozenset(_TOOL_CAPABILITY_BY_NAME[c] for c in declared_capabilities)
    except KeyError as exc:
        raise MCPConfigError(
            f"MCP 工具 {name} 声明未知 capability: {exc.args[0]!r}"
        ) from None
    try:
        sides = frozenset(_TOOL_SIDE_EFFECT_BY_NAME[s] for s in declared_side_effects)
    except KeyError as exc:
        raise MCPConfigError(
            f"MCP 工具 {name} 声明未知 side_effect: {exc.args[0]!r}"
        ) from None

    return ToolMetadata(
        name=name,
        source=ToolSourceKind.MCP,
        capabilities=caps,
        side_effects=sides,
    )


def default_policy_for_mcp_tool() -> ToolPolicy:
    """MCP 工具默认策略：默认 disabled，由用户显式启用并按主体授权。

    区别于 sandbox_* 的"主 Agent 默认 deny"——MCP 工具需要用户在
    管理页中分别授权每个主体，不在代码里预设。
    """
    return ToolPolicy(
        enabled=False,
        allowed_principals=frozenset(),
    )


# ---------------------------------------------------------------------------
# 策略注册表（用于 lookup）
# ---------------------------------------------------------------------------

BUILTIN_TOOL_METADATA: dict[str, ToolMetadata] = {
    "calculator": CALCULATOR_METADATA,
    "history_search": HISTORY_SEARCH_METADATA,
    "history_read": HISTORY_READ_METADATA,
    "task_note": TASK_NOTE_METADATA,
    "project_read_file": PROJECT_READ_FILE_METADATA,
    "project_glob": PROJECT_GLOB_METADATA,
    "project_grep": PROJECT_GREP_METADATA,
    "project_execute": PROJECT_EXECUTE_METADATA,
    "subagent_submit": SUBAGENT_METADATA,
    "sandbox_read_file": SANDBOX_READ_FILE_METADATA,
    "sandbox_write_file": SANDBOX_WRITE_FILE_METADATA,
    "sandbox_run_command": SANDBOX_RUN_COMMAND_METADATA,
    # TOOL-06：网络只读工具
    "web_search": WEB_SEARCH_METADATA,
    "web_read": WEB_READ_METADATA,
}


def default_policy_for(name: str) -> ToolPolicy | None:
    """返回内置工具的默认策略。未知工具返回 None。"""
    if name == "calculator":
        return default_policy_for_calculator()
    if name in ("history_search", "history_read"):
        return default_policy_for_history_tools()
    if name == "task_note":
        return default_policy_for_task_note()
    if name in ("project_read_file", "project_glob", "project_grep"):
        return default_policy_for_project_read()
    if name == "project_execute":
        return default_policy_for_project_execute()
    if name == "subagent_submit":
        return default_policy_for_subagent()
    if name.startswith("sandbox_"):
        return default_policy_for_sandbox()
    if name in ("web_search", "web_read"):
        return default_policy_for_web()
    if name.startswith("mcp__"):
        return default_policy_for_mcp_tool()
    return None
