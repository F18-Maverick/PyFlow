"""Login gate, account store and role checks of the server web backend.

The server web UI is the administration surface of the TCP server, so these
tests pin the security-relevant contract: anonymous visitors only get the
landing page, every administration endpoint needs a session (extensions and
configuration need an administrator), and the seeded ``admin``/``admin``
account is hashed and flagged until its credentials are changed.
"""

import contextlib
import os
import sqlite3
import stat

import pytest

from PyFlow.transfer_web.web_backend import server_backend
from PyFlow.transfer_web.web_backend.user_database import UserDatabase


@pytest.fixture
def web(tmp_path, monkeypatch):
    """A ServerWebApp whose account store and mail settings live under ``tmp_path``."""
    monkeypatch.setattr(server_backend, "FLOW_WEB_DIR", str(tmp_path))
    monkeypatch.setattr(server_backend, "SECRET_KEY_FILE", str(tmp_path / "web_secret_key"))
    monkeypatch.setattr(
        server_backend, "SERVER_EXTENSIONS_UI_FILE", str(tmp_path / "server_extensions_ui.json")
    )
    app = server_backend.ServerWebApp(
        db_path=str(tmp_path / "flow_web.db"),
        mail_config_path=str(tmp_path / "email_config.json"),
    )
    app.app.config.update(TESTING=True)
    yield app
    app.users.close()


@pytest.fixture
def client(web):
    return web.app.test_client()


def login(client, identify="admin", password="admin"):
    return client.post("/api/login", json={"identify": identify, "password": password})


def add_user(client, username, password, email=None, role="user"):
    return client.post(
        "/api/users",
        json={
            "username": username,
            "email": email or f"{username}@example.com",
            "password": password,
            "role": role,
        },
    )


def page(client):
    """The page an authenticated visitor gets, checked for the warning flag."""
    return client.get("/").get_data(as_text=True)


def test_landing_page_for_anonymous_visitors(client):
    body = client.get("/").get_data(as_text=True)
    assert "The PyFlow Server is running! Connect it in clients by the server host." in body
    assert 'id="login-btn"' in body
    assert 'id="register-btn"' in body  # self-service registration
    assert 'id="reset-btn"' in body  # self-service password change
    assert "Change Config" not in body  # no administration UI before a login


def test_protected_endpoints_reject_anonymous_requests(client):
    for method, path in [
        ("get", "/api/status"),
        ("get", "/api/clients"),
        ("get", "/api/events"),
        ("get", "/api/extensions_ui"),
        ("get", "/api/users"),
        ("get", "/api/email_config"),
        ("get", "/api/contact_requests"),
        ("post", "/api/send_msg"),
        ("post", "/api/save_config"),
        ("post", "/api/users"),
        ("post", "/api/run_extension"),
        ("post", "/api/email_config"),
        ("post", "/api/contacts/search"),
    ]:
        resp = getattr(client, method)(path, json={})
        assert resp.status_code == 401, (method, path)
    assert client.get("/config").status_code == 302  # back to the landing page


def test_server_info_stays_public_for_clients(client):
    # web clients discover the TCP address before they can log in
    resp = client.get("/api/server_info")
    assert resp.status_code != 401
    assert resp.status_code != 403


def test_registration_stays_public_for_new_accounts(client):
    # anyone who can reach the landing page may ask for a registration code
    resp = client.post("/api/register/send_code", json={"email": "new@example.com"})
    assert resp.status_code == 400  # no mailbox configured yet, not a login failure
    assert "mail service is not configured" in resp.get_json()["error"]


def test_default_admin_is_seeded_with_a_hashed_password(web, tmp_path):
    with contextlib.closing(sqlite3.connect(tmp_path / "flow_web.db")) as conn:
        rows = conn.execute("SELECT username, role, password FROM users").fetchall()
    admin = next(row for row in rows if row[0] == "admin")
    assert admin[1] == "admin"
    assert admin[2].startswith("pbkdf2_sha256$")
    assert "admin" not in admin[2]  # the record is stored, never the password
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(tmp_path / "flow_web.db").st_mode) == 0o600
    assert web.users.authenticate("admin", "admin")["role"] == "admin"


def test_login_with_default_credentials_warns_about_them(client):
    data = login(client).get_json()
    assert data["ok"] is True
    assert data["role"] == "admin"
    assert data["must_change_credentials"] is True
    # the page hands the warning flag to the modal (PyFlowAccount.mount)
    assert "mustChange: true" in page(client)


def test_login_accepts_the_email_of_an_account(client):
    login(client)
    add_user(client, "alice", "alices-password")
    client.post("/api/logout")

    assert login(client, "alice@example.com", "alices-password").get_json()["username"] == "alice"


def test_login_rejects_a_wrong_password(client):
    assert login(client, password="not-the-password").status_code == 401
    assert client.get("/api/status").status_code == 401  # and no session was created


def test_regular_user_gets_no_administration_access(client):
    login(client)
    assert add_user(client, "alice", "alices-password").status_code == 200
    client.post("/api/logout")

    data = login(client, "alice", "alices-password").get_json()
    assert data["role"] == "user"
    assert data["must_change_credentials"] is False

    assert client.get("/config").status_code == 302
    assert client.get("/api/users").status_code == 403
    assert client.get("/api/email_config").status_code == 403
    assert add_user(client, "mallory", "mallory-password").status_code == 403
    assert client.post("/api/run_extension", json={"command": "/anything"}).status_code == 403
    assert client.get("/api/status").status_code == 200  # normal functionality remains

    body = page(client)
    assert 'id="users-btn"' not in body
    assert 'id="add-ext-btn"' not in body
    assert 'id="plus-btn"' not in body
    assert "alice" in body  # the account is shown in the sidebar footer


def test_removed_user_loses_access_immediately(web):
    admin = web.app.test_client()
    carol = web.app.test_client()
    login(admin)
    add_user(admin, "carol", "carols-password")
    login(carol, "carol", "carols-password")
    assert carol.get("/api/status").status_code == 200

    assert admin.post("/api/users/delete", json={"username": "carol"}).status_code == 200
    assert carol.get("/api/status").status_code == 401


def test_change_credentials_clears_the_warning(client):
    login(client)
    resp = client.post(
        "/api/account",
        json={
            "current_password": "admin",
            "username": "root",
            "password": "a-good-password",
            "email": "root@example.com",
        },
    )
    assert resp.get_json()["must_change_credentials"] is False
    assert resp.get_json()["user"]["email"] == "root@example.com"

    client.post("/api/logout")
    assert login(client, "admin", "admin").status_code == 401  # the default account is gone
    assert login(client, "root", "a-good-password").get_json()["must_change_credentials"] is False
    assert "mustChange: false" in page(client)


def test_changing_only_the_password_clears_the_warning(client):
    login(client)
    assert "mustChange: true" in page(client)
    resp = client.post(
        "/api/account",
        json={"current_password": "admin", "username": "admin", "password": "a-good-password"},
    )
    assert resp.get_json()["must_change_credentials"] is False
    assert "mustChange: false" in page(client)

    client.post("/api/logout")
    assert login(client, "admin", "a-good-password").get_json()["must_change_credentials"] is False


def test_account_changes_require_the_current_password(client):
    login(client)
    resp = client.post(
        "/api/account",
        json={"current_password": "wrong", "username": "root", "password": "a-good-password"},
    )
    assert resp.status_code == 403
    # the rejected change left the account (and the warning) alone
    assert "mustChange: true" in page(client)


def test_admin_cannot_remove_its_own_account(client):
    login(client)
    resp = client.post("/api/users/delete", json={"username": "admin"})
    assert resp.status_code == 400
    assert "own account" in resp.get_json()["error"]


def test_store_rejects_duplicates_short_passwords_and_orphaning_admins(web):
    web.users.add_user("bob", "bob@example.com", "bobs-password")
    with pytest.raises(ValueError):
        web.users.add_user("bob", "other@example.com", "another-password")
    with pytest.raises(ValueError):
        web.users.add_user("shorty", "shorty@example.com", "abc")
    with pytest.raises(ValueError):
        web.users.remove_user("admin")  # the only administrator
    with pytest.raises(ValueError):
        web.users.update_credentials("admin", new_username="bob", new_password="a-good-password")


def test_corrupt_store_does_not_restore_the_default_account(tmp_path):
    (tmp_path / "flow_web.db").write_text("{ not a database", encoding="utf-8")
    store = UserDatabase(str(tmp_path / "flow_web.db"))
    with pytest.raises(ValueError):
        store.authenticate("admin", "admin")  # a damaged store never re-seeds
    store.close()  # the failed open released its connection already
