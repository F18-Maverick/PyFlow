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

def read_text_or_none(path):
    """Return a file's text, or None while it is missing or unreadable.

    A receiving leg opens its target as soon as the transfer starts (truncated
    for a fresh transfer), so ``exists()`` turning true says nothing about the
    bytes having arrived. Polling the content must tolerate the moment between
    the open and the first chunk.
    """
    try:
        return path.read_text()
    except OSError:
        return None


def wait_for_text(path, expected, timeout=10.0):
    """Poll a file until its text equals ``expected``.

    Waiting for the content, not for ``exists()``, is what makes a receive
    assertion race-free: the receiver creates (and truncates) the target when
    the leg starts, before any byte is written.

    Args:
        path (Path): File the transfer writes.
        expected (str): Text the finished file must hold.
        timeout (float): Seconds to wait.

    Returns:
        bool: True once the content matched.
    """
    return wait_until(lambda: read_text_or_none(path) == expected, timeout=timeout)


def isolate_instance_files(instance, tmp_path, name):
    """Point one instance's logs and transfer progress file at a temp directory.

    Every instance defaults to the same files under the protocol's project
    directory: concurrent instances would race on them, and a fresh client
    would read (and resume) the interrupted transfers another test left
    behind.
    """
    instance.messages_log_file = str(tmp_path / f"{name}_messages_log.json")
    instance.events_log_file = str(tmp_path / f"{name}_events_log.json")
    instance.file_trans_progress_file = str(tmp_path / f"{name}_file_trans_progress.json")
