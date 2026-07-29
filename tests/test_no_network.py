"""The test suite must never touch the network.

"No real network calls in tests, ever" is a rule that decays into a convention
unless something enforces it. pytest-socket enforces it via ``--disable-socket``
in pyproject.toml; these tests prove the enforcement is actually switched on, so
a future change to addopts fails here rather than silently letting live calls
back into the suite.
"""

from __future__ import annotations

import socket

import pytest
from pytest_socket import SocketBlockedError


def test_creating_an_inet_socket_is_blocked() -> None:
    with pytest.raises(SocketBlockedError):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM)


def test_outbound_tcp_connection_is_blocked() -> None:
    with pytest.raises(SocketBlockedError):
        socket.create_connection(("example.com", 443), timeout=1)


def test_dns_resolution_is_blocked() -> None:
    with pytest.raises(SocketBlockedError):
        socket.gethostbyname("example.com")


def test_unix_sockets_remain_allowed() -> None:
    """asyncio's event loop needs a self-pipe, hence --allow-unix-socket.

    If this ever starts failing, the async tests will hang rather than fail
    clearly, so it is worth asserting explicitly.
    """
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.close()
