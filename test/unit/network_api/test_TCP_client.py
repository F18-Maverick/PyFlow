

from PyFlow.network_api.connect_tcp import TCP_Client_Base


def test_tcp_client_init():
    client = TCP_Client_Base(
        host="127.0.0.1", port=65003, client_host="127.0.0.1", is_extend_command=True
    )
    assert client.host == "127.0.0.1"
    assert client.port == 65003  # noqa: PLR2004
    assert callable(client.connect)
    assert client.is_enable_encrypto is True
    assert client.is_custom_keys is None

import socket  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

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
