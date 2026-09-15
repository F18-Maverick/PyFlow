"""Login gate, account store and role checks of the server web backend.

The server web UI is the administration surface of the TCP server, so these
tests pin the security-relevant contract: anonymous visitors only get the
landing page, every administration endpoint needs a session (extensions and
configuration need an administrator), and the seeded ``admin``/``admin``
account is hashed and flagged until its credentials are changed.
"""

import json
import os
import stat

import pytest

from PyFlow.transfer_web.web_backend import server_backend


@pytest.fixture
def web(tmp_path, monkeypatch):
    """A ServerWebApp that keeps its account store under ``tmp_path``."""
    monkeypatch.setattr(server_backend, "FLOW_WEB_DIR", str(tmp_path))
    monkeypatch.setattr(server_backend, "USERS_FILE", str(tmp_path / "users.json"))
    monkeypatch.setattr(server_backend, "SECRET_KEY_FILE", str(tmp_path / "web_secret_key"))
    monkeypatch.setattr(
        server_backend, "SERVER_EXTENSIONS_UI_FILE", str(tmp_path / "server_extensions_ui.json")
    )
    app = server_backend.ServerWebApp()
    app.app.config.update(TESTING=True)
    return app


@pytest.fixture
def client(web):
    return web.app.test_client()


def login(client, username="admin", password="admin"):
    return client.post("/api/login", json={"username": username, "password": password})


def add_user(client, username, password, role="user"):
    return client.post(
        "/api/users", json={"username": username, "password": password, "role": role}
    )


def page(client):
    """The page an authenticated visitor gets, checked for the warning flag."""
    return client.get("/").get_data(as_text=True)


def test_landing_page_for_anonymous_visitors(client):
    body = client.get("/").get_data(as_text=True)
    assert "The PyFlow Server is running! Connect it in clients by the server host." in body
    assert 'id="login-btn"' in body
    assert "Change Config" not in body  # no administration UI before a login


def test_protected_endpoints_reject_anonymous_requests(client):
    for method, path in [
        ("get", "/api/status"),
        ("get", "/api/clients"),
        ("get", "/api/events"),
        ("get", "/api/extensions_ui"),
        ("get", "/api/users"),
        ("post", "/api/send_msg"),
        ("post", "/api/save_config"),
        ("post", "/api/users"),
        ("post", "/api/run_extension"),
    ]:
        resp = getattr(client, method)(path, json={})
        assert resp.status_code == 401, (method, path)
    assert client.get("/config").status_code == 302  # back to the landing page


def test_server_info_stays_public_for_clients(client):
    # web clients discover the TCP address before they can log in
    resp = client.get("/api/server_info")
    assert resp.status_code != 401
    assert resp.status_code != 403


def test_default_admin_is_seeded_with_a_hashed_password(web, tmp_path):
    stored = json.loads((tmp_path / "users.json").read_text(encoding="utf-8"))["users"]
    admin = next(u for u in stored if u["username"] == "admin")
    assert admin["role"] == "admin"
    assert admin["password"].startswith("pbkdf2_sha256$")
    assert "admin" not in admin["password"]  # the record is stored, never the password
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(tmp_path / "users.json").st_mode) == 0o600
    assert web.users.authenticate("admin", "admin")["role"] == "admin"


def test_login_with_default_credentials_warns_about_them(client):
    data = login(client).get_json()
    assert data["ok"] is True
    assert data["role"] == "admin"
    assert data["must_change_credentials"] is True
    # the page hands the warning flag to the modal (PyFlowAccount.mount)
    assert "mustChange: true" in page(client)


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
        json={"current_password": "admin", "username": "root", "password": "a-good-password"},
    )
    assert resp.get_json()["must_change_credentials"] is False

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
    web.users.add("bob", "bobs-password")
    with pytest.raises(ValueError):
        web.users.add("bob", "another-password")
    with pytest.raises(ValueError):
        web.users.add("shorty", "abc")
    with pytest.raises(ValueError):
        web.users.remove("admin")  # the only administrator
    with pytest.raises(ValueError):
        web.users.change_credentials("admin", "bob", "a-good-password")


def test_corrupt_store_does_not_restore_the_default_account(tmp_path, monkeypatch):
    monkeypatch.setattr(server_backend, "USERS_FILE", str(tmp_path / "users.json"))
    (tmp_path / "users.json").write_text("{ not json", encoding="utf-8")
    assert server_backend.UserStore().authenticate("admin", "admin") is None
