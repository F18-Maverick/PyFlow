"""Command-control extension for the TCP protocol.

One command batch, run by every listed client, with each client's output
collected on the server:

  /command <command_1> ... <command_n> <client_1> ... <client_m>
      typed on the server console. Every client runs the whole command list in
      a single shell session, in order, so state such as a changed working
      directory carries over from one command to the next: ``/command "cd
      /path/to/search" "ls ./" "(ip, port)"`` lists the files of
      ``/path/to/search``, while sending ``cd`` and ``ls`` as two separate
      ``/command`` lines does not.

Commands come first and client addresses last; every address is written as a
Python address tuple, e.g. ``"('127.0.0.1', 3000)"``. There is no limit on the
number of commands or on the number of clients, and every listed client
receives the same command list. Two internal commands carry the log back:

  /command_log_chunk <chunk_file> <log_file>
      sent by the client after it uploaded one log chunk through the file
      transfer channel; the server appends the chunk's entries to that
      client's log file
  /command_done <log_file>
      sent by the client when the batch is over; the server merges every
      collected log file into ``merged_logs.json``

A client captures the output of the running commands and uploads a chunk every
``_LOG_CHUNK_BYTES`` of captured output (a smaller final chunk closes each
command), so a command that never stops is still tracked in real time. Every
chunk is appended to the same log file - ``logs_<ip>_<port>.json`` in the
server's ``logs`` directory - which is therefore never re-sent and never
rewritten, only extended.
"""

import ast
import codecs
import json
import os
import re
import shlex
import subprocess
import threading
import time
import traceback
import uuid
from datetime import datetime

from .network_api import connect_tcp

server_instance = None
client_instance = None
command_log_lock = threading.Lock()  # serialises the logs_<id>.json read-modify-write
_batch_locks = {}  # client address -> lock, so one client never runs two batches in one log
_batch_locks_lock = threading.Lock()

_LOG_CHUNK_BYTES = 64 * 1024  # a client uploads its log every this much captured output
_FLUSH_ATTEMPTS = 3  # upload attempts for the entries still queued when a batch ends
_READ_SIZE = 65536
_COMMAND_END_MARKER = "__pyflow_command_end__"
_SHELL_LINE_END = "\r\n" if os.name == "nt" else "\n"


def _load_json_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_json_file(path, data):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _merge_log_dicts(base_data, extra_data):
    merged = {}
    for command, entries in base_data.items():
        merged[command] = list(entries) if isinstance(entries, list) else [entries]
    for command, entries in extra_data.items():
        if command not in merged:
            merged[command] = []
        if isinstance(entries, list):
            merged[command].extend(entries)
        else:
            merged[command].append(entries)
    return merged


def _merge_all_logs(log_dir, merged_filename="merged_logs.json"):
    merged_data = {}
    for filename in sorted(os.listdir(log_dir)):
        if not filename.endswith(".json") or filename == merged_filename:
            continue
        file_path = os.path.join(log_dir, filename)
        if os.path.isfile(file_path):
            merged_data = _merge_log_dicts(merged_data, _load_json_file(file_path))
    _write_json_file(os.path.join(log_dir, merged_filename), merged_data)


def _logs_dir():
    return os.path.join(os.path.dirname(__file__), "logs")


def _log_filename_for_address(address):
    """Return the log file name that holds every batch of one client.

    Args:
        address (tuple): Client address ``(ip, port)`` the server knows it by.

    Returns:
        str: File name; the port is part of it, so a client that reconnects on
            another port keeps its own log and two clients never share one.
    """
    host = str(address[0]).replace(":", "_")  # IPv6 addresses carry colons
    return f"logs_{host}_{address[1]}.json"


def _parse_client_address(token):
    """Return the ``(ip, port)`` tuple a client address token carries.

    Args:
        token (str): One token of a ``/command`` line.

    Returns:
        tuple | None: The address, or None when the token is not an address.
    """
    if not (token.startswith("(") and token.endswith(")")):
        return None
    try:
        address = ast.literal_eval(token)
    except (ValueError, SyntaxError):
        return None
    if isinstance(address, tuple) and len(address) == 2 and isinstance(address[0], str):
        return address
    return None


def _split_commands_and_clients(tokens):
    """Split ``/command`` tokens into commands and trailing client addresses.

    Args:
        tokens (list[str]): Tokens after the command name.

    Returns:
        tuple: ``(commands, clients)``; every trailing token that parses as an
            address is a client, everything before it is a command, and both
            lists keep the order they were given in.
    """
    commands = list(tokens)
    clients = []
    while commands:
        address = _parse_client_address(commands[-1])
        if address is None:
            break
        clients.insert(0, address)
        commands.pop()
    return commands, clients


def _entries_by_command(entries):
    """Group log entries by the command they belong to, keeping their order."""
    grouped = {}
    for entry in entries:
        grouped.setdefault(entry["command"], []).append(entry)
    return grouped


def _marker_prefix_suffix(text, prefix):
    """Return the length of the longest suffix of ``text`` that starts ``prefix``.

    Args:
        text (str): Captured output that is not logged yet.
        prefix (str): Marker text awaited from the shell.

    Returns:
        int: Characters to hold back until the next read, so a marker split
            across two reads is never logged as command output.
    """
    for length in range(min(len(text), len(prefix)), 0, -1):
        if text.endswith(prefix[:length]):
            return length
    return 0


def _could_still_be_marker_tail(text):
    """Report whether ``text`` may still grow into the tail of an end marker."""
    return re.fullmatch(r" ?\d*\r?", text) is not None


def _without_echoed_lines(text, echoed_lines):
    """Drop the complete lines of ``text`` that a shell echoed back.

    A shell that echoes the lines it reads from its stdin (``cmd.exe``) sends
    them back mixed into the output of the commands; they are control lines of
    the batch, not command output.

    Args:
        text (str): Captured output that has not been logged yet.
        echoed_lines (set[str]): Lines already written to the shell's stdin.

    Returns:
        str: ``text`` without those lines; a line that is not complete yet stays
            in the result until the next read delivers its end.
    """
    kept = []
    for line in text.splitlines(keepends=True):
        if line.endswith(("\n", "\r")) and line.rstrip("\r\n") in echoed_lines:
            continue
        kept.append(line)
    return "".join(kept)


def _shell_argv():
    """Return the command line of the shell that runs one batch's commands.

    Returns:
        list[str]: ``cmd.exe`` on Windows, ``/bin/sh`` elsewhere; the same
            shells ``subprocess`` picks for ``shell=True``.
    """
    if os.name == "nt":
        return [os.environ.get("COMSPEC", "cmd.exe")]
    return ["/bin/sh"]


def _shell_echoes_stdin():
    """Report whether the batch's shell echoes the lines it reads from its stdin.

    Returns:
        bool: True for ``cmd.exe``, which prints every line of its piped input
            back into the output stream, so those lines are filtered out of the
            log; ``/bin/sh`` never echoes them.
    """
    return os.name == "nt"


def _shell_end_marker_line(token):
    """Return the shell line that prints one command's end marker.

    Args:
        token (str): Token unique to the command that just ran.

    Returns:
        str: Line for the batch's shell; it prints ``<marker><token> <code>``,
            where ``<code>`` is the command's exit status.
    """
    if os.name == "nt":
        return f"echo {_COMMAND_END_MARKER}{token} %ERRORLEVEL%\r\n"
    return "printf '%s %s\\n' '{}{}' \"$?\"\n".format(_COMMAND_END_MARKER, token)


def _batch_lock(address):
    """Return the lock that keeps one client's batches out of each other.

    Args:
        address (tuple): Client address ``(ip, port)``.

    Returns:
        threading.Lock: Locks the client's log and shell session for one batch.
    """
    with _batch_locks_lock:
        lock = _batch_locks.get(address)
        if lock is None:
            lock = threading.Lock()
            _batch_locks[address] = lock
        return lock


class _CommandBatch:
    """One ``/command`` batch: every command runs in a single shell session.

    The commands are written to the stdin of one long-lived shell, so state such
    as a changed working directory carries over from one command to the next. A
    command that reads that stdin consumes the commands after it; a command that
    needs input must redirect it. Two reader threads capture the shell's stdout
    and stderr while the commands run, and the captured text is uploaded to the
    server in log chunks, which keeps a command that never stops tracked in real
    time.
    """

    def __init__(self, client_address, commands, peer_id):
        """Create the runner for one batch.

        Args:
            client_address (tuple): Address the server knows this client by;
                names the log file and labels the log entries.
            commands (list[str]): Command lines to run, in order; at least one.
            peer_id (object): Description of this client recorded in the entries.
        """
        self._client_address = client_address
        self._client_id = "{}:{}".format(client_address[0], client_address[1])
        self._commands = list(commands)
        self._peer_id = str(peer_id)
        self._log_dir = _logs_dir()
        self._log_filename = _log_filename_for_address(client_address)
        self._log_lock = threading.Lock()
        self._pending = []
        self._pending_bytes = 0
        self._chunk_seq = 0
        self._stdout_buffer = ""
        self._echoed_lines = set()  # lines cmd.exe echoes back (Windows only)
        self._current_command = ""
        self._current_cmd_id = "0"
        self._sentinel_prefix = ""
        self._returncode = None
        self._command_finished = threading.Event()
        self._shell_closed = False
        self._proc = None
        self._readers = []

    def run(self):
        """Run every command of the batch and stream the log to the server."""
        os.makedirs(self._log_dir, exist_ok=True)
        try:
            self._start_shell()
        except OSError as e:
            print(f"ErrorWhileStartingTheCommandShell: {e}")
            return
        try:
            for cmd_id, command in enumerate(self._commands, start=1):
                if not self._run_command(cmd_id, command):
                    print(
                        f"command: the shell of {self._client_id} exited "
                        f"before command {cmd_id} finished"
                    )
                    break
        except Exception as e:
            traceback.print_exc()
            print(f"ErrorWhileRunningTheCommandBatch: the batch stopped early: {e}")
        finally:
            self._close_shell()
        self._log_leftover_output()
        self._flush_remaining()
        self._report_done()

    def _start_shell(self):
        """Start the shell session that runs this batch's commands."""
        self._proc = subprocess.Popen(
            _shell_argv(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        if os.name == "nt":  # ask cmd to stop echoing its input lines
            self._write_stdin("@echo off" + _SHELL_LINE_END)
        self._readers = [
            threading.Thread(target=self._read_stream, args=("stdout",), daemon=True),
            threading.Thread(target=self._read_stream, args=("stderr",), daemon=True),
        ]
        for reader in self._readers:
            reader.start()

    def _run_command(self, cmd_id, command):
        """Run one command and report whether the shell is still alive after it.

        Args:
            cmd_id (int): 1-based position of the command in the batch.
            command (str): Command line to run.

        Returns:
            bool: True when the shell ran the command to its end marker.
        """
        token = uuid.uuid4().hex
        with self._log_lock:
            self._current_command = command
            self._current_cmd_id = str(cmd_id)
            self._sentinel_prefix = _COMMAND_END_MARKER + token
            self._command_finished.clear()
        print(f"Running command {cmd_id}/{len(self._commands)} on {self._client_id}: {command}")
        self._write_stdin(command + _SHELL_LINE_END)
        if self._shell_closed:
            return False
        self._write_stdin(_shell_end_marker_line(token))
        self._command_finished.wait()
        return not self._shell_closed

    def _read_stream(self, stream_name):
        """Capture one of the shell's output streams until the shell closes it.

        Args:
            stream_name (str): "stdout" or "stderr".
        """
        stream = self._proc.stdout if stream_name == "stdout" else self._proc.stderr
        decoder = codecs.getincrementaldecoder("utf-8")("replace")

        def capture(raw):
            text = decoder.decode(raw)
            with self._log_lock:
                self._pending_bytes += len(raw)
                self._capture_locked(stream_name, text)
                self._maybe_flush_locked()

        while True:
            raw = stream.read(_READ_SIZE)
            if not raw:
                break
            capture(raw)
        capture(b"")  # flush a character the decoder still holds
        if stream_name == "stdout":
            self._on_shell_closed()

    def _capture_locked(self, stream_name, text):
        """Log captured text; stdout is screened for the command end markers."""
        if stream_name == "stdout":
            self._consume_stdout_locked(text)
        elif text:
            self._emit_locked("stderr", text)

    def _consume_stdout_locked(self, text):
        """Log stdout text, stripping the end markers of finished commands."""
        self._stdout_buffer += text
        if self._echoed_lines:
            self._stdout_buffer = _without_echoed_lines(self._stdout_buffer, self._echoed_lines)
        while self._stdout_buffer:
            if not self._sentinel_prefix:
                self._emit_locked("stdout", self._stdout_buffer)
                self._stdout_buffer = ""
                return
            position = self._stdout_buffer.find(self._sentinel_prefix)
            if position < 0:
                hold = _marker_prefix_suffix(self._stdout_buffer, self._sentinel_prefix)
                end = len(self._stdout_buffer) - hold
                if end <= 0:
                    return
                self._emit_locked("stdout", self._stdout_buffer[:end])
                self._stdout_buffer = self._stdout_buffer[end:]
                return
            line_start = self._stdout_buffer.rfind("\n", 0, position) + 1
            if line_start != position:
                # the marker sits inside a line, and an end marker always starts
                # its own line: this is text of the command's output, or a
                # control line the shell echoed back
                if self._marker_line_may_be_echoed(position, line_start):
                    return  # log nothing until that line is complete
                end = position + len(self._sentinel_prefix)
                self._emit_locked("stdout", self._stdout_buffer[:end])
                self._stdout_buffer = self._stdout_buffer[end:]
                continue
            if position > 0:  # the marker starts its line, but earlier output precedes it
                self._emit_locked("stdout", self._stdout_buffer[:position])
                self._stdout_buffer = self._stdout_buffer[position:]
                continue
            remainder = self._stdout_buffer[len(self._sentinel_prefix) :]
            match = re.match(r" (\d+)\r?\n", remainder)
            if match:
                self._stdout_buffer = remainder[match.end() :]
                self._returncode = int(match.group(1))
                self._emit_locked("end", "")
                self._maybe_flush_locked(force=True)
                self._command_finished.set()
                continue
            if _could_still_be_marker_tail(remainder):  # still arriving
                return
            if not any(end in remainder for end in ("\n", "\r")):
                return  # the line the marker starts is not complete yet
            # the marker text came from the command's own output: log it and scan on
            self._emit_locked("stdout", self._sentinel_prefix)
            self._stdout_buffer = remainder

    def _marker_line_may_be_echoed(self, position, line_start):
        """Report whether the text ahead of a marker may still become an echoed line.

        Args:
            position (int): Offset of the marker inside ``_stdout_buffer``.
            line_start (int): Offset of the first character of the marker's line.

        Returns:
            bool: True while that text is a prefix of a line written to the
                shell's stdin and the line is not complete yet; logging it now
                would put a control line the shell echoed back into the log.
        """
        if not self._echoed_lines:
            return False
        head = self._stdout_buffer[line_start:position]
        if not any(echo.startswith(head) for echo in self._echoed_lines):
            return False
        return not any(end in self._stdout_buffer[position:] for end in ("\n", "\r"))

    def _emit_locked(self, stream, text):
        """Append one log entry for captured text; the caller holds the log lock.

        Args:
            stream (str): "stdout", "stderr", or "end" for the entry that closes
                a command and carries its exit status.
            text (str): Captured text; empty for an "end" entry.
        """
        entry = {
            "timestamp": datetime.now().isoformat(),
            "command": self._current_command,
            "cmd_id": self._current_cmd_id,
            "client_id": self._client_id,
            "client": str(self._client_address),
            "from": self._peer_id,
            "stream": stream,
            "output": text if stream == "stdout" else "",
            "error": text if stream == "stderr" else "",
            "returncode": self._returncode if stream == "end" else None,
        }
        self._pending.append(entry)

    def _maybe_flush_locked(self, force=False):
        """Upload the queued entries once enough output was captured.

        Args:
            force (bool): Upload even when less than ``_LOG_CHUNK_BYTES`` was
                captured, which closes a command or the batch itself.
        """
        if not self._pending:
            return
        if not force and self._pending_bytes < _LOG_CHUNK_BYTES:
            return
        self._flush_locked()

    def _flush_locked(self):
        """Upload the queued entries as one log chunk the server appends.

        Returns:
            bool: True when the server received the chunk; entries that could
                not be delivered stay queued for the next flush.
        """
        entries = self._pending
        flushed_bytes = self._pending_bytes
        self._pending = []
        self._pending_bytes = 0
        self._chunk_seq += 1
        base_name = os.path.splitext(self._log_filename)[0]
        chunk_path = os.path.join(self._log_dir, f"{base_name}_chunk{self._chunk_seq}.json")
        chunk_filename = os.path.basename(chunk_path)
        _write_json_file(chunk_path, _entries_by_command(entries))
        sent = client_instance.file_transfer_client_recv_client_start(
            message=f'/file "{chunk_path}"', file_folder_abspath=None
        )
        if sent is False:
            self._requeue_locked(
                entries, flushed_bytes, f"chunk {chunk_filename} was not delivered"
            )
            return False
        try:
            client_instance.send_message(
                client_socket=client_instance.client_socket,
                message=f'/command_log_chunk "{chunk_filename}" "{self._log_filename}"\n',
            )
        except OSError as e:
            self._requeue_locked(
                entries, flushed_bytes, f"chunk {chunk_filename} was not announced ({e})"
            )
            return False
        try:
            os.remove(chunk_path)
        except OSError:
            pass
        print(f"Log chunk {chunk_filename} appended to {self._log_filename} on the server")
        return True

    def _requeue_locked(self, entries, flushed_bytes, reason):
        """Keep the entries of a chunk the server did not receive, oldest first.

        Args:
            entries (list[dict]): Entries that were sent but not delivered.
            flushed_bytes (int): Captured size those entries accounted for.
            reason (str): Console text naming why the chunk did not arrive.
        """
        self._pending = entries + self._pending
        self._pending_bytes += flushed_bytes
        print(f"ErrorWhileAppendingTheLogChunk: {reason}, its {len(entries)} entries stay queued")

    def _flush_remaining(self):
        """Upload every entry still queued, retrying an upload that failed."""
        with self._log_lock:
            for attempt in range(_FLUSH_ATTEMPTS):
                if not self._pending:
                    return
                if self._flush_locked():
                    return
                if attempt + 1 < _FLUSH_ATTEMPTS:
                    time.sleep(1)
            print(
                f"ErrorWhileAppendingTheLogChunk: the last {len(self._pending)} entries "
                f"of {self._log_filename} were not delivered"
            )

    def _report_done(self):
        """Tell the server the batch is over, so it merges the collected logs."""
        try:
            client_instance.send_message(
                client_socket=client_instance.client_socket,
                message=f'/command_done "{self._log_filename}"\n',
            )
        except OSError as e:
            print(f"ErrorWhileReportingTheCommandDone: {e}")
            return
        print(f"Command log {self._log_filename} is complete on the server")
        print("Dealing the command successfully!")

    def _log_leftover_output(self):
        """Log the stdout text the stream end left in the buffer.

        The scanner holds a line back while it may still grow into a marker or
        into a line the shell echoed back; text still buffered when the shell
        stops is command output and is logged here instead of being dropped.
        """
        with self._log_lock:
            leftover = self._stdout_buffer
            self._stdout_buffer = ""
            if self._echoed_lines:
                leftover = _without_echoed_lines(leftover, self._echoed_lines)
            if leftover:
                self._emit_locked("stdout", leftover)

    def _close_shell(self):
        """Close the shell's stdin and let the readers drain its output."""
        if self._proc is None:
            return
        try:
            self._proc.stdin.close()
        except OSError:
            pass
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        for reader in self._readers:
            reader.join(timeout=5)

    def _write_stdin(self, text):
        """Write one shell line, treating a dead shell as a closed session."""
        try:
            if _shell_echoes_stdin():  # those lines come back mixed into the output
                self._echoed_lines.add(text.rstrip("\r\n"))
            self._proc.stdin.write(text.encode("utf-8"))
            self._proc.stdin.flush()
        except OSError as e:
            print(f"ErrorWhileWritingToTheCommandShell: {e}")
            self._on_shell_closed()

    def _on_shell_closed(self):
        """Report that the shell stopped, releasing the command waiting for it."""
        self._shell_closed = True
        self._command_finished.set()


def _setup_command():
    if server_instance is None:
        print("control extension: server instance is not set up")
        return
    print("Setting up server command...")
    server_instance.register_command(
        command_name="/command", handler=_command_handler, where_to_run="client", run_in_thread=True
    )
    server_instance.register_command(
        command_name="/command_log_chunk",
        handler=_command_log_chunk_dealing_server,
        where_to_run="server",
    )
    server_instance.register_command(
        command_name="/command_done",
        handler=_command_done_dealing_server,
        where_to_run="server",
    )


def _setup_client_command():
    if client_instance is None:
        print("control extension: client instance is not set up")
        return
    print("Setting up client command...")
    client_instance.register_command(
        command_name="/command",
        handler=_command_handler_server_setup,
        where_to_run="server",
        run_in_thread=True,
    )


def _command_handler(sock, addr, cmd):
    """Send one command batch from the server console to every listed client.

    Console syntax: ``/command <command...> <client address...>``. Every client
    runs the whole batch in one shell session, so the commands of one batch
    share their environment (a ``cd`` carries over to the next command).
    """
    if server_instance is None:
        print("control extension: server instance is not set up")
        return
    print(f"Received command from {addr}: {cmd}")
    try:
        tokens = shlex.split(cmd)[1:]
    except ValueError as e:
        print(f"Error parsing command: {e}")
        return
    commands, clients = _split_commands_and_clients(tokens)
    if not commands or not clients:
        print(
            'Invalid command format, use /command "command_1" ... "command_n" "(\'ip\', port)" ...'
        )
        return
    for client in clients:
        client_info = server_instance.clients.get(client)
        if client_info is None:
            print(f"command: client {client} is not connected, skipped")
            continue
        message = "/command " + " ".join(
            [shlex.quote(str(client))] + [shlex.quote(command) for command in commands]
        )
        server_instance.send_message(client_socket=client_info["socket"], message=message + "\n")
        print(f"Sending commands {commands} to client {client}")


def _command_handler_server_setup(sock, addr, cmd):
    """Run one command batch on this client and stream its log to the server.

    The wire format is ``/command <(ip, port)> <command_1> ... <command_n>``.
    Every command runs in one shell session, in order, and the captured output
    is pushed to the server as log chunks.
    """
    print(f"Received command from {addr}: {cmd}")
    try:
        cmd_parts = shlex.split(cmd)
    except ValueError as e:
        print(f"Error parsing command: {e}")
        return
    if len(cmd_parts) < 3 or cmd_parts[0] != "/command":
        print("Invalid command format.")
        return
    client_address = _parse_client_address(cmd_parts[1])
    if client_address is None:
        print(f"Invalid command format, '{cmd_parts[1]}' is not a client address.")
        return
    if client_instance is None or client_instance.client_socket is None:
        print("control extension: client instance is not set up")
        return
    with _batch_lock(client_address):
        _CommandBatch(client_address, cmd_parts[2:], addr).run()


def _command_log_chunk_dealing_server(sock, addr, cmd):
    """Append one streamed log chunk to its client's log on the server.

    The chunk arrives through the file transfer channel; its entries are
    appended to the log file the client's earlier chunks were appended to, so a
    growing log is never re-sent and never rewritten as a whole.
    """
    if server_instance is None:
        print("control extension: server instance is not set up")
        return
    try:
        cmd_parts = shlex.split(cmd)
        chunk_filename = os.path.basename(cmd_parts[1])
        log_filename = os.path.basename(cmd_parts[2])
    except (IndexError, ValueError) as e:
        print(f"Invalid command_log_chunk format: {e}")
        return
    received_path = os.path.join(server_instance.file_transfer_dir, chunk_filename)
    log_path = os.path.join(_logs_dir(), log_filename)
    if not os.path.exists(received_path):
        print(f"ErrorWhileAppendingTheLogChunk: chunk {chunk_filename} was not received.")
        return
    with command_log_lock:
        try:
            log_data = _merge_log_dicts(_load_json_file(log_path), _load_json_file(received_path))
            _write_json_file(log_path, log_data)
        except Exception:
            traceback.print_exc()
            print("ErrorWhileAppendingTheLogChunk: appending the log chunk failed.")
            return
    try:
        os.remove(received_path)
    except OSError:
        pass
    print(f"Log chunk {chunk_filename} appended to {log_path}")


def _command_done_dealing_server(sock, addr, cmd):
    """Merge every collected log file once a client finished its batch."""
    if server_instance is None:
        print("control extension: server instance is not set up")
        return
    try:
        cmd_parts = shlex.split(cmd)
        log_filename = os.path.basename(cmd_parts[1])
    except (IndexError, ValueError) as e:
        print(f"Invalid command_done format: {e}")
        return
    log_dir = _logs_dir()
    os.makedirs(log_dir, exist_ok=True)
    try:
        _merge_all_logs(log_dir)
    except Exception:
        traceback.print_exc()
        print("ErrorWhileMergingTheLogFile: merging the collected logs failed.")
        return
    print(f"Log of {log_filename} collected, all logs merged into merged_logs.json")
    print("Command Done!")


def setup_server_commands(server):
    """Register the control-extension commands on a server instance."""
    global server_instance  # noqa: PLW0603
    server_instance = server
    _setup_command()


def setup_client_commands(client):
    """Register the control-extension commands on a client instance."""
    global client_instance  # noqa: PLW0603
    client_instance = client
    _setup_client_command()


def client_setup(instance=None, is_input_command_in_console=True):
    global client_instance
    if instance is None:
        client_instance = connect_tcp.TCP_Client_Base(
            host="127.0.0.1",
            port=65000,
            client_host="127.0.0.1",
            is_input_command_in_console=is_input_command_in_console,
            is_extend_command=True,
        )
    else:
        client_instance = instance
    setup_client_commands(client_instance)
    if is_input_command_in_console:
        client_instance.start_TCP_client()
    else:
        threading.Thread(target=client_instance.start_TCP_client, daemon=True).start()


def server_setup(instance=None, is_input_command_in_console=True):
    global server_instance
    if instance is None:
        server_instance = connect_tcp.TCP_Server_Base(
            host="127.0.0.1",
            port=65000,
            max_clients=10,
            is_input_command_in_console=is_input_command_in_console,
            is_extend_command=True,
        )
    else:
        server_instance = instance
    setup_server_commands(server_instance)
    if is_input_command_in_console:
        server_instance.start_TCP_Server()
    else:
        threading.Thread(target=server_instance.start_TCP_Server, daemon=True).start()
