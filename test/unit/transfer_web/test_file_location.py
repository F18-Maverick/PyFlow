"""Chat file links: a transferred file opens its location through the backend.

Every file a backend receives or hands to a transfer is recorded under an
opaque token; the chat message links the file name to ``/api/open_location``,
which passes the recorded path to the file manager of the host running the
backend.  The frontend never names a filesystem path itself.
"""

import io
import json
import os
import threading
from types import SimpleNamespace
from urllib.parse import quote

import pytest

from PyFlow.transfer_web.web_backend import server_backend
from PyFlow.transfer_web.web_front import client_backend


class FakeSocket:
    """Socket stand-in exposing the bound address of a client."""

    def getsockname(self):
        return ("127.0.0.1", 40000)


class FakeTcpServer:
    """TCP_Server_Base stand-in recording registrations and pushes."""

    def __init__(self, root):
        self.running = True
        self.host = "127.0.0.1"
        self.port = 65432
        self.clients = {}
        self.client_lock = threading.Lock()
        self.file_transfer_dir = str(root / "received_files")
        self.pushes = []

    def register_command(self, name, handler, where_to_run="server", run_in_thread=False):
        """Accept one registered command."""

    def send_message(self, sock, message):
        """Accept one line written to a client."""
        return True

    def file_transfer_server_recv_client_start(self, message, file_folder_abspath=None):
        """Record one file push."""
        self.pushes.append(("file", message))

    def folder_file_transfer_server_recv_client_start(self, message):
        """Record one folder push."""
        self.pushes.append(("folder", message))


class FakeTcpClient:
    """TCP_Client_Base stand-in recording what the backend sends."""

    def __init__(self, root):
        self.running = True
        self.host = "127.0.0.1"
        self.port = 65432
        self.client_host = "127.0.0.1"
        self.client_port = 40000
        self.client_socket = FakeSocket()
        self.file_transfer_dir = str(root / "received_files")
        self._crypto_lock = threading.Lock()
        self._encrypted_sockets = set()
        self.sent = []
        self.pushes = []

    def file_transfer_client_recv_client_start(self, message, file_folder_abspath=None):
        """Record one file sent to the server."""
        self.pushes.append(("file", message))

    def folder_file_transfer_client_recv_client_start(self, message):
        """Record one folder sent to the server."""
        self.pushes.append(("folder", message))


@pytest.fixture
def revealed(monkeypatch):
    """Record every path handed to the file manager instead of launching one."""
    calls = []
    for module in (server_backend, client_backend):
        monkeypatch.setattr(module, "_reveal_in_file_manager", calls.append)
    return calls


@pytest.fixture
def web(tmp_path, monkeypatch):
    """A ServerWebApp with a fake TCP server and its files under ``tmp_path``."""
    monkeypatch.setattr(server_backend, "FLOW_WEB_DIR", str(tmp_path))
    monkeypatch.setattr(server_backend, "SECRET_KEY_FILE", str(tmp_path / "web_secret_key"))
    monkeypatch.setattr(server_backend, "UPLOAD_DIR", str(tmp_path / "uploads"))
    app = server_backend.ServerWebApp(
        db_path=str(tmp_path / "flow_web.db"),
        mail_config_path=str(tmp_path / "email_config.json"),
    )
    app.app.config.update(TESTING=True)
    app.server = FakeTcpServer(tmp_path)
    app.mode = "status"
    yield app
    app.users.close()


@pytest.fixture
def client(web):
    return web.app.test_client()


@pytest.fixture
def client_web(tmp_path, monkeypatch):
    """A ClientWebApp with a fake TCP client; returns ``(app, flask client)``."""
    monkeypatch.setattr(client_backend, "FLOW_WEB_DIR", str(tmp_path))
    monkeypatch.setattr(client_backend, "UPLOAD_DIR", str(tmp_path / "uploads"))
    app = client_backend.ClientWebApp(web_port=5099)
    app.app.config.update(TESTING=True)
    app.connected = True
    app.client = FakeTcpClient(tmp_path)
    return app, app.app.test_client()


def login(client, identify="admin", password="admin"):
    return client.post("/api/login", json={"identify": identify, "password": password})


def open_location(client, token):
    return client.post("/api/open_location", json={"token": token})


# ---- received files ----------------------------------------------------------


def test_server_file_event_carries_a_resolvable_token(web, client, revealed, tmp_path):
    """A file received by the server links its name to the saved location."""
    saved = tmp_path / "received_files" / "note.txt"
    saved.parent.mkdir(parents=True)
    saved.write_text("note", encoding="utf-8")
    web._on_incoming_file("127.0.0.1:40000", str(saved), "note.txt", 4, f"/file {saved}")

    login(client)
    event = client.get("/api/events?since=0").get_json()["events"][-1]
    assert event["type"] == "file"
    assert event["path"] == "note.txt"  # relative to the transfer folder
    assert open_location(client, event["open_token"]).get_json() == {"ok": True}
    assert revealed == [str(saved)]


def test_client_file_event_carries_a_resolvable_token(client_web, revealed, tmp_path):
    """A file received by a web client links its name to the saved location."""
    app, client = client_web
    saved = tmp_path / "received_files" / "sub" / "beta.bin"
    saved.parent.mkdir(parents=True)
    saved.write_bytes(b"beta")
    app._on_incoming_file(str(saved), "beta.bin", 4, f"/file {saved}")

    event = client.get("/api/events?since=0").get_json()["events"][-1]
    assert event["name"] == "beta.bin"
    assert event["path"] == os.path.join("sub", "beta.bin")
    assert open_location(client, event["open_token"]).get_json() == {"ok": True}
    assert revealed == [str(saved)]


# ---- sent files --------------------------------------------------------------


def test_sent_file_links_the_staged_copy(client_web, revealed, tmp_path):
    """The sender's message links the uploaded copy the transfer actually sent."""
    _app, client = client_web
    resp = client.post(
        "/api/send_file",
        data={"target": json.dumps("server"), "files": (io.BytesIO(b"note"), "note.txt")},
        content_type="multipart/form-data",
    )
    items = resp.get_json()["open_items"]
    assert [item["name"] for item in items] == ["note.txt"]
    staged = tmp_path / "uploads" / "note.txt"
    assert staged.read_bytes() == b"note"

    assert open_location(client, items[0]["token"]).get_json() == {"ok": True}
    assert revealed == [str(staged)]


def test_sent_folder_links_the_staged_tree(client_web, revealed, tmp_path):
    """A sent folder links the staged folder the transfer walked through."""
    _app, client = client_web
    resp = client.post(
        "/api/send_folder",
        data={"target": json.dumps("server"), "files": (io.BytesIO(b"leaf"), "tree/sub/leaf.txt")},
        content_type="multipart/form-data",
    )
    items = resp.get_json()["open_items"]
    assert [item["name"] for item in items] == ["tree"]
    staged = tmp_path / "uploads" / "tree"
    assert (staged / "sub" / "leaf.txt").read_bytes() == b"leaf"

    assert open_location(client, items[0]["token"]).get_json() == {"ok": True}
    assert revealed == [str(staged)]


def test_server_send_file_returns_the_staged_location(web, client, revealed, tmp_path):
    """The server page links its own uploaded copy the same way."""
    web.server.clients[("127.0.0.1", 40000)] = {"socket": FakeSocket()}
    login(client)
    resp = client.post(
        "/api/send_file",
        data={
            "target": json.dumps(["127.0.0.1", 40000]),
            "files": (io.BytesIO(b"note"), "note.txt"),
        },
        content_type="multipart/form-data",
    )
    items = resp.get_json()["open_items"]
    assert [item["name"] for item in items] == ["note.txt"]
    staged = tmp_path / "uploads" / "note.txt"
    assert staged.read_bytes() == b"note"

    assert open_location(client, items[0]["token"]).get_json() == {"ok": True}
    assert revealed == [str(staged)]


# ---- refusals and bounds -----------------------------------------------------


def test_open_location_refuses_unknown_and_gone_paths(web, client, revealed, tmp_path):
    """A token that names no recorded file, or one that vanished, is refused."""
    login(client)
    unknown = open_location(client, "does-not-exist")
    assert unknown.status_code == 404
    assert unknown.get_json()["error"] == "unknown file location"

    gone = web._register_open_target(str(tmp_path / "gone.txt"))
    resp = open_location(client, gone)
    assert resp.status_code == 404
    assert resp.get_json()["error"] == "the file is no longer at its location"
    assert revealed == []


def test_open_location_reports_a_desktop_that_cannot_be_asked(web, client, monkeypatch, tmp_path):
    """A host without a usable file-manager command answers with an error."""

    def refuse(path):
        raise OSError("no desktop")

    monkeypatch.setattr(server_backend, "_reveal_in_file_manager", refuse)
    saved = tmp_path / "received_files" / "note.txt"
    saved.parent.mkdir(parents=True)
    saved.write_text("note", encoding="utf-8")
    token = web._register_open_target(str(saved))

    login(client)
    resp = open_location(client, token)
    assert resp.status_code == 500
    assert "cannot open the file manager: no desktop" in resp.get_json()["error"]


def test_the_token_table_stays_bounded(client_web):
    """Old tokens are dropped, so a long session cannot grow the table forever."""
    app, _client = client_web
    for _ in range(1001):
        app._register_open_target("/tmp/irrelevant")
    assert len(app._open_targets) == 1000
    assert "1" not in app._open_targets


# ---- file manager commands ---------------------------------------------------


@pytest.mark.parametrize(
    "backend", [server_backend, client_backend], ids=["server", "client"]
)
def test_reveal_helper_picks_the_platform_command(backend, tmp_path, monkeypatch):
    """Windows selects the entry, macOS reveals it, Linux asks the desktop bus."""
    target = tmp_path / "note.txt"
    target.write_text("note", encoding="utf-8")
    popen = []
    runs = []
    monkeypatch.setattr(backend.subprocess, "Popen", lambda argv, **kwargs: popen.append(argv))
    monkeypatch.setattr(
        backend.subprocess,
        "run",
        lambda argv, **kwargs: runs.append(argv) or SimpleNamespace(returncode=0),
    )

    monkeypatch.setattr(backend.os, "name", "nt")
    backend._reveal_in_file_manager(str(target))
    assert popen == [f'explorer /select,"{target}"']

    popen.clear()
    monkeypatch.setattr(backend.os, "name", "posix")
    monkeypatch.setattr(backend.sys, "platform", "darwin")
    backend._reveal_in_file_manager(str(target))
    assert popen == [["open", "-R", str(target)]]

    popen.clear()
    monkeypatch.setattr(backend.sys, "platform", "linux")
    backend._reveal_in_file_manager(str(target))
    # the whole call is pinned: dbus-send defaults to --type=signal, which a
    # file manager ignores, and the URI must carry the percent-encoded path
    assert runs[0] == [
        "dbus-send",
        "--session",
        "--dest=org.freedesktop.FileManager1",
        "--type=method_call",
        "/org/freedesktop/FileManager1",
        "org.freedesktop.FileManager1.ShowItems",
        f"array:string:file://{quote(str(target))}",
        "string:",
    ]
    assert popen == []  # the desktop bus selected the entry, nothing else to do

    popen.clear()
    monkeypatch.setattr(
        backend.subprocess, "run", lambda argv, **kwargs: SimpleNamespace(returncode=1)
    )
    backend._reveal_in_file_manager(str(target))
    assert popen == [["xdg-open", str(tmp_path)]]  # fallback: open the folder