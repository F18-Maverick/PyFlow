"""Integration tests for the transfer reconnect (resume) mechanism.

A transfer interrupted by a disconnect keeps its progress entry and its
partial file; both sides write the entries to ``.Flow`` when the connection
drops, and after a reconnect the receiver asks the sender to continue the file
from the bytes it already holds. The tests drive that exchange over real
sockets, with the interrupted state either produced by a real disconnected
session or loaded from the persisted file exactly as a disconnect leaves it.
"""

import json
import os
import shlex
import socket
import threading
import time

from helpers import free_port, isolate_instance_files, server_ready, wait_until

from PyFlow.network_api.connect_tcp import TCP_Client_Base, TCP_Server_Base


def _isolate(instance, tmp_path, name):
    """Keep an instance's logs and its transfer progress file out of the repo."""
    isolate_instance_files(instance, tmp_path, name)


def _wait_port(instance, timeout=10.0):
    """Wait for the transfer port the instance's peer announced."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with instance.file_transfer_server_port_lock:
            ports = list(instance.file_server_port_list)
        if ports:
            return ports[0][0]
        time.sleep(0.05)
    return None


def _redirect_crypto(crypto, tmp_path, ssh_dir, subdir="pub_key"):
    """Keep one instance's key material inside the test's temp directory."""
    crypto.pvt_key_dir = str(tmp_path / "pvt_key")
    crypto.pub_key_dir = str(tmp_path / subdir)
    crypto.ssh_dir = str(ssh_dir)
    crypto.registry_path = os.path.join(crypto.pub_key_dir, "pub_key.json")
    os.makedirs(crypto.pvt_key_dir, exist_ok=True)
    os.makedirs(crypto.pub_key_dir, exist_ok=True)


def _start_server(tmp_path, name="server", **kwargs):
    port = free_port()
    server = TCP_Server_Base(
        host="127.0.0.1",
        port=port,
        is_extend_command=True,
        is_input_command_in_console=False,
        is_enable_encrypto=False,
        **kwargs,
    )
    _isolate(server, tmp_path, name)
    threading.Thread(target=server.start_TCP_Server, daemon=True).start()
    assert server_ready(server), "server did not start"
    return server


def _start_client(tmp_path, server, name="client", recv_dir=None, **kwargs):
    client = TCP_Client_Base(
        host="127.0.0.1",
        port=server.port,
        client_host="127.0.0.1",
        is_extend_command=True,
        is_input_command_in_console=False,
        is_enable_encrypto=False,
        **kwargs,
    )
    _isolate(client, tmp_path, name)
    if recv_dir is not None:
        client.file_transfer_dir = str(recv_dir)
    assert client.connect()
    return client


def test_progress_entries_round_trip_through_the_progress_file(tmp_path):
    """Both stores are written with their peer tuples and read back intact."""
    server = _start_server(tmp_path)
    try:
        server.file_recv_progress[7] = [128, 1024, None, "/tmp/part.bin", ("127.0.0.1", 5000)]
        server.file_upload_progress[9] = [64, 512, None, "/tmp/src.bin", ("127.0.0.1", 6000)]
        server._persist_transfer_progress()

        with open(server.file_trans_progress_file, "r", encoding="utf-8") as f:
            written = json.load(f)
        assert written["download"]["7"] == [
            128,
            1024,
            None,
            "/tmp/part.bin",
            ["127.0.0.1", 5000],
        ]
        assert written["upload"]["9"] == [64, 512, None, "/tmp/src.bin", ["127.0.0.1", 6000]]

        server.file_recv_progress.clear()
        server.file_upload_progress.clear()
        assert server._load_transfer_progress() == 2  # noqa: PLR2004
        assert server.file_recv_progress[7] == [
            128,
            1024,
            None,
            "/tmp/part.bin",
            ("127.0.0.1", 5000),
        ]
        assert server.file_upload_progress[9] == [
            64,
            512,
            None,
            "/tmp/src.bin",
            ("127.0.0.1", 6000),
        ]

        # a live transfer owns its fid: a load must not overwrite it
        server.file_recv_progress.clear()
        server.file_upload_progress.clear()
        server.file_recv_progress[7] = [999, 1024, None, "/tmp/live.bin", None]
        assert server._load_transfer_progress() == 1
        assert server.file_recv_progress[7] == [999, 1024, None, "/tmp/live.bin", None]
        assert server.file_upload_progress[9][0] == 64  # noqa: PLR2004
    finally:
        server.stop()


def test_paused_transfer_is_persisted_when_the_connection_drops(tmp_path):
    """A disconnect during a transfer writes both stores for the next connect.

    The uploader is held paused, so the in-flight state is deterministic: the
    receiver keeps its (empty) partial file, and both sides persist their
    entries — the receiver with the sender's address, the uploader with the
    receiver's — under the protocol's ``.Flow`` directory.
    """
    server = _start_server(tmp_path)
    server_recv = tmp_path / "server_recv"
    server_recv.mkdir()
    server.file_transfer_dir = str(server_recv)
    client = None
    try:
        client = _start_client(tmp_path, server)
        src = tmp_path / "payload.bin"
        payload = os.urandom(64 * 1024)
        src.write_bytes(payload)
        fid = 76  # even low bit: a plain leg's id

        server_sock = server.clients[client.client_socket.getsockname()]["socket"]
        sender_id = "%s:%s" % client.client_socket.getsockname()
        server.file_transfer_server_recv_server_start_thread(
            sender_id, server_sock, f"/file {src} {fid}"
        )
        assert _wait_port(client) is not None

        client.pause_trans(fid)  # hold the leg before its first chunk
        sender = threading.Thread(
            target=client.file_transfer_mode,
            args=(str(src), "127.0.0.1", _wait_port(client) or 0, 0),
            kwargs={"fid": fid, "peer": (server.host, server.port)},
            daemon=True,
        )
        sender.start()
        assert wait_until(lambda: server.file_recv_progress.get(fid)), (
            "the receiver never registered the transfer"
        )

        # drop the control connection the way a network fault would; the
        # receive loop persists the transfers that died with it
        client.client_socket.shutdown(2)
        assert wait_until(
            lambda: os.path.exists(client.file_trans_progress_file), timeout=10
        ), "the client did not persist its interrupted transfers"

        with open(client.file_trans_progress_file, "r", encoding="utf-8") as f:
            client_state = json.load(f)
        assert client_state["upload"][str(fid)][3] == str(src)
        assert client_state["upload"][str(fid)][4] == [server.host, server.port]

        assert wait_until(
            lambda: os.path.exists(server.file_trans_progress_file), timeout=10
        ), "the server did not persist its interrupted transfers"
        with open(server.file_trans_progress_file, "r", encoding="utf-8") as f:
            server_state = json.load(f)
        entry = server_state["download"][str(fid)]
        assert entry[3] == str(server_recv / "payload.bin")
        assert entry[4] == list(client.client_socket.getsockname())

        client.start_trans(fid)  # release the paused leg so the test can end
        sender.join(timeout=10)
    finally:
        if client is not None:
            client.close()
        server.stop()


def test_upload_resumes_after_a_reconnect(tmp_path):
    """A client upload interrupted by a disconnect finishes after reconnecting.

    The client reconnects automatically (``is_auto_reconnect``); the server
    reads the persisted state when it comes back and asks it to continue the
    file from the bytes already on disk — the partially transferred file
    completes without any byte being sent twice.
    """
    server = _start_server(tmp_path)
    server_recv = tmp_path / "server_recv"
    server_recv.mkdir()
    server.file_transfer_dir = str(server_recv)
    client = None
    try:
        src = tmp_path / "payload.bin"
        payload = os.urandom(256 * 1024)
        src.write_bytes(payload)
        fid = 100  # even low bit: a plain leg's id

        # a fixed client port keeps the client's address stable across the
        # reconnect, so the server can match the interrupted transfer to it
        client = TCP_Client_Base(
            host="127.0.0.1",
            port=server.port,
            client_host="127.0.0.1",
            client_port=free_port(),
            is_extend_command=True,
            is_input_command_in_console=False,
            is_enable_encrypto=False,
            is_auto_reconnect=True,
        )
        _isolate(client, tmp_path, "client")
        threading.Thread(target=client.start_TCP_client, daemon=True).start()
        assert wait_until(lambda: client.running and client.client_socket is not None)
        client_addr = client.client_socket.getsockname()

        # the server registers an accepted client on its own thread: wait for it
        assert wait_until(lambda: client_addr in server.clients, timeout=10), (
            "server never registered the reconnecting client"
        )
        server_sock = server.clients[client_addr]["socket"]
        client_id = "%s:%s" % client_addr
        server.file_transfer_server_recv_server_start_thread(
            client_id, server_sock, f"/file {shlex.quote(str(src))} {fid}"
        )
        transfer_port = _wait_port(client)
        assert transfer_port is not None, "the client never announced a transfer port"

        client.pause_trans(fid)  # freeze the leg, then give it a partial file
        sender = threading.Thread(
            target=client.file_transfer_mode,
            args=(str(src), "127.0.0.1", transfer_port, 0),
            kwargs={"fid": fid, "peer": (server.host, server.port)},
            daemon=True,
        )
        sender.start()
        assert wait_until(lambda: server.file_recv_progress.get(fid)), (
            "the receiver never registered the transfer"
        )
        partial = server_recv / "payload.bin"
        half = len(payload) // 2
        with open(partial, "wb") as f:  # what an interrupted transfer leaves behind
            f.write(payload[:half])

        # a fault drops the control connection: the server's side goes away,
        # the way a crash looks to the client. The client's own release then
        # aborts the dead session (shutdown + linger-0 close), which keeps its
        # configured port out of TIME_WAIT: the reconnect must be able to take
        # the same port back (Windows refuses it otherwise) and the server must
        # match the stored transfer to the same peer address.
        dead_socket = client.client_socket
        server_side = server.clients[client_addr]["socket"]
        server_side.shutdown(2)  # a read blocked in the server only wakes on a shutdown
        server_side.close()
        assert wait_until(lambda: dead_socket.fileno() == -1, timeout=20), (
            "the reconnect did not release the dead session's socket"
        )
        assert wait_until(lambda: server.clients.get(client_addr) is not None, timeout=20), (
            "the client did not come back"
        )
        assert client.client_socket.getsockname() == client_addr, (
            "the client came back from a different port"
        )

        def got():
            return partial.exists() and partial.stat().st_size == len(payload)

        assert wait_until(got, timeout=30), "the resumed upload did not finish"
        assert partial.read_bytes() == payload
        assert wait_until(lambda: fid not in server.file_recv_progress, timeout=10)
        assert wait_until(lambda: fid not in client.file_upload_progress, timeout=10)
        client.start_trans(fid)
        sender.join(timeout=10)
    finally:
        if client is not None:
            client.close()
        server.stop()


def test_download_resumes_from_the_partial_file(tmp_path):
    """The receiver drives a resume of a file the server was sending.

    The interrupted state is exactly what a disconnect leaves: the partial
    file on disk plus the persisted entry naming the server as the peer. The
    receiver asks the sender to continue from the partial file's length.
    """
    server = _start_server(tmp_path)
    client = None
    try:
        recv_dir = tmp_path / "client_recv"
        recv_dir.mkdir()
        client = _start_client(tmp_path, server, recv_dir=recv_dir)
        src = tmp_path / "download.bin"
        payload = os.urandom(128 * 1024)
        src.write_bytes(payload)
        fid = 202  # even low bit: a plain leg's id

        partial = recv_dir / "download.bin"
        with open(partial, "wb") as f:
            f.write(payload[:4096])
        client.file_recv_progress[fid] = [
            len(payload[:4096]),
            len(payload),
            None,
            str(partial),
            (server.host, server.port),
        ]
        server.file_upload_progress[fid] = [
            0,
            len(payload),
            None,
            str(src),
            client.client_socket.getsockname(),
        ]

        client._request_resumes()  # the same call connect() dispatches on reconnect

        def got():
            return partial.stat().st_size == len(payload)

        assert wait_until(got, timeout=20), "the resumed download did not finish"
        assert partial.read_bytes() == payload
        assert wait_until(lambda: fid not in client.file_recv_progress, timeout=10)
        assert wait_until(lambda: fid not in server.file_upload_progress, timeout=10)
    finally:
        if client is not None:
            client.close()
        server.stop()


def test_forwarded_file_resumes_through_the_server(tmp_path):
    """A target that lost a forwarded file gets the remainder re-sent.

    The target asks the server to continue the transfer; the server routes
    the request to the originator (its relay record names it) and the
    originator re-forwards only what the target is missing. Both the target's
    download entry and the originator's upload entry name the original peer,
    never the relay.
    """
    server = _start_server(tmp_path)
    recv_dir = tmp_path / "target_recv"
    recv_dir.mkdir()
    target = None
    forwarder = None
    try:
        target = _start_client(tmp_path, server, name="target", recv_dir=recv_dir)
        forwarder = _start_client(tmp_path, server, name="forwarder")
        src = tmp_path / "payload.bin"
        payload = os.urandom(96 * 1024)
        src.write_bytes(payload)
        fid = 303  # odd low bit: a relay leg's id
        tfid = 305  # odd low bit: a relay target's id
        target_addr = target.client_socket.getsockname()
        forwarder_addr = forwarder.client_socket.getsockname()

        # the state a disconnect leaves behind: the target's partial file and
        # both sides' persisted entries, plus the relay record the server
        # keeps for the dead relay
        partial = recv_dir / "payload.bin"
        with open(partial, "wb") as f:
            f.write(payload[:2048])
        target.file_recv_progress[tfid] = [
            2048,
            len(payload),
            None,
            str(partial),
            forwarder_addr,
        ]
        forwarder.file_upload_progress[fid] = [0, len(payload), None, str(src), target_addr]
        forwarder._forward_resume_records[fid] = {
            "fid": fid,
            "kind": "file",
            "rel_dir": "",
            "fname": "payload.bin",
            "path": str(src),
            "destination": None,
            "targets": [list(target_addr)],
        }
        server._forward_resume_records[tfid] = {
            "fid": fid,
            "originator": list(forwarder_addr),
            "targets": [list(target_addr)],
            "tfids": [tfid],
            "kind": "file",
            "rel_dir": "",
            "fname": "payload.bin",
            "destination": None,
        }

        assert target._request_forward_resume(
            tfid, target.file_recv_progress[tfid], forwarder_addr
        )

        def got():
            return partial.stat().st_size == len(payload)

        assert wait_until(got, timeout=20), "the forwarded resume did not finish"
        assert partial.read_bytes() == payload
        assert wait_until(lambda: tfid not in target.file_recv_progress, timeout=10)
        assert wait_until(lambda: fid not in forwarder.file_upload_progress, timeout=10)
    finally:
        if forwarder is not None:
            forwarder.close()
        if target is not None:
            target.close()
        server.stop()


def test_pause_and_start_trans_control_a_plain_transfer(tmp_path):
    """``pause_trans``/``start_trans`` hold and release any outbound leg.

    The receiver stops reading until the sender is released, so the pause
    works on a plain (non-forwarded) transfer: nothing arrives while the leg
    is held, and the file completes after ``start_trans``.
    """
    server = _start_server(tmp_path)
    server_recv = tmp_path / "server_recv"
    server_recv.mkdir()
    server.file_transfer_dir = str(server_recv)
    client = None
    try:
        client = _start_client(tmp_path, server)
        src = tmp_path / "payload.bin"
        payload = os.urandom(256 * 1024)
        src.write_bytes(payload)
        fid = 404  # even low bit: a plain leg's id

        server_sock = server.clients[client.client_socket.getsockname()]["socket"]
        server.file_transfer_server_recv_server_start_thread(
            "cid", server_sock, f"/file {src} {fid}"
        )
        assert _wait_port(client) is not None

        assert client.pause_trans(fid) is False  # armed before the leg starts
        sender = threading.Thread(
            target=client.file_transfer_mode,
            args=(str(src), "127.0.0.1", _wait_port(client) or 0, 0),
            kwargs={"fid": fid, "peer": (server.host, server.port)},
            daemon=True,
        )
        sender.start()
        assert wait_until(lambda: client.file_upload_progress.get(fid)), (
            "the uploader never registered the transfer"
        )
        time.sleep(0.5)
        output = server_recv / "payload.bin"
        assert not output.exists() or output.stat().st_size == 0, "the paused leg sent data"

        assert client.start_trans(fid) is True  # a live leg is released
        assert wait_until(
            lambda: output.exists() and output.stat().st_size == len(payload), timeout=20
        ), "the released transfer did not finish"
        assert output.read_bytes() == payload
        sender.join(timeout=10)
    finally:
        if client is not None:
            client.close()
        server.stop()


def test_download_resumes_over_the_encrypted_channel(tmp_path):
    """The resume exchange travels on the framed channel like any transfer line.

    With encryption enabled the resume drive waits for the handshake to finish
    before it writes ``/resume_trans``, so the request and the resumed leg
    both speak frames.
    """
    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir()
    port = free_port()
    server = TCP_Server_Base(
        host="127.0.0.1",
        port=port,
        is_extend_command=True,
        is_input_command_in_console=False,
        is_enable_encrypto=True,
    )
    _isolate(server, tmp_path, "server")
    _redirect_crypto(server.crypto, tmp_path, ssh_dir, "pub_key")
    threading.Thread(target=server.start_TCP_Server, daemon=True).start()
    assert server_ready(server), "server did not start"
    client = None
    try:
        recv_dir = tmp_path / "client_recv"
        recv_dir.mkdir()
        client = TCP_Client_Base(
            host="127.0.0.1",
            port=port,
            client_host="127.0.0.1",
            is_extend_command=True,
            is_input_command_in_console=False,
            is_enable_encrypto=True,
        )
        _isolate(client, tmp_path, "client")
        _redirect_crypto(client.crypto, tmp_path, ssh_dir, "pub_key_client")
        client.file_transfer_dir = str(recv_dir)
        assert client.connect()
        assert wait_until(lambda: client.client_socket in client._encrypted_sockets, timeout=15), (
            "the channel never flipped to frames"
        )

        src = tmp_path / "download.bin"
        payload = os.urandom(64 * 1024)
        src.write_bytes(payload)
        fid = 908  # even low bit: a plain leg's id
        partial = recv_dir / "download.bin"
        with open(partial, "wb") as f:
            f.write(payload[:1024])
        client.file_recv_progress[fid] = [
            1024,
            len(payload),
            None,
            str(partial),
            (server.host, server.port),
        ]
        server.file_upload_progress[fid] = [
            0,
            len(payload),
            None,
            str(src),
            client.client_socket.getsockname(),
        ]

        client._request_resumes()  # the same call connect() dispatches on reconnect
        assert wait_until(lambda: partial.stat().st_size == len(payload), timeout=20), (
            "the encrypted resume did not finish"
        )
        assert partial.read_bytes() == payload
        assert wait_until(lambda: fid not in client.file_recv_progress, timeout=10)
        assert wait_until(lambda: fid not in server.file_upload_progress, timeout=10)
    finally:
        if client is not None:
            client.close()
        server.stop()


def test_client_connects_when_its_configured_port_is_taken(tmp_path):
    """A configured client port that is held elsewhere does not stop the client.

    The port can be stuck after a session ends (TIME_WAIT on Windows), so the
    client must still connect — from an OS-assigned port — instead of never
    coming back.
    """
    server = _start_server(tmp_path)
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    taken = free_port()
    holder.bind(("127.0.0.1", taken))  # no SO_REUSEADDR: the bind must fail
    client = None
    try:
        client = TCP_Client_Base(
            host="127.0.0.1",
            port=server.port,
            client_host="127.0.0.1",
            client_port=taken,
            is_extend_command=True,
            is_input_command_in_console=False,
            is_enable_encrypto=False,
        )
        _isolate(client, tmp_path, "client")
        assert client.connect(), "the client did not connect around the taken port"
        assert client.client_socket.getsockname()[1] != taken, (
            "the client reported the port it could not bind"
        )
        assert wait_until(
            lambda: server.clients.get(client.client_socket.getsockname()) is not None
        ), "the server never saw the client"
    finally:
        holder.close()
        if client is not None:
            client.close()
        server.stop()


def test_resume_with_a_different_total_is_refused_and_keeps_the_partial_file(tmp_path):
    """A continued leg whose size disagrees with the progress entry is refused.

    The sender answers ``/resume_trans`` with a file whose length differs from
    the one the interrupted entry recorded. The receiver must refuse the leg and
    leave the partial bytes and the progress entry untouched, so a later
    reconnect still has what it needs to decide.
    """
    server = _start_server(tmp_path)
    client = None
    try:
        recv_dir = tmp_path / "client_recv"
        recv_dir.mkdir()
        client = _start_client(tmp_path, server, recv_dir=recv_dir)
        src = tmp_path / "download.bin"
        payload = os.urandom(128 * 1024)
        src.write_bytes(payload)
        fid = 302  # even low bit: a plain leg's id
        kept = payload[:4096]
        partial = recv_dir / "download.bin"
        with open(partial, "wb") as f:
            f.write(kept)
        stale_total = len(payload) + 1  # the entry recorded a different file of this name
        client.file_recv_progress[fid] = [
            len(kept),
            stale_total,
            None,
            str(partial),
            (server.host, server.port),
        ]
        server.file_upload_progress[fid] = [
            0,
            len(payload),
            None,
            str(src),
            client.client_socket.getsockname(),
        ]
        messages = []
        log = client._log

        def record(message):
            messages.append(message)
            log(message)

        client._log = record
        client._request_resumes()
        assert wait_until(lambda: any("resume rejected" in m for m in messages), timeout=20), (
            "the mismatched resume was not refused"
        )
        assert partial.read_bytes() == kept
        assert client.file_recv_progress[fid][1] == stale_total
    finally:
        if client is not None:
            client.close()
        server.stop()


def test_download_resumes_after_connect_without_a_manual_call(tmp_path):
    """A download interrupted by a disconnect resumes via the client's own reconnect.

    Nothing calls the resume driver by hand: the client reconnects
    (``is_auto_reconnect``) and its connect path asks the server to continue the
    file from the bytes already on disk. This covers the trigger itself, which
    the partial-file tests inject directly.
    """
    server = _start_server(tmp_path)
    recv_dir = tmp_path / "client_recv"
    recv_dir.mkdir()
    client = None
    try:
        src = tmp_path / "payload.bin"
        payload = os.urandom(32 * 1024 * 1024)  # still in flight when the fault hits
        src.write_bytes(payload)

        # a fixed client port keeps its address stable, so the server can match
        # the interrupted download to the reconnecting peer
        client = TCP_Client_Base(
            host="127.0.0.1",
            port=server.port,
            client_host="127.0.0.1",
            client_port=free_port(),
            is_extend_command=True,
            is_input_command_in_console=False,
            is_enable_encrypto=False,
            is_auto_reconnect=True,
        )
        _isolate(client, tmp_path, "client")
        client.file_transfer_dir = str(recv_dir)
        threading.Thread(target=client.start_TCP_client, daemon=True).start()
        assert wait_until(lambda: client.running and client.client_socket is not None)
        client_addr = client.client_socket.getsockname()
        assert wait_until(lambda: client_addr in server.clients, timeout=10), (
            "server never registered the client"
        )

        partial = recv_dir / src.name
        # the server stamps the leg's fid from its own allocator, and the pause
        # switch is keyed by fid: predict the id the next leg will take and arm
        # the pause before it starts, so the fault lands while the transfer is
        # still incomplete
        alloc = server._fid_alloc
        with alloc._lock:
            fid = ((alloc._counter + 1) << 32) | (alloc._epoch << 1)
        assert server.pause_trans(fid) is False, "the leg was already running"

        # the server pushes the file: the client opens its listener and the
        # server connects from a send leg
        server.file_transfer_server_recv_client_start_thread(
            "/file {} {}".format(shlex.quote(str(src)), shlex.quote(repr(client_addr)))
        )
        assert wait_until(lambda: server.file_upload_progress.get(fid), timeout=30), (
            "the server never registered the outgoing transfer"
        )
        # the leg is parked before its first chunk, so the receiver holds an
        # empty target; give it the bytes an interrupted transfer would have
        # left behind (the leg's own writes would only be the same file)
        assert wait_until(lambda: partial.exists(), timeout=20), (
            "the receiver never created the target file"
        )
        half = len(payload) // 2
        partial.write_bytes(payload[:half])

        # a fault drops the control connection from the server side; the parked
        # leg cannot finish the file, so the partial stays on disk
        dead_leg = server._active_transfer_sockets.get(fid)
        assert dead_leg is not None, "the send leg was not registered"
        dead_socket = client.client_socket
        server_sock = server.clients[client_addr]["socket"]
        server_sock.shutdown(2)
        server_sock.close()
        assert wait_until(lambda: dead_socket.fileno() == -1, timeout=20), (
            "the reconnect did not release the dead session's socket"
        )
        # the reconnecting client sweeps the dead session's legs and then asks
        # for the remainder; only the swept leg may be released, or it would
        # write from its own offset into the file the resumed leg continues
        assert wait_until(
            lambda: server._active_transfer_sockets.get(fid) is not dead_leg, timeout=20
        ), "the dead session's leg was never cleared"
        server.start_trans(fid)  # the leg was parked by the test, not by the protocol
        assert wait_until(lambda: server.clients.get(client_addr) is not None, timeout=20), (
            "the client did not come back"
        )
        assert client.client_socket.getsockname() == client_addr, (
            "the client came back from a different port"
        )

        assert wait_until(lambda: partial.stat().st_size == len(payload), timeout=30), (
            "the resumed download did not finish"
        )
        assert partial.read_bytes() == payload, "the resumed download did not continue the partial"
    finally:
        if client is not None:
            client.close()
        server.stop()