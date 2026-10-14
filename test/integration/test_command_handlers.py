import json
import shlex
import socket
import threading
import time
from pathlib import Path

import pytest
from helpers import free_port, wait_until

import PyFlow.command_control_extension_tcp as ctl
from PyFlow.network_api.connect_tcp import TCP_Client_Base, TCP_Server_Base


class DummySocket:
    """Collects what a client socket would have received."""

    def __init__(self):
        self.data = b""

    def sendall(self, b):
        self.data += b

    def shutdown(self, how):
        pass

    def close(self):
        pass

    def sent_tokens(self):
        """Tokens of the /command line this socket received."""
        return shlex.split(self.data.decode("utf-8"))


def add_dummy_client(server, port):
    """Register a fake connected client and return its socket."""
    address = ("127.0.0.1", port)
    dummy = DummySocket()
    server.clients[address] = {
        "socket": dummy,
        "address": address,
        "id": "127.0.0.1:{}".format(port),
        "connected_time": "now",
    }
    return dummy


def console_batch(commands, address):
    """Build the /command line a server console would carry for one client."""
    tokens = [shlex.quote(command) for command in commands] + [shlex.quote(str(address))]
    return "/command " + " ".join(tokens)


def log_of(log_path):
    """Return the parsed log, or None while the file is missing or mid-write."""
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def entries_of(log_path, command):
    """Log entries collected for one command so far."""
    return (log_of(log_path) or {}).get(command, [])


def output_bytes(log_path, command):
    """Bytes of output collected for one command so far."""
    return sum(
        len(entry.get("output") or "") + len(entry.get("error") or "")
        for entry in entries_of(log_path, command)
    )


def finished(log_path, command):
    """Report whether one command of the batch ended, and with status 0."""
    return any(
        entry.get("stream") == "end" and entry.get("returncode") == 0
        for entry in entries_of(log_path, command)
    )


@pytest.fixture
def logs_dir(tmp_path, monkeypatch):
    """Point the extension's log directory at the test's temporary directory."""
    directory = tmp_path / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(ctl, "_logs_dir", lambda: str(directory))
    return directory


@pytest.fixture
def live_pair(tmp_path, monkeypatch, logs_dir):
    """Run a server and client pair with the control extension loaded."""
    server_port = free_port()
    client_port = free_port()
    address = ("127.0.0.1", client_port)
    server = TCP_Server_Base(
        host="127.0.0.1",
        port=server_port,
        is_extend_command=True,
        is_input_command_in_console=False,
        is_enable_encrypto=False,
    )
    server.file_transfer_dir = str(tmp_path / "received")
    monkeypatch.setattr(ctl, "server_instance", server)
    client = TCP_Client_Base(
        host="127.0.0.1",
        port=server_port,
        client_host="127.0.0.1",
        client_port=client_port,
        is_extend_command=True,
        is_input_command_in_console=False,
        is_enable_encrypto=False,
    )
    monkeypatch.setattr(ctl, "client_instance", client)
    ctl.setup_server_commands(server)
    ctl.setup_client_commands(client)
    threading.Thread(target=server.start_TCP_Server, daemon=True).start()
    threading.Thread(target=client.start_TCP_client, daemon=True).start()
    try:
        assert wait_until(lambda: address in server.clients, timeout=20)
        yield server, client, address
    finally:
        client.close()
        server.stop()


def test_command_handler_parsing(server):
    """The server sends one /command line per client, carrying every command."""
    server.running = True
    dummy = add_dummy_client(server, 12345)

    ctl._command_handler(None, ("127.0.0.1", 9999), '/command "echo_test" "(\'127.0.0.1\', 12345)"')

    tokens = dummy.sent_tokens()
    assert tokens[0] == "/command"
    assert tokens[1] == "('127.0.0.1', 12345)"  # the client, addressed first on the wire
    assert tokens[2:] == ["echo_test"]


def test_command_handler_sends_every_command_to_every_client(server):
    """Any number of commands reaches any number of clients, in one batch each."""
    server.running = True
    first = add_dummy_client(server, 12345)
    second = add_dummy_client(server, 12346)

    cmd = '/command "cd /tmp" "ls ./" "echo done" "(\'127.0.0.1\', 12345)" "(\'127.0.0.1\', 12346)"'
    ctl._command_handler(None, ("127.0.0.1", 9999), cmd)

    for port, dummy in ((12345, first), (12346, second)):
        tokens = dummy.sent_tokens()
        assert tokens[1] == "('127.0.0.1', {})".format(port)
        assert tokens[2:] == ["cd /tmp", "ls ./", "echo done"]


def test_command_handler_skips_unconnected_client(server, capsys):
    server.running = True

    ctl._command_handler(None, ("127.0.0.1", 9999), '/command "ls" "(\'127.0.0.1\', 4242)"')

    assert "is not connected, skipped" in capsys.readouterr().out


def test_command_handler_requires_command_and_client(server, capsys):
    server.running = True

    ctl._command_handler(None, ("127.0.0.1", 9999), '/command "ls"')
    assert "Invalid command format" in capsys.readouterr().out

    ctl._command_handler(None, ("127.0.0.1", 9999), "/command \"('127.0.0.1', 4242)\"")
    assert "Invalid command format" in capsys.readouterr().out


def test_split_commands_and_clients_keeps_address_like_commands():
    """Only the trailing addresses are clients; everything before them is a command."""
    commands, clients = ctl._split_commands_and_clients(
        ["echo ('a', 1)", "ls", "('127.0.0.1', 3000)", "('10.0.0.2', 3001)"]
    )

    assert commands == ["echo ('a', 1)", "ls"]
    assert clients == [("127.0.0.1", 3000), ("10.0.0.2", 3001)]


def test_log_filename_is_per_client_address():
    assert ctl._log_filename_for_address(("127.0.0.1", 3000)) == "logs_127.0.0.1_3000.json"
    assert ctl._log_filename_for_address(("::1", 3000)) == "logs___1_3000.json"


def test_command_handler_server_setup_invalid_format(monkeypatch, capsys):
    ctl._command_handler_server_setup(None, ("127.0.0.1", 9999), "not-a-command")
    assert "Invalid command format" in capsys.readouterr().out


def test_command_handler_server_setup_rejects_non_address(client, capsys):
    ctl._command_handler_server_setup(None, ("127.0.0.1", 9999), '/command "ls" "not-an-address"')
    assert "is not a client address" in capsys.readouterr().out


def test_command_handler_server_setup_without_client_instance(monkeypatch, capsys):
    monkeypatch.setattr(ctl, "client_instance", None)
    ctl._command_handler_server_setup(
        None, ("127.0.0.1", 9999), '/command "(\'127.0.0.1\', 1)" "ls"'
    )
    assert "client instance is not set up" in capsys.readouterr().out


def test_command_batch_reports_shell_start_failure(logs_dir, monkeypatch, capsys):
    """A shell that cannot start is reported, and no log is announced."""

    def broken_popen(*args, **kwargs):
        raise OSError("no shell")

    monkeypatch.setattr(ctl.subprocess, "Popen", broken_popen)
    ctl._CommandBatch(("127.0.0.1", 3000), ["ls"], "peer").run()

    assert "ErrorWhileStartingTheCommandShell" in capsys.readouterr().out


def test_command_batch_strips_end_markers_split_across_reads(client, logs_dir, monkeypatch):
    """Output is logged as it arrives and the marker never reaches the log."""
    chunks = []
    announced = []

    def fake_transfer(message, file_folder_abspath=None):
        chunks.append(json.loads(Path(shlex.split(message)[1]).read_text(encoding="utf-8")))
        return True

    def fake_send(client_socket, message):
        announced.append(message)

    monkeypatch.setattr(client, "file_transfer_client_recv_client_start", fake_transfer)
    monkeypatch.setattr(client, "send_message", fake_send)

    batch = ctl._CommandBatch(("127.0.0.1", 3000), ["ls"], "peer")
    prefix = ctl._COMMAND_END_MARKER + "abc123"
    with batch._log_lock:
        batch._current_command = "ls"
        batch._current_cmd_id = "1"
        batch._sentinel_prefix = prefix
        batch._consume_stdout_locked("partial line\n")
        batch._consume_stdout_locked(prefix[: len(prefix) - 2])  # marker split in half
        batch._consume_stdout_locked(prefix[len(prefix) - 2 :] + " 1")
        batch._consume_stdout_locked("2\n")

    entries = chunks[-1]["ls"]
    assert [entry["stream"] for entry in entries] == ["stdout", "end"]
    assert entries[0]["output"] == "partial line\n"
    assert entries[1]["returncode"] == 12
    assert announced[-1].startswith('/command_log_chunk "logs_127.0.0.1_3000_chunk1.json"')
    assert announced[-1].strip().endswith('"logs_127.0.0.1_3000.json"')
    assert batch._pending == []  # the marker was consumed, not queued as output


def test_command_batch_keeps_entries_when_a_chunk_upload_fails(
    client, logs_dir, monkeypatch, capsys
):
    """A chunk the server did not receive goes out with the next one, in order."""
    attempts = []
    uploaded = []

    def fake_transfer(message, file_folder_abspath=None):
        attempts.append(message)
        if len(attempts) == 1:
            return False
        uploaded.append(json.loads(Path(shlex.split(message)[1]).read_text(encoding="utf-8")))
        return True

    monkeypatch.setattr(client, "file_transfer_client_recv_client_start", fake_transfer)
    monkeypatch.setattr(client, "send_message", lambda client_socket, message: None)

    batch = ctl._CommandBatch(("127.0.0.1", 3000), ["ls"], "peer")
    with batch._log_lock:
        batch._current_command = "ls"
        batch._current_cmd_id = "1"
        batch._sentinel_prefix = "never-matches"
        batch._pending_bytes = ctl._LOG_CHUNK_BYTES
        batch._consume_stdout_locked("first half\n")
        batch._maybe_flush_locked()  # the cutoff is reached, but the upload fails
    assert [entry["output"] for entry in batch._pending] == ["first half\n"]  # kept, not lost
    assert "was not delivered" in capsys.readouterr().out

    with batch._log_lock:
        batch._consume_stdout_locked("second half\n")  # crosses the cutoff again
        batch._maybe_flush_locked()  # and this upload carries both halves
    assert [entry["output"] for entry in uploaded[-1]["ls"]] == ["first half\n", "second half\n"]


def test_command_log_chunk_appends_to_the_same_log(server, tmp_path, logs_dir):
    """Every chunk of a client extends that client's log; nothing is overwritten."""
    server.file_transfer_dir = str(tmp_path / "received")
    Path(server.file_transfer_dir).mkdir()

    chunk_1 = Path(server.file_transfer_dir) / "logs_127.0.0.1_3000_chunk1.json"
    chunk_1.write_text(json.dumps({"ls": [{"output": "first"}]}), encoding="utf-8")
    ctl._command_log_chunk_dealing_server(
        None,
        ("127.0.0.1", 9999),
        '/command_log_chunk "logs_127.0.0.1_3000_chunk1.json" "logs_127.0.0.1_3000.json"',
    )

    chunk_2 = Path(server.file_transfer_dir) / "logs_127.0.0.1_3000_chunk2.json"
    chunk_2.write_text(json.dumps({"ls": [{"output": "second"}]}), encoding="utf-8")
    ctl._command_log_chunk_dealing_server(
        None,
        ("127.0.0.1", 9999),
        '/command_log_chunk "logs_127.0.0.1_3000_chunk2.json" "logs_127.0.0.1_3000.json"',
    )

    collected = log_of(logs_dir / "logs_127.0.0.1_3000.json")
    assert [entry["output"] for entry in collected["ls"]] == ["first", "second"]
    assert not chunk_1.exists()  # the received copies are consumed, not kept
    assert not chunk_2.exists()


def test_command_log_chunk_without_received_file(server, tmp_path, capsys):
    server.file_transfer_dir = str(tmp_path / "received")
    Path(server.file_transfer_dir).mkdir()

    ctl._command_log_chunk_dealing_server(
        None, ("127.0.0.1", 9999), '/command_log_chunk "gone.json" "logs_x.json"'
    )

    assert "was not received" in capsys.readouterr().out


def test_command_log_chunk_invalid_format(server, capsys):
    ctl._command_log_chunk_dealing_server(None, ("127.0.0.1", 9999), "/command_log_chunk")
    assert "Invalid command_log_chunk format" in capsys.readouterr().out


def test_command_done_merges_every_collected_log(server, logs_dir, capsys):
    (logs_dir / "logs_127.0.0.1_3000.json").write_text(
        json.dumps({"cmd1": [{"output": "ok"}]}), encoding="utf-8"
    )
    (logs_dir / "logs_127.0.0.1_3001.json").write_text(
        json.dumps({"cmd1": [{"error": "none"}]}), encoding="utf-8"
    )

    ctl._command_done_dealing_server(
        None, ("127.0.0.1", 9999), '/command_done "logs_127.0.0.1_3000.json"'
    )

    merged = log_of(logs_dir / "merged_logs.json")
    assert merged["cmd1"][0] == {"output": "ok"}
    assert merged["cmd1"][1] == {"error": "none"}
    assert "Command Done!" in capsys.readouterr().out


def test_command_done_invalid_format(server, capsys):
    ctl._command_done_dealing_server(None, ("127.0.0.1", 9999), "/command_done")
    assert "Invalid command_done format" in capsys.readouterr().out


def test_command_batch_shares_one_shell_between_its_commands(live_pair, logs_dir, tmp_path):
    """A cd of the first command still applies to the second command of the batch."""
    _server, _client, address = live_pair
    (tmp_path / "file_a.txt").write_text("a", encoding="utf-8")
    log_path = logs_dir / ctl._log_filename_for_address(address)
    change_dir = "cd {}".format(shlex.quote(str(tmp_path)))

    ctl._command_handler(None, ("127.0.0.1", 1), console_batch([change_dir, "ls -1"], address))

    assert wait_until(lambda: finished(log_path, "ls -1"), timeout=30)
    listing = "".join(entry["output"] for entry in entries_of(log_path, "ls -1"))
    assert "file_a.txt" in listing  # listed from the directory the batch changed into
    assert finished(log_path, change_dir)
    assert wait_until(
        lambda: (log_of(logs_dir / "merged_logs.json") or {}).get("ls -1"), timeout=30
    )


def test_command_batch_streams_a_running_command_in_chunks(live_pair, logs_dir, capsys):
    """A command that keeps producing output is tracked while it still runs."""
    _server, _client, address = live_pair
    log_path = logs_dir / ctl._log_filename_for_address(address)
    command = (
        "i=0; while [ $i -lt 3200 ]; do echo 0123456789012345678901234567890123456789; "
        "i=$((i+1)); done; sleep 5; echo tail"
    )

    ctl._command_handler(None, ("127.0.0.1", 1), console_batch([command], address))

    assert wait_until(lambda: output_bytes(log_path, command) >= ctl._LOG_CHUNK_BYTES, timeout=30)
    running = entries_of(log_path, command)
    assert [entry for entry in running if entry["stream"] == "stdout"]  # output already collected
    assert not [entry for entry in running if entry["stream"] == "end"]  # while the command runs

    assert wait_until(lambda: finished(log_path, command), timeout=30)
    assert "tail" in "".join(entry["output"] for entry in entries_of(log_path, command))
    assert "Log chunk" in capsys.readouterr().out


def test_call_extension_functions_runs_batches_of_a_live_pair(live_pair, logs_dir):
    """Both public entry points reach the control extension without a console.

    The server call goes through the console group (the handler a typed line
    would run) and the client call through the inbound group (the handler a
    line pushed by the server would run).
    """
    server, client, address = live_pair
    log_path = logs_dir / ctl._log_filename_for_address(address)

    assert server.call_extension_functions(console_batch(["echo from_server"], address)) is None
    assert wait_until(lambda: finished(log_path, "echo from_server"), timeout=30)

    assert client.call_extension_functions(f'/command "{address}" "echo from_client"') is None
    assert wait_until(lambda: finished(log_path, "echo from_client"), timeout=30)


def test_injectable_setup_registers_commands(server, client):
    """setup_server_commands/setup_client_commands register the control
    extension on existing instances (used by flow_setup's unified launch)."""
    ctl.setup_server_commands(server)
    ctl.setup_client_commands(client)
    assert "/command" in server._custom_handlers[1]  # server console group
    assert "/command_log_chunk" in server._custom_handlers[0]  # client messages group
    assert "/command_done" in server._custom_handlers[0]  # client messages group
    assert "/command" in client._custom_handlers[0]  # server messages group


def test_client_setup_with_existing_instance_threaded():
    """client_setup(instance=..., is_input_command_in_console=False) registers
    the control extension on the given instance and really starts it in a
    background thread: the client connects to a live server and returns."""
    with socket.socket() as s0:
        s0.bind(("127.0.0.1", 0))
        port = s0.getsockname()[1]
    s = ctl.connect_tcp.TCP_Server_Base(
        host="127.0.0.1",
        port=port,
        is_extend_command=True,
        is_input_command_in_console=False,
        is_enable_encrypto=False,
    )
    threading.Thread(target=s.start_TCP_Server, daemon=True).start()
    c = None
    try:
        for _ in range(50):
            if s.running:
                break
            time.sleep(0.02)
        assert s.running
        c = ctl.connect_tcp.TCP_Client_Base(
            host="127.0.0.1",
            port=port,
            client_host="127.0.0.1",
            is_extend_command=True,
            is_input_command_in_console=False,
            is_enable_encrypto=False,
        )
        ctl.client_setup(instance=c, is_input_command_in_console=False)
        assert ctl.client_instance is c
        assert "/command" in c._custom_handlers[0]
        for _ in range(50):
            if c.running:
                break
            time.sleep(0.02)
        assert c.running  # the background thread really connected
        assert c.client_socket is not None
        assert c.send_message(c.client_socket, "ping")  # and the link is usable
    finally:
        if c is not None:
            c.close()
        s.stop()
