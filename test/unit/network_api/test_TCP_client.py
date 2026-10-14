

from PyFlow.network_api.connect_tcp import CommandNotFoundError, TCP_Client_Base


def test_tcp_client_init():
    client = TCP_Client_Base(
        host="127.0.0.1", port=65003, client_host="127.0.0.1", is_extend_command=True
    )
    assert client.host == "127.0.0.1"
    assert client.port == 65003  # noqa: PLR2004
    assert callable(client.connect)
    assert client.is_enable_encrypto is True
    assert client.is_custom_keys is None
    assert client.recv_messages_chunk_len == 4096  # noqa: PLR2004
    assert client.file_transfer_chunk_len == 65536  # noqa: PLR2004

import socket  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

import pytest  # noqa: E402

from PyFlow.network_api import connect_tcp as _connect_tcp  # noqa: E402


def _client(**kwargs):
    options = {"host": "127.0.0.1", "port": 65004, "client_host": "127.0.0.1"}
    options.update(kwargs)
    return TCP_Client_Base(is_extend_command=True, **options)


def test_console_custom_command_matches_the_whole_first_token(monkeypatch):
    """Reach a console handler registered as ``/mycmd``.

    The lookup used to key on the line's first *character* ("/"), so no
    registered console command could ever match and every line was sent as
    chat text.
    """
    client = _client()
    seen = []
    assert client.register_command(
        "/mycmd", lambda sock, addr, cmd: seen.append(cmd), "client"
    ) is None
    client.running = True
    sent = []
    monkeypatch.setattr(client, "send_message", lambda sock, msg: sent.append(msg))
    monkeypatch.setattr(client, "close", lambda: None)
    monkeypatch.setattr(_connect_tcp.time, "sleep", lambda seconds: None)

    lines = iter(["/mycmd hello"])

    def fake_input():
        try:
            return next(lines)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr("builtins.input", fake_input)
    client.interactive_mode()

    assert seen == ["/mycmd hello"]
    assert "/mycmd hello" not in sent  # not forwarded to the server as chat


def test_multiple_file_keeps_the_semaphore_slot_during_the_transfer(monkeypatch):
    """Cap concurrent ``/multiple_file`` sends at ``max_thread_num``.

    The slot must be held while the worker runs instead of being released
    right after its thread is started.
    """
    client = _client(max_thread_num=1)
    started = threading.Event()
    release = threading.Event()

    def fake_worker(message, file_folder_abspath):
        started.set()
        release.wait(5)

    monkeypatch.setattr(client, "file_transfer_client_recv_client_start", fake_worker)
    try:
        client.multiple_file_transfer_client_recv_client_start("/multiple_file a.txt b.txt")
        assert started.wait(5), "the first transfer never started"
        assert client.file_semaphore.acquire(blocking=False) is False, (
            "the semaphore was free while a transfer was running"
        )
    finally:
        release.set()


def test_close_before_connect_keeps_the_client_closed():
    """A ``close()`` that lands while ``connect()`` is in flight must stick.

    ``connect`` sets ``running`` back to True after the socket is up, which used
    to revive a client the caller had already closed (its receive thread then
    kept the connection alive behind the caller's back).
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def fake_server():
        try:
            conn, _addr = listener.accept()
        except OSError:
            return
        try:
            conn.recv(1024)  # the client's /crypto_mode greeting
            conn.sendall(b"/crypto_mode 0\n")  # matching mode: negotiation succeeds
            time.sleep(0.5)
        except OSError:
            pass
        finally:
            conn.close()

    threading.Thread(target=fake_server, daemon=True).start()
    client = _client(port=port, is_wait_server=False, is_enable_encrypto=False)
    try:
        client.close()
        assert client.connect() is False
        assert client.running is False
        assert client.client_socket is None
    finally:
        client.close()
        listener.close()


# ---- call_extension_functions ----------------------------------------------


def test_call_extension_functions_passes_this_clients_own_context():
    """A command registered for the console runs from code, line unchanged."""
    client = _client()
    client.client_port = 40000
    seen = []
    client.register_command(
        "/mycmd", lambda sock, addr, cmd: seen.append((sock, addr, cmd)) or "done", "client"
    )

    assert client.call_extension_functions("/mycmd hello") == "done"
    assert seen == [(client.client_socket, "127.0.0.1:40000", "/mycmd hello")]


def test_call_extension_functions_finds_the_inbound_group_case_insensitively():
    """A handler for the lines the server sends is reachable by its name too."""
    client = _client()
    seen = []
    client.register_command("/inbound", lambda sock, addr, cmd: seen.append(cmd), "server")

    client.call_extension_functions("/INBOUND x y")

    assert seen == ["/INBOUND x y"]


def test_call_extension_functions_prefers_the_console_group():
    """The console group wins a name both groups register."""
    client = _client()
    client.register_command("/dup", lambda sock, addr, cmd: "console", "client")
    client.register_command("/dup", lambda sock, addr, cmd: "inbound", "server")

    assert client.call_extension_functions("/dup") == "console"


def test_call_extension_functions_runs_synchronously():
    """``run_in_thread`` belongs to the dispatch paths, not to a direct call."""
    client = _client()
    seen = []
    client.register_command(
        "/slow", lambda sock, addr, cmd: seen.append(cmd), "client", run_in_thread=True
    )

    client.call_extension_functions("/slow now")

    assert seen == ["/slow now"]  # finished before the call returned


def test_call_extension_functions_rejects_unregistered_commands():
    """An unknown first token raises, and the message names the line."""
    client = _client()
    client.register_command("/known", lambda sock, addr, cmd: None, "client")

    with pytest.raises(CommandNotFoundError) as exc:
        client.call_extension_functions("/ghost arg")
    assert "/ghost arg" in str(exc.value)
    with pytest.raises(CommandNotFoundError):  # an empty line selects nothing
        client.call_extension_functions("   ")
