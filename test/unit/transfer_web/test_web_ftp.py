"""The web "ftp" share: server-side sharing/listing and the client browse path.

This is not the FTP protocol: the server exposes one folder of its own host, the
client asks for a listing over the protocol's own commands and the picked
entries are pushed with the native ``/file`` and ``/file_folder`` transfers. The
tests use the fake TCP instances of the neighbouring web test modules, so no
socket is opened and no crypto library is needed.
"""

import json
import os
import threading

import pytest

from PyFlow.transfer_web.web_backend import server_backend
from PyFlow.transfer_web.web_front import client_backend


class FakeSocket:
    """Socket stand-in exposing the bound address of the client."""

    def getsockname(self):
        """Return the local address of the pretend connection."""
        return ("127.0.0.1", 40000)


class FakeTcpServer:
    """TCP_Server_Base stand-in recording registrations, messages and pushes."""

    def __init__(self, clients=None):
        self.running = True
        self.host = "127.0.0.1"
        self.port = 65432
        self.is_enable_encrypto = False
        self.clients = dict(clients or {})
        self.client_lock = threading.Lock()
        self.file_transfer_dir = "/tmp"
        self.commands = {}  # (where_to_run, name) -> handler
        self.pushes = []  # ("file"|"folder", command)
        self.messages = []  # (socket, text)

    def register_command(self, name, handler, where_to_run="server", run_in_thread=False):
        """Record one registered command."""
        self.commands[(where_to_run, name)] = handler

    def send_message(self, sock, message):
        """Record one line written to a client."""
        self.messages.append((sock, message))
        return True

    def file_transfer_server_recv_client_start(self, message, file_folder_abspath=None):
        """Record one file push."""
        self.pushes.append(("file", message))

    def folder_file_transfer_server_recv_client_start(self, message):
        """Record one folder push."""
        self.pushes.append(("folder", message))

    def stop(self):
        """Drop the pretend connection."""
        self.running = False


class FakeTcpClient:
    """TCP_Client_Base stand-in that answers commands like the server would.

    ``replier(message)`` returns the reply line the server would send back (or
    None); the reply is dispatched to the handler this client registered for
    that command, exactly like the receive thread does on a real connection.
    """

    def __init__(self, replier=None):
        self.running = True
        self.is_enable_encrypto = False
        self.is_extend_command = False
        self.is_custom_keys = None
        self.is_input_command_in_console = False
        self.is_wait_server = True
        self.host = "127.0.0.1"
        self.port = 65432
        self.client_host = "127.0.0.1"
        self.client_port = 40000
        self.timeout = None
        self.port_add_step = 1
        self.max_thread_num = 10
        self.max_mem_buff = 2048 * 1024 * 1024
        self.client_socket = FakeSocket()
        self._crypto_lock = threading.Lock()
        self._encrypted_sockets = set()
        self.sent = []
        self.commands = {}
        self.replier = replier

    def register_command(self, name, handler, where_to_run="server", run_in_thread=False):
        """Record one handler for a line pushed by the server."""
        self.commands[name] = handler

    def send_message(self, client_socket, message):
        """Record one command and let the fake server answer it."""
        self.sent.append(message)
        reply = self.replier(message) if self.replier else None
        if reply:
            for name, handler in self.commands.items():
                if reply == name or reply.startswith(name + " "):
                    handler(self.client_socket, ("127.0.0.1", 65432), reply)
                    break
        return True

    def close(self):
        """Drop the pretend connection."""
        self.running = False


def _request_id(line):
    """Request id of one ``/<command> <id> <payload>`` line."""
    return line.split(" ", 2)[1]


@pytest.fixture
def web(tmp_path, monkeypatch):
    """A ServerWebApp with a fake TCP server and its files under ``tmp_path``."""
    monkeypatch.setattr(server_backend, "FLOW_WEB_DIR", str(tmp_path))
    monkeypatch.setattr(server_backend, "SECRET_KEY_FILE", str(tmp_path / "web_secret_key"))
    monkeypatch.setattr(server_backend, "SERVER_CONFIG_FILE", str(tmp_path / "setup_server.json"))
    app = server_backend.ServerWebApp(
        db_path=str(tmp_path / "flow_web.db"),
        mail_config_path=str(tmp_path / "email_config.json"),
    )
    app.app.config.update(TESTING=True)
    app.server = FakeTcpServer()
    app.mode = "status"
    app._register_ftp_commands()
    yield app
    app.users.close()


@pytest.fixture
def client(web):
    return web.app.test_client()


@pytest.fixture
def share(tmp_path):
    """A shared folder holding one file and one subfolder with one file."""
    root = tmp_path / "share"
    (root / "sub").mkdir(parents=True)
    (root / "alpha.txt").write_text("alpha", encoding="utf-8")
    (root / "sub" / "beta.bin").write_bytes(b"beta")
    return root


@pytest.fixture
def client_web(tmp_path, monkeypatch):
    """A ClientWebApp with a fake TCP client; returns ``(app, flask client)``."""
    monkeypatch.setattr(client_backend, "FLOW_WEB_DIR", str(tmp_path))
    monkeypatch.setattr(client_backend, "CLIENT_LOGIN_FILE", str(tmp_path / "client_login.json"))
    monkeypatch.setattr(
        client_backend, "CLIENT_LAST_SERVER_FILE", str(tmp_path / "client_last_server.json")
    )
    monkeypatch.setattr(client_backend, "CLIENT_CONFIG_FILE", str(tmp_path / "setup_client.json"))
    monkeypatch.setattr(
        client_backend, "CLIENT_EXTENSIONS_UI_FILE", str(tmp_path / "client_extensions_ui.json")
    )
    app = client_backend.ClientWebApp(web_port=5099)
    app.app.config.update(TESTING=True)
    app.connected = True
    app.client = FakeTcpClient()
    app._register_ftp_commands()
    return app, app.app.test_client()


def login(client, identify="admin", password="admin"):
    return client.post("/api/login", json={"identify": identify, "password": password})


# ---- configuration flags -----------------------------------------------------


def test_config_forms_list_the_new_flags(web, client, client_web):
    """Every new switch is editable in the web startup configuration."""
    server_keys = [key for key, *_ in server_backend.SERVER_PARAM_FIELDS]
    client_keys = [key for key, *_ in client_backend.CLIENT_PARAM_FIELDS]
    assert "is_asynic_clients_io" in server_keys
    assert "is_debug" in server_keys and "is_print_log" in server_keys
    assert "is_debug" in client_keys and "is_print_log" in client_keys
    # the client class has no coroutine mode: offering it would fail validation
    assert "is_asynic_clients_io" not in client_keys

    login(client)
    server_form = client.get("/config").get_data(as_text=True)
    for key in ("is_asynic_clients_io", "is_debug", "is_print_log"):
        assert f'id="f-{key}"' in server_form, key
    client_app, client_flask = client_web
    client_form = client_flask.get("/config").get_data(as_text=True)
    for key in ("is_debug", "is_print_log"):
        assert f'id="f-{key}"' in client_form, key
    assert 'id="f-is_asynic_clients_io"' not in client_form


# ---- server side -------------------------------------------------------------


def test_share_starts_empty_and_needs_an_admin(client, tmp_path):
    """Nothing is shared until an administrator picks a folder."""
    assert client.get("/api/ftp").status_code == 401  # anonymous
    login(client)
    body = client.get("/api/ftp").get_json()
    assert body == {"ok": True, "root": None, "shared": False}
    assert client.get("/api/ftp/list").status_code == 404
    added = client.post("/api/ftp/add", json={"path": str(tmp_path)})  # admin
    assert added.status_code == 200


def test_add_and_remove_the_shared_folder(web, client, share):
    """Adding a folder shares it, removing it takes the share away."""
    login(client)
    missing = client.post("/api/ftp/add", json={"path": str(share / "nope")})
    assert missing.status_code == 400
    assert "not a folder" in missing.get_json()["error"]

    added = client.post("/api/ftp/add", json={"path": str(share)})
    assert added.status_code == 200
    assert added.get_json()["root"] == str(share)
    status = client.get("/api/ftp").get_json()
    assert status["shared"] is True and status["root"] == str(share)
    # both protocol commands are registered on the running TCP server
    assert ("server", server_backend.FTP_LIST_COMMAND) in web.server.commands
    assert ("server", server_backend.FTP_GET_COMMAND) in web.server.commands

    listing = client.get("/api/ftp/list").get_json()["listing"]
    assert listing["path"] == "" and listing["parent"] is None
    assert [e["name"] for e in listing["entries"]] == ["sub", "alpha.txt"]  # folders first
    assert listing["entries"][1]["size"] == 5  # noqa: PLR2004
    assert client.get("/api/ftp/list?path=sub").get_json()["listing"]["parent"] == ""

    assert client.post("/api/ftp/remove").get_json() == {"ok": True}
    assert client.get("/api/ftp").get_json()["shared"] is False


def test_listing_refuses_paths_outside_the_share(web, client, share):
    """A share-relative path can never walk out of the shared folder."""
    login(client)
    client.post("/api/ftp/add", json={"path": str(share)})
    for escape in ("..", "../..", str(share.parent), "/etc"):
        response = client.get(f"/api/ftp/list?path={escape}")
        assert response.status_code == 400, escape
    handler = web.server.commands[("server", server_backend.FTP_LIST_COMMAND)]
    reply = handler(None, ("127.0.0.1", 5000), f"{server_backend.FTP_LIST_COMMAND} 9 ..")
    assert reply.startswith(f"{server_backend.FTP_ERROR_COMMAND} 9 ")


def test_protocol_listing_handler_answers_the_client(web, share):
    """``/ftp_list`` answers with the JSON listing of the requested folder."""
    handler = web.server.commands[("server", server_backend.FTP_LIST_COMMAND)]
    reply = handler(None, ("127.0.0.1", 5000), f"{server_backend.FTP_LIST_COMMAND} 3 ")
    assert reply.startswith(f"{server_backend.FTP_ERROR_COMMAND} 3 no folder is shared")

    with web._ftp_lock:
        web.ftp_root = str(share)
    # the web client sends the folder as a JSON string; a raw path works too
    json_path = f'{server_backend.FTP_LIST_COMMAND} 4 "sub"'
    raw_path = f"{server_backend.FTP_LIST_COMMAND} 5 sub"
    for request in (json_path, raw_path):
        reply = handler(None, ("127.0.0.1", 5000), request)
        command, _, payload = reply.split(" ", 2)
        assert command == server_backend.FTP_LIST_OK_COMMAND, reply
        listing = json.loads(payload)
        assert listing["path"] == "sub" and listing["parent"] == ""
        assert [e["name"] for e in listing["entries"]] == ["beta.bin"]
    root_reply = handler(None, ("127.0.0.1", 5000), f'{server_backend.FTP_LIST_COMMAND} 6 ""')
    assert json.loads(root_reply.split(" ", 2)[2])["path"] == ""


def test_protocol_download_handler_pushes_the_selection(web, share):
    """``/ftp_get`` pushes files and folders with the native transfer commands."""
    handler = web.server.commands[("server", server_backend.FTP_GET_COMMAND)]
    address = ("127.0.0.1", 41000)
    web.server.clients[address] = {"socket": FakeSocket(), "address": address}
    with web._ftp_lock:
        web.ftp_root = str(share)

    reply = handler(
        None,
        address,
        f"{server_backend.FTP_GET_COMMAND} 1 {json.dumps(['alpha.txt', 'sub', '../escape'])}",
    )
    assert reply == f"{server_backend.FTP_GET_OK_COMMAND} 1 2 1"
    kinds = [kind for kind, _cmd in web.server.pushes]
    assert kinds == ["file", "folder"]
    assert str(share / "alpha.txt") in web.server.pushes[0][1]
    assert str(share / "sub") in web.server.pushes[1][1]
    assert "41000" in web.server.pushes[0][1]  # addressed to the asking client

    unknown = ("127.0.0.1", 41001)
    assert handler(None, unknown, f"{server_backend.FTP_GET_COMMAND} 2 []").startswith(
        server_backend.FTP_ERROR_COMMAND
    )


def test_protocol_download_handler_honours_the_destination(web, share):
    """A client-chosen download folder travels on the native transfer commands."""
    handler = web.server.commands[("server", server_backend.FTP_GET_COMMAND)]
    address = ("127.0.0.1", 41000)
    web.server.clients[address] = {"socket": FakeSocket(), "address": address}
    with web._ftp_lock:
        web.ftp_root = str(share)

    request = {"paths": ["alpha.txt", "sub"], "destination": "/tmp/picked"}
    reply = handler(None, address, f"{server_backend.FTP_GET_COMMAND} 5 {json.dumps(request)}")
    assert reply == f"{server_backend.FTP_GET_OK_COMMAND} 5 2 0"
    assert [kind for kind, _cmd in web.server.pushes] == ["file", "folder"]
    assert all("/tmp/picked" in command for _kind, command in web.server.pushes)

    # an empty destination keeps the receiver's default transfer folder
    web.server.pushes.clear()
    request = {"paths": ["alpha.txt"], "destination": "  "}
    reply = handler(None, address, f"{server_backend.FTP_GET_COMMAND} 6 {json.dumps(request)}")
    assert reply == f"{server_backend.FTP_GET_OK_COMMAND} 6 1 0"
    assert "/tmp/picked" not in web.server.pushes[0][1]

    malformed = {"paths": "alpha.txt", "destination": "/tmp/picked"}
    reply = handler(None, address, f"{server_backend.FTP_GET_COMMAND} 7 {json.dumps(malformed)}")
    assert reply == f"{server_backend.FTP_ERROR_COMMAND} 7 malformed request"


def test_shared_folder_is_persisted_and_restored(web, client, share, tmp_path, monkeypatch):
    """Adding a share records it in the startup config; a restart restores it."""
    login(client)
    assert client.post("/api/ftp/add", json={"path": str(share)}).status_code == 200
    config_path = tmp_path / "setup_server.json"
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["web"]["ftp_root"] == str(share)

    # a restart with a saved server entry brings the share back
    saved["servers"] = [{"host": "127.0.0.1", "port": 65432}]
    config_path.write_text(json.dumps(saved), encoding="utf-8")
    restarted = server_backend.ServerWebApp(
        db_path=str(tmp_path / "restart.db"), mail_config_path=str(tmp_path / "mail.json")
    )
    started = []
    monkeypatch.setattr(restarted, "_start_server", started.append)
    restarted.start_from_config()
    assert started == [{"host": "127.0.0.1", "port": 65432}]
    assert restarted._ftp_shared_root() == str(share)
    restarted.users.close()

    assert client.post("/api/ftp/remove").get_json() == {"ok": True}
    assert "ftp_root" not in json.loads(config_path.read_text(encoding="utf-8"))["web"]


def test_saving_the_startup_config_keeps_the_shared_folder(web, client, share, monkeypatch):
    """Re-saving the TCP parameters does not drop the shared "ftp" folder."""
    login(client)
    assert client.post("/api/ftp/add", json={"path": str(share)}).status_code == 200
    started = []
    monkeypatch.setattr(web, "_start_server", started.append)
    web._bound_port = 5000
    response = client.post(
        "/api/save_config",
        json={"params": {"host": "127.0.0.1", "port": 65432}, "web_port": 5000},
    )
    assert response.status_code == 200
    assert started == [{"host": "127.0.0.1", "port": 65432}]
    saved = server_backend._read_config_file()
    assert saved["servers"] == [{"host": "127.0.0.1", "port": 65432}]
    assert saved["web"] == {"port": 5000, "ftp_root": str(share)}


# ---- client side -------------------------------------------------------------


def _server_reply(web, shared=True, started=2):
    """A replier answering the client's ftp commands like the real server."""

    def reply(line):
        if line.startswith(client_backend.FTP_LIST_COMMAND):
            if not shared:
                return f"{client_backend.FTP_ERROR_COMMAND} {_request_id(line)} no folder is shared"
            rel = json.loads(line.split(" ", 2)[2])
            listing = _fake_listing(rel)
            return f"{client_backend.FTP_LIST_OK_COMMAND} {_request_id(line)} {json.dumps(listing)}"
        if line.startswith(client_backend.FTP_GET_COMMAND):
            return f"{client_backend.FTP_GET_OK_COMMAND} {_request_id(line)} {started} 0"
        return None

    return reply


def _fake_listing(rel):
    return {
        "path": rel,
        "parent": None if rel == "" else "",
        "entries": [
            {"name": "sub", "dir": True, "size": 0, "mtime": 1700000000},
            {"name": "alpha.txt", "dir": False, "size": 5, "mtime": 1700000000},
        ],
    }


def test_client_lists_the_server_share(client_web):
    """The client backend turns one protocol round trip into the listing."""
    app, client = client_web
    app.client.replier = _server_reply(app, shared=True)
    response = client.post("/api/ftp/list", json={"path": ""})
    assert response.status_code == 200
    listing = response.get_json()["listing"]
    assert [e["name"] for e in listing["entries"]] == ["sub", "alpha.txt"]
    assert app.client.sent[0].startswith(client_backend.FTP_LIST_COMMAND + " 1 ")
    assert json.loads(app.client.sent[0].split(" ", 2)[2]) == ""  # the path travels as JSON


def test_client_downloads_the_selection(client_web):
    """The picked entries are handed to the server's transfer commands."""
    app, client = client_web
    app.client.replier = _server_reply(app, shared=True, started=3)
    response = client.post(
        "/api/ftp/download", json={"paths": ["alpha.txt", "sub", "sub/beta.bin"]}
    )
    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "started": 3, "skipped": 0}
    sent = app.client.sent[0]
    assert sent.startswith(client_backend.FTP_GET_COMMAND + " ")
    assert json.loads(sent.split(" ", 2)[2]) == {
        "paths": ["alpha.txt", "sub", "sub/beta.bin"],
        "destination": "",
    }


def test_client_download_passes_the_destination(client_web):
    """A chosen download folder reaches the server; an empty one is dropped."""
    app, client = client_web
    app.client.replier = _server_reply(app, shared=True, started=1)
    response = client.post(
        "/api/ftp/download", json={"paths": ["alpha.txt"], "destination": " /tmp/picked "}
    )
    assert response.status_code == 200
    payload = json.loads(app.client.sent[0].split(" ", 2)[2])
    assert payload == {"paths": ["alpha.txt"], "destination": "/tmp/picked"}


def test_client_download_needs_a_selection(client_web):
    """An empty selection is refused before anything is sent."""
    app, client = client_web
    assert client.post("/api/ftp/download", json={"paths": []}).status_code == 400
    assert app.client.sent == []


def test_client_reports_server_refusals_and_timeouts(client_web, monkeypatch):
    """A refusal and a silent server both surface as HTTP errors."""
    app, client = client_web
    app.client.replier = _server_reply(app, shared=False)
    refused = client.post("/api/ftp/list", json={"path": ""})
    assert refused.status_code == 502
    assert "no folder is shared" in refused.get_json()["error"]

    app.client.replier = None  # the server never answers
    monkeypatch.setattr(client_backend, "REQUEST_TIMEOUT", 0.2)
    silent = client.post("/api/ftp/list", json={"path": ""})
    assert silent.status_code == 504

    app.connected = False
    assert client.post("/api/ftp/list", json={"path": ""}).status_code == 503


def test_ftp_helpers_resolve_and_refuse(tmp_path):
    """The share helpers are the single gate for every listing and download."""
    root = tmp_path / "root"
    root.mkdir()
    inside = root / "a"
    inside.write_text("x", encoding="utf-8")
    assert server_backend._ftp_resolve(str(root), "a")[1] == "a"
    assert server_backend._ftp_listing(str(root), "")["entries"][0]["name"] == "a"
    for bad in ("..", "/etc", "a/../../b"):
        with pytest.raises(ValueError):
            server_backend._ftp_resolve(str(root), bad)
    with pytest.raises(ValueError):
        server_backend._ftp_listing(str(root), "a")  # a file is not a folder
    os.makedirs(root / "b")
