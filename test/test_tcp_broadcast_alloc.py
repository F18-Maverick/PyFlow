"""Tests for TCP_Server_Base broadcast / targeted messaging and the
client-side manual port-allocation machinery."""

import os
import threading
import time
from unittest.mock import MagicMock

import pytest


from PyFlow.network_api.connect_tcp import TCP_Client_Base, TCP_Server_Base

_PORT = 65410


def _server():
    return TCP_Server_Base(
        host="127.0.0.1",
        port=_PORT,
        is_extend_command=True,
        is_input_command_in_console=False,
    )


@pytest.fixture
def server():
    s = _server()
    yield s
    try:
        s.stop()
    except Exception:
        pass


# ---- broadcast --------------------------------------------------------------


def test_broadcast_sends_to_all_clients(server, monkeypatch):
    c1, c2 = MagicMock(), MagicMock()
    server.running = True
    server.clients = {
        ("127.0.0.1", 1): {"socket": c1, "address": ("127.0.0.1", 1)},
        ("127.0.0.1", 2): {"socket": c2, "address": ("127.0.0.1", 2)},
    }
    server.broadcast("hello all")
    c1.sendall.assert_called_once()
    c2.sendall.assert_called_once()
    assert b"hello all" in c1.sendall.call_args.args[0]


def test_broadcast_excludes_client(server):
    c1, c2 = MagicMock(), MagicMock()
    server.running = True
    server.clients = {
        ("127.0.0.1", 1): {"socket": c1, "address": ("127.0.0.1", 1)},
        ("127.0.0.1", 2): {"socket": c2, "address": ("127.0.0.1", 2)},
    }
    server.broadcast("msg", exclude_client=("127.0.0.1", 2))
    c1.sendall.assert_called_once()
    c2.sendall.assert_not_called()


def test_broadcast_removes_disconnected_clients(server):
    """send_message re-raises socket errors, so broadcast drops a dead
    peer from the client registry and closes its socket.
    """
    dead = MagicMock()
    dead.sendall.side_effect = OSError("broken pipe")
    alive = MagicMock()
    server.running = True
    server.clients = {
        ("127.0.0.1", 1): {"socket": dead, "address": ("127.0.0.1", 1)},
        ("127.0.0.1", 2): {"socket": alive, "address": ("127.0.0.1", 2)},
    }
    server.broadcast("msg")
    assert ("127.0.0.1", 1) not in server.clients  # dead peer dropped
    assert ("127.0.0.1", 2) in server.clients
    dead.close.assert_called_once()


# ---- send_msg_to_specific_client --------------------------------------------


def test_send_msg_to_specific_client(server, monkeypatch):
    c1 = MagicMock()
    server.running = True
    # parameters are quoted, the (ip, port) tuple comes last:
    #   /send_msg "<message>" "(ip, port)"
    server.clients = {("127.0.0.1", 52162): {"socket": c1, "address": ("127.0.0.1", 52162)}}
    sent = []
    monkeypatch.setattr(server, "send_message", lambda sock, msg: sent.append((sock, msg)))
    server.send_msg_to_specific_client('/send_msg "111" "(\'127.0.0.1\', 52162)"')
    assert sent == [(c1, "111")]


def test_send_msg_to_specific_client_unknown_target(server, monkeypatch, capsys):
    server.running = True
    sent = []
    c2 = MagicMock()
    server.clients = {("127.0.0.1", 52162): {"socket": c2, "address": ("127.0.0.1", 52162)}}
    monkeypatch.setattr(server, "send_message", lambda sock, msg: sent.append((sock, msg)))
    server.send_msg_to_specific_client('/send_msg "111" "(\'127.0.0.1\', 9999)"')
    assert sent == []
    assert "not found" in capsys.readouterr().out


def test_send_msg_to_specific_client_multiple_messages(server, monkeypatch):
    """Multiple messages before the tuple are all delivered."""
    c1 = MagicMock()
    server.running = True
    server.clients = {("127.0.0.1", 52162): {"socket": c1, "address": ("127.0.0.1", 52162)}}
    sent = []
    monkeypatch.setattr(server, "send_message", lambda sock, msg: sent.append((sock, msg)))
    server.send_msg_to_specific_client(
        '/send_msg "hello" "there" "(\'127.0.0.1\', 52162)"'
    )
    assert sent == [(c1, "hello"), (c1, "there")]


def test_send_msg_to_specific_client_string_ip_rejected(server, monkeypatch, capsys):
    """An unquoted (ip, port) tuple is split by shlex and cannot be
    parsed: the helper falls into its error branch."""
    server.running = True
    server.clients = {("127.0.0.1", 1): MagicMock()}
    server.send_msg_to_specific_client("/send_msg (127.0.0.1,1) hello")
    assert "not a valid client address" in capsys.readouterr().out


def test_send_msg_to_specific_client_bad_address(server, monkeypatch, capsys):
    monkeypatch.setattr(server, "send_message", lambda sock, msg: None)
    server.send_msg_to_specific_client("/sendmsg (not-a-tuple) hello")
    assert "not a valid client address" in capsys.readouterr().out


# ---- manual port allocation -------------------------------------------------


@pytest.fixture
def alloc_client():
    c = TCP_Client_Base(
        host="127.0.0.1",
        port=_PORT,
        client_host="127.0.0.1",
        is_extend_command=True,
        is_input_command_in_console=False,
    )
    yield c
    try:
        c.close()
    except Exception:
        pass


def test_palloc_returns_zero_without_manual_alloc(alloc_client):
    assert alloc_client.palloc() == 0


def test_file_palloc_spy_palloc_manual_range(alloc_client):
    alloc_client.is_hand_alloc_port = True
    alloc_client.port = 10000
    alloc_client.port_add_step = 1
    alloc_client.port_range_num = 10
    alloc_client.hand_alloc_port(1, 10)
    # hand_alloc_port seeds add_latest_port = port + 1, minus_latest = port
    first = alloc_client.file_palloc()
    assert first == alloc_client.port + 2
    second = alloc_client.file_palloc()
    assert second == first + 1
    spy_first = alloc_client.spy_palloc()
    assert spy_first == alloc_client.port - 1
    alloc_client.file_pfree(second)
    assert second not in alloc_client.all_allocated_ports_list


def test_file_palloc_exhaustion(alloc_client):
    alloc_client.is_hand_alloc_port = True
    alloc_client.port = 10000
    alloc_client.port_add_step = 1
    alloc_client.port_range_num = 1
    alloc_client.hand_alloc_port(1, 1)
    allocated = [alloc_client.file_palloc() for _ in range(10)]
    # two ports fit in the range, then the scan finds nothing new
    assert allocated[:2] == [10002, 10001]
    assert all(p is None for p in allocated[2:])


def test_hand_free_port_removes_entry(tmp_path, monkeypatch, alloc_client):
    alloc_client.is_hand_alloc_port = True
    alloc_client.port = 10000
    alloc_client.port_add_step = 1
    alloc_client.port_range_num = 10
    alloc_client.hand_alloc_port(1, 10)
    info_path = os.path.join(alloc_client.port_temp_info_path)
    assert os.path.exists(info_path)
    alloc_client.client_num = 0
    alloc_client.hand_free_port()
    assert not os.path.exists(info_path)


def test_is_client_port_temp_info_file_locked(alloc_client):
    assert alloc_client.is_client_port_temp_info_file_locked() is False
    alloc_client.client_port_temp_info_file_lock()
    assert alloc_client.is_client_port_temp_info_file_locked() is True
    alloc_client.client_port_temp_info_file_unlock()
    assert alloc_client.is_client_port_temp_info_file_locked() is False


# ---- interactive console ----------------------------------------------------


def test_console_input_status_clients_stop(server, monkeypatch, capsys):
    """Console commands /status, /clients and /stop drive the loop."""
    server.running = True
    server.clients = {
        ("127.0.0.1", 1): {
            "socket": MagicMock(),
            "address": ("127.0.0.1", 1),
            "id": "127.0.0.1:1",
            "connected_time": "2026-01-01 00:00:00",
        }
    }
    inputs = iter(["/status", "/clients", "/stop"])
    monkeypatch.setattr("builtins.input", lambda: next(inputs))
    monkeypatch.setattr(server, "stop", MagicMock())
    server.console_input()
    out = capsys.readouterr().out
    assert "current connection count: 1" in out
    assert "127.0.0.1:1" in out
    assert "shutting down" in out
    server.stop.assert_called_once()


def test_console_input_help_and_unknown(server, monkeypatch, capsys):
    server.running = True
    monkeypatch.setattr(server, "stop", MagicMock())
    inputs = iter(["/help", "/no_such_cmd", "/stop"])
    monkeypatch.setattr("builtins.input", lambda: next(inputs))
    server.console_input()
    out = capsys.readouterr().out
    assert "/file <file_path>" in out  # help text


def test_console_input_send_msg(server, monkeypatch, capsys):
    server.running = True
    server.clients = {(1, 2): {"socket": MagicMock(), "address": (1, 2)}}
    monkeypatch.setattr(server, "stop", MagicMock())
    inputs = iter(["/send_msg (1,2) hi", "/stop"])
    monkeypatch.setattr("builtins.input", lambda: next(inputs))
    server.console_input()
    assert "shutting down" in capsys.readouterr().out  # no crash on send_msg
