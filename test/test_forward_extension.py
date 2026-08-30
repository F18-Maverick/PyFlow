"""Tests for the TCP forward extension (forward_extension_tcp.py)."""

import os
import socket
import threading
import time
from types import SimpleNamespace

import pytest

import PyFlow.forward_extension_tcp as fwd


class DummySocket:
    """Minimal socket stand-in capturing what the server would send."""

    def __init__(self):
        self.data = b""

    def sendall(self, data):
        self.data += data

    def close(self):
        pass


@pytest.fixture
def dummy_client_socket():
    return DummySocket()


def test_parse_items_and_addrs():
    tokens = ["111", "222", "('127.0.0.1', 3000)", "('127.0.0.1', 3500)", "plain"]
    items, addrs = fwd._parse_items_and_addrs(tokens)
    assert items == ["111", "222", "plain"]
    assert addrs == [("127.0.0.1", 3000), ("127.0.0.1", 3500)]


def test_parse_items_and_addrs_rejects_malformed_tuple():
    items, addrs = fwd._parse_items_and_addrs(["('127.0.0.1', notaport)", "ok"])
    assert items == ["('127.0.0.1', notaport)", "ok"]
    assert addrs == []


def test_send_msg_forward_relays_to_reachable_only(server, dummy_client_socket, capsys):
    fwd.server_instance = server
    server.running = True
    reachable = ("127.0.0.1", 12345)
    server.clients[reachable] = {"socket": dummy_client_socket}
    fwd._forward_send_msg_handler(
        None,
        None,
        "/forward_send_msg \"111\" \"222\" \"('127.0.0.1', 12345)\" \"('127.0.0.1', 99999)\"",
    )
    sent = dummy_client_socket.data.decode("utf-8")
    assert "111" in sent
    assert "222" in sent
    assert "99999" not in sent
    assert "skipped" in capsys.readouterr().out


def test_file_forward_relay_skips_unreachable(server, monkeypatch, capsys, tmp_path):
    fwd.server_instance = server
    server.running = True
    server.file_transfer_dir = str(tmp_path / "received")
    os.makedirs(server.file_transfer_dir, exist_ok=True)
    (tmp_path / "received" / "a.txt").write_text("data")
    reachable = ("127.0.0.1", 12345)
    server.clients[reachable] = {"socket": DummySocket()}
    pushed = []

    def record_push(message, file_folder_abspath=None):
        pushed.append(message)

    monkeypatch.setattr(server, "file_transfer_server_recv_client_start", record_push)
    fwd._forward_files_handler(
        None,
        None,
        "/forward_file \"a.txt\" \"('127.0.0.1', 12345)\" \"('127.0.0.1', 99999)\"",
    )
    assert len(pushed) == 1
    assert "/file" in pushed[0] and "a.txt" in pushed[0]
    assert "skipped" in capsys.readouterr().out


def test_folder_forward_relay_skips_unreachable(server, monkeypatch, capsys, tmp_path):
    fwd.server_instance = server
    server.running = True
    server.file_transfer_dir = str(tmp_path / "received")
    os.makedirs(server.file_transfer_dir, exist_ok=True)
    os.makedirs(tmp_path / "received" / "folder")
    reachable = ("127.0.0.1", 12345)
    server.clients[reachable] = {"socket": DummySocket()}
    pushed = []
    monkeypatch.setattr(
        server,
        "folder_file_transfer_server_recv_client_start",
        pushed.append,
    )
    fwd._forward_folders_handler(
        None,
        None,
        "/forward_folder \"folder\" \"('127.0.0.1', 12345)\" \"('127.0.0.1', 99999)\"",
    )
    assert len(pushed) == 1
    assert "/file_folder" in pushed[0] and "folder" in pushed[0]
    assert "skipped" in capsys.readouterr().out


def test_forward_commands_are_client_only(client, server):
    fwd.setup_client_commands(client)
    fwd.setup_server_commands(server)
    for cmd in fwd._FORWARD_COMMANDS:
        assert cmd in client._custom_handlers[1]  # client console triggers it
        assert cmd not in server._custom_handlers[1]  # server console rejects it
        assert cmd not in client._custom_handlers[0]
    for relay in ("/forward_send_msg", "/forward_file", "/forward_folder"):
        assert relay in server._custom_handlers[0]  # client requests reach it


def test_send_msg_forward_asks_server(client, monkeypatch):
    fwd.setup_client_commands(client)
    client.client_socket = DummySocket()
    sent = []
    monkeypatch.setattr(client, "send_message", lambda sock, msg: sent.append(msg) or True)
    handler = client._custom_handlers[1]["/send_msg_forward"]
    handler(None, None, '/send_msg_forward "111" "222" "(\'127.0.0.1\', 3000)"')
    assert len(sent) == 1
    request = sent[0]
    assert request.startswith("/forward_send_msg")
    assert "111" in request and "222" in request
    assert "127.0.0.1" in request and "3000" in request


def test_file_forward_uploads_then_asks_server(client, monkeypatch, tmp_path):
    fwd.setup_client_commands(client)
    client.client_socket = DummySocket()
    payload = tmp_path / "payload.txt"
    payload.write_text("data")
    uploaded = []
    sent = []
    monkeypatch.setattr(
        client,
        "file_transfer_client_recv_client_start",
        lambda message, file_folder_abspath=None: uploaded.append(message),
    )
    monkeypatch.setattr(client, "send_message", lambda sock, msg: sent.append(msg) or True)
    handler = client._custom_handlers[1]["/file_forward"]
    handler(None, None, '/file_forward "{}" "(\'127.0.0.1\', 3000)"'.format(payload))
    assert any("payload.txt" in m for m in uploaded)
    assert sent[-1].startswith("/forward_file")
    assert "payload.txt" in sent[-1]
    assert "127.0.0.1" in sent[-1] and "3000" in sent[-1]


def test_folder_forward_uploads_sync_then_asks_server(client, monkeypatch, tmp_path):
    fwd.setup_client_commands(client)
    client.client_socket = DummySocket()
    folder = tmp_path / "data_folder"
    (folder / "sub").mkdir(parents=True)
    (folder / "a.txt").write_text("a")
    (folder / "sub" / "b.txt").write_text("b")
    uploaded = []
    sent = []
    monkeypatch.setattr(
        client,
        "file_transfer_client_recv_client_start",
        lambda message, file_folder_abspath=None: uploaded.append(message),
    )
    monkeypatch.setattr(client, "send_message", lambda sock, msg: sent.append(msg) or True)
    handler = client._custom_handlers[1]["/folder_forward"]
    handler(None, None, '/folder_forward "{}" "(\'127.0.0.1\', 3000)"'.format(folder))
    # root and sub-directory commands, then one file transfer per file
    dir_commands = [m for m in sent if m.startswith("/file_folder") and "(" not in m]
    expected_dirs = 2  # root + sub dir
    assert len(dir_commands) == expected_dirs
    file_uploads = [m for m in uploaded if m.startswith("/file_folder")]
    assert len(file_uploads) == expected_dirs  # a.txt + sub/b.txt
    assert sent[-1].startswith("/forward_folder")
    assert "data_folder" in sent[-1]


def test_forward_without_items_or_addrs_prints_usage(client, monkeypatch, capsys):
    fwd.setup_client_commands(client)
    client.client_socket = DummySocket()
    sent = []
    monkeypatch.setattr(client, "send_message", lambda sock, msg: sent.append(msg) or True)
    handler = client._custom_handlers[1]["/send_msg_forward"]
    handler(None, None, "/send_msg_forward")
    assert sent == []
    assert "need at least one item" in capsys.readouterr().out


def test_single_variant_rejects_multiple_items(client, monkeypatch, capsys):
    fwd.setup_client_commands(client)
    client.client_socket = DummySocket()
    sent = []
    monkeypatch.setattr(client, "send_message", lambda sock, msg: sent.append(msg) or True)
    handler = client._custom_handlers[1]["/file_forward"]
    handler(None, None, '/file_forward "a.txt" "b.txt" "(\'127.0.0.1\', 3000)"')
    assert sent == []
    assert "exactly one item" in capsys.readouterr().out


def test_server_setup_creates_instance(monkeypatch, tmp_path, capsys):
    """server_setup() builds a real server instance with relays registered."""
    original = fwd.connect_tcp.TCP_Server_Base
    created = []

    def fake_server(**kwargs):
        created.append(kwargs)
        s = original(**kwargs)
        s.start_TCP_Server = lambda: None
        return s

    monkeypatch.setattr(fwd.connect_tcp, "TCP_Server_Base", fake_server)
    fwd.server_setup()
    assert created and created[0]["is_extend_command"] is True
    assert fwd.server_instance is not None
    for relay in ("/forward_send_msg", "/forward_file", "/forward_folder"):
        assert relay in fwd.server_instance._custom_handlers[0]


def test_server_setup_with_existing_instance_threaded(monkeypatch):
    """server_setup(instance=..., is_input_command_in_console=False) registers
    the relays on the given instance and really starts it in a background
    thread: the accept loop is live and accepts a plain connection."""
    with socket.socket() as s0:
        s0.bind(("127.0.0.1", 0))
        port = s0.getsockname()[1]
    s = fwd.connect_tcp.TCP_Server_Base(
        host="127.0.0.1",
        port=port,
        is_extend_command=True,
        is_input_command_in_console=False,
        is_enable_encrypto=False,
    )
    try:
        fwd.server_setup(instance=s, is_input_command_in_console=False)
        assert fwd.server_instance is s
        for relay in ("/forward_send_msg", "/forward_file", "/forward_folder"):
            assert relay in s._custom_handlers[0]
        for _ in range(50):
            if s.running:
                break
            time.sleep(0.02)
        assert s.running  # the background thread really started the server
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.settimeout(2)
        probe.connect(("127.0.0.1", port))
        probe.close()
    finally:
        s.stop()