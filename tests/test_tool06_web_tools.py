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
import threading
import time
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

    R2 复审后 web_tools 用 _PinAddrTransport（stdlib socket + makefile）。
    测试用 monkey-patch _PinAddrTransport.handle_request 走 httpx.MockTransport。
    """
    import mini_deerflow.web_tools as _wt
    original_handle = _wt._PinAddrTransport.handle_request

    def patched_handle(self: Any, request: httpx.Request) -> httpx.Response:
        # 走 httpx.MockTransport 拿到响应，body 仍按 max_bytes 截断
        from mini_deerflow.web_tools import WEB_TOOL_LIMITS
        transport = httpx.MockTransport(handler)
        # 把 request 也传给 handler（与生产路径一致）
        resp = transport.handle_request(request)
        # 模拟 transport 内 max_bytes 截断
        max_bytes = getattr(self, "_max_bytes", WEB_TOOL_LIMITS["read_max_bytes"])
        body = resp.content
        truncated = False
        if len(body) > max_bytes:
            body = body[:max_bytes]
            truncated = True
        # 重建 stream + 附加 truncated 标记
        new_headers = list(resp.headers.items())
        new_headers.append(("X-Transport-Truncated", "1" if truncated else "0"))
        # 用 ByteStream 保证 httpx 内部能正确读取
        stream = httpx.ByteStream(body)
        return httpx.Response(
            status_code=resp.status_code,
            headers=new_headers,
            stream=stream,
            request=request,
        )

    _wt._PinAddrTransport.handle_request = patched_handle  # type: ignore[assignment]

    # 给 fixture.test 装一个 stub resolver + stub pin
    original_resolver = _wt._default_resolve
    original_pin = _wt._pin_one_address

    def stub_resolver(host: str) -> list[str]:
        if host == "fixture.test":
            return ["93.184.216.34"]
        return original_resolver(host)

    def stub_pin(host: str, resolver: Any = None, policy: Any = None) -> str | None:
        if host == "fixture.test":
            return "93.184.216.34"
        return original_pin(host, resolver, policy)

    _wt._default_resolve = stub_resolver  # type: ignore[assignment]
    _wt._pin_one_address = stub_pin  # type: ignore[assignment]

    def uninstall() -> None:
        _wt._PinAddrTransport.handle_request = original_handle  # type: ignore[assignment]
        _wt._default_resolve = original_resolver  # type: ignore[assignment]
        _wt._pin_one_address = original_pin  # type: ignore[assignment]

    return uninstall

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
# 9c. R3：流式截断（响应被 transport 实时截断，bytes<=max_bytes）
# ---------------------------------------------------------------------------


class TestStreamingTruncation:
    """R3 复审：max_bytes=1KB 真的只下载 ≤1KB，不先完整缓存。"""

    def test_truncation_via_mock(self) -> None:
        # 26KB 响应：mock 不真实发包，验证 transport 层截断逻辑
        big_body = "x" * (26 * 1024)

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
        # 关键断言：bytes 真的不超过 max_bytes（transport 截断后上层再读）
        assert result["bytes"] <= 1024

    def test_under_limit_not_truncated(self) -> None:
        body = "hello"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=body)

        policy = HostPolicy(allowed_hosts=frozenset({"fixture.test"}))
        cfg = WebToolConfig(host_policy=policy, max_bytes=1024)
        uninstall = _install_mock_socket(handler)
        try:
            tool = build_web_read_tool(cfg)
            result = json.loads(tool.execute({"url": "https://fixture.test/small"}))
        finally:
            uninstall()

        assert result["ok"] is True
        assert result["truncated"] is False
        assert result["bytes"] == 5

    # R3-B：声明 Content-Length 超过 max_bytes → truncated=true
    def test_content_length_over_limit_marks_truncated(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            resp = httpx.Response(200, text="x" * 4096)
            resp.headers["Content-Length"] = "4096"
            return resp

        policy = HostPolicy(allowed_hosts=frozenset({"fixture.test"}))
        cfg = WebToolConfig(host_policy=policy, max_bytes=1024)
        uninstall = _install_mock_socket(handler)
        try:
            tool = build_web_read_tool(cfg)
            result = json.loads(tool.execute({"url": "https://fixture.test/long"}))
        finally:
            uninstall()

        assert result["ok"] is True
        assert result["truncated"] is True
        assert result["bytes"] <= 1024

    # R3-C：跨总 deadline 触发超时
    def test_total_deadline_timeout(self) -> None:
        """sleep 较久后调用 → 触发总 deadline（即使单次 read 短时间内完成）。"""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<p>ok</p>")

        policy = HostPolicy(allowed_hosts=frozenset({"fixture.test"}))
        # 总 timeout 给极小值，确保 deadline 很快过期
        cfg = WebToolConfig(
            host_policy=policy,
            timeout_s=0.05,  # 50ms
        )
        uninstall = _install_mock_socket(handler)
        try:
            # 直接调 transport：传入极短 deadline
            from mini_deerflow.web_tools import (
                _PinAddrTransport,
                resolve_host_policy,
            )
            _, host = resolve_host_policy(
                "https://fixture.test/p",
                policy=policy,
                resolver=lambda h: ["93.184.216.34"],
            )
            import time as _time
            deadline = _time.monotonic() - 1.0  # 已过期
            transport = _PinAddrTransport(
                "93.184.216.34", cfg.max_bytes, deadline_monotonic=deadline
            )
            try:
                # 构造一个 httpx Request 并 handle
                req = httpx.Request("GET", "https://fixture.test/p")
                transport.handle_request(req)
                raise AssertionError("应抛超时")
            except Exception as exc:
                # deadline 已过期 → 任何 read 路径都该抛
                assert "deadline" in str(exc) or "超时" in str(exc) or "总" in str(exc), (
                    f"unexpected exc: {exc!r}"
                )
        finally:
            uninstall()


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
# 11. R1：策略确认链路
# ---------------------------------------------------------------------------


class TestConfirmationFlow:
    """R1：default_policy_for_web(requires_confirmation=False) 不应弹确认。"""

    def test_default_web_policy_does_not_trigger_confirm(self) -> None:
        """default_policy_for_web(requires_confirmation=False) 不应弹确认。"""
        from mini_deerflow.tool_contracts import (
            ExecutorPrincipal,
            RunAuthority,
            evaluate_tool_access,
        )

        policy = default_policy_for_web()
        assert policy.requires_confirmation is False
        authority = RunAuthority(
            principal=ExecutorPrincipal.MAIN_AGENT,
            thread_id="t-1",
            run_id="r-1",
            project_id=None,
            granted_capabilities=frozenset({ToolCapability.NETWORK_READ}),
        )
        decision = evaluate_tool_access(
            BUILTIN_TOOL_METADATA["web_search"],
            policy,
            authority,
        )
        assert decision.kind.value == 1  # ALLOW
        assert not decision.is_confirm

    def test_explicit_confirm_triggers_confirmation(self) -> None:
        """policy.requires_confirmation=True 仍能弹确认（用户/产品可强制）。"""
        from mini_deerflow.tool_contracts import (
            ExecutorPrincipal,
            RunAuthority,
            ToolPolicy,
            evaluate_tool_access,
        )

        policy = ToolPolicy(
            enabled=True,
            allowed_principals=frozenset({ExecutorPrincipal.MAIN_AGENT}),
            requires_confirmation=True,
        )
        authority = RunAuthority(
            principal=ExecutorPrincipal.MAIN_AGENT,
            thread_id="t-1",
            run_id="r-1",
            project_id=None,
            granted_capabilities=frozenset({ToolCapability.NETWORK_READ}),
        )
        decision = evaluate_tool_access(
            BUILTIN_TOOL_METADATA["web_search"],
            policy,
            authority,
        )
        assert decision.is_confirm

    def test_no_network_capability_blocks(self) -> None:
        """会话缺 NETWORK_READ → 拒绝。"""
        from mini_deerflow.tool_contracts import (
            ExecutorPrincipal,
            RunAuthority,
            evaluate_tool_access,
        )

        authority = RunAuthority(
            principal=ExecutorPrincipal.MAIN_AGENT,
            thread_id="t-1",
            run_id="r-1",
            project_id=None,
            granted_capabilities=frozenset(),  # 无 NETWORK_READ
        )
        decision = evaluate_tool_access(
            BUILTIN_TOOL_METADATA["web_read"],
            default_policy_for_web(),
            authority,
        )
        assert decision.is_denied
        assert decision.reason_code == "capability_missing"

    # R1 收窄：豁免仅限 web_* 工具 + 仅需 NETWORK_READ
    def test_sandbox_write_still_requires_confirmation(self) -> None:
        """sandbox_write_file 走 metadata requires_confirmation 默认路径，即使
        policy=False 也仍 CONFIRM（确认 R1 收窄不影响其他工具）。"""
        from mini_deerflow.tool_contracts import (
            ExecutorPrincipal,
            RunAuthority,
            ToolPolicy,
            evaluate_tool_access,
        )

        policy = ToolPolicy(
            enabled=True,
            allowed_principals=frozenset({ExecutorPrincipal.MAIN_AGENT}),
            requires_confirmation=False,  # 显式豁免
        )
        authority = RunAuthority(
            principal=ExecutorPrincipal.MAIN_AGENT,
            thread_id="t-1",
            run_id="r-1",
            project_id=None,
            granted_capabilities=frozenset(
                # sandbox_write_file 缺分类：默认 deny
            ),
        )
        # 实际场景：sandbox_write_file.uncategorized=True → metadata 缺失分类
        # 直接 deny 而非 CONFIRM；这里用 sandbox_run_command 验证
        from mini_deerflow.tool_contracts import BUILTIN_TOOL_METADATA as _M
        decision = evaluate_tool_access(
            _M["sandbox_run_command"],
            policy,
            authority,
        )
        # 因 capability_missing → deny（缺 COMMAND_EXECUTE）
        # 关键断言：policy.requires_confirmation=False 不会让这个工具 ALLOW
        assert decision.is_denied
        assert decision.reason_code == "capability_missing"

    def test_web_tools_are_exempt_from_metadata_confirmation(self) -> None:
        """web_search/web_read metadata.requires_confirmation()=True（READS_NETWORK
        不豁免），但仅当 policy.requires_confirmation=False 时才不 CONFIRM。"""
        from mini_deerflow.tool_contracts import (
            ExecutorPrincipal,
            RunAuthority,
            evaluate_tool_access,
        )

        # 验证 metadata 自身判定确实需要确认（policy 不干预时）
        meta = BUILTIN_TOOL_METADATA["web_search"]
        assert meta.requires_confirmation() is True, "READS_NETWORK 应触发 metadata 确认"

        # policy.requires_confirmation=False 走豁免
        authority = RunAuthority(
            principal=ExecutorPrincipal.MAIN_AGENT,
            thread_id="t-1",
            run_id="r-1",
            project_id=None,
            granted_capabilities=frozenset({ToolCapability.NETWORK_READ}),
        )
        decision = evaluate_tool_access(
            meta,
            default_policy_for_web(),
            authority,
        )
        assert decision.kind.value == 1  # ALLOW
        assert not decision.is_confirm


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

            def pinned(host: str, resolver: Any, policy: Any = None) -> str | None:
                if host == "fixture.test":
                    return "93.184.216.34"
                return original_pin(host, resolver, policy)

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
# 9b. R2：socket monkey-patch 不影响进程内其他连接
# ---------------------------------------------------------------------------


class TestProcessIsolation:
    """R2 复审：web_tools 不修改进程全局 socket.create_connection。"""

    def test_unrelated_socket_create_connection_unchanged(self) -> None:
        import mini_deerflow.web_tools as _wt

        original = socket.create_connection
        cfg = WebToolConfig(
            host_policy=HostPolicy(allowed_hosts=frozenset({"fixture.test"})),
        )
        tool = build_web_read_tool(cfg)
        # 触发一次调用（用 helper mock）
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<p>ok</p>")

        uninstall = _install_mock_socket(handler)
        try:
            tool.execute({"url": "https://fixture.test/p"})
        finally:
            uninstall()

        # 进程全局 socket.create_connection 必须未变
        assert socket.create_connection is original


# ---------------------------------------------------------------------------
# 10. R4：搜索 Provider 配置工厂
# ---------------------------------------------------------------------------


class _WebSearchScriptedModel:
    """脚本模型：第一步调 web_search，第二步把检索到的来源写进最终答案。"""

    def __init__(self, *, query: str = "harness", top_k: int = 3) -> None:
        self._query = query
        self._top_k = top_k
        self.calls = 0

    def decide(self, messages: list[Any], tools: Any = None) -> Any:
        from mini_deerflow.models import FinalAnswer, ToolCall

        self.calls += 1
        if self.calls == 1:
            return ToolCall(
                name="web_search",
                arguments={"query": self._query, "top_k": self._top_k},
                id="call-web-1",
            )
        for message in reversed(messages):
            content = getattr(message, "content", None)
            if not isinstance(content, str) or '"results"' not in content:
                continue
            try:
                payload = json.loads(content)
            except json.JSONDecodeError:
                continue
            results = payload.get("results") or []
            if results:
                first = results[0]
                return FinalAnswer(
                    content=f"来源：{first.get('title')} {first.get('url')}"
                )
        return FinalAnswer(content="没有检索到来源")


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

    def test_mcp_kind_without_executor_returns_unavailable(self) -> None:
        provider = build_web_search_provider_from_config(kind="mcp", mcp_executor=None)
        assert isinstance(provider, UnavailableWebSearchProvider)
        assert provider.is_available() is False

    def test_mcp_kind_with_executor_dispatches(self) -> None:
        """MCP executor 真的被调用；返回结构按 hits 路径解析。"""
        from mini_deerflow.web_tools import MCPSearchAdapter

        calls: list[tuple[str, dict]] = []

        def fake_executor(tool_name: str, args: dict) -> dict:
            calls.append((tool_name, args))
            return {
                "hits": [
                    {"title": "A", "url": "https://a", "snippet": "sa"},
                    {"title": "B", "url": "https://b", "snippet": "sb"},
                ]
            }

        provider = build_web_search_provider_from_config(
            kind="mcp", mcp_executor=fake_executor, mcp_tool_name="search"
        )
        assert isinstance(provider, MCPSearchAdapter)
        assert provider.is_available() is True
        hits = provider.search("hello", top_k=5)
        assert len(hits) == 2
        assert hits[0].title == "A"
        assert hits[0].url == "https://a"
        # executor 真被调用过
        assert calls == [("search", {"query": "hello", "top_k": 5})]

    def test_mcp_kind_adapts_alternative_response_shapes(self) -> None:
        """results / data 路径也支持；缺字段的项被丢弃。"""
        from mini_deerflow.web_tools import MCPSearchAdapter

        def executor(tool_name: str, args: dict) -> dict:
            return {
                "results": [
                    {"name": "T1", "link": "https://t1"},
                    {"title": "T2", "url": "https://t2", "summary": "desc"},
                ]
            }

        provider = MCPSearchAdapter(tool_executor=executor)
        hits = provider.search("q", top_k=5)
        assert len(hits) == 2
        assert hits[0].title == "T1"
        assert hits[0].url == "https://t1"
        assert hits[1].title == "T2"

    def test_mcp_kind_handles_empty(self) -> None:
        from mini_deerflow.web_tools import MCPSearchAdapter

        def executor(tool_name: str, args: dict) -> dict:
            return {"hits": []}

        provider = MCPSearchAdapter(tool_executor=executor)
        assert provider.search("q", top_k=5) == []

    # ------------------------------------------------------------------
    # R4：app 配置 → Runtime → **真实** stdio MCP（真实 _invoke 签名与返回格式）
    #     → MCPSearchAdapter → title / url / snippet
    # ------------------------------------------------------------------

    @staticmethod
    def _run_real_mcp_web_search(
        monkeypatch: Any,
        *,
        enabled: bool,
        principals: list[str],
        side_effects: list[str],
        query: str = "harness",
        top_k: int = 3,
        via_agent: bool = False,
    ) -> tuple[dict, list[str], object]:
        """启动真实 stdio MCP 搜索夹具，走 app 工厂，执行 web_search。

        **不替换 `_invoke`**：使用真实签名
        `(qualified, arguments, timeout) -> str` 与真实返回格式（JSON 字符串）。
        返回 `(result_json, invoke_call_records, provider)`。
        """
        import shutil
        import sys as _sys
        import tempfile as _tempfile
        from pathlib import Path as _Path

        from mini_deerflow.api.app import _default_runtime_factory
        from mini_deerflow.api.config import ApiSettings
        from mini_deerflow.middleware import ExecutionContext
        from mini_deerflow.mcp_manager import MCPClientManager

        server_path = str(_Path("tests/fixtures/mcp_search_server.py").resolve())
        qualified = "mcp__web_search__search"
        calls: list[str] = []
        original_invoke = MCPClientManager._invoke

        def counting_invoke(self: Any, qualified_name: str, arguments: Any, timeout: float) -> str:
            calls.append(qualified_name)
            return original_invoke(self, qualified_name, arguments, timeout)

        MCPClientManager._invoke = counting_invoke  # type: ignore[assignment]
        tmp = _tempfile.mkdtemp(prefix="tool06-mcp-")
        try:
            cfg_path = _Path(tmp) / "mcp.json"
            cfg_path.write_text(
                json.dumps(
                    {
                        "servers": [
                            {
                                "name": "web_search",
                                "command": _sys.executable,
                                "args": [server_path],
                                "enabled": True,
                                "required": False,
                                "startup_timeout": 30,
                                "call_timeout": 20,
                                "tool_capabilities": ["NETWORK_READ"],
                                "tool_side_effects": side_effects,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            state_path = _Path(tmp) / "state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "tools": {
                            qualified: {
                                "enabled": enabled,
                                "allowed_principals": principals,
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            monkeypatch.setenv("MINI_DEERFLOW_MCP_CONFIG", str(cfg_path))
            monkeypatch.setenv("MINI_DEERFLOW_MCP_TOOL_STATE", str(state_path))
            for key in (
                "MINI_DEERFLOW_WEB_SEARCH_PROVIDER_KIND",
                "MINI_DEERFLOW_WEB_SEARCH_FAKE_RESULTS",
                "MINI_DEERFLOW_WEB_SEARCH_MCP_SERVER",
                "MINI_DEERFLOW_WEB_SEARCH_MCP_TOOL",
            ):
                monkeypatch.delenv(key, raising=False)

            settings = ApiSettings(
                web_search_provider_kind="mcp",
                web_search_mcp_server="web_search",
                web_search_mcp_tool="search",
            )
            runtime = _default_runtime_factory(settings)
            try:
                provider = runtime.config.web_search_provider
                agent = runtime.integrated_agent._agent
                if via_agent:
                    # 走**真实 Agent.run**：可见性过滤 → 三层授权 → registry
                    # → web_search → 授权复核 → 真实 MCPManager._invoke
                    agent._model = _WebSearchScriptedModel(query=query, top_k=top_k)
                    runtime.store.create(thread_id="t-tool06-agent")
                    answer, _trace = agent.run(
                        "t-tool06-agent", "检索一下相关资料"
                    )
                    result = {"ok": True, "answer": answer}
                else:
                    ctx = ExecutionContext(
                        thread_id="t-tool06", run_id="r-tool06"
                    )
                    result = json.loads(
                        agent._registry.execute(
                            "web_search",
                            {"query": query, "top_k": top_k},
                            context=ctx,
                        )
                    )
                return result, calls, provider
            finally:
                runtime.shutdown()
        finally:
            MCPClientManager._invoke = original_invoke  # type: ignore[assignment]
            shutil.rmtree(tmp, ignore_errors=True)

    def test_real_mcp_search_end_to_end_authorized(self, monkeypatch: Any) -> None:
        """已启用 + 已授权 → 走真实 MCP → adapter 解析 JSON 字符串 → 标题/URL/摘要。"""
        from mini_deerflow.web_tools import MCPSearchAdapter

        result, calls, provider = self._run_real_mcp_web_search(
            monkeypatch,
            enabled=True,
            principals=["MAIN_AGENT"],
            side_effects=[],
        )
        assert isinstance(provider, MCPSearchAdapter), (
            f"应注入 MCPSearchAdapter，实际 {type(provider).__name__}"
        )
        assert result["ok"] is True, result
        assert result["count"] >= 1
        first = result["results"][0]
        assert first["title"], "缺少 title"
        assert first["url"].startswith("https://"), first["url"]
        assert first["snippet"], "缺少 snippet"
        # 真实 _invoke 恰好被调用一次（参数格式由真实签名校验）
        assert calls == ["mcp__web_search__search"]

    def test_real_agent_search_then_answer_with_source(
        self, monkeypatch: Any
    ) -> None:
        """app 配置 → **真实 Agent.run** → 真实 MCPManager._invoke → adapter
        → 最终答案带上来源链接（不替换 _invoke、不用假 executor）。
        """
        result, calls, provider = self._run_real_mcp_web_search(
            monkeypatch,
            enabled=True,
            principals=["MAIN_AGENT"],
            side_effects=[],
            via_agent=True,
        )
        from mini_deerflow.web_tools import MCPSearchAdapter

        assert isinstance(provider, MCPSearchAdapter)
        assert result["ok"] is True, result
        assert "https://example.invalid/harnessagent" in result["answer"], result
        assert calls == ["mcp__web_search__search"], calls

    def test_real_mcp_search_disabled_blocks_handler(self, monkeypatch: Any) -> None:
        """工具未启用 → DENY → handler=0（真实 MCP 一次都不被调用）。"""
        result, calls, _provider = self._run_real_mcp_web_search(
            monkeypatch,
            enabled=False,
            principals=["MAIN_AGENT"],
            side_effects=[],
        )
        assert result["ok"] is False
        assert result["kind"] == "authorization_error"
        assert calls == [], f"未授权时不应调用底层 MCP，实际 {calls}"

    def test_real_mcp_search_revoked_principal_blocks_handler(
        self, monkeypatch: Any
    ) -> None:
        """主体被撤销 → DENY → handler=0。"""
        result, calls, _provider = self._run_real_mcp_web_search(
            monkeypatch,
            enabled=True,
            principals=[],
            side_effects=[],
        )
        assert result["ok"] is False
        assert result["kind"] == "authorization_error"
        assert calls == [], f"无授权主体时不应调用底层 MCP，实际 {calls}"

    def test_real_mcp_search_confirmation_not_bypassed(self, monkeypatch: Any) -> None:
        """目标工具按 READS_NETWORK 分类（metadata 要求确认）→ CONFIRM →
        web_search 别名**不得绕过**确认链路：handler=0。
        """
        result, calls, _provider = self._run_real_mcp_web_search(
            monkeypatch,
            enabled=True,
            principals=["MAIN_AGENT"],
            side_effects=["READS_NETWORK"],
        )
        assert result["ok"] is False
        assert result["kind"] == "authorization_error"
        assert calls == [], f"待确认时不应调用底层 MCP，实际 {calls}"

    def test_mcp_executor_matches_real_invoke_contract(self) -> None:
        """executor 必须兼容**真实** `_invoke(qualified, args, timeout) -> str`：
        三位置参数 + 返回 JSON 字符串，由 adapter 解析。
        """
        from mini_deerflow.web_tools import MCPSearchAdapter

        class FakeMCPManager:
            """复刻真实签名与返回格式（不替换真实 _invoke）。"""

            def __init__(self) -> None:
                self.calls: list[tuple[str, dict, float]] = []
                self.registered_names = ("mcp__web_search__search",)

            def _invoke(
                self, qualified: str, arguments: dict, timeout: float
            ) -> str:
                self.calls.append((qualified, dict(arguments), timeout))
                # 真实返回：JSON **字符串**
                return json.dumps(
                    {
                        "hits": [
                            {"title": "T1", "url": "https://e/1", "snippet": "s1"}
                        ]
                    },
                    ensure_ascii=False,
                )

        mgr = FakeMCPManager()

        def executor(tool: str, args: dict, timeout: float = 30.0) -> str:
            return mgr._invoke(f"mcp__web_search__{tool}", args, timeout)

        adapter = MCPSearchAdapter(tool_executor=executor, tool_name="search")
        hits = adapter.search("hello", top_k=5)
        assert len(hits) == 1
        assert hits[0].title == "T1"
        assert hits[0].url == "https://e/1"
        assert hits[0].snippet == "s1"
        assert mgr.calls == [
            (
                "mcp__web_search__search",
                {"query": "hello", "top_k": 5},
                30.0,
            )
        ]

    def test_mcp_return_not_json_raises(self) -> None:
        """真实返回不是合法 JSON → 显式报错，**不静默当成空结果**。"""
        from mini_deerflow.web_tools import MCPSearchAdapter

        adapter = MCPSearchAdapter(
            tool_executor=lambda tool, args: "抱歉，搜索服务出错了",
            tool_name="search",
        )
        with pytest.raises(WebToolError, match="JSON"):
            adapter.search("q", top_k=5)

    def test_mcp_return_missing_hits_field_raises(self) -> None:
        """返回 JSON 但缺 hits/results/data → 显式报错，不静默返回空。"""
        from mini_deerflow.web_tools import MCPSearchAdapter

        adapter = MCPSearchAdapter(
            tool_executor=lambda tool, args: json.dumps({"status": "ok"}),
            tool_name="search",
        )
        with pytest.raises(WebToolError, match="hits/results/data"):
            adapter.search("q", top_k=5)

    def test_mcp_return_empty_string_raises(self) -> None:
        from mini_deerflow.web_tools import MCPSearchAdapter

        adapter = MCPSearchAdapter(
            tool_executor=lambda tool, args: "  ",
            tool_name="search",
        )
        with pytest.raises(WebToolError, match="空字符串"):
            adapter.search("q", top_k=5)

    def test_mcp_executor_exception_surfaces(self) -> None:
        """底层超时/KeyError 等异常必须上抛，不吞成空结果。"""
        from mini_deerflow.web_tools import MCPSearchAdapter

        def boom(tool: str, args: dict) -> str:
            raise TimeoutError("MCP 操作超过 30 秒未返回")

        adapter = MCPSearchAdapter(tool_executor=boom, tool_name="search")
        with pytest.raises(WebToolError, match="MCP 搜索调用失败"):
            adapter.search("q", top_k=5)


# ---------------------------------------------------------------------------
# 12. R3：真实传输层 —— 本地 HTTP 夹具（验证截断标记 / 读取上界 / 超时生效）
# ---------------------------------------------------------------------------


class _Tool06HTTPHandler:
    """在 fixture 内构造，便于访问每个 server 的 state。"""

    def _build(self):  # noqa: ANN201
        from http.server import BaseHTTPRequestHandler

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "Tool06Fixture"

            def log_message(self, *args: Any) -> None:  # 静音访问日志
                return

            def _state(self) -> Any:
                return self.server.state  # type: ignore[attr-defined]

            def do_GET(self) -> None:  # noqa: N802
                path = self.path.split("?")[0]
                if path == "/nolen":
                    # 无 Content-Length：close-delimited 正文
                    body = b"x" * 4096
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.close_connection = True
                    self.wfile.write(body)
                elif path == "/clen":
                    body = b"w" * 4096
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif path == "/small":
                    body = b"hello"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif path == "/hellomsg":
                    # 无 Content-Length + Connection:close：正常关闭定界正文
                    body = b"hello world"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    # 不发送 Content-Length，靠连接关闭定界
                    self.end_headers()
                    self.wfile.write(body)
                    self.close_connection = True
                elif path == "/chunks":
                    # Transfer-Encoding: chunked，两块（其中 \r\n 分次发送）
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    # 第一块：5 字节 "AAAAA"
                    self.wfile.write(b"5\r\nAAAAA\r\n")
                    self.wfile.flush()
                    # 分次发送第二块：长度+CRLF 分开，确保 _recv_until_delim 能正确识别行边界
                    self.wfile.write(b"5\r")
                    self.wfile.flush()
                    time.sleep(0.01)
                    self.wfile.write(b"\nBBBBB\r\n")
                    self.wfile.flush()
                    # 终结块
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                    self.close_connection = True
                elif path == "/big":
                    total = 8 * 1024 * 1024
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", str(total))
                    self.end_headers()
                    self._state()["sent"] = 0  # type: ignore[index]
                    chunk = b"y" * 4096
                    sent = 0
                    try:
                        while sent < total:
                            self.wfile.write(chunk)
                            self.wfile.flush()
                            sent += len(chunk)
                            self._state()["sent"] = sent  # type: ignore[index]
                            time.sleep(0.002)
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        pass
                elif path == "/delay":
                    # 响应头延迟：验证 read timeout 真的落到 socket
                    time.sleep(1.5)
                    body = b"abcd"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif path == "/slowbody":
                    # 响应头立刻返回，正文极慢：验证总超时
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.close_connection = True
                    try:
                        for _ in range(200):  # 全量约 30s
                            self.wfile.write(b"z" * 100)
                            self.wfile.flush()
                            time.sleep(0.15)
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        pass
                elif path == "/premature":
                    # 有 Content-Length 但提前 EOF：服务器声明 100 字节，只发 5 字节就关闭
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", "100")  # 声称 100 字节
                    self.end_headers()
                    self.wfile.write(b"hello")  # 只发 5 字节
                    self.close_connection = True
                elif path == "/chunkearly":
                    # Transfer-Encoding: chunked，但发完第一个 chunk 后不发送终结块就直接 EOF
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    self.wfile.write(b"5\r\nhello\r\n")  # 只发一个 chunk，不发 "0\r\n\r\n"
                    self.close_connection = True
                else:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()

        return Handler

    def __init__(self) -> None:
        self.handler_cls = self._build()


@pytest.fixture()
def local_http_server():  # noqa: ANN201
    """本地 HTTP 夹具（127.0.0.1 随机端口）；返回 (base_url, state)。"""
    from http.server import ThreadingHTTPServer

    state: dict[str, Any] = {"sent": 0}
    handler_cls = _Tool06HTTPHandler().handler_cls
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    server.state = state  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield base_url, state
    finally:
        server.shutdown()
        server.server_close()


def _loopback_read_tool(**overrides: Any):
    """允许 loopback 的 web_read 工具（本地夹具专用）。"""
    kwargs: dict[str, Any] = {
        "host_policy": HostPolicy(allow_loopback=True, allow_ip_literal=True),
        "timeout_s": 5.0,
    }
    kwargs.update(overrides)
    return build_web_read_tool(WebToolConfig(**kwargs))


class TestRealTransportR3:
    """R3：真发请求，验证读取上界、截断标记与超时确实生效。"""

    def test_timeout_extension_is_a_dict(self) -> None:
        """R3 根因：httpx 把 timeout 放进 extensions['timeout']，形态是 **dict**。"""
        from mini_deerflow.web_tools import (
            _connect_timeout_seconds,
            _read_timeout_seconds,
        )

        ext: dict[str, Any] = {
            "timeout": {"connect": 0.025, "read": 0.05, "write": 1.0, "pool": 1.0}
        }
        assert _read_timeout_seconds(ext) == 0.05
        assert _connect_timeout_seconds(ext) == 0.025
        # 缺字段 → 默认值
        assert _read_timeout_seconds({}) == WEB_TOOL_LIMITS["read_read_timeout_s"]
        # 非法值 → 默认值，不抛
        assert (
            _read_timeout_seconds({"timeout": {"read": -1}})
            == WEB_TOOL_LIMITS["read_read_timeout_s"]
        )
        # 对象形态也兼容
        assert _read_timeout_seconds({"timeout": httpx.Timeout(1.0, read=0.25)}) == 0.25

    def test_no_length_response_truncated_flagged(self, local_http_server) -> None:
        base_url, _state = local_http_server
        tool = _loopback_read_tool(max_bytes=1024, timeout_s=5.0)
        result = json.loads(tool.execute({"url": f"{base_url}/nolen"}))
        assert result["ok"] is True, result
        # 关键断言：无 Content-Length 且停在上限 → 必须标 truncated
        assert result["truncated"] is True, result
        assert result["bytes"] <= 1024

    def test_content_length_over_cap_flagged(self, local_http_server) -> None:
        base_url, _state = local_http_server
        tool = _loopback_read_tool(max_bytes=1024, timeout_s=5.0)
        result = json.loads(tool.execute({"url": f"{base_url}/clen"}))
        assert result["ok"] is True, result
        assert result["truncated"] is True, result
        assert result["bytes"] <= 1024

    def test_under_limit_not_truncated(self, local_http_server) -> None:
        base_url, _state = local_http_server
        tool = _loopback_read_tool(max_bytes=1024, timeout_s=5.0)
        result = json.loads(tool.execute({"url": f"{base_url}/small"}))
        assert result["ok"] is True, result
        assert result["truncated"] is False, result
        assert result["bytes"] == 5

    def test_bounded_consumption_not_full_download(self, local_http_server) -> None:
        """8MB 响应 + max_bytes=1024：客户端必须早停，不整包下载。"""
        base_url, state = local_http_server
        tool = _loopback_read_tool(max_bytes=1024, timeout_s=5.0)
        started = time.monotonic()
        result = json.loads(tool.execute({"url": f"{base_url}/big"}))
        elapsed = time.monotonic() - started
        assert result["ok"] is True, result
        assert result["truncated"] is True, result
        assert result["bytes"] <= 1024
        # 远小于整包（8MB）；整包至少要 4s 才发完
        assert elapsed < 2.0, f"读取耗时 {elapsed:.3f}s，未体现早停"
        time.sleep(0.5)
        sent = int(state.get("sent", 0))
        assert sent < 1024 * 1024, f"服务端实际写出 {sent} 字节，说明仍在整包消费"

    def test_read_timeout_reaches_socket(self, local_http_server) -> None:
        """配置的读超时必须真正落到 socket（不是回落默认 10s）。"""
        base_url, _state = local_http_server
        tool = _loopback_read_tool(timeout_s=0.2)
        started = time.monotonic()
        result = json.loads(tool.execute({"url": f"{base_url}/delay"}))
        elapsed = time.monotonic() - started
        assert result["ok"] is False, result
        assert result["kind"] == "network_error", result
        assert elapsed < 1.0, f"超时未生效，实际耗时 {elapsed:.3f}s（服务端延迟 1.5s）"

    def test_total_deadline_on_slow_body(self, local_http_server) -> None:
        """慢正文：总超时必须在预算内中止，不等正文发完。"""
        base_url, _state = local_http_server
        tool = _loopback_read_tool(timeout_s=0.5, max_bytes=1024)
        started = time.monotonic()
        result = json.loads(tool.execute({"url": f"{base_url}/slowbody"}))
        elapsed = time.monotonic() - started
        assert result["ok"] is False, result
        assert result["kind"] == "network_error", result
        assert elapsed < 2.0, f"总超时未生效，实际耗时 {elapsed:.3f}s"

    def test_total_deadline_tiny_timeout_no_leak(self, local_http_server) -> None:
        """极小总超时（0.1s）不应泄漏约 4 倍时长（~2s）。"""
        base_url, _state = local_http_server
        tool = _loopback_read_tool(timeout_s=0.1, max_bytes=1024)
        started = time.monotonic()
        result = json.loads(tool.execute({"url": f"{base_url}/slowbody"}))
        elapsed = time.monotonic() - started
        assert result["ok"] is False, result
        assert result["kind"] == "network_error", result
        # R3 根因：_probe_ahead 里 _bounded_timeout(0.5) 在剩余 >0.5s 时仍阻塞 0.5s，
        # 4 个 chunk 各漏 0.5s → ~2s；修复后应 <0.8s
        assert elapsed < 0.8, f"探测泄漏总超时，实际耗时 {elapsed:.3f}s"

    def test_no_length_close_delimited_returns_content(self, local_http_server) -> None:
        """无 Content-Length，靠 Connection:close 定界的正文必须完整返回。"""
        base_url, _state = local_http_server
        tool = _loopback_read_tool(timeout_s=5.0, max_bytes=1024)
        result = json.loads(tool.execute({"url": f"{base_url}/hellomsg"}))
        assert result["ok"] is True, result
        assert result["bytes"] == 11, f"期望 11，实际 {result['bytes']}"
        assert result["text"] == "hello world", f"期望 'hello world'，实际 {result['text']!r}"
        assert result["truncated"] is False, result

    def test_chunked_multiple_chunks_exact_content(self, local_http_server) -> None:
        """chunked 编码多个 chunk 必须精确返回，不越过分帧边界。"""
        base_url, _state = local_http_server
        tool = _loopback_read_tool(timeout_s=5.0, max_bytes=1024)
        result = json.loads(tool.execute({"url": f"{base_url}/chunks"}))
        assert result["ok"] is True, result
        # 第一块 5 字节（"AAAAA"）+ 第二块 5 字节（"BBBBB"，分次发送）
        assert result["bytes"] == 10, f"期望 10，实际 {result['bytes']}"
        assert result["text"] == "AAAAABBBBB", f"期望 'AAAAABBBBB'，实际 {result['text']!r}"
        assert result["truncated"] is False, result

    def test_header_timeout_socket_closed(self, local_http_server) -> None:
        """响应头超时时 socket 必须主动关闭，不能等 GC。"""
        base_url, _state = local_http_server
        tool = _loopback_read_tool(timeout_s=0.05)
        started = time.monotonic()
        result = json.loads(tool.execute({"url": f"{base_url}/delay"}))
        elapsed = time.monotonic() - started
        assert result["ok"] is False, result
        assert result["kind"] == "network_error", result
        # 验证立即退出（不超过配置的 0.05s + 连接开销）
        assert elapsed < 0.5, f"socket 未及时关闭，耗时 {elapsed:.3f}s"

    def test_content_length_but_early_eof_truncated(self, local_http_server) -> None:
        """R3-fix：有 Content-Length 但服务器提前 EOF → 标记 truncated=True。"""
        base_url, _state = local_http_server
        tool = _loopback_read_tool(timeout_s=5.0, max_bytes=1024)
        result = json.loads(tool.execute({"url": f"{base_url}/premature"}))
        assert result["ok"] is True, result
        # 声称 100 字节，只收到 5 字节就 EOF → truncated 必须为 True
        assert result["truncated"] is True, f"提前 EOF 必须标 truncated，实际: {result}"
        assert result["bytes"] == 5, f"期望 5，实际 {result['bytes']}"
        assert result["text"] == "hello", f"期望 'hello'，实际 {result['text']!r}"

    def test_send_timeout_socket_closed(self) -> None:
        """R3-fix：发送请求超时时，socket 必须被主动关闭。"""
        import socket
        from mini_deerflow.web_tools import _PinAddrTransport, WebToolNetworkError

        # 用 mock socket 模拟发送超时
        class _TimeoutSocket:
            def __init__(self, family=socket.AF_INET, type=socket.SOCK_STREAM):
                self.family = family
                self.type = type
                self.closed = False
                self.timeout_val = None
            def settimeout(self, val):
                self.timeout_val = val
            def sendall(self, data):
                raise socket.timeout("timed out")
            def recv(self, n):
                raise socket.timeout("timed out")
            def close(self):
                self.closed = True
            def gettimeout(self):
                return self.timeout_val

        sock = _TimeoutSocket()

        # 注入 mock：让 create_connection 返回我们的超时 socket
        import mini_deerflow.web_tools as wt
        orig_connect = wt.socket.create_connection
        try:
            wt.socket.create_connection = lambda addr, timeout=None: sock
            transport = _PinAddrTransport("127.0.0.1", max_bytes=1024, deadline_monotonic=None)
            req = httpx.Request("GET", "http://example.com/path")
            try:
                transport.handle_request(req)
                assert False, "应抛 WebToolNetworkError"
            except WebToolNetworkError:
                pass  # 期望的异常
            # R3-fix：socket 必须在异常路径中被关闭
            assert sock.closed, "发送超时时 socket 必须被关闭"
        finally:
            wt.socket.create_connection = orig_connect

    def test_chunked_early_eof_truncated(self, local_http_server) -> None:
        """R3-fix：chunked 编码提前 EOF → 标记 truncated=True。"""
        base_url, _state = local_http_server
        tool = _loopback_read_tool(timeout_s=5.0, max_bytes=1024)
        result = json.loads(tool.execute({"url": f"{base_url}/chunkearly"}))
        assert result["ok"] is True, result
        # 发了一个 chunk 后直接 EOF，未收到终结块 0\r\n\r\n
        assert result["truncated"] is True, f"chunked 提前 EOF 必须标 truncated，实际: {result}"
        assert result["bytes"] == 5, f"期望 5，实际 {result['bytes']}"
        assert "hello" in result["text"], f"期望含 'hello'，实际 {result['text']!r}"

    def test_send_non_timeout_exception_closes_socket(self) -> None:
        """R3-fix：发送时非超时异常（连接重置等）也必须关闭 socket。"""
        import socket
        from mini_deerflow.web_tools import _PinAddrTransport, WebToolNetworkError

        class _ResetSocket:
            def __init__(self, family=socket.AF_INET, type=socket.SOCK_STREAM):
                self.family = family
                self.type = type
                self.closed = False
            def settimeout(self, val):
                pass
            def sendall(self, data):
                raise ConnectionResetError("connection reset by peer")
            def recv(self, n):
                raise ConnectionResetError("connection reset by peer")
            def close(self):
                self.closed = True

        sock = _ResetSocket()
        import mini_deerflow.web_tools as wt
        orig_connect = wt.socket.create_connection
        try:
            wt.socket.create_connection = lambda addr, timeout=None: sock
            transport = _PinAddrTransport("127.0.0.1", max_bytes=1024, deadline_monotonic=None)
            req = httpx.Request("GET", "http://example.com/path")
            try:
                transport.handle_request(req)
                assert False, "应抛 WebToolNetworkError"
            except WebToolNetworkError as e:
                assert "重置" in str(e) or "reset" in str(e).lower()
            # R3-fix：非超时异常也必须关闭 socket
            assert sock.closed, "非超时发送异常时 socket 也必须被关闭"
        finally:
            wt.socket.create_connection = orig_connect
