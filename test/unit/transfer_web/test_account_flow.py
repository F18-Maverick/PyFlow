"""Registration, password reset, client login and contacts over the web API.

The client-facing half of the server: accounts are registered with a mailed code,
passwords are reset the same way, web clients log in with a password or a code and
receive a session token, and the instance list a client receives holds only the
accounts that accepted it as a contact.
"""

import threading

import pytest

from PyFlow.transfer_web.web_backend import server_backend


class FakeTcpServer:
    """Stand-in for ``TCP_Server_Base`` that records what the web layer pushes."""

    def __init__(self):
        self.running = True
        self.host = "127.0.0.1"
        self.port = 65432
        self.is_enable_encrypto = False
        self.clients = {}
        self.client_lock = threading.RLock()
        self.file_transfer_dir = "/tmp"
        self.pushes = []  # (socket, text)

    def send_message(self, socket, message):
        self.pushes.append((socket, message))
        return True


@pytest.fixture
def web(tmp_path, monkeypatch):
    """A ServerWebApp with a fake TCP server and a recording mailbox."""
    monkeypatch.setattr(server_backend, "FLOW_WEB_DIR", str(tmp_path))
    monkeypatch.setattr(server_backend, "SECRET_KEY_FILE", str(tmp_path / "web_secret_key"))
    app = server_backend.ServerWebApp(
        db_path=str(tmp_path / "flow_web.db"),
        mail_config_path=str(tmp_path / "email_config.json"),
    )
    app.app.config.update(TESTING=True)
    app.server = FakeTcpServer()
    app.sent = []

    def record(to_address, code, purpose, expires_in):
        app.sent.append(
            {"to": to_address, "code": code, "purpose": purpose, "expires_in": expires_in}
        )

    monkeypatch.setattr(app.mail, "send_code", record)
    yield app
    app.users.close()


@pytest.fixture
def client(web):
    return web.app.test_client()


def code_for(web, purpose, address):
    """Return the last code the fake mailbox delivered."""
    matching = [c for c in web.sent if c["purpose"] == purpose and c["to"] == address]
    assert matching, f"no {purpose} code was sent to {address}"
    return matching[-1]["code"]


def register(client, web, username, email):
    """Run the full registration flow and return the new account."""
    assert client.post("/api/register/send_code", json={"email": email}).status_code == 200
    resp = client.post(
        "/api/register",
        json={
            "username": username,
            "email": email,
            "password": f"{username}s-password",
            "code": code_for(web, "register", email),
        },
    )
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()["user"]


def client_login(client, identify, password=None, code=None):
    return client.post(
        "/api/client_login", json={"identify": identify, "password": password, "code": code}
    )


def login_token(client, web, identify, password):
    """Run the full two-factor client login and return the session token."""
    assert client.post("/api/login/send_code", json={"identify": identify}).status_code == 200
    account = web.users.find(identify)
    code = code_for(web, "login", account["email"])
    data = client_login(client, identify, password=password, code=code).get_json()
    assert data["ok"], data
    return data["token"]


def bind(web, address, token):
    """Attach a fake TCP connection to the account owning ``token``."""
    socket = object()
    web.server.clients[address] = {
        "socket": socket,
        "address": address,
        "id": f"{address[0]}:{address[1]}",
    }
    web._on_web_bind(socket, address, f"/web_bind {token}")
    return socket


def test_the_public_server_info_and_status_survive_a_running_tcp_server(client, web):
    assert client.get("/api/server_info").get_json() == {
        "host": "127.0.0.1",
        "port": 65432,
        "is_enable_encrypto": False,
    }
    login = client.post("/api/login", json={"identify": "admin", "password": "admin"})
    assert login.status_code == 200
    status = client.get("/api/status").get_json()
    assert status["running"] is True
    assert status["server_info"]["port"] == 65432
    assert status["clients"] == []


def test_registration_mails_a_code_and_creates_the_account(client, web):
    first = client.post("/api/register/send_code", json={"email": "carol@example.com"})
    assert first.status_code == 200
    issued = client.post("/api/register/send_code", json={"email": "carol@example.com"})
    assert issued.status_code == 400  # one code per minute
    assert "wait" in issued.get_json()["error"]

    code = code_for(web, "register", "carol@example.com")
    assert code.isdigit() and len(code) == 6

    resp = client.post(
        "/api/register",
        json={
            "username": "carol",
            "email": "carol@example.com",
            "password": "carols-password",
            "code": "000000" if code != "000000" else "111111",
        },
    )
    assert resp.status_code == 400
    assert "incorrect" in resp.get_json()["error"]

    resp = client.post(
        "/api/register",
        json={
            "username": "carol",
            "email": "carol@example.com",
            "password": "carols-password",
            "code": code,
        },
    )
    assert resp.status_code == 200, resp.get_json()
    user = resp.get_json()["user"]
    assert user["username"] == "carol"
    assert user["email"] == "carol@example.com"
    assert user["role"] == "user"
    assert len(user["user_id"]) == 8
    assert "password" not in user

    # the console login accepts the new account, by name and by email
    assert client.post(
        "/api/login", json={"identify": "carol", "password": "carols-password"}
    ).status_code == 200


def test_registration_rejects_a_taken_or_malformed_email(client, web):
    register(client, web, "carol", "carol@example.com")
    resp = client.post("/api/register/send_code", json={"email": "CAROL@example.com"})
    assert resp.status_code == 400
    assert "already registered" in resp.get_json()["error"]
    assert client.post("/api/register/send_code", json={"email": "nope"}).status_code == 400


def test_registration_needs_a_mailbox_that_works(tmp_path, monkeypatch):
    monkeypatch.setattr(server_backend, "SECRET_KEY_FILE", str(tmp_path / "key"))
    app = server_backend.ServerWebApp(
        db_path=str(tmp_path / "flow_web.db"), mail_config_path=str(tmp_path / "mail.json")
    )
    app.app.config.update(TESTING=True)
    resp = app.app.test_client().post("/api/register/send_code", json={"email": "a@example.com"})
    assert resp.status_code == 400
    assert "not configured" in resp.get_json()["error"]
    app.users.close()


def test_password_reset_verifies_the_code_before_storing_the_new_password(client, web):
    user = register(client, web, "carol", "carol@example.com")

    resp = client.post("/api/password/send_code", json={"identify": user["user_id"]})
    assert resp.status_code == 200
    assert resp.get_json()["masked_email"] == "c***@example.com"
    assert resp.get_json()["expires_in"] == 300
    code = code_for(web, "reset_password", "carol@example.com")

    resp = client.post(
        "/api/password/reset",
        json={"identify": "carol", "code": "000000" if code != "000000" else "111111",
              "password": "brand-new-password"},
    )
    assert resp.status_code == 400
    assert client.post(
        "/api/login", json={"identify": "carol", "password": "carols-password"}
    ).status_code == 200  # the rejected reset left the old password alone

    resp = client.post(
        "/api/password/reset",
        json={"identify": "carol", "code": code, "password": "brand-new-password"},
    )
    assert resp.status_code == 200
    assert client.post(
        "/api/login", json={"identify": "carol", "password": "carols-password"}
    ).status_code == 401
    assert client.post(
        "/api/login", json={"identify": "carol", "password": "brand-new-password"}
    ).status_code == 200


def test_password_reset_rejects_unknown_accounts_and_accounts_without_email(client, web):
    resp = client.post("/api/password/send_code", json={"identify": "nobody"})
    assert resp.status_code == 400
    assert "no account matches" in resp.get_json()["error"]

    resp = client.post("/api/password/send_code", json={"identify": "admin"})  # seeded, no email
    assert resp.status_code == 400
    assert "no email address" in resp.get_json()["error"]


def test_client_login_needs_the_password_and_a_mailed_code(client, web):
    user = register(client, web, "carol", "carol@example.com")
    assert client.post("/api/login/send_code", json={"identify": "carol"}).status_code == 200
    code = code_for(web, "login", "carol@example.com")

    resp = client_login(client, "carol@example.com", password="carols-password", code=code)
    assert resp.status_code == 200, resp.get_json()
    data = resp.get_json()
    assert data["user"]["user_id"] == user["user_id"]
    assert data["user"]["username"] == "carol"
    token = data["token"]

    assert client.post("/api/client_verify", json={"token": token}).get_json()["ok"] is True
    assert client.post("/api/client_verify", json={"token": "bogus"}).status_code == 401
    assert client.post("/api/client_logout", json={"token": token}).status_code == 200
    assert client.post("/api/client_verify", json={"token": token}).status_code == 401


def test_client_login_rejects_a_single_factor(client, web):
    register(client, web, "carol", "carol@example.com")
    assert client.post("/api/login/send_code", json={"identify": "carol"}).status_code == 200
    code = code_for(web, "login", "carol@example.com")

    only_code = client_login(client, "carol@example.com", code=code)
    assert only_code.status_code == 400
    assert "password and the mailed verification code" in only_code.get_json()["error"]

    only_password = client_login(client, "carol@example.com", password="carols-password")
    assert only_password.status_code == 400
    assert "password and the mailed verification code" in only_password.get_json()["error"]

    # neither attempt spent the code, so the full pair still works afterwards
    assert client_login(
        client, "carol@example.com", password="carols-password", code=code
    ).status_code == 200


def test_client_login_rejects_a_wrong_password_or_code(client, web):
    register(client, web, "carol", "carol@example.com")
    assert client.post("/api/login/send_code", json={"identify": "carol"}).status_code == 200
    code = code_for(web, "login", "carol@example.com")

    wrong_password = client_login(
        client, "carol@example.com", password="not-the-password", code=code
    )
    assert wrong_password.status_code == 401
    assert "invalid account or password" in wrong_password.get_json()["error"]

    wrong_code = "000000" if code != "000000" else "111111"
    assert client_login(
        client, "carol@example.com", password="carols-password", code=wrong_code
    ).status_code == 401

    # the code is spent by the successful login only
    assert client_login(
        client, "carol@example.com", password="carols-password", code=code
    ).status_code == 200
    assert client_login(
        client, "carol@example.com", password="carols-password", code=code
    ).status_code == 401  # a code is single use


def test_client_login_rejects_unknown_accounts(client, web):
    assert client_login(client, "carol").status_code == 400
    assert client_login(client, "", password="carols-password", code="123456").status_code == 400
    assert client.post("/api/login/send_code", json={"identify": "nobody"}).status_code == 400


def test_client_verify_checks_the_saved_credentials_against_the_token(client, web):
    user = register(client, web, "carol", "carol@example.com")
    assert client.post("/api/login/send_code", json={"identify": "carol"}).status_code == 200
    code = code_for(web, "login", "carol@example.com")
    token = client_login(
        client, "carol", password="carols-password", code=code
    ).get_json()["token"]

    replay = {"token": token, "identify": "carol", "password": "carols-password"}
    assert client.post("/api/client_verify", json=replay).get_json()["user"]["user_id"] == user[
        "user_id"
    ]

    stale = dict(replay, password="an-old-password")
    rejected = client.post("/api/client_verify", json=stale)
    assert rejected.status_code == 401
    assert "no longer open this account" in rejected.get_json()["error"]

    half = {"token": token, "identify": "carol"}
    assert client.post("/api/client_verify", json=half).status_code == 400

    # credentials of another account never unlock this session
    register(client, web, "dave", "dave@example.com")
    other = dict(replay, identify="dave", password="daves-password")
    assert client.post("/api/client_verify", json=other).status_code == 401


def test_a_client_sees_no_instance_until_the_contact_is_accepted(web):
    admin = web.app.test_client()
    register(admin, web, "alice", "alice@example.com")
    register(admin, web, "bob", "bob@example.com")

    alice_addr, bob_addr = ("127.0.0.1", 1111), ("127.0.0.1", 2222)
    alice_token = login_token(admin, web, "alice", "alices-password")
    bob_token = login_token(admin, web, "bob", "bobs-password")
    bind(web, alice_addr, alice_token)
    bind(web, bob_addr, bob_token)

    # bound but not contacts: neither sees the other
    assert web._client_list_for(alice_addr) == []
    assert web._client_list_for(bob_addr) == []

    search = admin.post("/api/contacts/search", json={"token": alice_token, "query": "bob"})
    assert search.status_code == 200
    found = search.get_json()["results"][0]
    assert found["username"] == "bob"
    assert found["online"] is True
    assert found["relation"] == "none"

    assert admin.post(
        "/api/contacts/request", json={"token": alice_token, "user_id": found["user_id"]}
    ).status_code == 200
    assert admin.post(
        "/api/contacts/search", json={"token": alice_token, "query": "bob"}
    ).get_json()["results"][0]["relation"] == "outgoing"

    requests = admin.get(f"/api/contact_requests?token={bob_token}").get_json()
    assert [r["user"]["username"] for r in requests["incoming"]] == ["alice"]
    assert admin.get(f"/api/contact_requests?token={alice_token}").get_json()["outgoing"]

    resp = admin.post(
        "/api/contacts/respond",
        json={"token": bob_token, "request_id": requests["incoming"][0]["id"], "accept": True},
    )
    assert resp.status_code == 200
    assert resp.get_json()["user"]["username"] == "alice"

    # both directions now see each other, with the account name attached
    alice_view = web._client_list_for(alice_addr)
    assert [(c["ip"], c["port"], c["username"]) for c in alice_view] == [
        ("127.0.0.1", 2222, "bob")
    ]
    bob_view = web._client_list_for(bob_addr)
    assert [(c["ip"], c["port"], c["username"]) for c in bob_view] == [("127.0.0.1", 1111, "alice")]
    assert admin.post(
        "/api/contacts/search", json={"token": alice_token, "query": "bob"}
    ).get_json()["results"][0]["relation"] == "contact"


def test_unbound_or_logged_out_clients_see_nothing(web):
    admin = web.app.test_client()
    alice = register(admin, web, "alice", "alice@example.com")
    bob = register(admin, web, "bob", "bob@example.com")
    alice_addr, bob_addr = ("127.0.0.1", 1111), ("127.0.0.1", 2222)
    alice_token = login_token(admin, web, "alice", "alices-password")
    bob_token = login_token(admin, web, "bob", "bobs-password")
    bind(web, alice_addr, alice_token)
    bind(web, bob_addr, bob_token)

    # bob has nothing: alice (not a contact yet) has not asked him
    assert admin.get(f"/api/contact_requests?token={bob_token}").get_json()["incoming"] == []
    admin.post("/api/contacts/request", json={"token": alice_token, "user_id": bob["user_id"]})
    requests = admin.get(f"/api/contact_requests?token={bob_token}").get_json()
    request_id = requests["incoming"][0]["id"]
    admin.post(
        "/api/contacts/respond", json={"token": bob_token, "request_id": request_id, "accept": True}
    )
    assert web._client_list_for(alice_addr)
    assert web._client_list_for(("127.0.0.1", 3333)) == []  # no /web_bind, no contacts

    # logging the account out unbinds its connection and drops its requests
    admin.post("/api/client_logout", json={"token": alice_token})
    assert web._client_list_for(alice_addr) == []
    assert alice["user_id"] != bob["user_id"]


def test_bind_acknowledges_the_address_and_pushes_the_list(web):
    admin = web.app.test_client()
    register(admin, web, "alice", "alice@example.com")
    token = login_token(admin, web, "alice", "alices-password")
    address = ("127.0.0.1", 4444)
    socket = bind(web, address, token)

    acks = [
        text
        for sock, text in web.server.pushes
        if sock is socket and text.startswith("/web_bind_ok")
    ]
    assert len(acks) == 1
    assert '"port": 4444' in acks[0]
    pushes = [text for sock, text in web.server.pushes if text.startswith("/web_clients_update")]
    assert pushes and pushes[-1] == "/web_clients_update []"

    # an unknown token is refused and binds nothing
    bind(web, ("127.0.0.1", 5555), "not-a-token")
    assert web._client_list_for(("127.0.0.1", 5555)) == []


def test_contacts_endpoints_require_a_valid_token(web):
    anonymous = web.app.test_client()
    assert anonymous.get("/api/contact_requests").status_code == 401
    assert anonymous.post("/api/contacts/search", json={"query": "x"}).status_code == 401
    assert anonymous.post("/api/contacts/request", json={"user_id": "x"}).status_code == 401
    assert anonymous.post(
        "/api/contacts/respond", json={"request_id": 1, "accept": True}
    ).status_code == 401

    register(anonymous, web, "alice", "alice@example.com")
    token = login_token(anonymous, web, "alice", "alices-password")
    assert anonymous.post(
        "/api/contacts/search", json={"token": token, "query": "  "}
    ).status_code == 400


def test_contact_requests_are_answered_one_way_only(web):
    admin = web.app.test_client()
    alice = register(admin, web, "alice", "alice@example.com")
    bob = register(admin, web, "bob", "bob@example.com")
    register(admin, web, "carol", "carol@example.com")
    alice_token = login_token(admin, web, "alice", "alices-password")
    bob_token = login_token(admin, web, "bob", "bobs-password")
    carol_token = login_token(admin, web, "carol", "carols-password")

    admin.post("/api/contacts/request", json={"token": alice_token, "user_id": bob["user_id"]})
    requests = admin.get(f"/api/contact_requests?token={bob_token}").get_json()
    request_id = requests["incoming"][0]["id"]

    assert admin.post(
        "/api/contacts/respond",
        json={"token": carol_token, "request_id": request_id, "accept": True},
    ).status_code == 400  # not addressed to carol
    assert admin.post(
        "/api/contacts/respond", json={"token": bob_token, "request_id": 4242, "accept": True}
    ).status_code == 400

    assert admin.post(
        "/api/contacts/respond",
        json={"token": bob_token, "request_id": request_id, "accept": False},
    ).status_code == 200
    assert web.users.are_contacts(alice["user_id"], bob["user_id"]) is False

    # adding an account twice is refused once the two are contacts
    admin.post("/api/contacts/request", json={"token": alice_token, "user_id": bob["user_id"]})
    incoming = admin.get(f"/api/contact_requests?token={bob_token}").get_json()
    request_id = incoming["incoming"][0]["id"]
    admin.post(
        "/api/contacts/respond", json={"token": bob_token, "request_id": request_id, "accept": True}
    )
    again = admin.post(
        "/api/contacts/request", json={"token": alice_token, "user_id": bob["user_id"]}
    )
    assert again.status_code == 400
    assert "already a contact" in again.get_json()["error"]


def test_email_config_is_validated_and_the_password_is_not_echoed(client, web, monkeypatch):
    assert client.post(
        "/api/login", json={"identify": "admin", "password": "admin"}
    ).status_code == 200

    config = {
        "host": "smtp.example.com",
        "port": 465,
        "username": "bot@example.com",
        "password": "authorization-code",
        "from": "PyFlow <bot@example.com>",
        "encryption": "ssl",
    }
    refused = client.post("/api/email_config", json={"config": config})
    assert refused.status_code == 400  # no such server behind the host name
    assert client.get("/api/email_config").get_json()["enabled"] is False

    monkeypatch.setattr(web.mail, "_connect", lambda settings: FakeSession())
    accepted = client.post("/api/email_config", json={"config": config})
    assert accepted.status_code == 200, accepted.get_json()
    assert accepted.get_json()["enabled"] is True
    assert "password" not in accepted.get_json()["config"]

    stored = client.get("/api/email_config").get_json()
    assert stored["enabled"] is True
    assert stored["config"]["host"] == "smtp.example.com"
    assert "password" not in stored["config"]
    assert web.mail.is_enabled() is True


class FakeSession:
    """Stand-in for an authenticated SMTP session."""

    def quit(self):
        """Close the session."""

    def close(self):
        """Close the session."""
