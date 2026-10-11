"""Client login, auto-login and contact proxying of the web client backend.

The client backend holds the account session and the saved credentials, so these
tests pin the contract the browser relies on: a connected but anonymous client
gets the login window, a successful login writes
``.Flow_Web/client_login.json``, every later page load logs in again from that
file, the session token is bound to the TCP connection, and logging out deletes
the file.  A server without a verification mailbox needs no client accounts at
all: its login window and account controls disappear, which the tests below pin
as well.  The client talks to a real server web backend through its Flask test
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


# A validated mailbox switches the server into public mode: clients log in.
# The file is written before the app is built, like a restarted configured
# server reads it back.
MAILBOX_CONFIG = {
    "host": "smtp.example.com",
    "port": 465,
    "username": "bot@example.com",
    "password": "authorization-code",
    "from": "PyFlow <bot@example.com>",
    "encryption": "ssl",
}


class FakeTcpServer:
    """TCP_Server_Base stand-in: enough for the server's own API surface."""

    def __init__(self):
        self.running = True
        self.host = "127.0.0.1"
        self.port = 65432
        self.is_enable_encrypto = False
        self.clients = {}
        self.client_lock = threading.Lock()
        self.pushes = []

    def send_message(self, sock, message):
        """Record one line written to a client."""
        self.pushes.append((sock, message))
        return True


class FakeSession:
    """Stand-in for an authenticated SMTP session."""

    def quit(self):
        """Close the session."""

    def close(self):
        """Close the session."""


def make_server(tmp_path, monkeypatch, name="server", with_mailbox=True):
    """Build a server web backend; a mailbox makes it public (clients log in)."""
    monkeypatch.setattr(server_backend, "SECRET_KEY_FILE", str(tmp_path / f"{name}_key"))
    mail_path = tmp_path / f"{name}_mail.json"
    if with_mailbox:
        mail_path.write_text(json.dumps(MAILBOX_CONFIG), encoding="utf-8")
    app = server_backend.ServerWebApp(
        db_path=str(tmp_path / f"{name}.db"), mail_config_path=str(mail_path)
    )
    app.app.config.update(TESTING=True)
    app.server = FakeTcpServer()
    app.sent = []

    def record_code(to_address, code, purpose, expires_in):
        app.sent.append({"to": to_address, "code": code, "purpose": purpose})

    monkeypatch.setattr(app.mail, "send_code", record_code)
    return app


def connect_client(tmp_path, monkeypatch, server_app, login_name="client_login.json"):
    """A ClientWebApp whose account requests reach ``server_app`` in-process."""
    monkeypatch.setattr(client_backend, "FLOW_WEB_DIR", str(tmp_path))
    monkeypatch.setattr(client_backend, "CLIENT_LOGIN_FILE", str(tmp_path / login_name))
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

    server_client = server_app.app.test_client()

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
def server(tmp_path, monkeypatch):
    """The public-mode server web backend the client backend logs in to."""
    app = make_server(tmp_path, monkeypatch)
    yield app
    app.users.close()


@pytest.fixture
def internal_server(tmp_path, monkeypatch):
    """A server web backend without a mailbox: its clients need no accounts."""
    app = make_server(tmp_path, monkeypatch, name="internal", with_mailbox=False)
    yield app
    app.users.close()


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
    """A ClientWebApp connected to the in-process public-mode server."""
    return connect_client(tmp_path, monkeypatch, server)


@pytest.fixture
def internal_web(tmp_path, monkeypatch, internal_server):
    """A ClientWebApp connected to the in-process account-free server."""
    return connect_client(tmp_path, monkeypatch, internal_server, login_name="internal_login.json")


@pytest.fixture
def client(web):
    return web.app.test_client()


@pytest.fixture
def internal_client(internal_web):
    return internal_web.app.test_client()


def login(client, server, identify="alice", password="alices-password", code=None):
    """Log the web client in with both factors, mailing a fresh code first."""
    if code is None:
        sent = client.post("/api/login/send_code", json={"identify": identify})
        assert sent.status_code == 200, sent.get_json()
        code = [c for c in server.sent if c["purpose"] == "login"][-1]["code"]
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


def test_internal_server_needs_no_login_and_shows_no_account_controls(
    internal_web, internal_client
):
    with rendered_templates(internal_web.app) as names:
        resp = internal_client.get("/")
    assert resp.status_code == 200
    assert names == ["client_main.html"]
    body = resp.get_data(as_text=True)
    assert "Contacts" not in body and "User info" not in body and "Log out" not in body
    assert 'id="ftp-btn"' in body  # the tool itself stays usable
    assert internal_web.client.sent == []  # no account was bound over TCP

    status = internal_client.get("/api/status").get_json()
    assert status["login_required"] is False
    assert status["logged_in"] is False
    assert status["user"] is None


def test_internal_server_refuses_the_account_actions(internal_web, internal_client):
    internal_client.get("/")  # a page load asks the server and switches the mode off
    cases = [
        ("post", "/api/login", {"identify": "alice", "password": "x", "code": "1"}),
        ("post", "/api/login/send_code", {"identify": "alice"}),
        ("post", "/api/contacts/search", {"query": "alice"}),
        ("get", "/api/contact_requests", None),
    ]
    for method, path, payload in cases:
        if method == "get":
            resp = internal_client.get(path)
        else:
            resp = internal_client.post(path, json=payload)
        assert resp.status_code == 400, (method, path)
        assert "client accounts are not enabled" in resp.get_json()["error"], (method, path)


def test_configuring_the_mailbox_makes_the_next_page_load_ask_for_a_login(
    internal_web, internal_client, internal_server, monkeypatch
):
    with rendered_templates(internal_web.app) as first:
        internal_client.get("/")
    assert first == ["client_main.html"]

    # the administrator stores a mailbox: the server is public from now on
    monkeypatch.setattr(internal_server.mail, "_connect", lambda settings: FakeSession())
    internal_server.mail.configure(dict(MAILBOX_CONFIG))

    with rendered_templates(internal_web.app) as second:
        internal_client.get("/")
    assert second == ["client_login.html"]


def test_forced_login_saves_the_password_and_the_session_token(
    web, client, server, account, tmp_path
):
    resp = login(client, server)
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["user"]["user_id"] == account["user_id"]

    saved = json.loads((tmp_path / "client_login.json").read_text(encoding="utf-8"))
    assert saved["server"] == "http://server.test"
    assert saved["identify"] == "alice"
    assert saved["password"] == "alices-password"
    assert saved["token"] == web.session["token"]
    assert saved["token"]
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


def test_forced_login_needs_the_password_and_a_mailed_code(web, client, server, account):
    assert client.post("/api/login/send_code", json={"identify": "alice"}).status_code == 200
    code = [c for c in server.sent if c["purpose"] == "login"][-1]["code"]

    only_password = login(client, server, password="alices-password", code="")
    assert only_password.status_code == 400
    assert "password and the mailed verification code" in only_password.get_json()["error"]

    only_code = login(client, server, password="", code=code)
    assert only_code.status_code == 400
    assert "password and the mailed verification code" in only_code.get_json()["error"]

    assert web.session is None  # neither attempt opened a session
    assert login(client, server, code=code).status_code == 200


def test_forced_login_reports_a_wrong_password_or_code(web, client, server, account):
    assert client.post("/api/login/send_code", json={"identify": "alice"}).status_code == 200
    code = [c for c in server.sent if c["purpose"] == "login"][-1]["code"]

    wrong_password = login(client, server, password="not-the-password", code=code)
    assert wrong_password.status_code == 401
    assert "invalid" in wrong_password.get_json()["error"]

    bogus = "000000" if code != "000000" else "111111"
    wrong_code = login(client, server, password="alices-password", code=bogus)
    assert wrong_code.status_code == 401
    assert "incorrect" in wrong_code.get_json()["error"]

    assert login(client, server, password="alices-password", code=code).status_code == 200


def test_forced_login_needs_an_account(client):
    missing = client.post("/api/login", json={"identify": "", "password": "x", "code": "1"})
    assert missing.status_code == 400
    assert (
        client.post(
            "/api/login", json={"identify": "alice", "password": "x", "code": "1"}
        ).status_code
        == 401
    )  # unknown account, refused by the server


def test_a_later_page_load_logs_in_again_from_the_saved_file(web, client, server, account):
    assert login(client, server).status_code == 200
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


def test_rejected_saved_credentials_are_explained_on_the_login_page(
    web, client, server, tmp_path, account
):
    assert login(client, server).status_code == 200
    token = web.session["token"]
    web.session = None
    # the session token is still known, the saved password is not
    (tmp_path / "client_login.json").write_text(
        json.dumps(
            {
                "server": "http://server.test",
                "identify": "alice",
                "password": "stale-password",
                "token": token,
            }
        ),
        encoding="utf-8",
    )
    body = client.get("/").get_data(as_text=True)
    assert "stale-password" not in body
    assert "saved credentials were rejected" in body
    assert "alice" in body  # the account is prefilled for the retry
    assert web.session is None


def test_a_saved_login_without_a_token_or_password_needs_a_fresh_login(
    web, client, server, tmp_path, account
):
    (tmp_path / "client_login.json").write_text(
        json.dumps(
            {"server": "http://server.test", "identify": "alice", "password": "alices-password"}
        ),
        encoding="utf-8",
    )
    body = client.get("/").get_data(as_text=True)
    assert "the saved login is incomplete" in body
    assert web.session is None


def test_the_session_token_is_bound_to_the_tcp_connection(web, client, server, account):
    assert login(client, server).status_code == 200
    assert wait_for_bind(web)
    assert web.client.sent[0] == f"/web_bind {web.session['token']}"

    web._on_bind_ok(None, None, '/web_bind_ok {"ip": "10.0.0.5", "port": 4321}')
    assert web._own_address() == {"ip": "10.0.0.5", "port": 4321, "id": "10.0.0.5:4321"}


def test_logout_deletes_the_file_and_ends_the_server_session(
    web, client, server, tmp_path, account
):
    assert login(client, server).status_code == 200
    token = web.session["token"]

    assert client.post("/api/logout").get_json()["ok"] is True
    assert web.session is None
    assert not (tmp_path / "client_login.json").exists()
    verify = server.app.test_client().post("/api/client_verify", json={"token": token})
    assert verify.status_code == 401


def test_contact_proxies_need_a_session_and_pass_the_token_through(web, client, server, account):
    assert client.post("/api/contacts/search", json={"query": "alice"}).status_code == 401
    assert client.get("/api/contact_requests").status_code == 401

    second = account
    assert login(client, server, identify="alice", password="alices-password").status_code == 200
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
    assert login(client, server, "alice", "alices-password").status_code == 200

    results = client.post("/api/contacts/search", json={"query": bob["user_id"]})
    found = results.get_json()["results"]
    assert [entry["relation"] for entry in found] == ["none"]
    assert client.post("/api/contacts/request", json={"user_id": bob["user_id"]}).status_code == 200
    assert (
        client.post("/api/contacts/search", json={"query": "bob"}).get_json()["results"][0][
            "relation"
        ]
        == "outgoing"
    )

    bob_client, bob_token = server_login(server, "bob", "bobs-password")
    pending = bob_client.get(f"/api/contact_requests?token={bob_token}").get_json()["incoming"]
    assert [entry["user"]["username"] for entry in pending] == ["alice"]
    assert (
        bob_client.post(
            "/api/contacts/respond",
            json={"token": bob_token, "request_id": pending[0]["id"], "accept": True},
        ).status_code
        == 200
    )

    # the contact is mutual now, and both sides are back in the search results
    assert server.users.are_contacts(account["user_id"], bob["user_id"]) is True
    assert server.users.are_contacts(bob["user_id"], account["user_id"]) is True
    assert (
        client.post("/api/contacts/search", json={"query": "bob"}).get_json()["results"][0][
            "relation"
        ]
        == "contact"
    )


def test_a_server_401_drops_the_session_but_keeps_the_credentials(
    web, client, server, account, tmp_path
):
    assert login(client, server).status_code == 200
    web.session["token"] = "revoked-token"  # the server no longer knows this session

    resp = client.get("/api/contact_requests")
    assert resp.status_code == 401
    assert "login required" in resp.get_json()["error"]
    assert web.session is None
    assert (tmp_path / "client_login.json").exists()  # the saved credentials survive


def server_login(server, identify, password):
    """Run the two-factor client login straight against the server backend."""
    client = server.app.test_client()
    assert client.post("/api/login/send_code", json={"identify": identify}).status_code == 200
    code = [c for c in server.sent if c["purpose"] == "login"][-1]["code"]
    resp = client.post(
        "/api/client_login",
        json={"identify": identify, "password": password, "code": code},
    )
    assert resp.status_code == 200, resp.get_json()
    return client, resp.get_json()["token"]


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
