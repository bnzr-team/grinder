"""Fixture network airgap guard (PR-NETLOCK-1).

When activated, patches three socket entry points to block all non-loopback
connections:
- socket.socket.connect
- socket.socket.connect_ex
- socket.create_connection

Catches httpx, websockets, aiohttp, raw sockets — any library that uses
Python's socket module.

Allowlist: any address where ipaddress.ip_address(host).is_loopback is True,
plus literal "localhost". This permits the metrics HTTP server and Redis HA.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from typing import Any

logger = logging.getLogger(__name__)

_LOCALHOST_LITERALS = frozenset({"127.0.0.1", "::1", "localhost"})

_original_connect: Any = None
_original_connect_ex: Any = None
_original_create_connection: Any = None


class FixtureNetworkBlockedError(RuntimeError):
    """Raised when fixture mode blocks an outbound network connection."""


def _is_localhost(address: Any) -> bool:
    """Check if address tuple targets a loopback address.

    Returns True for:
    - Literal "localhost", "127.0.0.1", "::1"
    - Any IP where ipaddress.ip_address(host).is_loopback is True
      (covers full 127.0.0.0/8 and ::1)
    """
    if not isinstance(address, tuple) or len(address) < 2:
        return False
    host = str(address[0])
    if host in _LOCALHOST_LITERALS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _make_error_msg(address: Any) -> str:
    host = address[0] if isinstance(address, tuple) else address
    return (
        f"FIXTURE NETWORK BLOCKED: connection to {host} denied. "
        "Fixture mode blocks all external network by design. "
        "To use live network, run without --fixture."
    )


def _guarded_connect(self: socket.socket, address: Any) -> Any:
    """Replacement for socket.socket.connect — blocks non-loopback."""
    if _is_localhost(address):
        return _original_connect(self, address)
    msg = _make_error_msg(address)
    logger.warning(msg)
    raise FixtureNetworkBlockedError(msg)


def _guarded_connect_ex(self: socket.socket, address: Any) -> Any:
    """Replacement for socket.socket.connect_ex — blocks non-loopback.

    Raises FixtureNetworkBlockedError instead of returning errno,
    for fail-fast consistency with connect().
    """
    if _is_localhost(address):
        return _original_connect_ex(self, address)
    msg = _make_error_msg(address)
    logger.warning(msg)
    raise FixtureNetworkBlockedError(msg)


def _guarded_create_connection(
    address: Any, timeout: Any = ..., source_address: Any = None, **kwargs: Any
) -> socket.socket:
    """Replacement for socket.create_connection — blocks non-loopback.

    Checks the target address before delegating to original create_connection.
    """
    if isinstance(address, tuple) and len(address) >= 2 and not _is_localhost(address):
        msg = _make_error_msg(address)
        logger.warning(msg)
        raise FixtureNetworkBlockedError(msg)
    if timeout is ...:
        result: socket.socket = _original_create_connection(
            address, source_address=source_address, **kwargs
        )
        return result
    result2: socket.socket = _original_create_connection(
        address, timeout=timeout, source_address=source_address, **kwargs
    )
    return result2


def install_fixture_network_guard() -> None:
    """Activate network airgap — block all non-loopback socket connections.

    Patches socket.socket.connect, socket.socket.connect_ex, and
    socket.create_connection. Idempotent: calling twice is a no-op.
    """
    global _original_connect, _original_connect_ex, _original_create_connection  # noqa: PLW0603
    if _original_connect is not None:
        return  # Already installed
    _original_connect = socket.socket.connect
    _original_connect_ex = socket.socket.connect_ex
    _original_create_connection = socket.create_connection
    socket.socket.connect = _guarded_connect  # type: ignore[assignment]
    socket.socket.connect_ex = _guarded_connect_ex  # type: ignore[assignment]
    socket.create_connection = _guarded_create_connection
    logger.info("FIXTURE NETWORK GUARD: activated — external connections blocked")


def uninstall_fixture_network_guard() -> None:
    """Deactivate network airgap — restore original socket methods.

    Idempotent: calling when not installed is a no-op.
    """
    global _original_connect, _original_connect_ex, _original_create_connection  # noqa: PLW0603
    if _original_connect is None:
        return
    socket.socket.connect = _original_connect  # type: ignore[method-assign]
    socket.socket.connect_ex = _original_connect_ex  # type: ignore[method-assign]
    socket.create_connection = _original_create_connection
    _original_connect = None
    _original_connect_ex = None
    _original_create_connection = None
    logger.info("FIXTURE NETWORK GUARD: deactivated — connections restored")
