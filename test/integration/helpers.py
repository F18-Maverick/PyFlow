"""Shared helpers for the integration tests."""

import collections
import socket
import time


def wait_until(predicate, timeout=5.0, interval=0.05):
    """Poll predicate until it returns True or timeout elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def server_ready(server, timeout=5.0):
    """Wait until a TCP server is running."""
    return wait_until(lambda: server.running, timeout=timeout)


_recent_ports = collections.deque(maxlen=16)  # ports handed out by free_port


def free_port():
    """Return a loopback port the OS is willing to bind.

    Asking the OS keeps the tests out of the ranges Windows reserves for
    Hyper-V/WinNAT, where binding fails with ``WSAEACCES`` (WinError 10013)
    instead of taking the port. The port is released again on return, so it is
    only guaranteed free until the test binds it; ports handed out recently are
    skipped, so two endpoints built in the same test never share one.
    """
    for _ in range(64):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        if port not in _recent_ports:
            _recent_ports.append(port)
            return port
    raise RuntimeError("the OS kept handing out recently used ports")
