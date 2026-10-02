"""MCP 服务器配置（22 阶段）。

只支持**本地 stdio** MCP 服务器；不支持远程 HTTP / OAuth / 动态市场。

设计原则：`command` / `args` **只来自配置文件**，绝不由 HTTP 请求提供——
否则任何能调 API 的人都能在这台机器上执行任意程序。前端与后端都没有
接收 command/args 的接口。

### 配置来源

```
MINI_DEERFLOW_MCP_CONFIG  →  JSON 文件路径（未设置 = 不启用 MCP，默认关闭）
```

JSON 形如：

```json
{
  "servers": [
    {
      "name": "demo",
      "command": "python",
      "args": ["-m", "mypackage.server"],
      "env": {"MY_API_TOKEN": "只给子进程的值"},
      "enabled": true,
      "required": false,
      "startup_timeout": 15,
      "call_timeout": 30,
      "max_output_bytes": 65536
    }
  ]
}
```

### env 白名单

`env` 是**显式白名单**：只有列出的变量会传给 MCP 子进程。
SDK 自身的 `get_default_environment()` 提供 PATH / SYSTEMROOT 等
启动进程必需的系统变量（**不含任何应用密钥**），在此之上叠加白名单。
父进程其余环境（含各种 API Key）一律不继承。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from mini_deerflow.mcp_errors import MCPError

CONFIG_PATH_ENV = "MINI_DEERFLOW_MCP_CONFIG"

DEFAULT_STARTUP_TIMEOUT = 15.0
DEFAULT_CALL_TIMEOUT = 30.0
DEFAULT_MAX_OUTPUT_BYTES = 64 * 1024


class MCPConfigError(MCPError):
    """MCP 配置非法（继承 MCPError，与其余 MCP 领域异常同属一个层级）。"""


@dataclass(frozen=True)
class MCPServerConfig:
    """单个本地 stdio MCP 服务器的配置。"""

    name: str
    command: str
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    enabled: bool = True
    required: bool = False
    startup_timeout: float = DEFAULT_STARTUP_TIMEOUT
    call_timeout: float = DEFAULT_CALL_TIMEOUT
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES

    def __repr__(self) -> str:
        # 显式重写：env 可能装着子进程专用凭据，不能被默认 repr 打出来
        return (
            f"MCPServerConfig(name={self.name!r}, command={self.command!r}, "
            f"args={list(self.args)!r}, env_keys={sorted(self.env)}, "
            f"enabled={self.enabled}, required={self.required}, "
            f"startup_timeout={self.startup_timeout}, "
            f"call_timeout={self.call_timeout})"
        )

    __str__ = __repr__


def _require_str(raw: Mapping[str, Any], key: str, where: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise MCPConfigError(f"{where} 的 {key} 必须是非空字符串")
    return value.strip()


def parse_server_config(raw: Mapping[str, Any], index: int) -> MCPServerConfig:
    """解析单个服务器配置；字段非法立即抛 MCPConfigError（不静默降级）。"""
    where = f"servers[{index}]"
    if not isinstance(raw, Mapping):
        raise MCPConfigError(f"{where} 必须是对象")

    name = _require_str(raw, "name", where)
    command = _require_str(raw, "command", where)

    raw_args = raw.get("args", [])
    if not isinstance(raw_args, list) or not all(isinstance(a, str) for a in raw_args):
        raise MCPConfigError(f"{where}.args 必须是字符串数组")
    args = tuple(raw_args)

    raw_env = raw.get("env", {})
    if not isinstance(raw_env, Mapping) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in raw_env.items()
    ):
        raise MCPConfigError(f"{where}.env 必须是字符串到字符串的映射")

    enabled = raw.get("enabled", True)
    required = raw.get("required", False)
    if not isinstance(enabled, bool) or not isinstance(required, bool):
        raise MCPConfigError(f"{where} 的 enabled / required 必须是布尔值")

    def _positive_float(key: str, default: float) -> float:
        value = raw.get(key, default)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            raise MCPConfigError(f"{where}.{key} 必须是正数")
        return float(value)

    max_output = raw.get("max_output_bytes", DEFAULT_MAX_OUTPUT_BYTES)
    if not isinstance(max_output, int) or isinstance(max_output, bool) or max_output <= 0:
        raise MCPConfigError(f"{where}.max_output_bytes 必须是正整数")

    return MCPServerConfig(
        name=name,
        command=command,
        args=args,
        env=dict(raw_env),
        enabled=enabled,
        required=required,
        startup_timeout=_positive_float("startup_timeout", DEFAULT_STARTUP_TIMEOUT),
        call_timeout=_positive_float("call_timeout", DEFAULT_CALL_TIMEOUT),
        max_output_bytes=max_output,
    )


def load_mcp_config(path: str | os.PathLike[str] | None = None) -> list[MCPServerConfig]:
    """加载 MCP 配置。

    Args:
        path: JSON 文件路径；为 None 时读 `MINI_DEERFLOW_MCP_CONFIG`。
              都没有 → 返回空列表（**默认不启用 MCP**）。

    Returns:
        启用（enabled=true）的服务器配置列表。

    Raises:
        MCPConfigError: 文件不存在、JSON 非法、字段非法、服务器名重复。
    """
    resolved = str(path) if path is not None else os.environ.get(CONFIG_PATH_ENV, "")
    if not resolved:
        return []

    file_path = Path(resolved)
    if not file_path.is_file():
        raise MCPConfigError(f"MCP 配置文件不存在: {file_path}")

    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MCPConfigError(f"MCP 配置 JSON 解析失败: {exc.msg}") from None

    if not isinstance(payload, Mapping):
        raise MCPConfigError("MCP 配置顶层必须是对象")
    servers = payload.get("servers", [])
    if not isinstance(servers, list):
        raise MCPConfigError("servers 必须是数组")

    configs: list[MCPServerConfig] = []
    seen: set[str] = set()
    for index, raw in enumerate(servers):
        config = parse_server_config(raw, index)
        if config.name in seen:
            # 名字会被用作工具命名空间的一部分，重复会导致工具名冲突
            raise MCPConfigError(f"服务器名重复: {config.name}")
        seen.add(config.name)
        if config.enabled:
            configs.append(config)

    return configs
