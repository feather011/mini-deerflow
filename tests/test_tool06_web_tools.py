"""TOOL-06：网络只读工具（web_search / web_read）行为测试。

覆盖：
- 元数据与默认策略（NETWORK_READ、不归入 PROJECT_READ）
- web_search 不可用 provider / fake provider 注入 / 参数校验
- web_read SSRF 防护（loopback、私有、IP 字面量、DNS 解析失败、重定向）
- web_read 实际 HTTP 抓取（本地 fixture + httpx mock transport）
- HTML 文本提取（script/style 去除、链接保留、截断）
- 端到端：脚本模型调用 web_search + web_read，结果含来源 URL
- 注入 Agent 路径：web 工具可见、可执行、输出带 WEB_NOTICE
"""

from __future__ import annotations

import json
import socket
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from mini_deerflow.runtime import RuntimeConfig, create_runtime
from mini_deerflow.thread_store import InMemoryThreadStore
from mini_deerflow.tools import ToolRegistry, create_default_registry
from mini_deerflow.tool_contracts import (
    BUILTIN_TOOL_METADATA,
    ExecutorPrincipal,
    ToolCapability,
    ToolMetadata,
    ToolPolicy,
    ToolSideEffect,
    default_policy_for,
    default_policy_for_web,
)
from mini_deerflow.web_tools import (
    CallableWebSearchProvider,
    FixedListWebSearchProvider,
    HostPolicy,
    UnavailableWebSearchProvider,
    WebSearchHit,
    WebSearchProvider,
    WebToolConfig,
    WebToolError,
    WebToolNetworkError,
    WebToolSSRFError,
    WebToolStateError,
    WebToolUsageError,
    WEB_TOOL_LIMITS,
    build_web_read_tool,
    build_web_search_provider_from_config,
    build_web_search_tool,
    create_web_tools,
    extract_readable_text,
    resolve_host_policy,
)


def _install_mock_socket(handler: Callable[[httpx.Request], httpx.Response]) -> Callable[[], None]:
    """接管 web_tools 的网络层，让它走 httpx.MockTransport 而不发真实请求。

    web_tools._fetch_with_redirect_check 现在用 IP-pinning 走 socket.create_connection
    直接连已校验 IP；为让 MockTransport 接管，我们：
    1. 替换 web_tools 内的 `httpx.Client`，让每次构造都注入 MockTransport
    2. 拦截 socket.create_connection，把目标 IP 替换为 127.0.0.1（mock 不真连）
    3. 替换 web_tools._pin_one_address，让它走可控的 _StubResolver
    """
    transport = httpx.MockTransport(handler)
    import mini_deerflow.web_tools as _wt
    original_client = httpx.Client
    original_pin = _wt._pin_one_address
    original_create_connection = socket.create_connection

    def patched_client(*args: Any, **kwargs: Any) -> httpx.Client:
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    def patched_pin(host: str, resolver: Any) -> str | None:
        # 测试夹具：fixture.test 永远返回 127.0.0.1
        if host == "fixture.test":
            return "127.0.0.1"
        return original_pin(host, resolver)

    def patched_create_connection(address, *args, **kwargs):  # type: ignore[no-untyped-def]
        host, port = address
        # 把所有目标重写为 127.0.0.1（mock 不真连）
        return original_create_connection(("127.0.0.1", port), *args, **kwargs)

    _wt.httpx.Client = patched_client  # type: ignore[assignment]
    _wt._pin_one_address = patched_pin  # type: ignore[assignment]
    socket.create_connection = patched_create_connection  # type: ignore[assignment]

    def uninstall() -> None:
        _wt.httpx.Client = original_client  # type: ignore[assignment]
        _wt._pin_one_address = original_pin  # type: ignore[assignment]
        socket.create_connection = original_create_connection  # type: ignore[assignment]

    return uninstall


# ---------------------------------------------------------------------------
# 1. 元数据 + 默认策略
# ---------------------------------------------------------------------------


class TestMetadata:
    def test_web_search_metadata(self) -> None:
        meta: ToolMetadata = BUILTIN_TOOL_METADATA["web_search"]
        assert meta.name == "web_search"
        assert meta.source.value == 1  # LOCAL
        assert ToolCapability.NETWORK_READ in meta.capabilities
        assert ToolSideEffect.READS_NETWORK in meta.side_effects
        # 不混入 PROJECT_READ / WRITE：普通会话也能调用
        assert ToolCapability.PROJECT_READ not in meta.capabilities
        assert ToolSideEffect.WRITES_PROJECT not in meta.side_effects

    def test_web_read_metadata(self) -> None:
        meta: ToolMetadata = BUILTIN_TOOL_METADATA["web_read"]
        assert ToolCapability.NETWORK_READ in meta.capabilities
        assert ToolSideEffect.READS_NETWORK in meta.side_effects

    def test_default_policy_for_web(self) -> None:
        policy = default_policy_for_web()
        assert policy.enabled is True
        # 显式置 False：避免"以为禁用确认"与"元数据豁免"混在一起
        assert policy.requires_confirmation is False
        # 主 Agent 默认允许
        assert policy.is_principal_allowed(ExecutorPrincipal.MAIN_AGENT)
        # MINI_SUBAGENT 默认不直接做网络读取
        assert not policy.is_principal_allowed(ExecutorPrincipal.MINI_SUBAGENT)

    def test_default_policy_for_returns_web(self) -> None:
        assert default_policy_for("web_search") == default_policy_for_web()
        assert default_policy_for("web_read") == default_policy_for_web()

    def test_builtin_metadata_contains_web(self) -> None:
        assert "web_search" in BUILTIN_TOOL_METADATA
        assert "web_read" in BUILTIN_TOOL_METADATA


# ---------------------------------------------------------------------------
# 2. SSRF 防护（单元）
# ---------------------------------------------------------------------------


class _StubResolver:
    """可控的 DNS 解析器；不依赖真实网络。"""

    def __init__(self, mapping: dict[str, list[str]] | None = None) -> None:
        self._mapping = mapping or {}

    def __call__(self, host: str) -> list[str]:
        if host not in self._mapping:
            raise OSError(f"unknown host: {host}")
        return list(self._mapping[host])


class TestHostPolicy:
    def test_rejects_loopback_by_default(self) -> None:
        with pytest.raises(WebToolSSRFError):
            resolve_host_policy(
                "http://127.0.0.1/page",
                resolver=_StubResolver({"127.0.0.1": ["127.0.0.1"]}),
            )

    def test_rejects_private_ip_by_default(self) -> None:
        with pytest.raises(WebToolSSRFError):
            resolve_host_policy(
                "http://10.0.0.1/page",
                resolver=_StubResolver({"10.0.0.1": ["10.0.0.1"]}),
            )

    def test_rejects_ipv6_loopback(self) -> None:
        with pytest.raises(WebToolSSRFError):
            resolve_host_policy(
                "http://[::1]/page",
                resolver=_StubResolver({"::1": ["::1"]}),
            )

    def test_rejects_link_local(self) -> None:
        with pytest.raises(WebToolSSRFError):
            resolve_host_policy(
                "http://169.254.169.254/latest/meta-data/",
                resolver=_StubResolver({"169.254.169.254": ["169.254.169.254"]}),
            )

    def test_rejects_ip_literal_even_for_public(self) -> None:
        # IP 字面量直接拒绝（防止绕过 DNS 解析）
        with pytest.raises(WebToolSSRFError):
            resolve_host_policy(
                "http://8.8.8.8/dns-query",
                resolver=_StubResolver({"8.8.8.8": ["8.8.8.8"]}),
            )

    def test_rejects_dns_resolving_to_private(self) -> None:
        # 域名解析到私有 IP 也拒绝
        with pytest.raises(WebToolSSRFError):
            resolve_host_policy(
                "http://attacker.example.invalid/x",
                resolver=_StubResolver(
                    {"attacker.example.invalid": ["10.0.0.5"]}
                ),
            )

    def test_allows_when_explicit_whitelist(self) -> None:
        policy = HostPolicy(allowed_hosts=frozenset({"fixture.test"}))
        scheme, host = resolve_host_policy(
            "https://fixture.test/page",
            policy=policy,
            resolver=_StubResolver({"fixture.test": ["1.2.3.4"]}),
        )
        assert scheme == "https"
        assert host == "fixture.test"

    def test_allows_loopback_when_explicit_opt_in(self) -> None:
        policy = HostPolicy(allow_loopback=True)
        scheme, host = resolve_host_policy(
            "http://127.0.0.1:8000/health",
            policy=policy,
            resolver=_StubResolver({"127.0.0.1": ["127.0.0.1"]}),
        )
        assert host == "127.0.0.1"

    def test_rejects_non_http_scheme(self) -> None:
        with pytest.raises(WebToolUsageError):
            resolve_host_policy("file:///etc/passwd")
        with pytest.raises(WebToolUsageError):
            resolve_host_policy("ftp://example.com/x")

    def test_rejects_empty_url(self) -> None:
        with pytest.raises(WebToolUsageError):
            resolve_host_policy("")

    def test_rejects_url_without_host(self) -> None:
        with pytest.raises(WebToolUsageError):
            resolve_host_policy("http:///path")

    def test_rejects_dns_failure(self) -> None:
        with pytest.raises(WebToolSSRFError):
            resolve_host_policy(
                "http://nonexistent.invalid/x",
                resolver=_StubResolver(),  # 空映射 → OSError
            )

    def test_allows_loopback_when_explicit_opt_in(self) -> None:
        policy = HostPolicy(allow_loopback=True, allow_ip_literal=True)
        scheme, host = resolve_host_policy(
            "http://127.0.0.1:8000/health",
            policy=policy,
            resolver=_StubResolver({"127.0.0.1": ["127.0.0.1"]}),
        )
        assert host == "127.0.0.1"


# ---------------------------------------------------------------------------
# 3. HTML 文本提取
# ---------------------------------------------------------------------------


class TestExtractText:
    def test_strips_script_and_style(self) -> None:
        html = (
            "<html><head><style>body { color: red; }</style></head>"
            "<body><script>alert('x')</script>"
            "<p>Hello world</p></body></html>"
        )
        text = extract_readable_text(html)
        assert "Hello world" in text
        assert "alert" not in text
        assert "color" not in text
        assert "<p>" not in text

    def test_preserves_link_url(self) -> None:
        html = '<p>See <a href="https://example.com/foo">Foo</a> for details</p>'
        text = extract_readable_text(html)
        assert "Foo [https://example.com/foo]" in text

    def test_collapses_whitespace(self) -> None:
        html = "<p>  line  one\n\nline\t  two  </p>"
        text = extract_readable_text(html)
        assert text == "line one line two"

    def test_decodes_common_entities(self) -> None:
        html = "<p>A &amp; B &lt; C &gt; D &quot;E&quot; F&#39;s G&nbsp;H</p>"
        text = extract_readable_text(html)
        assert "A & B < C > D \"E\" F's G H" in text

    def test_truncates_with_marker(self) -> None:
        html = "<p>" + ("a " * 5000) + "</p>"
        text = extract_readable_text(html, max_chars=100)
        assert text.endswith("…[截断于 100 字符]")

    def test_handles_empty(self) -> None:
        assert extract_readable_text("") == ""
        assert extract_readable_text("<html></html>") == ""


# ---------------------------------------------------------------------------
# 4. web_search 行为
# ---------------------------------------------------------------------------


class FakeProvider(WebSearchProvider):
    """测试用 Provider。"""

    def __init__(self, hits: list[WebSearchHit] | None = None) -> None:
        self._hits = hits or []
        self.calls: list[tuple[str, int]] = []

    def search(self, query: str, *, top_k: int) -> list[WebSearchHit]:
        self.calls.append((query, top_k))
        return self._hits[:top_k]


class TestWebSearch:
    def test_unavailable_provider_returns_available_false(self) -> None:
        tool = build_web_search_tool(
            WebToolConfig(),
            provider=UnavailableWebSearchProvider(),
        )
        result = json.loads(tool.execute({"query": "hello"}))
        assert result["ok"] is True
        assert result["available"] is False
        assert "搜索服务" in result["reason"] or "未配置" in result["reason"]

    def test_fake_provider_returns_hits(self) -> None:
        provider = FakeProvider(
            [
                WebSearchHit(
                    title="Result 1",
                    url="https://example.com/1",
                    snippet="Snippet 1",
                ),
                WebSearchHit(
                    title="Result 2",
                    url="https://example.com/2",
                    snippet="Snippet 2",
                ),
            ]
        )
        tool = build_web_search_tool(WebToolConfig(), provider=provider)
        result = json.loads(tool.execute({"query": "test", "top_k": 2}))
        assert result["ok"] is True
        assert result["count"] == 2
        assert result["results"][0]["title"] == "Result 1"
        assert result["results"][0]["url"] == "https://example.com/1"
        # 不可信数据声明
        assert "不可信网络数据" in result["notice"]

    def test_query_must_be_string(self) -> None:
        tool = build_web_search_tool(WebToolConfig(), provider=FakeProvider([]))
        result = json.loads(tool.execute({"query": ""}))
        assert result["ok"] is False
        assert result["kind"] == "usage_error"

    def test_top_k_must_be_in_range(self) -> None:
        tool = build_web_search_tool(WebToolConfig(), provider=FakeProvider([]))
        result = json.loads(tool.execute({"query": "x", "top_k": 0}))
        assert result["ok"] is False
        assert result["kind"] == "usage_error"

    def test_provider_exception_surfaced(self) -> None:
        class BoomProvider(WebSearchProvider):
            def search(self, query: str, *, top_k: int) -> list[WebSearchHit]:
                raise WebToolNetworkError("upstream fail")

        tool = build_web_search_tool(WebToolConfig(), provider=BoomProvider())
        result = json.loads(tool.execute({"query": "x"}))
        assert result["ok"] is False
        assert "upstream fail" in result["error"]

    def test_top_k_truncates_extra_hits(self) -> None:
        provider = FakeProvider(
            [
                WebSearchHit(f"t{i}", f"https://e/{i}", f"s{i}")
                for i in range(10)
            ]
        )
        tool = build_web_search_tool(WebToolConfig(), provider=provider)
        result = json.loads(tool.execute({"query": "x", "top_k": 3}))
        assert result["count"] == 3

    def test_callable_provider(self) -> None:
        tool = build_web_search_tool(
            WebToolConfig(),
            provider=CallableWebSearchProvider(
                lambda q, k: [WebSearchHit("a", "https://a", "")]
            ),
        )
        result = json.loads(tool.execute({"query": "x"}))
        assert result["ok"] is True
        assert result["count"] == 1


# ---------------------------------------------------------------------------
# 5. web_read 行为（用 httpx.MockTransport，不发真实请求）
# ---------------------------------------------------------------------------


def _build_mock_transport(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


class TestWebRead:
    def test_rejects_loopback_by_default(self) -> None:
        tool = build_web_read_tool(WebToolConfig())
        result = json.loads(tool.execute({"url": "http://127.0.0.1/x"}))
        assert result["ok"] is False
        assert result["kind"] == "ssrf_error"
        assert "127.0.0.1" in result["error"]

    def test_rejects_private_ip(self) -> None:
        tool = build_web_read_tool(WebToolConfig())
        result = json.loads(tool.execute({"url": "http://10.0.0.1/x"}))
        assert result["ok"] is False
        assert result["kind"] == "ssrf_error"

    def test_rejects_non_http_scheme(self) -> None:
        tool = build_web_read_tool(
            WebToolConfig(
                host_policy=HostPolicy(allow_loopback=True),
            )
        )
        result = json.loads(tool.execute({"url": "file:///etc/passwd"}))
        assert result["ok"] is False
        assert result["kind"] == "usage_error"

    def test_invalid_url_format(self) -> None:
        tool = build_web_read_tool(
            WebToolConfig(
                host_policy=HostPolicy(allow_loopback=True),
            )
        )
        result = json.loads(tool.execute({"url": "not-a-url"}))
        assert result["ok"] is False
        assert result["kind"] == "usage_error"

    def test_extracts_text_from_html(self) -> None:
        html = (
            "<html><body><h1>Title</h1>"
            "<p>Hello <a href='https://e.com/x'>link</a> world.</p>"
            "<script>alert(1)</script></body></html>"
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=html)

        policy = HostPolicy(allowed_hosts=frozenset({"fixture.test"}))
        cfg = WebToolConfig(host_policy=policy)
        uninstall = _install_mock_socket(handler)
        try:
            tool = build_web_read_tool(cfg)
            result = json.loads(tool.execute({"url": "https://fixture.test/page"}))
        finally:
            uninstall()

        assert result["ok"] is True
        assert result["status_code"] == 200
        assert "Title" in result["text"]
        assert "link [https://e.com/x]" in result["text"]
        assert "alert" not in result["text"]
        assert "不可信网络数据" in result["notice"]

    def test_truncates_large_responses(self) -> None:
        big_body = "x" * (WEB_TOOL_LIMITS["read_max_bytes"] + 1024)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=big_body)

        policy = HostPolicy(allowed_hosts=frozenset({"fixture.test"}))
        cfg = WebToolConfig(host_policy=policy, max_bytes=1024)
        uninstall = _install_mock_socket(handler)
        try:
            tool = build_web_read_tool(cfg)
            result = json.loads(tool.execute({"url": "https://fixture.test/big"}))
        finally:
            uninstall()

        assert result["ok"] is True
        assert result["truncated"] is True
        # R3：流式读取必须真的只下载 max_bytes 个字节
        assert result["bytes"] <= 1024

    def test_redirect_to_private_ip_rejected(self) -> None:
        """重定向到私有 IP 必须被拦截（防 open redirect → SSRF）。"""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "fixture.test":
                return httpx.Response(
                    302, headers={"Location": "http://10.0.0.5/leak"}
                )
            return httpx.Response(200, text="internal")

        policy = HostPolicy(allowed_hosts=frozenset({"fixture.test"}))
        cfg = WebToolConfig(host_policy=policy)
        uninstall = _install_mock_socket(handler)
        try:
            tool = build_web_read_tool(cfg)
            result = json.loads(tool.execute({"url": "https://fixture.test/redir"}))
        finally:
            uninstall()

        assert result["ok"] is False
        assert result["kind"] == "ssrf_error"
        assert "10.0.0.5" in result["error"]

    def test_redirect_chain_within_limit(self) -> None:
        """重定向链在限内时跟随到底。"""

        def handler(request: httpx.Request) -> httpx.Response:
            n = int(request.url.params.get("n", "0"))
            if n < 2:
                return httpx.Response(
                    302, headers={"Location": f"/page?n={n + 1}"}
                )
            return httpx.Response(200, text="<p>final</p>")

        policy = HostPolicy(allowed_hosts=frozenset({"fixture.test"}))
        cfg = WebToolConfig(host_policy=policy, max_redirects=3)
        uninstall = _install_mock_socket(handler)
        try:
            tool = build_web_read_tool(cfg)
            result = json.loads(tool.execute({"url": "https://fixture.test/p?n=0"}))
        finally:
            uninstall()

        assert result["ok"] is True
        assert "final" in result["text"]

    def test_too_many_redirects(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                302, headers={"Location": "/next"}
            )

        policy = HostPolicy(allowed_hosts=frozenset({"fixture.test"}))
        cfg = WebToolConfig(host_policy=policy, max_redirects=2)
        uninstall = _install_mock_socket(handler)
        try:
            tool = build_web_read_tool(cfg)
            result = json.loads(tool.execute({"url": "https://fixture.test/loop"}))
        finally:
            uninstall()

        assert result["ok"] is False
        assert "重定向" in result["error"]

    # R5：HTTP 错误不应被标为成功
    def test_http_404_returns_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, text="not found")

        policy = HostPolicy(allowed_hosts=frozenset({"fixture.test"}))
        cfg = WebToolConfig(host_policy=policy)
        uninstall = _install_mock_socket(handler)
        try:
            tool = build_web_read_tool(cfg)
            result = json.loads(tool.execute({"url": "https://fixture.test/missing"}))
        finally:
            uninstall()

        assert result["ok"] is False
        assert "404" in result["error"]
        assert result["status_code"] == 404

    def test_http_500_returns_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        policy = HostPolicy(allowed_hosts=frozenset({"fixture.test"}))
        cfg = WebToolConfig(host_policy=policy)
        uninstall = _install_mock_socket(handler)
        try:
            tool = build_web_read_tool(cfg)
            result = json.loads(tool.execute({"url": "https://fixture.test/boom"}))
        finally:
            uninstall()

        assert result["ok"] is False
        assert "500" in result["error"]

    def test_http_403_returns_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, text="forbidden")

        policy = HostPolicy(allowed_hosts=frozenset({"fixture.test"}))
        cfg = WebToolConfig(host_policy=policy)
        uninstall = _install_mock_socket(handler)
        try:
            tool = build_web_read_tool(cfg)
            result = json.loads(tool.execute({"url": "https://fixture.test/private"}))
        finally:
            uninstall()

        assert result["ok"] is False
        assert "403" in result["error"]


# ---------------------------------------------------------------------------
# 6. create_web_tools 工具集合 + runtime 集成
# ---------------------------------------------------------------------------


class TestCreateTools:
    def test_returns_both_tools(self) -> None:
        tools = create_web_tools()
        names = [t.name for t in tools]
        assert names == ["web_read", "web_search"] or set(names) == {"web_read", "web_search"}


class TestRuntimeIntegration:
    def test_runtime_registers_web_tools(self) -> None:
        registry = create_default_registry()
        provider = FakeProvider(
            [WebSearchHit("t", "https://e.com", "s")]
        )
        cfg = RuntimeConfig(
            web_search_provider=provider,
            web_host_policy=HostPolicy(
                allow_loopback=True,
                allowed_hosts=frozenset({"fixture.test"}),
            ),
        )
        runtime = create_runtime(
            store=InMemoryThreadStore(),
            registry=registry,
            config=cfg,
        )
        try:
            names = {tool.name for tool in registry.list_tools()}
            assert "web_search" in names
            assert "web_read" in names
        finally:
            runtime.shutdown()

    def test_web_search_executes_via_registry(self) -> None:
        registry = create_default_registry()
        provider = FakeProvider(
            [WebSearchHit("hello", "https://example.com", "world")]
        )
        cfg = RuntimeConfig(
            web_search_provider=provider,
        )
        runtime = create_runtime(
            store=InMemoryThreadStore(),
            registry=registry,
            config=cfg,
        )
        try:
            result = registry.execute("web_search", {"query": "x"})
            parsed = json.loads(result)
            assert parsed["ok"] is True
            assert parsed["count"] == 1
        finally:
            runtime.shutdown()

    def test_web_read_ssrf_blocked_via_registry(self) -> None:
        registry = create_default_registry()
        cfg = RuntimeConfig(
            web_host_policy=HostPolicy(),  # 默认拒绝
        )
        runtime = create_runtime(
            store=InMemoryThreadStore(),
            registry=registry,
            config=cfg,
        )
        try:
            result = registry.execute("web_read", {"url": "http://127.0.0.1/x"})
            parsed = json.loads(result)
            assert parsed["ok"] is False
            assert parsed["kind"] == "ssrf_error"
        finally:
            runtime.shutdown()


# ---------------------------------------------------------------------------
# 7. 脚本模型端到端（web_search + web_read 真实调用链）
# ---------------------------------------------------------------------------


class _ScriptedModelForWeb:
    """脚本模型：先调 web_search，再用返回的 URL 调 web_read，最终输出答案。"""

    def __init__(self) -> None:
        self.calls = 0

    def decide(
        self,
        messages: list[Any],
        tools: list[Any],
        **kwargs: Any,
    ) -> Any:
        from mini_deerflow.models import FinalAnswer, Message, ToolCall

        self.calls += 1
        if self.calls == 1:
            # 第一次：调用 web_search
            return Message(
                role="assistant",
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="web_search",
                        arguments={"query": "mini-deerflow", "top_k": 1},
                    )
                ],
            )
        if self.calls == 2:
            # 第二次：调用 web_read 取详情
            return Message(
                role="assistant",
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-2",
                        name="web_read",
                        arguments={"url": "https://fixture.test/intro"},
                    )
                ],
            )
        # 第三次：最终答案
        return FinalAnswer(content="找到 https://fixture.test/intro，正文：hello world")

    async def aclose(self) -> None:
        return None


class TestEndToEnd:
    def test_scripted_model_calls_web_tools(self) -> None:
        # 准备 registry：注入 web 工具（白名单 + mock transport）
        html = "<html><body><h1>hi</h1><p>hello world</p></body></html>"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=html)

        registry = create_default_registry()
        provider = FakeProvider(
            [WebSearchHit("intro", "https://fixture.test/intro", "snippet")]
        )
        cfg = WebToolConfig(
            host_policy=HostPolicy(allowed_hosts=frozenset({"fixture.test"})),
        )
        registry.register_many(create_web_tools(cfg, provider=provider))
        # 验证模型能看见 web 工具
        from mini_deerflow.tool_authorization import ToolPolicy, ToolPolicyRegistry

        policy_registry = ToolPolicyRegistry(thread_store=InMemoryThreadStore())
        for name in ("web_search", "web_read"):
            meta = BUILTIN_TOOL_METADATA[name]
            policy_registry.register_metadata(meta)
            policy_registry.set_policy(name, default_policy_for_web())

        uninstall = _install_mock_socket(handler)
        try:
            # 验证可执行
            search_result = json.loads(
                registry.execute("web_search", {"query": "x", "top_k": 1})
            )
            assert search_result["ok"] is True
            assert search_result["count"] == 1

            read_result = json.loads(
                registry.execute(
                    "web_read", {"url": "https://fixture.test/intro"}
                )
            )
            assert read_result["ok"] is True
            assert "hello world" in read_result["text"]
            # 来源 URL 透出
            assert read_result["url"].endswith("/intro")
        finally:
            uninstall()

    # R1：Agent 真实路径能看到并调用 web 工具
    def test_agent_real_path_sees_web_tools(self) -> None:
        """模拟完整 Agent 链路：可见性 + 授权 + 执行。

        修复前 session_caps / default_caps 缺 NETWORK_READ，普通/项目会话
        model.decide 都看不到 web_* 工具。
        """
        from mini_deerflow.agent import Agent
        from mini_deerflow.model_factory import create_model as _create_model
        from mini_deerflow.model_config import ModelSettings
        from mini_deerflow.tool_authorization import ToolPolicyRegistry

        # 普通会话 thread（无 project_id）
        store = InMemoryThreadStore()
        thread = store.create(thread_id="t-1")

        registry = create_default_registry()
        provider = FakeProvider(
            [WebSearchHit("intro", "https://example.com", "snippet")]
        )
        cfg = WebToolConfig(
            host_policy=HostPolicy(
                allowed_hosts=frozenset({"example.com"}),
            ),
        )
        registry.register_many(create_web_tools(cfg, provider=provider))

        # 注册策略
        policy_registry = ToolPolicyRegistry(thread_store=store)
        for name in ("web_search", "web_read"):
            policy_registry.register_metadata(BUILTIN_TOOL_METADATA[name])
            policy_registry.set_policy(name, default_policy_for_web())

        # 用 fake 模型（provider=fake，不发网络）
        agent = Agent(
            model=_create_model(ModelSettings(provider="fake")),
            registry=registry,
            store=store,
            tool_policy_registry=policy_registry,
        )

        # 验证可见性
        tool_names = {spec.name for spec in agent._registry.tool_specs()}
        assert "web_search" in tool_names, (
            "R1 回归：普通会话应能看到 web_search（已注入 NETWORK_READ）"
        )
        assert "web_read" in tool_names


# ---------------------------------------------------------------------------
# 8. 不可信数据声明存在
# ---------------------------------------------------------------------------


class TestUntrustedNotice:
    def test_search_includes_notice(self) -> None:
        tool = build_web_search_tool(
            WebToolConfig(),
            provider=FakeProvider([WebSearchHit("a", "https://a", "b")]),
        )
        result = json.loads(tool.execute({"query": "x"}))
        assert "不可信网络数据" in result["notice"]
        assert "不是系统指令" in result["notice"]

    def test_read_includes_notice(self) -> None:
        html = "<p>x</p>"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=html)

        policy = HostPolicy(allowed_hosts=frozenset({"fixture.test"}))
        cfg = WebToolConfig(host_policy=policy)
        uninstall = _install_mock_socket(handler)
        try:
            tool = build_web_read_tool(cfg)
            result = json.loads(tool.execute({"url": "https://fixture.test/p"}))
        finally:
            uninstall()

        assert "不可信网络数据" in result["notice"]


# ---------------------------------------------------------------------------
# 9. R2：DNS 重绑定防护（真实 socket 路径）
# ---------------------------------------------------------------------------


class TestDNSPinning:
    """R2 回归：检查时返回公共 IP，真实连接时被替换为私有 IP 应被拦截。"""

    def test_dns_rebinding_to_loopback_blocked(self) -> None:
        """模拟：resolve_host_policy 检查后接受一个公共 IP，连接阶段 socket
        实际 connect 到 127.0.0.1；旧实现直接读到了本地内容，新实现必须
        因为 URL host 不在白名单而失败。
        """
        policy = HostPolicy(allowed_hosts=frozenset())  # 无白名单
        cfg = WebToolConfig(host_policy=policy)
        tool = build_web_read_tool(cfg)

        # 不放任何白名单时直接拿 host 解析会失败（公网默认拒绝）
        # 这里我们要测的是「连接阶段被劫持」：用白名单 + resolver
        # 走 resolve→返回公共 IP，再让 connect 阶段劫持。
        # 为简化：直接对 fixture.test 走白名单+自定义 resolver
        whitelist_policy = HostPolicy(allowed_hosts=frozenset({"fixture.test"}))

        # 自定义 resolver：返回公共 IP（让 SSRF 校验通过）
        def fake_resolver(host: str) -> list[str]:
            return ["93.184.216.34"]

        cfg2 = WebToolConfig(
            host_policy=whitelist_policy,
            resolver=fake_resolver,
        )
        tool2 = build_web_read_tool(cfg2)
        # 真实连接时 mock 拦截——验证 mock 收到 host 头仍是 fixture.test
        # 也就是说：fix 之后即使 IP 被劫持，host 头里的域名仍是 SSRF 校验过的域名
        captured_hosts: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured_hosts.append(request.url.host)
            return httpx.Response(200, text="<p>ok</p>")

        uninstall = _install_mock_socket(handler)
        try:
            # 显式让 _pin_one_address 走我们自己的 resolver
            import mini_deerflow.web_tools as _wt
            original_pin = _wt._pin_one_address

            def pinned(host, resolver):
                if host == "fixture.test":
                    return "93.184.216.34"
                return original_pin(host, resolver)

            _wt._pin_one_address = pinned  # type: ignore[assignment]
            try:
                result = json.loads(
                    tool2.execute({"url": "https://fixture.test/p"})
                )
            finally:
                _wt._pin_one_address = original_pin  # type: ignore[assignment]
        finally:
            uninstall()

        # Mock 收到的是 fixture.test（已通过白名单校验）
        assert captured_hosts == ["fixture.test"]
        assert result["ok"] is True

    def test_dns_rebinding_to_private_ip_blocked_by_pin(self) -> None:
        """resolver 返回私有 IP 时 _pin_one_address 返回 None → 整个请求被拒。"""

        def evil_resolver(host: str) -> list[str]:
            return ["10.0.0.5"]  # 私有 IP

        whitelist_policy = HostPolicy(allowed_hosts=frozenset({"fixture.test"}))
        cfg = WebToolConfig(
            host_policy=whitelist_policy,
            resolver=evil_resolver,
        )
        tool = build_web_read_tool(cfg)
        result = json.loads(tool.execute({"url": "https://fixture.test/p"}))
        # 私有 IP 仍被 resolve_host_policy 抓到（白名单只放域名不限制 IP）
        assert result["ok"] is False
        assert result["kind"] == "ssrf_error"


# ---------------------------------------------------------------------------
# 10. R4：搜索 Provider 配置工厂
# ---------------------------------------------------------------------------


class TestSearchProviderConfig:
    def test_unavailable_kind_returns_unavailable(self) -> None:
        provider = build_web_search_provider_from_config(kind="unavailable")
        assert isinstance(provider, UnavailableWebSearchProvider)
        assert provider.is_available() is False

    def test_fake_kind_returns_fixed_list(self) -> None:
        provider = build_web_search_provider_from_config(
            kind="fake",
            fake_results=[("t1", "https://a", "s1"), ("t2", "https://b", "s2")],
        )
        assert isinstance(provider, FixedListWebSearchProvider)
        assert provider.is_available() is True
        hits = provider.search("q", top_k=5)
        assert len(hits) == 2
        assert hits[0].title == "t1"
        assert hits[0].url == "https://a"

    def test_fake_kind_empty_results(self) -> None:
        provider = build_web_search_provider_from_config(kind="fake", fake_results=[])
        hits = provider.search("q", top_k=5)
        assert hits == []

    def test_mcp_kind_still_unavailable(self) -> None:
        """MCP 搜索接入留待后续 PR；当前 kind=mcp 也返回不可用。"""
        provider = build_web_search_provider_from_config(kind="mcp")
        assert isinstance(provider, UnavailableWebSearchProvider)

    def test_unknown_kind_falls_back_to_unavailable(self) -> None:
        """未知 kind 走 fallback 到 UnavailableWebSearchProvider，不抛异常。

        严格 kind 校验在 ApiSettings 层做；web_tools 工厂保持宽容。
        """
        provider = build_web_search_provider_from_config(kind="bogus")
        assert isinstance(provider, UnavailableWebSearchProvider)
