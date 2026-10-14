

import errno

from PyFlow.network_api.connect_tcp import (
    CommandNotFoundError,
    TCP_Server_Base,
    _is_closed_socket_error,
)


def test_is_closed_socket_error_covers_teardown_signals():
    """Peer-drop and shutdown errors are teardown signals, not faults:
    they must not produce tracebacks in the receive/send loops."""
    assert _is_closed_socket_error(ConnectionResetError(errno.ECONNRESET, "reset"))
    assert _is_closed_socket_error(BrokenPipeError(errno.EPIPE, "pipe"))
    assert _is_closed_socket_error(OSError(errno.EBADF, "bad fd"))
    assert _is_closed_socket_error(OSError(errno.ENOTSOCK, "not a socket"))
    assert _is_closed_socket_error(OSError(10038, "winsock"))  # WSAENOTSOCK
    assert _is_closed_socket_error(RuntimeError("connection error"))
    assert not _is_closed_socket_error(OSError(errno.ETIMEDOUT, "timeout"))  # real fault
    assert not _is_closed_socket_error(ValueError("nope"))


def test_tcp_server_init():
    server = TCP_Server_Base(host="127.0.0.1", port=65002, is_extend_command=True)
    assert server.host == "127.0.0.1"
    assert server.port == 65002  # noqa: PLR2004
    assert callable(server.start_TCP_Server)
    assert server.is_enable_encrypto is True
    assert server.recv_messages_chunk_len == 4096  # noqa: PLR2004
    assert server.file_transfer_chunk_len == 65536  # noqa: PLR2004
    assert server.is_custom_keys is None


# ---- manual port allocation and the /quit handshake -------------------------

import ast  # noqa: E402
import os  # noqa: E402
import socket  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

import pytest  # noqa: E402

from PyFlow.network_api import connect_tcp as _connect_tcp  # noqa: E402


def wait_until(predicate, timeout=5.0, interval=0.05):
    """Poll ``predicate`` until it returns True or the timeout elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False

MANUAL_PORT = 64111


def test_manual_port_mode_reserves_a_range_and_records_it_as_live():
    """Build the range record for a manual-mode server without crashing.

    The record reports ``is_running``, and an entry marked as not running is
    swept away immediately (which used to leave an empty log behind).
    """
    # The allocator waits on the lock file until it disappears, and a crashed
    # manual-mode instance leaves that file behind (see the port-allocation
    # docs): clear it so the test cannot hang on someone else's leftovers.
    _clear_temp_info("server_port_lock.lock")
    server = TCP_Server_Base(
        host="127.0.0.1",
        port=MANUAL_PORT,
        is_extend_command=True,
        is_hand_alloc_port=True,
    )
    try:
        assert server.port_range_num == 100  # noqa: PLR2004
        assert server.min_port == server.port - server.port_add_step * server.port_range_num
        assert server.max_port == server.port + 1 + server.port_add_step * server.port_range_num
        with open(server.port_temp_info_path, "r", encoding="utf-8") as f:
            record = ast.literal_eval(f.read())
        own = [entry for entry in record if entry["server_id"] == server.server_num]
        assert own, "the instance dropped its own range record"
        assert own[0]["is_running"] is True
        assert own[0]["max_port"] == server.max_port
    finally:
        server.free_port()
    if os.path.exists(server.port_temp_info_path):
        with open(server.port_temp_info_path, "r", encoding="utf-8") as f:
            record = ast.literal_eval(f.read())
        assert all(entry["server_id"] != server.server_num for entry in record)


def test_palloc_does_not_wait_when_allocation_is_disabled(monkeypatch):
    """Return ``0`` immediately in automatic mode.

    ``palloc`` used to sleep 0.1 s after every attempt, so even the automatic
    "the OS picks the port" answer paid the retry wait.
    """
    server = TCP_Server_Base(host="127.0.0.1", port=MANUAL_PORT + 2, is_extend_command=True)
    sleeps = []
    monkeypatch.setattr(_connect_tcp.time, "sleep", sleeps.append)
    assert server.palloc() == 0
    assert sleeps == []


def test_palloc_returns_without_waiting_when_a_port_is_free(monkeypatch):
    """Return a free port without paying the exhausted-range wait.

    The 0.1 s sleep belongs to the retry loop only; a successful allocation
    used to sleep on every call.
    """
    server = TCP_Server_Base(
        host="127.0.0.1",
        port=MANUAL_PORT + 1,
        is_extend_command=True,
        is_hand_alloc_port=True,
    )
    sleeps = []
    monkeypatch.setattr(_connect_tcp.time, "sleep", sleeps.append)
    try:
        port = server.palloc()
        assert port == server.port + 1 + server.port_add_step
        assert sleeps == []
    finally:
        server.free_port()


def test_quit_command_replies_and_closes_the_connection():
    """Answer ``/quit`` and end the session.

    The peer must see EOF instead of staying connected to a server that only
    answered.
    """
    server = TCP_Server_Base(host="127.0.0.1", port=MANUAL_PORT + 2, is_extend_command=True)
    server.running = True
    server_sock, peer_sock = socket.socketpair()
    try:
        assert server.handle_command(server_sock, ("127.0.0.1", 1), "/quit") is None
        assert peer_sock.recv(1024) == b"Bye!\n"
        assert peer_sock.recv(1024) == b""
    finally:
        server_sock.close()
        peer_sock.close()


def test_stop_before_start_keeps_the_server_stopped():
    """A ``stop()`` that arrives before ``start_TCP_Server()`` must stick.

    The start used to set ``running`` back to True after binding, reviving a
    server the caller had already stopped (and leaving its listener up).
    """
    port = MANUAL_PORT + 40
    server = TCP_Server_Base(host="127.0.0.1", port=port, is_extend_command=True)
    server.stop()
    threading.Thread(target=server.start_TCP_Server, daemon=True).start()
    assert not wait_until(lambda: server.running, timeout=2), "the stopped server started anyway"
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=2)


def test_free_port_keeps_other_live_instances():
    """Stopping one manual-mode server must leave the other records alone.

    The record was rewritten by deleting the own entry by index while iterating
    over the list, which ran past its end as soon as that entry was not the
    last one; with every start sweeping everything, at most one entry survived,
    so it could not happen before.
    """
    first = TCP_Server_Base(
        host="127.0.0.1", port=MANUAL_PORT + 50, is_extend_command=True, is_hand_alloc_port=True
    )
    second = TCP_Server_Base(
        host="127.0.0.1", port=MANUAL_PORT + 60, is_extend_command=True, is_hand_alloc_port=True
    )
    try:
        with open(first.port_temp_info_path, encoding="utf-8") as f:
            entries = ast.literal_eval(f.read())
        assert {first.server_num, second.server_num} <= {e["server_id"] for e in entries}
        # every live holder records its own process, not just the first one
        assert {e["pid"] for e in entries} == {os.getpid()}

        first.free_port()  # the first entry, not the last one

        with open(second.port_temp_info_path, encoding="utf-8") as f:
            left = ast.literal_eval(f.read())
        assert [entry["server_id"] for entry in left] == [second.server_num]
    finally:
        second.free_port()
        first.free_port()

def _temp_info_path(name):
    return os.path.join(
        os.path.dirname(_connect_tcp.__file__), ".Flow", "temp_info", name
    )


def _clear_temp_info(*names):
    """Remove leftover lock/record files of an interrupted manual-mode run."""
    for name in names:
        path = _temp_info_path(name)
        if os.path.exists(path):
            os.remove(path)


def test_port_record_liveness_helpers():
    """A record entry is reclaimable only once its owning process is gone."""
    assert _connect_tcp._process_is_alive(os.getpid()) is True
    assert _connect_tcp._process_is_alive(None) is True  # record written before pids
    assert _connect_tcp._process_is_alive(2**30) is False  # far beyond any pid_max

    assert _connect_tcp._port_record_stale({"is_running": False}) is True  # legacy rule
    assert _connect_tcp._port_record_stale({"is_running": True}) is False
    assert _connect_tcp._port_record_stale({"pid": os.getpid()}) is False
    assert _connect_tcp._port_record_stale({"pid": 2**30}) is True


def test_dead_holders_are_reclaimed_from_the_port_record():
    """Reclaim the range of a killed instance on the next allocation.

    A killed instance never reaches ``hand_free_port``, so the recorded pid is
    what lets the next instance drop its entry instead of keeping it forever.
    """
    _clear_temp_info("server_port_info.log", "server_port_lock.lock")
    record = _temp_info_path("server_port_info.log")
    with open(record, "w", encoding="utf-8") as f:
        f.write(
            str(
                [
                    {
                        "server_id": 7,
                        "host": "127.0.0.1",
                        "port": 64200,
                        "min_port": 64100,
                        "max_port": 64301,
                        "is_running": True,
                        "pid": 2**30,  # no such process
                        "started_at": time.time(),
                    }
                ]
            )
        )
    server = TCP_Server_Base(
        host="127.0.0.1",
        port=MANUAL_PORT + 70,
        is_extend_command=True,
        is_hand_alloc_port=True,
    )
    try:
        with open(server.port_temp_info_path, encoding="utf-8") as f:
            entries = ast.literal_eval(f.read())
        assert [entry["server_id"] for entry in entries] == [server.server_num]
        assert entries[0]["pid"] == os.getpid()
        assert entries[0]["started_at"] > 0
    finally:
        server.free_port()


# ---- call_extension_functions ----------------------------------------------


def test_call_extension_functions_runs_a_console_command_from_code():
    """A command registered for the console runs from code, line unchanged."""
    server = TCP_Server_Base(host="127.0.0.1", port=65002, is_extend_command=True)
    seen = []
    server.register_command(
        "/mycmd", lambda sock, addr, cmd: seen.append((sock, addr, cmd)) or "done", "client"
    )

    assert server.call_extension_functions("/mycmd hello world") == "done"
    # a direct call names no peer, so the handler gets the None context
    assert seen == [(None, None, "/mycmd hello world")]


def test_call_extension_functions_finds_the_inbound_group_case_insensitively():
    """A handler for the lines clients send is reachable by its name too."""
    server = TCP_Server_Base(host="127.0.0.1", port=65002, is_extend_command=True)
    seen = []
    server.register_command("/inbound", lambda sock, addr, cmd: seen.append(cmd), "server")

    server.call_extension_functions("/INBOUND x y")

    assert seen == ["/INBOUND x y"]


def test_call_extension_functions_prefers_the_console_group():
    """The console group wins a name both groups register."""
    server = TCP_Server_Base(host="127.0.0.1", port=65002, is_extend_command=True)
    server.register_command("/dup", lambda sock, addr, cmd: "console", "client")
    server.register_command("/dup", lambda sock, addr, cmd: "inbound", "server")

    assert server.call_extension_functions("/dup") == "console"


def test_call_extension_functions_runs_synchronously():
    """``run_in_thread`` belongs to the dispatch paths, not to a direct call."""
    server = TCP_Server_Base(host="127.0.0.1", port=65002, is_extend_command=True)
    seen = []
    server.register_command(
        "/slow", lambda sock, addr, cmd: seen.append(cmd), "client", run_in_thread=True
    )

    server.call_extension_functions("/slow now")

    assert seen == ["/slow now"]  # finished before the call returned


def test_call_extension_functions_rejects_unregistered_commands():
    """An unknown first token raises, and the message names the line."""
    server = TCP_Server_Base(host="127.0.0.1", port=65002, is_extend_command=True)
    server.register_command("/known", lambda sock, addr, cmd: None, "client")

    with pytest.raises(CommandNotFoundError) as exc:
        server.call_extension_functions("/ghost arg")
    assert "/ghost arg" in str(exc.value)
    with pytest.raises(CommandNotFoundError):  # an empty line selects nothing
        server.call_extension_functions("   ")


def test_call_extension_functions_rejects_an_unsplittable_command():
    """A line whose quoting cannot be parsed is a caller error, not a lookup miss."""
    server = TCP_Server_Base(host="127.0.0.1", port=65002, is_extend_command=True)
    with pytest.raises(ValueError, match="No closing quotation"):
        server.call_extension_functions('/known "unclosed')
