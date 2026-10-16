"""The web transfer-progress surface: ``/api/transfers`` and its page widgets.

Both backends answer the endpoint from the TCP instance's progress stores, so
the shared frontend can draw the live speed readout and one progress bar per
running transfer.  The tests use fake TCP instances: no socket is opened and no
crypto library is needed.
"""

import threading

import pytest

from PyFlow.transfer_web.web_backend import server_backend
from PyFlow.transfer_web.web_front import client_backend


class FakeTcpServer:
    """TCP_Server_Base stand-in exposing the protocol's progress stores."""

    def __init__(self):
        self.running = True
        self.host = "127.0.0.1"
        self.port = 65432
        self.is_enable_encrypto = False
        self.clients = {}
        self.client_lock = threading.Lock()
        self.is_enable_file_transfer_progress_bar = True
        self.is_enable_real_time_network_speed = True
        self.file_recv_progress = {}
        self.file_upload_progress = {}
        self.upload_network_speed = None
        self.recv_network_speed = None


class FakeTcpClient:
    """TCP_Client_Base stand-in exposing the protocol's progress stores."""

    def __init__(self):
        self.running = True
        self.host = "127.0.0.1"
        self.port = 65432
        self.client_host = "127.0.0.1"
        self.client_port = 40000
        self.is_enable_encrypto = False
        self.is_enable_file_transfer_progress_bar = True
        self.is_enable_real_time_network_speed = True
        self.file_recv_progress = {}
        self.file_upload_progress = {}
        self.upload_network_speed = None
        self.recv_network_speed = None


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
    yield app
    app.users.close()


@pytest.fixture
def client_web(tmp_path, monkeypatch):
    """A ClientWebApp with a fake TCP client; returns ``(app, flask client)``."""
    monkeypatch.setattr(client_backend, "FLOW_WEB_DIR", str(tmp_path))
    app = client_backend.ClientWebApp(web_port=5099)
    app.app.config.update(TESTING=True)
    app.connected = True
    app.client = FakeTcpClient()
    return app, app.app.test_client()


def _logged_in_flask_client(web):
    """A Flask test client carrying the administrator's session cookie."""
    flask_client = web.app.test_client()
    flask_client.post("/api/login", json={"identify": "admin", "password": "admin"})
    return flask_client


# ---- server backend ----------------------------------------------------------


def test_server_transfers_report_both_directions(web):
    """Report both directions of one running transfer.

    A row carries the fid, the file name, the bytes and the live speed; the two
    speed attributes ride along for the topbar readout.
    """
    web.server.file_recv_progress[7] = [512, 2048, 1024.0, "alpha.bin"]
    web.server.file_upload_progress[9] = [10, 100, None, "beta.bin"]
    web.server.upload_network_speed = 2048.0
    web.server.recv_network_speed = 4096.0

    body = _logged_in_flask_client(web).get("/api/transfers").get_json()

    assert body["ok"] is True
    assert body["progress_enabled"] is True
    assert body["speed_enabled"] is True
    assert body["upload_network_speed"] == 2048.0
    assert body["recv_network_speed"] == 4096.0
    assert body["downloading"] == [
        {"fid": 7, "name": "alpha.bin", "transferred": 512, "total": 2048, "speed": 1024.0}
    ]
    assert body["uploading"] == [
        {"fid": 9, "name": "beta.bin", "transferred": 10, "total": 100, "speed": None}
    ]


def test_server_rows_are_ordered_by_name(web):
    """Concurrent transfers of a folder list in a stable, readable order."""
    web.server.file_upload_progress[2] = [0, 10, None, "zeta.bin"]
    web.server.file_upload_progress[1] = [0, 10, None, "alpha.bin"]

    body = _logged_in_flask_client(web).get("/api/transfers").get_json()

    assert [row["name"] for row in body["uploading"]] == ["alpha.bin", "zeta.bin"]


def test_server_transfers_need_a_login(web):
    """The console page is behind an account, so its transfer data is too."""
    assert web.app.test_client().get("/api/transfers").status_code == 401


def test_server_transfers_are_empty_while_the_tcp_server_is_down(web):
    """Without a TCP instance nothing is tracked and no speed is reported."""
    web.server = None

    body = _logged_in_flask_client(web).get("/api/transfers").get_json()

    assert body == {
        "ok": True,
        "progress_enabled": False,
        "speed_enabled": False,
        "upload_network_speed": None,
        "recv_network_speed": None,
        "uploading": [],
        "downloading": [],
    }


# ---- client backend ----------------------------------------------------------


def test_client_transfers_report_the_running_transfer(client_web):
    """The client page reads its own instance's download row and speed."""
    app, flask_client = client_web
    app.client.file_recv_progress[11] = [1024, 4096, 2048.0, "pack.bin"]
    app.client.recv_network_speed = 2048.0

    body = flask_client.get("/api/transfers").get_json()

    assert body["progress_enabled"] is True
    assert body["speed_enabled"] is True
    assert body["uploading"] == []
    assert body["downloading"] == [
        {"fid": 11, "name": "pack.bin", "transferred": 1024, "total": 4096, "speed": 2048.0}
    ]
    assert body["recv_network_speed"] == 2048.0


def test_client_transfers_are_empty_before_connecting(client_web):
    """The connect page polls the endpoint before any TCP client exists."""
    app, flask_client = client_web
    app.client = None

    body = flask_client.get("/api/transfers").get_json()

    assert body["uploading"] == [] and body["downloading"] == []
    assert body["progress_enabled"] is False
    assert body["speed_enabled"] is False


def test_switched_off_tracking_is_reported_as_disabled(client_web):
    """The frontend hides the bars and the speed readout from these flags."""
    app, flask_client = client_web
    app.client.is_enable_file_transfer_progress_bar = False
    app.client.is_enable_real_time_network_speed = False

    body = flask_client.get("/api/transfers").get_json()

    assert body["progress_enabled"] is False
    assert body["speed_enabled"] is False
    assert body["uploading"] == [] and body["downloading"] == []


# ---- UI wiring ---------------------------------------------------------------


def test_both_console_pages_carry_the_transfer_widgets(web, client_web):
    """The shared frontend only renders into these elements when they exist."""
    app, flask_client = client_web
    app.login_required = False
    client_page = flask_client.get("/").get_data(as_text=True)
    server_page = _logged_in_flask_client(web).get("/").get_data(as_text=True)

    for page in (client_page, server_page):
        assert 'id="net-speed"' in page
        assert 'id="transfer-panel"' in page


def test_web_configs_expose_the_progress_switches(web, client_web):
    """Both startup-configuration pages can toggle the two protocol switches."""
    app, flask_client = client_web
    # no live client: the form falls back to the parameter defaults
    app.client = None
    client_form = flask_client.get("/config").get_data(as_text=True)
    server_form = _logged_in_flask_client(web).get("/config").get_data(as_text=True)
    keys = ("is_enable_file_transfer_progress_bar", "is_enable_real_time_network_speed")

    for key in keys:
        assert key in [entry[0] for entry in server_backend.SERVER_PARAM_FIELDS]
        assert key in [entry[0] for entry in client_backend.CLIENT_PARAM_FIELDS]
        assert f'id="f-{key}"' in client_form
        assert f'id="f-{key}"' in server_form