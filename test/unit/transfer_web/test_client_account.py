"""Client login, auto-login and contact proxying of the web client backend.

The client backend holds the account session and the saved credentials, so these
tests pin the contract the browser relies on: a connected but anonymous client
gets the login window, a successful login writes
``.Flow_Web/client_login.json``, every later page load logs in again from that
file, the session token is bound to the TCP connection, and logging out deletes
the file. The client talks to a real server web backend through its Flask test
client, so the two sides are exercised together without opening sockets.
"""

import contextlib
import json
import os
import stat
import threading
import time
from urllib.parse import parse_qs

import flask
import pytest

from PyFlow.transfer_web.web_backend import server_backend
from PyFlow.transfer_web.web_front import client_backend
from PyFlow.transfer_web.web_front.client_backend import _ServerRequestError


class FakeSocket:
    """Socket stand-in exposing the bound address of the client."""

    def getsockname(self):
        """Return the local address of the pretend connection."""
        return ("127.0.0.1", 40000)


class FakeTcpClient:
    """Minimal ``TCP_Client_Base`` stand-in that records written commands."""

    def __init__(self):
        self.running = True
        self.is_enable_encrypto = False
        self.client_host = "127.0.0.1"
        self.client_port = 40000
        self.client_socket = FakeSocket()
        self._crypto_lock = threading.Lock()
        self._encrypted_sockets = set()
        self.sent = []

    def send_message(self, client_socket, message):
        """Record one command written to the connection."""
        self.sent.append(message)
        return True

    def close(self):
        """Drop the pretend connection."""
        self.running = False


@contextlib.contextmanager
def rendered_templates(app):
    """Collect the template names Flask renders for one request."""
    names = []

    def record(sender, template, context, **extra):
        names.append(template.name)

    flask.template_rendered.connect(record, app)
    try:
        yield names
    finally:
        flask.template_rendered.disconnect(record, app)


@pytest.fixture
def server(tmp_path, monkeypatch):
    """The server web backend the client backend logs in to."""
    monkeypatch.setattr(server_backend, "SECRET_KEY_FILE", str(tmp_path / "server_key"))
    app = server_backend.ServerWebApp(
        db_path=str(tmp_path / "server.db"), mail_config_path=str(tmp_path / "mail.json")
    )
    app.app.config.update(TESTING=True)
    app.sent = []

    def record_code(to_address, code, purpose, expires_in):
        app.sent.append({"to": to_address, "code": code, "purpose": purpose})

    monkeypatch.setattr(app.mail, "send_code", record_code)
    return app


@pytest.fixture
def account(server):
    """Register an account on the server and return its record."""
    client = server.app.test_client()
    sent = client.post("/api/register/send_code", json={"email": "alice@example.com"})
    assert sent.status_code == 200
    code = server.sent[-1]["code"]
    resp = client.post(
        "/api/register",
        json={
            "username": "alice",
            "email": "alice@example.com",
            "password": "alices-password",
            "code": code,
        },
    )
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()["user"]


@pytest.fixture
def web(tmp_path, monkeypatch, server):
    """A ClientWebApp connected to the in-process server backend."""
    monkeypatch.setattr(client_backend, "FLOW_WEB_DIR", str(tmp_path))
    monkeypatch.setattr(client_backend, "CLIENT_LOGIN_FILE", str(tmp_path / "client_login.json"))
    monkeypatch.setattr(
        client_backend, "CLIENT_LAST_SERVER_FILE", str(tmp_path / "client_last_server.json")
    )
    monkeypatch.setattr(client_backend, "CLIENT_CONFIG_FILE", str(tmp_path / "setup_client.json"))
    monkeypatch.setattr(
        client_backend,
        "CLIENT_EXTENSIONS_UI_FILE",
        str(tmp_path / "client_extensions_ui.json"),
    )
    app = client_backend.ClientWebApp(web_port=5099)
    app.app.config.update(TESTING=True)
    app.connected = True
    app.client = FakeTcpClient()
    app._server_base = "http://server.test"

    server_client = server.app.test_client()

    def request(path, payload=None):
        """Call the server backend the way the real HTTP request would."""
        route, _, query = path.partition("?")
        if query:
            params = {key: value[0] for key, value in parse_qs(query).items()}
            response = server_client.get(route, query_string=params)
        elif payload is None:
            response = server_client.get(route)
        else:
            response = server_client.post(route, json=payload)
        data = response.get_json() or {}
        if response.status_code >= 400 or data.get("ok") is False:
            raise _ServerRequestError(
                response.status_code, data.get("error") or f"HTTP {response.status_code}"
            )
        return data

    monkeypatch.setattr(app, "_server_request", request)
    return app


@pytest.fixture
def client(web):
    return web.app.test_client()


def login(client, identify="alice", password="alices-password", code=""):
    return client.post(
        "/api/login", json={"identify": identify, "password": password, "code": code}
    )


def wait_for_bind(web, timeout=5):
    """Wait until the bind loop wrote ``/web_bind`` on the connection."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if any(message.startswith("/web_bind ") for message in web.client.sent):
            return True
        time.sleep(0.05)
    return False


def test_connected_client_without_a_session_gets_the_login_window(web, client):
    with rendered_templates(web.app) as names:
        resp = client.get("/")
    assert resp.status_code == 200
    assert names == ["client_login.html"]


def test_disconnected_client_gets_the_connect_page(web, client):
    web.connected = False
    with rendered_templates(web.app) as names:
        client.get("/")
    assert names == ["client_connect.html"]


def test_login_with_a_password_saves_the_credentials_file(web, client, account, tmp_path):
    resp = login(client)
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["user"]["user_id"] == account["user_id"]

    saved = json.loads((tmp_path / "client_login.json").read_text(encoding="utf-8"))
    assert saved == {
        "server": "http://server.test",
        "identify": "alice",
        "password": "alices-password",
        "token": "",
    }
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(tmp_path / "client_login.json").st_mode) == 0o600

    status = client.get("/api/status").get_json()
    assert status["logged_in"] is True
    assert status["user"]["username"] == "alice"
    assert status["server_address"] == "http://server.test"

    with rendered_templates(web.app) as names:
        assert client.get("/").status_code == 200
    assert names == ["client_main.html"]
    assert web.client.sent and wait_for_bind(web)


def test_login_with_a_mailed_code_saves_the_token_instead_of_a_password(
    web, client, server, account, tmp_path
):
    sent = client.post("/api/login/send_code", json={"identify": "alice@example.com"})
    assert sent.status_code == 200
    assert server.sent[-1]["purpose"] == "login"
    code = server.sent[-1]["code"]
    resp = login(client, identify="alice@example.com", password="", code=code)

    assert resp.status_code == 200, resp.get_json()

    saved = json.loads((tmp_path / "client_login.json").read_text(encoding="utf-8"))
    assert saved["identify"] == "alice@example.com"
    assert saved["password"] == ""  # no password was used, so a token is stored
    assert saved["token"] == web.session["token"]
    assert web.session["token"]


def test_login_reports_incomplete_and_wrong_credentials(client):
    assert login(client, password="").status_code == 400
    assert login(client, identify="", password="x").status_code == 400
    resp = login(client, password="wrong-password")
    assert resp.status_code == 401
    assert "invalid" in resp.get_json()["error"]


def test_a_later_page_load_logs_in_again_from_the_saved_file(web, client, account):
    assert login(client).status_code == 200
    web.session = None  # as after the client backend is restarted

    with rendered_templates(web.app) as names:
        assert client.get("/").status_code == 200
    assert names == ["client_main.html"]
    assert web.session["user"]["username"] == "alice"


def test_saved_credentials_of_another_server_are_ignored(web, client, tmp_path, account):
    (tmp_path / "client_login.json").write_text(
        json.dumps(
            {"server": "http://other.test", "identify": "alice", "password": "alices-password"}
        ),
        encoding="utf-8",
    )
    with rendered_templates(web.app) as names:
        client.get("/")
    assert names == ["client_login.html"]
    assert web.session is None


def test_rejected_saved_credentials_are_explained_on_the_login_page(web, client, tmp_path, account):
    (tmp_path / "client_login.json").write_text(
        json.dumps(
            {"server": "http://server.test", "identify": "alice", "password": "stale-password"}
        ),
        encoding="utf-8",
    )
    body = client.get("/").get_data(as_text=True)
    assert "stale-password" not in body
    assert "saved credentials were rejected" in body
    assert "alice" in body  # the account is prefilled for the retry
    assert web.session is None


def test_the_session_token_is_bound_to_the_tcp_connection(web, client, account):
    assert login(client).status_code == 200
    assert wait_for_bind(web)
    assert web.client.sent[0] == f"/web_bind {web.session['token']}"

    web._on_bind_ok(None, None, '/web_bind_ok {"ip": "10.0.0.5", "port": 4321}')
    assert web._own_address() == {"ip": "10.0.0.5", "port": 4321, "id": "10.0.0.5:4321"}


def test_logout_deletes_the_file_and_ends_the_server_session(
    web, client, server, tmp_path, account
):
    assert login(client).status_code == 200
    token = web.session["token"]

    assert client.post("/api/logout").get_json()["ok"] is True
    assert web.session is None
    assert not (tmp_path / "client_login.json").exists()
    verify = server.app.test_client().post("/api/client_verify", json={"token": token})
    assert verify.status_code == 401


def test_contact_proxies_need_a_session_and_pass_the_token_through(web, client, account):
    assert client.post("/api/contacts/search", json={"query": "alice"}).status_code == 401
    assert client.get("/api/contact_requests").status_code == 401

    second = account
    assert login(client, identify="alice", password="alices-password").status_code == 200
    token = web.session["token"]

    # alice finds herself nowhere and cannot add her own account
    found = client.post("/api/contacts/search", json={"query": "alice"}).get_json()
    assert found["results"] == []
    own = client.post("/api/contacts/request", json={"user_id": second["user_id"]})
    assert own.status_code == 400
    assert "your own account" in own.get_json()["error"]

    requests = client.get("/api/contact_requests").get_json()
    assert requests["incoming"] == []
    assert web.session["token"] == token


def test_contacts_can_be_added_answered_and_become_mutual(web, client, server, account):
    bob = _register(server, "bob", "bob@example.com")
    assert login(client, "alice", "alices-password").status_code == 200

    results = client.post("/api/contacts/search", json={"query": bob["user_id"]})
    found = results.get_json()["results"]
    assert [entry["relation"] for entry in found] == ["none"]
    assert client.post("/api/contacts/request", json={"user_id": bob["user_id"]}).status_code == 200
    assert client.post("/api/contacts/search", json={"query": "bob"}).get_json()["results"][0][
        "relation"
    ] == "outgoing"

    bob_client = server.app.test_client()
    bob_token = bob_client.post(
        "/api/client_login", json={"identify": "bob", "password": "bobs-password"}
    ).get_json()["token"]
    pending = bob_client.get(f"/api/contact_requests?token={bob_token}").get_json()["incoming"]
    assert [entry["user"]["username"] for entry in pending] == ["alice"]
    assert bob_client.post(
        "/api/contacts/respond",
        json={"token": bob_token, "request_id": pending[0]["id"], "accept": True},
    ).status_code == 200

    # the contact is mutual now, and both sides are back in the search results
    assert server.users.are_contacts(account["user_id"], bob["user_id"]) is True
    assert server.users.are_contacts(bob["user_id"], account["user_id"]) is True
    assert client.post("/api/contacts/search", json={"query": "bob"}).get_json()["results"][0][
        "relation"
    ] == "contact"


def test_a_server_401_drops_the_session_but_keeps_the_credentials(web, client, account, tmp_path):
    assert login(client).status_code == 200
    web.session["token"] = "revoked-token"  # the server no longer knows this session

    resp = client.get("/api/contact_requests")
    assert resp.status_code == 401
    assert "login required" in resp.get_json()["error"]
    assert web.session is None
    assert (tmp_path / "client_login.json").exists()  # the saved credentials survive


def _register(server, username, email):
    """Register one extra account on the test server."""
    client = server.app.test_client()
    assert client.post("/api/register/send_code", json={"email": email}).status_code == 200
    code = server.sent[-1]["code"]
    resp = client.post(
        "/api/register",
        json={
            "username": username,
            "email": email,
            "password": f"{username}s-password",
            "code": code,
        },
    )
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()["user"]
