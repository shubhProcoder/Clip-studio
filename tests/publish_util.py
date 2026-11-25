"""Shared bits of the publish tests: a network guard (only 127.0.0.1 may be reached) and a temp config dir."""

import ipaddress
import socket

import pytest

from streamclipper import paths
from streamclipper.publish import http

_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_getaddrinfo = socket.getaddrinfo
LOOPBACK_NAMES = ("127.0.0.1", "localhost", "::1")


def _loopback(host) -> bool:
    if host in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _check(address):
    host = address[0] if isinstance(address, tuple) else address
    if not _loopback(host):
        raise AssertionError(f"network access in a publish test: {address!r}")


def guard(monkeypatch, tmp_path):
    """No socket may leave this PC, the real transport may not be built, tokens go to tmp_path/config."""

    def connect(self, address):
        _check(address)
        return _real_connect(self, address)

    def connect_ex(self, address):
        _check(address)
        return _real_connect_ex(self, address)

    def getaddrinfo(host, *a, **k):
        _check((host,))
        return _real_getaddrinfo(host, *a, **k)

    def no_real_transport(*_a, **_k):
        raise AssertionError("UrllibTransport built in a test")

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(http.UrllibTransport, "__init__", no_real_transport)
    cfg = tmp_path / "config"
    monkeypatch.setattr(paths, "CONFIG_DIR", cfg)
    return cfg


@pytest.fixture
def no_net(monkeypatch, tmp_path):
    return guard(monkeypatch, tmp_path)
