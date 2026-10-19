"""API 配置（19A 阶段，19F 扩展 CORS）。

所有配置项支持环境变量覆盖。
环境变量格式：MINI_DEERFLOW_<UPPER_NAME>
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field


def _get_env(name: str, default: str | None) -> str | None:
    return os.environ.get(f"MINI_DEERFLOW_{name}", default)


def _get_env_int(name: str, default: int) -> int:
    raw = os.environ.get(f"MINI_DEERFLOW_{name}")
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(
            f"环境变量 MINI_DEERFLOW_{name} 必须为整数，当前值: {raw!r}"
        )


def _parse_cors_origins(raw: str | None) -> list[str] | None:
    """解析逗号分隔的 Origin 列表。"""
    if not raw:
        return None
    seen: set[str] = set()
    result: list[str] = []
    for origin in raw.split(","):
        origin = origin.strip()
        if not origin:
            continue
        if origin in seen:
            continue
        seen.add(origin)
        result.append(origin)
    if not result:
        return None
    return result


# --------------------------------------------------------------------------- ApiSettings


@dataclass(frozen=True)
class ApiSettings:
    title: str = "Mini DeerFlow API"
    version: str = "0.1.0"
    api_prefix: str = "/api"

    # ---- CORS（19F）----
    cors_origins: list[str] | None = None
    """允许的 CORS Origin 列表。None = 不启用 CORS。"""
    cors_allow_credentials: bool = False
    """CORS 是否允许凭据。默认 False（安全默认值）。"""

    # ---- Workspace 文件服务（19E）----
    workspace_root: str | None = None
    """Workspace 根目录。None = 使用临时目录（lifespan 内创建）。"""
    max_upload_bytes: int = 10 * 1024 * 1024
    """单次上传最大字节数。"""
    max_list_entries: int = 1000
    """文件列表最大条目数。"""

    # ---- Thread 持久化（23）----
    db_path: str | None = None
    """SQLite 数据库路径。None = 内存 ThreadStore（开发/测试默认，重启即失）。"""

    # ---- 项目工作模式（第 2 步 → 托管工作区）----
    workspaces_root: str | None = None
    """托管工作区根目录。None = ~/.mini-deerflow/workspaces（自动 resolve）。"""

    # ---- Harness 执行器（WM-3C 项目工作模式）----
    harness_root: str | None = None
    """Harness 非交互入口所在仓库根目录。设置后 project_execute 工具才注册。
    None = 不启用执行器（普通讨论/澄清/只读探索模式）。"""
    harness_timeout_seconds: float = 600.0
    """单次 project_execute 任务的硬超时（秒），超时后强制终止进程树。"""

    # ---- TOOL-06：网络只读工具 ----
    web_read_timeout_s: float = 10.0
    """web_read HTTP 总超时（秒）。"""
    web_allow_loopback: bool = False
    """放行 loopback 主机（127.0.0.1 / ::1）；CI 跑本地 fixture 时需显式置 True。
    生产不应开启。"""
    web_allowed_hosts: tuple[str, ...] = ()
    """URL 主机白名单（精确匹配，不区分大小写）；不在白名单时按其它策略拒绝。"""
    web_search_provider_kind: str = "unavailable"
    """搜索 Provider 种类：
    - "unavailable"：默认，未配置外部服务
    - "fake"：内置 fake provider（CI/测试用，按 env JSON 提供固定结果）
    - "mcp"：复用 MCP 接入，要求 mcp_servers 中存在 type=web_search 的 server
    """
    web_search_fake_results: tuple[tuple[str, str, str], ...] = ()
    """fake provider 的固定结果（title, url, snippet）。"""

    def __post_init__(self) -> None:
        """从环境变量覆盖各字段。"""
        # 使用 object.__setattr__ 因为 frozen=True
        # CORS
        env_origins = _get_env("CORS_ORIGINS", None)
        if env_origins is not None:
            parsed = _parse_cors_origins(env_origins)
            if parsed is not None:
                object.__setattr__(self, "cors_origins", parsed)
        creds_raw = _get_env("CORS_ALLOW_CREDENTIALS", None)
        if creds_raw is not None:
            object.__setattr__(
                self,
                "cors_allow_credentials",
                creds_raw.lower() in ("true", "1", "yes"),
            )
        # Workspace
        env_ws = _get_env("WORKSPACE_ROOT", None)
        if env_ws is not None:
            object.__setattr__(self, "workspace_root", env_ws)
        env_upload = _get_env("MAX_UPLOAD_BYTES", None)
        if env_upload is not None:
            object.__setattr__(self, "max_upload_bytes", int(env_upload))
        env_list = _get_env("MAX_LIST_ENTRIES", None)
        if env_list is not None:
            object.__setattr__(self, "max_list_entries", int(env_list))
        # Thread 持久化：未设置或空白 → 保持内存实现（默认行为不变）
        env_db = _get_env("DB_PATH", None)
        if env_db is not None and env_db.strip():
            object.__setattr__(self, "db_path", env_db.strip())
        # 项目工作区根目录
        env_ws_root = _get_env("WORKSPACES_ROOT", None)
        if env_ws_root is not None and env_ws_root.strip():
            object.__setattr__(self, "workspaces_root", env_ws_root.strip())
        # Harness 执行器根目录
        env_harness_root = _get_env("HARNESS_ROOT", None)
        if env_harness_root is not None and env_harness_root.strip():
            object.__setattr__(self, "harness_root", env_harness_root.strip())
        env_harness_timeout = _get_env("HARNESS_TIMEOUT_SECONDS", None)
        if env_harness_timeout is not None and env_harness_timeout.strip():
            try:
                object.__setattr__(
                    self, "harness_timeout_seconds", float(env_harness_timeout.strip())
                )
            except ValueError as exc:
                raise ValueError(
                    f"环境变量 MINI_DEERFLOW_HARNESS_TIMEOUT_SECONDS 必须为数字，当前值: {env_harness_timeout!r}"
                ) from exc
        # TOOL-06：网络只读
        env_web_timeout = _get_env("WEB_READ_TIMEOUT_S", None)
        if env_web_timeout is not None and env_web_timeout.strip():
            try:
                object.__setattr__(
                    self, "web_read_timeout_s", float(env_web_timeout.strip())
                )
            except ValueError as exc:
                raise ValueError(
                    f"环境变量 MINI_DEERFLOW_WEB_READ_TIMEOUT_S 必须为数字，当前值: {env_web_timeout!r}"
                ) from exc
        env_web_loopback = _get_env("WEB_ALLOW_LOOPBACK", None)
        if env_web_loopback is not None and env_web_loopback.strip():
            object.__setattr__(
                self,
                "web_allow_loopback",
                env_web_loopback.lower() in ("true", "1", "yes"),
            )
        env_web_hosts = _get_env("WEB_ALLOWED_HOSTS", None)
        if env_web_hosts is not None and env_web_hosts.strip():
            seen: set[str] = set()
            hosts: list[str] = []
            for raw in env_web_hosts.split(","):
                h = raw.strip().lower()
                if not h or h in seen:
                    continue
                seen.add(h)
                hosts.append(h)
            object.__setattr__(self, "web_allowed_hosts", tuple(hosts))
        # TOOL-06 R4：搜索 Provider 种类
        env_web_kind = _get_env("WEB_SEARCH_PROVIDER_KIND", None)
        if env_web_kind is not None and env_web_kind.strip():
            kind = env_web_kind.strip().lower()
            if kind not in ("unavailable", "fake", "mcp"):
                raise ValueError(
                    "环境变量 MINI_DEERFLOW_WEB_SEARCH_PROVIDER_KIND "
                    f"必须为 unavailable/fake/mcp，当前值: {kind!r}"
                )
            object.__setattr__(self, "web_search_provider_kind", kind)
        env_web_fake = _get_env("WEB_SEARCH_FAKE_RESULTS", None)
        if env_web_fake is not None and env_web_fake.strip():
            # JSON 数组，每个元素 [title, url, snippet]
            try:
                parsed = json.loads(env_web_fake)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"环境变量 MINI_DEERFLOW_WEB_SEARCH_FAKE_RESULTS 不是合法 JSON: {exc}"
                ) from exc
            if not isinstance(parsed, list):
                raise ValueError("WEB_SEARCH_FAKE_RESULTS 必须是 JSON 数组")
            triples: list[tuple[str, str, str]] = []
            for item in parsed:
                if (
                    not isinstance(item, list)
                    or len(item) != 3
                    or not all(isinstance(x, str) for x in item)
                ):
                    raise ValueError(
                        "WEB_SEARCH_FAKE_RESULTS 元素必须是 [title, url, snippet] 三元组"
                    )
                triples.append((item[0], item[1], item[2]))
            object.__setattr__(self, "web_search_fake_results", tuple(triples))
        # 验证
        if self.max_upload_bytes < 0:
            raise ValueError(
                f"max_upload_bytes 必须非负，当前值: {self.max_upload_bytes}"
            )
        if self.max_list_entries < 1:
            raise ValueError(
                f"max_list_entries 必须 >= 1，当前值: {self.max_list_entries}"
            )
        if self.api_prefix and not self.api_prefix.startswith("/"):
            raise ValueError(
                f"api_prefix 必须以 / 开头，当前值: {self.api_prefix!r}"
            )
        # CORS 去重（无论来源）
        if self.cors_origins is not None:
            seen: set[str] = set()
            deduped: list[str] = []
            for origin in self.cors_origins:
                if origin not in seen:
                    seen.add(origin)
                    deduped.append(origin)
            object.__setattr__(self, "cors_origins", deduped)
            for origin in deduped:
                if not origin.startswith(("http://", "https://")):
                    raise ValueError(
                        f"CORS Origin 必须以 http:// 或 https:// 开头，当前值: {origin!r}"
                    )
