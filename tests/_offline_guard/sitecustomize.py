"""Python 测试子进程的启动保护；仅显式测试环境启用，不属于 OS 沙箱。"""
import ipaddress
import os
import socket


def _check_host(host):
    if host is None:  # getaddrinfo 的本地绑定用途
        return
    if isinstance(host, bytes):
        host = host.decode("ascii", errors="replace")
    if host == "localhost":
        return
    try:
        if ipaddress.ip_address(str(host).split("%", 1)[0]).is_loopback:
            return
    except ValueError:
        pass
    # 不把实际主机名或凭据打印到报错里。
    raise RuntimeError("TEST_NETWORK_BLOCKED: only loopback network is allowed")


def _check_address(address):
    if isinstance(address, tuple):
        _check_host(address[0])
    # 非 tuple 是 AF_UNIX 的本地路径，不经 IP 网络。


def install():
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_getaddrinfo = socket.getaddrinfo
    original_sendto = socket.socket.sendto
    original_gethostbyname = socket.gethostbyname
    original_gethostbyname_ex = socket.gethostbyname_ex
    original_gethostbyaddr = socket.gethostbyaddr
    original_getnameinfo = socket.getnameinfo

    def connect(sock, address):
        _check_address(address)
        return original_connect(sock, address)

    def connect_ex(sock, address):
        _check_address(address)
        return original_connect_ex(sock, address)

    def getaddrinfo(host, *args, **kwargs):
        _check_host(host)
        return original_getaddrinfo(host, *args, **kwargs)

    def sendto(sock, data, *args):
        _check_address(args[-1])
        return original_sendto(sock, data, *args)

    def gethostbyname(host):
        _check_host(host)
        return original_gethostbyname(host)

    def gethostbyname_ex(host):
        _check_host(host)
        return original_gethostbyname_ex(host)

    def gethostbyaddr(host):
        _check_host(host)
        return original_gethostbyaddr(host)

    def getnameinfo(address, flags):
        _check_address(address)
        return original_getnameinfo(address, flags)

    socket.getnameinfo = getnameinfo
    socket.gethostbyname = gethostbyname
    socket.gethostbyname_ex = gethostbyname_ex
    socket.gethostbyaddr = gethostbyaddr
    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.getaddrinfo = getaddrinfo
    socket.socket.sendto = sendto


if os.environ.get("MINI_DEERFLOW_TEST_OFFLINE") == "1":
    install()
