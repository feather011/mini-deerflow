"""MCP 工具授权与启用状态存储（TOOL-03）。

职责：
- 持久化每个 discovered 工具的 enabled 状态
- 持久化分主体授权（MAIN_AGENT / MINI_SUBAGENT / HARNESS_AGENT）
- 加载/保存到 JSON 文件，与 MCPServerConfig 文件解耦

设计原则：
- **发现 ≠ 启用 ≠ 授权**：新发现工具默认 disabled，需要分别授权三个主体
- **状态文件与配置分离**：MCPServerConfig 在 MCP 配置 JSON 里，工具授权在 mcp_tool_state.json
- **重建 runtime 时恢复**：通过 load() 重建 ToolPolicyRegistry 状态
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Mapping

from mini_deerflow.mcp_errors import MCPError
from mini_deerflow.tool_contracts import ExecutorPrincipal

STATE_PATH_ENV = "MINI_DEERFLOW_MCP_TOOL_STATE"
DEFAULT_STATE_PATH = ".mini-deerflow/mcp_tool_state.json"


class MCPToolStateError(MCPError):
    """MCP 工具授权状态非法。"""


@dataclass(frozen=True)
class MCPToolAuthState:
    """单个 discovered 工具的授权状态。

    - enabled: 是否启用（默认 False；新发现必须显式启用）
    - allowed_principals: 已授权执行主体集合（必须为非空才允许调用）
    """

    enabled: bool = False
    allowed_principals: frozenset[ExecutorPrincipal] = field(default_factory=frozenset)


def _principal_value(p: ExecutorPrincipal) -> str:
    """Enum 序列化为字符串。"""
    return p.name


def _principal_from_str(s: str) -> ExecutorPrincipal:
    """字符串反序列化为 ExecutorPrincipal。未知名称 → MCPToolStateError。"""
    try:
        return ExecutorPrincipal[s]
    except KeyError as exc:
        raise MCPToolStateError(f"未知 ExecutorPrincipal: {s!r}") from exc


def _state_to_dict(state: MCPToolAuthState) -> dict[str, Any]:
    return {
        "enabled": state.enabled,
        "allowed_principals": sorted(p.name for p in state.allowed_principals),
    }


def _state_from_dict(raw: Mapping[str, Any]) -> MCPToolAuthState:
    if not isinstance(raw, Mapping):
        raise MCPToolStateError("工具授权状态必须是对象")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise MCPToolStateError("enabled 必须是布尔值")

    raw_principals = raw.get("allowed_principals", [])
    if not isinstance(raw_principals, list) or not all(
        isinstance(p, str) for p in raw_principals
    ):
        raise MCPToolStateError("allowed_principals 必须是字符串数组")
    principals = frozenset(_principal_from_str(p) for p in raw_principals)

    return MCPToolAuthState(
        enabled=enabled,
        allowed_principals=principals,
    )


class MCPToolStateStore:
    """工具授权状态存储。"""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        if path is None:
            env_path = os.environ.get(STATE_PATH_ENV, "")
            path = env_path or DEFAULT_STATE_PATH
        self._path = Path(path)
        self._states: dict[str, MCPToolAuthState] = {}
        self._loaded = False

    @property
    def path(self) -> Path:
        return self._path

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        if self._path.is_file():
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise MCPToolStateError(
                    f"MCP 工具状态 JSON 解析失败: {exc.msg}"
                ) from None
            if not isinstance(raw, Mapping):
                raise MCPToolStateError("MCP 工具状态顶层必须是对象")
            tools = raw.get("tools", {})
            if not isinstance(tools, Mapping):
                raise MCPToolStateError("tools 必须是对象")
            for name, state_raw in tools.items():
                self._states[str(name)] = _state_from_dict(state_raw)
        self._loaded = True

    def get(self, qualified_name: str) -> MCPToolAuthState:
        """获取工具的授权状态。不存在则返回默认（enabled=False，无授权）。"""
        self._ensure_loaded()
        return self._states.get(qualified_name, MCPToolAuthState())

    def set_enabled(self, qualified_name: str, enabled: bool) -> MCPToolAuthState:
        """设置启用状态。"""
        self._ensure_loaded()
        current = self._states.get(qualified_name, MCPToolAuthState())
        new_state = MCPToolAuthState(
            enabled=enabled,
            allowed_principals=current.allowed_principals,
        )
        self._states[qualified_name] = new_state
        self._save()
        return new_state

    def grant(self, qualified_name: str, principal: ExecutorPrincipal) -> MCPToolAuthState:
        """为某个主体授权。"""
        self._ensure_loaded()
        current = self._states.get(qualified_name, MCPToolAuthState())
        new_principals = current.allowed_principals | {principal}
        new_state = MCPToolAuthState(
            enabled=current.enabled,
            allowed_principals=new_principals,
        )
        self._states[qualified_name] = new_state
        self._save()
        return new_state

    def revoke(self, qualified_name: str, principal: ExecutorPrincipal) -> MCPToolAuthState:
        """撤销某主体的授权。"""
        self._ensure_loaded()
        current = self._states.get(qualified_name, MCPToolAuthState())
        new_principals = current.allowed_principals - {principal}
        new_state = MCPToolAuthState(
            enabled=current.enabled,
            allowed_principals=new_principals,
        )
        self._states[qualified_name] = new_state
        self._save()
        return new_state

    def all_states(self) -> dict[str, MCPToolAuthState]:
        """返回所有工具授权状态的快照。"""
        self._ensure_loaded()
        return dict(self._states)

    def _save(self) -> None:
        """原子写入：写临时文件 + os.replace。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "tools": {
                name: _state_to_dict(state)
                for name, state in self._states.items()
            }
        }
        # 原子写入：先写临时文件，再 rename
        import tempfile
        fd, tmp = tempfile.mkstemp(
            prefix="mcp-state-", suffix=".tmp", dir=self._path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._path)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise


__all__ = [
    "DEFAULT_STATE_PATH",
    "MCPToolAuthState",
    "MCPToolStateError",
    "MCPToolStateStore",
    "STATE_PATH_ENV",
]
