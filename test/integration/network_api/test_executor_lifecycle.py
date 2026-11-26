"""Real TCP acceptance for custom-executor socket-first shutdown and reconnect."""

import errno
import select
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Queue

import pytest
from helpers import (
    free_port,
    isolate_instance_files,
    request_transfer_listener,
    server_ready,
    wait_until,
)

from PyFlow.network_api import connect_tcp
from PyFlow.network_api.connect_tcp import TCP_Client_Base, TCP_Server_Base

WAIT = 10
_CLOSED_SOCKET_ERRNOS = {
    errno.EBADF,
    errno.ECONNABORTED,
    errno.ECONNRESET,
    errno.ENOTCONN,
    getattr(errno, "ESHUTDOWN", -1),
    10057,  # WSAENOTCONN
    10058,  # WSAESHUTDOWN
}


@pytest.fixture(autouse=True)
def isolate_runtime_files(tmp_path, monkeypatch):
    """Keep real manual-port and progress files inside this test's temp directory."""
    command_table = Path(connect_tcp.__file__).with_name("decode_command_table.json")
    shutil.copyfile(command_table, tmp_path / command_table.name)
    monkeypatch.setattr(connect_tcp, "__file__", str(tmp_path / "connect_tcp.py"))


def _server(tmp_path, name, *, port=None, **overrides):
    options = {
        "host": "127.0.0.1",
        "port": free_port() if port is None else port,
        "max_clients": 4,
        "is_extend_command": True,
        "is_input_command_in_console": False,
        "is_enable_encrypto": False,
        "is_print_log": False,
        "max_custom_workers": 1,
    }
    options.update(overrides)
    instance = TCP_Server_Base(**options)
    isolate_instance_files(instance, tmp_path, name)
    thread = threading.Thread(target=instance.start_TCP_Server, daemon=True)
    thread.start()
    assert server_ready(instance), "server did not start"
    return instance, thread


def _client(tmp_path, name, port, **overrides):
    options = {
        "host": "127.0.0.1",
        "port": port,
        "client_host": "127.0.0.1",
        "is_extend_command": True,
        "is_input_command_in_console": False,
        "is_wait_server": False,
        "is_enable_encrypto": False,
        "is_print_log": False,
        "max_custom_workers": 1,
    }
    options.update(overrides)
    instance = TCP_Client_Base(**options)
    isolate_instance_files(instance, tmp_path, name)
    return instance


def _peer_on(server, client):
    try:
        peer = client.client_socket.getsockname()
    except (AttributeError, OSError):
        return None
    return peer if peer in server.clients else None


def _observe_submissions(endpoint, monkeypatch):
    """Retain the real Future submitted by inbound command dispatch."""
    submissions = Queue()
    submit = endpoint.submit_task

    def observe(*args, **kwargs):
        future = submit(*args, **kwargs)
        submissions.put(future)
        return future

    monkeypatch.setattr(endpoint, "submit_task", observe)
    return submissions


def _stop_server(server, thread):
    server.stop()
    thread.join(timeout=WAIT)
    assert not thread.is_alive(), "start_TCP_Server did not return after stop"


def _request_blocked_transfer(server, client, receiver, sender, side):
    peer = client.client_socket.getsockname()
    assert wait_until(lambda: peer in server.clients)
    server_control = server.clients[peer]["socket"]
    send_socket = server_control if side == "client" else client.client_socket
    recv_socket = client.client_socket if side == "client" else server_control
    port, _stamp = request_transfer_listener(
        sender, receiver, "/file pending.bin 42"
    )
    transfer_listener = receiver._receive_transfers[42]["owner"]
    assert transfer_listener.getsockname()[1] == port
    return send_socket, recv_socket, transfer_listener


def _close_outside_pool(close, receiver, socket_closed, future, release_handler):
    with ThreadPoolExecutor(max_workers=1) as callers:
        closing = callers.submit(close)
        try:
            woke_on_close = socket_closed.wait(WAIT)
            if woke_on_close:
                assert not closing.done() and future.running(), (
                    "external close did not drain the worker"
                )
                with pytest.raises(RuntimeError):
                    receiver.submit_task(lambda: None)
        finally:
            release_handler.set()
        closing.result(timeout=WAIT)
        assert woke_on_close, (
            "handler did not observe control-socket shutdown; "
            f"Future result={future.result(timeout=1)!r}"
        )


def _assert_endpoint_drained(endpoint, resources):
    receiver, server, client, side = endpoint
    recv_socket, transfer_listener, worker, future = resources
    assert future.done() and future.result() is None
    assert not worker.is_alive() and receiver._custom_executor._in_flight == 0
    assert recv_socket.fileno() == transfer_listener.fileno() == -1
    assert wait_until(
        lambda: not receiver._receive_transfers and not receiver._active_transfer_sockets
    )
    if side == "client":
        assert client.receive_thread is None or not client.receive_thread.is_alive()
        assert not client._control_sockets and wait_until(lambda: not server.clients)
    else:
        assert server.server_socket.fileno() == -1 and not server.clients
        assert wait_until(lambda: not server._control_sockets)
    assert all(not thread.is_alive() for thread in receiver._custom_executor._threads)


@pytest.mark.parametrize(
    "side", ["threaded-server", "async-server", "client"],
    ids=["threaded-server", "async-server", "client"],
)
def test_real_socket_close_unblocks_handler_before_external_drain(
    tmp_path, monkeypatch, side
):
    server, server_thread = _server(
        tmp_path, "server", is_asynic_clients_io=side == "async-server"
    )
    client = _client(tmp_path, "client", server.port)
    receiver, sender = (client, server) if side == "client" else (server, client)
    close = client.close if side == "client" else server.stop
    entered, socket_closed, release_handler = Queue(), threading.Event(), threading.Event()
    submissions = _observe_submissions(receiver, monkeypatch)

    def wait_for_socket_shutdown(control_socket, _address, _command):
        # Observe the actual session socket. On Windows, select on socket.dup()
        # did not report shutdown even though the executor Future ended.
        entered.put(threading.current_thread())
        readable, _, _ = select.select([control_socket], [], [], WAIT)
        assert readable, "control read was not woken"
        try:
            data = control_socket.recv(1)
        except OSError as exc:
            assert exc.errno in _CLOSED_SOCKET_ERRNOS, f"unexpected socket error: {exc}"
        else:
            assert data == b"", f"unexpected control payload after shutdown: {data!r}"
        socket_closed.set()
        assert release_handler.wait(WAIT), "handler tail was not released"

    receiver.register_command(
        "/wait_for_socket_shutdown", wait_for_socket_shutdown, "server", run_in_thread=True
    )
    try:
        assert client.connect()
        send_socket, recv_socket, transfer_listener = _request_blocked_transfer(
            server, client, receiver, sender, side
        )
        assert sender.send_message(send_socket, "/wait_for_socket_shutdown")
        worker = entered.get(timeout=WAIT)
        future = submissions.get(timeout=WAIT)
        assert future.running() and not socket_closed.is_set()
        _close_outside_pool(close, receiver, socket_closed, future, release_handler)
        _assert_endpoint_drained(
            (receiver, server, client, side),
            (recv_socket, transfer_listener, worker, future),
        )
    finally:
        release_handler.set()
        client.close()
        _stop_server(server, server_thread)


def test_client_reconnect_reuses_executor_until_terminal_close(tmp_path):
    server, server_thread = _server(tmp_path, "server1")
    client = _client(
        tmp_path,
        "client",
        server.port,
        is_wait_server=True,
        is_auto_reconnect=True,
    )
    probes = Queue()

    def probe(control_socket, _address, _command):
        probes.put((control_socket, threading.current_thread()))

    client.register_command("/probe", probe, "server", run_in_thread=True)
    runner = threading.Thread(target=client.start_TCP_client, daemon=True)
    server2 = server_thread2 = None
    worker = None
    try:
        runner.start()
        assert wait_until(lambda: _peer_on(server, client) is not None)
        peer = _peer_on(server, client)
        assert server.send_message(server.clients[peer]["socket"], "/probe")
        old_control, worker = probes.get(timeout=WAIT)

        port = server.port
        _stop_server(server, server_thread)
        server2, server_thread2 = _server(tmp_path, "server2", port=port)
        assert wait_until(lambda: _peer_on(server2, client) is not None, timeout=WAIT)
        peer = _peer_on(server2, client)
        assert server2.send_message(server2.clients[peer]["socket"], "/probe")
        new_control, reused_worker = probes.get(timeout=WAIT)

        assert new_control is not old_control and old_control.fileno() == -1
        assert reused_worker is worker
        assert client.submit_task(threading.current_thread).result(timeout=WAIT) is worker
    finally:
        client.close()
        runner.join(timeout=WAIT)
        assert not runner.is_alive(), "auto-reconnect runner did not stop after close"
        if server2 is not None:
            _stop_server(server2, server_thread2)
        else:
            _stop_server(server, server_thread)
    assert worker is not None and not worker.is_alive()
