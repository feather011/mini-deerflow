"""TEST-01：测试配置生产者与 Python 子进程隔离，不触达真实网络。"""
import os
import subprocess
import sys
from pathlib import Path


def test_harness_fixture_drops_parent_credentials(tmp_path, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("integration", Path(__file__).with_name("test_harness_runner_integration.py"))
    integration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(integration)
    from mini_deerflow.harness_runner import HarnessRunner

    monkeypatch.setenv("MINIMAX_API_KEY", "TEST_POISON_PARENT_KEY")
    monkeypatch.setenv("MINIMAX_OPENAI_BASE_URL", "https://provider.invalid/v1")
    monkeypatch.setenv("HARNESS_MIMO_BASE_URL", "https://fallback.invalid/v1")
    monkeypatch.setenv("HARNESS_USE_FAKE_LLM", "1")
    monkeypatch.setenv("HARNESS_ENV_FILE", str(tmp_path / "developer.env"))
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8080")
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    # 只观察真正发往子进程的 build_env；此探针不启动网络/进程。
    from types import SimpleNamespace
    monkeypatch.setattr(HarnessRunner, "run", lambda self, **kw: SimpleNamespace(status="not-run", env=self.build_env(kw.get("extra_env"))))
    repo = tmp_path / "repo"
    repo.mkdir()
    env = integration._run_harness(repo, "http://127.0.0.1:12345", {}).env
    assert not any(v == "TEST_POISON_PARENT_KEY" for v in env.values())
    assert env["HARNESS_MIMO_BASE_URL"] == "http://127.0.0.1:12345"
    assert env.get("HARNESS_USE_FAKE_LLM") != "1"
    assert not env.get("HTTPS_PROXY")
    assert Path(env["HARNESS_ENV_FILE"]).is_file()


def test_python_child_guard_intercepts_before_socket(tmp_path):
    # 用假的底层 connect，哪怕保护缺失也不发包。先替换再重新执行启动保护。
    guard = Path(__file__).parent / "_offline_guard" / "sitecustomize.py"
    code = '''import socket, runpy, sys
socket.socket.connect = lambda *args: print("UNDERLYING_CONNECT_REACHED")
if __import__('pathlib').Path(sys.argv[1]).exists():
    runpy.run_path(sys.argv[1])
try:
    socket.socket().connect(("203.0.113.1", 443))
except Exception as exc:
    print(type(exc).__name__, str(exc))
'''
    child = subprocess.run([sys.executable, "-c", code, str(guard)], capture_output=True, text=True, timeout=10)
    assert child.returncode == 0, child.stderr
    assert "TEST_NETWORK_BLOCKED" in child.stdout
    assert "UNDERLYING_CONNECT_REACHED" not in child.stdout



def test_guard_blocks_dns_and_ipv6_without_touching_underlying(tmp_path, monkeypatch):
    import runpy
    import socket
    import pytest
    touched = []
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: touched.append("dns"))
    monkeypatch.setattr(socket.socket, "connect_ex", lambda *a, **kw: touched.append("connect"))
    runpy.run_path(str(Path(__file__).parent / "_offline_guard" / "sitecustomize.py"))
    with pytest.raises(RuntimeError, match="TEST_NETWORK_BLOCKED"):
        socket.getaddrinfo("provider.invalid", 443)
    with pytest.raises(RuntimeError, match="TEST_NETWORK_BLOCKED"):
        socket.socket(socket.AF_INET6).connect_ex(("2001:db8::1", 443))
    assert touched == []


def test_child_starts_with_guard_and_allows_loopback():
    code = """import socket, sys
assert 'sitecustomize' in sys.modules
assert hasattr(sys.modules['sitecustomize'], 'install')
assert socket.getaddrinfo('localhost', 80)
try:
    socket.getaddrinfo('provider.invalid', 443)
except RuntimeError as exc:
    assert 'TEST_NETWORK_BLOCKED' in str(exc)
else:
    raise AssertionError('external DNS was not blocked')
print('guard-loaded-before-script')
"""
    child = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=10)
    assert child.returncode == 0, child.stderr
    assert child.stdout.strip() == "guard-loaded-before-script"


import pytest


@pytest.mark.parametrize("resolver,address", [
    ("gethostbyname", "provider.invalid"),
    ("gethostbyname_ex", "provider.invalid"),
    ("gethostbyaddr", "203.0.113.1"),
    ("getnameinfo", ("203.0.113.1", 443)),
])
def test_legacy_resolvers_block_before_underlying(monkeypatch, resolver, address):
    import runpy
    import socket
    touched = []
    monkeypatch.setattr(socket, resolver, lambda *a: touched.append("resolver-reached"))
    runpy.run_path(str(Path(__file__).parent / "_offline_guard" / "sitecustomize.py"))
    with pytest.raises(RuntimeError, match="TEST_NETWORK_BLOCKED"):
        if resolver == "getnameinfo":
            socket.getnameinfo(address, 0)
        else:
            getattr(socket, resolver)(address)
    assert touched == []


def test_child_legacy_resolvers_block_before_underlying():
    guard = Path(__file__).parent / "_offline_guard" / "sitecustomize.py"
    code = '''import socket, runpy, sys
seen = []
for name in ('gethostbyname', 'gethostbyname_ex', 'gethostbyaddr'):
    setattr(socket, name, lambda *a: seen.append('underlying-reached'))
runpy.run_path(sys.argv[1])
for name, address in [('gethostbyname', 'provider.invalid'), ('gethostbyname_ex', 'provider.invalid'), ('gethostbyaddr', '203.0.113.1')]:
    try:
        getattr(socket, name)(address)
    except RuntimeError as exc:
        assert 'TEST_NETWORK_BLOCKED' in str(exc)
    else:
        raise AssertionError('resolver was not blocked')
assert not seen
'''
    child = subprocess.run([sys.executable, "-c", code, str(guard)], capture_output=True, text=True, timeout=10)
    assert child.returncode == 0, child.stderr


def test_manual_mock_is_closed_when_runner_raises(tmp_path, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("cleanup_probe", Path(__file__).with_name("test_harness_runner_integration.py"))
    integration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(integration)
    servers = []
    original = integration._MockServer

    def tracked_server():
        server = original()
        servers.append(server)
        return server

    def fail(*a, **kw):
        raise RuntimeError("controlled runner failure")

    monkeypatch.setattr(integration, "_MockServer", tracked_server)
    monkeypatch.setattr(integration, "_run_harness", fail)
    try:
        with pytest.raises(RuntimeError, match="controlled runner failure"):
            integration.test_正常调用_计数与seq跟随()
        assert all(s._server is None and s._thread is None for s in servers)
    finally:
        for server in servers:
            server.stop()
