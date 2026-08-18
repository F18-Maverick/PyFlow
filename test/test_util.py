"""Shared helpers for tests."""

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
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server.running:
            return True
        time.sleep(0.05)
    return False
