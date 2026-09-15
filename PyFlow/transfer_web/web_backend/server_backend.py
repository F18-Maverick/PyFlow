"""Flask backend wrapping the PyFlow TCP server for the web tool.

Two modes, one process:

- ``config`` mode: serves the server startup-configuration UI.  The UI
  shows every ``TCP_Server_Base`` parameter with its default value; on
  submit the config is written to ``.Flow_Web/setup_server.json`` (same
  shape as ``flow_setup``'s ``setup.json``) and the TCP server class is
  started.
- ``status`` mode: serves the minimal status page plus the same
  sidebar/input UI as the client frontend (forwarding disabled; native
  sends to connected clients allowed).  Also exposes the HTTP API that
  clients use to discover the TCP server address/port.

The backend monitors ``server.clients``: whenever a client connects or
disconnects it broadcasts the current instance list to every connected
client (``/web_clients_update``), and it re-checks the list every
minute.

Inbound events (plain-text messages and file uploads arriving from
clients) are captured on the TCP server's receive threads through
``TCP_Server_Base``'s ``add_message_listener``/``add_file_listener``
APIs, queued here, and polled by the frontend via ``/api/events``.

Authentication: anonymous visitors get a white landing page (the server
addresses plus a login button); the configuration and status pages need a
session.  Accounts live in ``.Flow_Web/users.json``; the first run seeds the
``admin``/``admin`` administrator, and the frontend warns on every login until
those default credentials are changed.
"""

import functools
import hashlib
import hmac
import json
import os
import re
import secrets
import shlex
import socket
import subprocess
import sys
import threading
import time
import traceback

from flask import Flask, jsonify, redirect, render_template, request, session

from PyFlow import add_extension
from PyFlow import forward_extension_tcp
from PyFlow.network_api.connect_tcp import TCP_Server_Base

WEB_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FLOW_WEB_DIR = os.path.join(WEB_ROOT, ".Flow_Web")
SERVER_CONFIG_FILE = os.path.join(FLOW_WEB_DIR, "setup_server.json")
SERVER_EXTENSIONS_UI_FILE = os.path.join(FLOW_WEB_DIR, "server_extensions_ui.json")
UPLOAD_DIR = os.path.join(FLOW_WEB_DIR, "uploads")
TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
STATIC_DIR = os.path.join(WEB_ROOT, "static")

DEFAULT_WEB_PORT = 5000

# Login/account store: the first run seeds DEFAULT_ADMIN_USERNAME with
# DEFAULT_ADMIN_PASSWORD, and administrators are warned while that seeded pair
# (and only that pair) is still in use.
USERS_FILE = os.path.join(FLOW_WEB_DIR, "users.json")
SECRET_KEY_FILE = os.path.join(FLOW_WEB_DIR, "web_secret_key")
DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "admin"
MIN_PASSWORD_LENGTH = 8
PBKDF2_ITERATIONS = 200000
_USERNAME_RE = re.compile(r"\S{1,64}")

# Ordered (key, label, type, default, help) for every TCP_Server_Base
# parameter shown in the startup-configuration UI.
SERVER_PARAM_FIELDS = [
    ("host", "Host", "text", "127.0.0.1", "IP address the TCP server binds to."),
    ("port", "Port", "number", 65432, "TCP port the server listens on."),
    ("max_clients", "Max clients", "number", 10, "Maximum number of concurrent clients."),
    ("port_add_step", "Port add step", "number", 1, "Step size for port allocation."),
    ("port_range_num", "Port range num", "number", 100, "Number of ports in the allocation range."),
    (
        "max_file_transfer_thread_num",
        "Max file transfer threads",
        "number",
        10,
        "Maximum concurrent file-transfer threads.",
    ),
    ("is_hand_alloc_port", "Hand-allocated ports", "bool", False, "Manually allocate transfer ports."),
    (
        "is_input_command_in_console",
        "Console input",
        "bool",
        False,
        "Forced False by the web architecture (the web UI is the input).",
    ),
    ("max_custom_workers", "Max custom workers", "number", 10, "Maximum custom-command worker threads."),
    (
        "is_extend_command",
        "Extend command",
        "bool",
        True,
        "Forced True by the web architecture (extensions are registered before start).",
    ),
    ("is_enable_encrypto", "Enable encryption", "bool", True, "RSA-encrypt the TCP channel."),
    ("is_custom_keys", "Custom keys", "text", "", "Optional [pub_key_path, pvt_key_path] pair."),
    ("max_mem_buff", "Max memory buffer (MB)", "number", 2048, "In-memory transfer buffer in MB."),
]

# Web-only settings (not TCP_Server_Base parameters).
WEB_FIELDS = [
    ("web_port", "Web port", "number", DEFAULT_WEB_PORT, "Port of this web backend (clients query it)."),
]


def _hash_password(password, salt, iterations=PBKDF2_ITERATIONS):
    """PBKDF2-SHA256 record: ``pbkdf2_sha256$<iterations>$<salt>$<hex digest>``."""
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("ascii"), iterations
    )
    return f"pbkdf2_sha256${iterations}${salt}${digest.hex()}"


def _new_password_record(password):
    return _hash_password(password, secrets.token_hex(16))


@functools.lru_cache(maxsize=1)
def _dummy_password_record():
    """Throwaway record: an unknown user must cost what a wrong password costs."""
    return _new_password_record(secrets.token_hex(32))


def _verify_password(password, record):
    try:
        algo, iterations, salt, digest = record.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        expected = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("ascii"), int(iterations)
        )
    except (AttributeError, TypeError, ValueError):
        return False
    return hmac.compare_digest(expected.hex(), digest)


def _validate_username(username):
    if not _USERNAME_RE.fullmatch(username):
        raise ValueError("the username must be 1-64 characters without spaces")


def _validate_password(password):
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"the password must be at least {MIN_PASSWORD_LENGTH} characters")


def _load_or_create_secret_key(path=None):
    """Persist the Flask session key so logins survive a restart."""
    path = path or SECRET_KEY_FILE
    try:
        with open(path, "r", encoding="utf-8") as f:
            key = f.read().strip()
        if key:
            return key
    except FileNotFoundError:
        pass
    except OSError:
        traceback.print_exc()
    key = secrets.token_hex(32)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(key)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError:
        traceback.print_exc()
    return key


class UserStore:
    """Account store backing the server web login (``.Flow_Web/users.json``).

    Passwords are PBKDF2-SHA256 records with a per-user salt.  A missing store
    file seeds the default ``admin``/``admin`` administrator; a store file that
    exists but cannot be read is *not* re-seeded, so a damaged file can never
    silently restore the default account.
    """

    def __init__(self, path=None):
        self.path = path or USERS_FILE
        self._lock = threading.Lock()
        self._users = {}
        self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            self._seed_default_admin()
            return
        except Exception:
            traceback.print_exc()
            return
        entries = data.get("users") if isinstance(data, dict) else None
        for entry in entries or []:
            if isinstance(entry, dict) and entry.get("username") and entry.get("password"):
                role = entry.get("role")
                self._users[entry["username"]] = {
                    "username": entry["username"],
                    "role": role if role in ("admin", "user") else "user",
                    "password": entry["password"],
                }

    def _seed_default_admin(self):
        self._users = {
            DEFAULT_ADMIN_USERNAME: {
                "username": DEFAULT_ADMIN_USERNAME,
                "role": "admin",
                "password": _new_password_record(DEFAULT_ADMIN_PASSWORD),
            }
        }
        self._save()

    def _save(self):
        """Write the store atomically; it holds password records, not passwords."""
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"users": list(self._users.values())}, f, indent=4, ensure_ascii=False)
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def authenticate(self, username, password):
        """Return ``{"username", "role"}`` for valid credentials, else ``None``."""
        with self._lock:
            entry = self._users.get(username)
        if entry is None:
            _verify_password(password, _dummy_password_record())
            return None
        if not _verify_password(password, entry["password"]):
            return None
        return {"username": entry["username"], "role": entry["role"]}

    def get(self, username):
        with self._lock:
            entry = self._users.get(username)
        if entry is None:
            return None
        return {"username": entry["username"], "role": entry["role"]}

    def list(self):
        with self._lock:
            entries = sorted(self._users.values(), key=lambda e: e["username"])
        return [{"username": e["username"], "role": e["role"]} for e in entries]

    def add(self, username, password, role="user"):
        _validate_username(username)
        _validate_password(password)
        with self._lock:
            if username in self._users:
                raise ValueError(f"user {username} already exists")
            self._users[username] = {
                "username": username,
                "role": role if role in ("admin", "user") else "user",
                "password": _new_password_record(password),
            }
            self._save()

    def remove(self, username):
        with self._lock:
            entry = self._users.get(username)
            if entry is None:
                raise ValueError(f"unknown user {username}")
            admins = [e for e in self._users.values() if e["role"] == "admin"]
            if entry["role"] == "admin" and len(admins) == 1:
                raise ValueError("the last administrator cannot be removed")
            del self._users[username]
            self._save()

    def change_credentials(self, username, new_username, new_password):
        """Rename ``username`` and set its password (self-service)."""
        _validate_username(new_username)
        _validate_password(new_password)
        with self._lock:
            entry = self._users.get(username)
            if entry is None:
                raise ValueError(f"unknown user {username}")
            if new_username != username and new_username in self._users:
                raise ValueError(f"user {new_username} already exists")
            del self._users[username]
            entry["username"] = new_username
            entry["password"] = _new_password_record(new_password)
            self._users[new_username] = entry
            self._save()


def _public_host(host):
    """Resolve a wildcard bind address to an address clients can reach."""
    if host not in ("0.0.0.0", "::", ""):
        return host
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("8.8.8.8", 80))
            return probe.getsockname()[0]
        finally:
            probe.close()
    except Exception:
        return "127.0.0.1"


def _find_free_port(base):
    """Return ``base`` if free, otherwise the next free port."""
    port = base
    while port < base + 100:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                port += 1
    return base


def _load_json_list(path):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except Exception:
            return []
    return []


def _config_display_value(key, value):
    """Render a saved config value for the config form input."""
    if value is None:
        return ""
    if key == "is_custom_keys" and isinstance(value, list):
        return json.dumps(value)
    return value


class ServerWebApp:
    """Flask app + TCP_Server_Base wrapper for the web tool."""

    def __init__(self, web_port=None):
        self.web_port = web_port or DEFAULT_WEB_PORT
        self.server = None
        self.mode = "config"  # "config" | "status"
        self._bound_port = None
        self._last_clients = set()
        self._monitor_stop = threading.Event()
        self._monitor_thread = None
        self._events = []  # inbound events surfaced to the frontend (/api/events)
        self._events_lock = threading.Lock()
        self._event_seq = 0

        self.users = UserStore()
        self.app = Flask(
            __name__,
            template_folder=TEMPLATE_DIR,
            static_folder=STATIC_DIR,
            static_url_path="/static",
        )
        self.app.secret_key = _load_or_create_secret_key()
        self.app.config.update(
            SESSION_COOKIE_HTTPONLY=True,
            SESSION_COOKIE_SAMESITE="Lax",
        )

        self._register_routes()

    # ------------------------------------------------------------------ setup

    def start_from_config(self):
        """Read ``.Flow_Web/setup_server.json`` and start the TCP server."""
        if not os.path.exists(SERVER_CONFIG_FILE):
            self.mode = "config"
            return
        with open(SERVER_CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        servers = data.get("servers", [])
        if not servers:
            self.mode = "config"
            return
        web = data.get("web", {}) or {}
        self.web_port = int(web.get("port", DEFAULT_WEB_PORT))
        self._start_server(servers[0])

    def _start_server(self, config):
        """Create, register and start the TCP_Server_Base instance."""
        params = dict(config)
        # Web architecture constraints: extensions must be registered
        # before start, and the web UI replaces the console input.
        params["is_extend_command"] = True
        params["is_input_command_in_console"] = False
        if params.get("is_custom_keys") in (None, ""):
            params["is_custom_keys"] = None
        elif isinstance(params["is_custom_keys"], str):
            try:
                parsed = json.loads(params["is_custom_keys"])
                params["is_custom_keys"] = parsed if isinstance(parsed, list) else None
            except Exception:
                params["is_custom_keys"] = None
        self.server = TCP_Server_Base(**params)
        forward_extension_tcp.setup_server_commands(self.server)
        self.server.register_command(
            "/web_sync_clients", self._on_sync_clients, where_to_run="server", run_in_thread=True
        )
        self.server.add_message_listener(self._on_incoming_message)
        self.server.add_file_listener(self._on_incoming_file)
        try:
            add_extension.load_registered_extensions(self.server, "server")
        except ImportError as e:
            print(f"Failed to load registered extensions: {e}")
        threading.Thread(target=self.server.start_TCP_Server, daemon=True).start()
        self.mode = "status"
        self._last_clients = set()
        self._monitor_stop.clear()
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()
        print(f"TCP server started: {self.server.host}:{self.server.port}")

    # ------------------------------------------------------------- monitoring

    def _monitor_loop(self):
        """Broadcast the client list on connect/disconnect; re-check every minute."""
        last_check = time.time()
        while not self._monitor_stop.is_set():
            time.sleep(1)
            if self.server is None or not self.server.running:
                continue
            with self.server.client_lock:
                current = set(self.server.clients.keys())
            if current != self._last_clients:
                self._last_clients = current
                self._broadcast_clients()
            if time.time() - last_check >= 60:
                last_check = time.time()
                self._broadcast_clients()

    def _client_list(self):
        if self.server is None:
            return []
        with self.server.client_lock:
            return [
                {"ip": addr[0], "port": addr[1], "id": info["id"]}
                for addr, info in self.server.clients.items()
            ]

    def _server_info_payload(self):
        return {
            "host": _public_host(self.server.host),
            "port": self.server.port,
            "is_enable_encrypto": self.server.is_enable_encrypto,
        }

    def _broadcast_clients(self):
        """Push the current instance list to every connected client."""
        if self.server is None or not self.server.running:
            return
        payload = json.dumps(self._client_list(), separators=(",", ":"))
        message = f"/web_clients_update {payload}"
        with self.server.client_lock:
            for info in list(self.server.clients.values()):
                try:
                    self.server.send_message(info["socket"], message)
                except Exception:
                    pass

    def _on_sync_clients(self, sock, addr, cmd):
        """A client asked for a fresh instance list: broadcast it."""
        self._broadcast_clients()
        return None

    # ------------------------------------------------ inbound event handling

    def _push_event(self, event):
        """Record an inbound event with a monotonically increasing id."""
        with self._events_lock:
            self._event_seq += 1
            event["id"] = self._event_seq
            self._events.append(event)
            if len(self._events) > 1000:
                del self._events[: len(self._events) - 1000]
        return self._event_seq

    def _on_incoming_message(self, client_id, text):
        """Server receive thread: a plain-text message arrived from a client."""
        text = (text or "").strip()
        if not text:
            return
        self._push_event(
            {"type": "msg", "from": client_id, "text": text, "at": time.strftime("%H:%M:%S")}
        )

    def _on_incoming_file(self, client_id, full_path, name, size, command):
        """Server receive thread: a file uploaded by a client was saved."""
        try:
            cmd_name = (command or "").strip().split(" ", 1)[0].lower()
        except Exception:
            cmd_name = ""
        if cmd_name == "/crypto_pub_key":  # handshake keys are not user data
            return
        rel = full_path
        if self.server is not None:
            try:
                candidate = os.path.relpath(full_path, self.server.file_transfer_dir)
                if not candidate.startswith(".."):
                    rel = candidate
            except Exception:
                pass
        self._push_event(
            {
                "type": "file",
                "name": name,
                "path": rel,
                "size": size,
                "from": client_id,
                "at": time.strftime("%H:%M:%S"),
            }
        )

    # ---------------------------------------------------------------- helpers

    def _require_server(self):
        if self.server is None or not self.server.running:
            return jsonify({"ok": False, "error": "TCP server is not running"}), 503
        return None

    # ------------------------------------------------------------------- auth

    def _current_user(self):
        """Session user re-resolved against the store, so removed users lose access."""
        username = session.get("username")
        if not username:
            return None
        user = self.users.get(username)
        if user is None:
            session.clear()
            return None
        return user

    def _require_login(self):
        if self._current_user() is None:
            return jsonify({"ok": False, "error": "login required"}), 401
        return None

    def _require_admin(self):
        user = self._current_user()
        if user is None:
            return jsonify({"ok": False, "error": "login required"}), 401
        if user["role"] != "admin":
            return jsonify({"ok": False, "error": "administrator privileges required"}), 403
        return None

    def _default_admin_credentials(self, user, password):
        """True only while the seeded username and the seeded password are both in use."""
        return (
            user["role"] == "admin"
            and user["username"] == DEFAULT_ADMIN_USERNAME
            and password == DEFAULT_ADMIN_PASSWORD
        )

    def _page_context(self, user):
        return {
            "role": user["role"],
            "username": user["username"],
            "must_change_credentials": bool(session.get("must_change_credentials")),
        }

    def _landing_context(self):
        """Addresses shown on the landing page (``None`` while the server is down)."""
        if self.server is None or not self.server.running:
            return None
        host = _public_host(self.server.host)
        return {
            "web": f"http://{host}:{self._bound_port or self.web_port}/",
            "tcp": f"{host}:{self.server.port}",
        }

    def _config_form_fields(self):
        """``(server, web)`` form rows for the startup-configuration page."""
        current = {}
        web_port = self.web_port
        if os.path.exists(SERVER_CONFIG_FILE):
            try:
                with open(SERVER_CONFIG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                servers = data.get("servers", [])
                if servers:
                    current = servers[0]
                web = data.get("web", {}) or {}
                web_port = int(web.get("port", web_port))
            except Exception:
                pass
        fields = [
            (key, label, ftype, _config_display_value(key, current.get(key, default)), help)
            for key, label, ftype, default, help in SERVER_PARAM_FIELDS
        ]
        web_fields = [
            (key, label, ftype, web_port, help) for key, label, ftype, default, help in WEB_FIELDS
        ]
        return fields, web_fields

    def _render_config(self, user):
        """Render the startup-configuration page for an authenticated administrator."""
        fields, web_fields = self._config_form_fields()
        return render_template(
            "server_config.html",
            fields=fields,
            web_fields=web_fields,
            **self._page_context(user),
        )

    def _target_info(self, target):
        addr = (target[0], int(target[1]))
        with self.server.client_lock:
            return self.server.clients.get(addr)

    def _run_extension(self, handler, command):
        try:
            self.server._execute_custom_handler(handler, command)
        except Exception:
            traceback.print_exc()

    def _send_file_to_client(self, target, path, destination=None):
        try:
            message = f"/file {shlex.quote(path)} {shlex.quote(str(target))}"
            if destination:
                message += f" {shlex.quote(destination)}"
            self.server.file_transfer_server_recv_client_start(message, None)
        except Exception:
            traceback.print_exc()

    def _send_folder_to_client(self, target, path, destination=None):
        try:
            message = f"/file_folder {shlex.quote(path)} {shlex.quote(str(target))}"
            if destination:
                message += f" {shlex.quote(destination)}"
            self.server.folder_file_transfer_server_recv_client_start(message)
        except Exception:
            traceback.print_exc()

    def _restart(self):
        time.sleep(1)
        # Spawn a fresh process and exit: ``os.execv`` would keep the Flask
        # dev-server socket (no FD_CLOEXEC) alive and strand the old web port.
        try:
            subprocess.Popen(
                [sys.executable] + sys.argv,
                close_fds=True,
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            traceback.print_exc()
        os._exit(0)

    def _stop_server(self):
        """Stop the running TCP server and release its port."""
        if self.server is None:
            return
        try:
            # Unblock the accept thread so the port is released before the
            # new server binds (``stop()`` alone leaves it held).
            self.server.server_socket.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            self.server.stop()
        except Exception:
            traceback.print_exc()

    # ------------------------------------------------------------------ routes

    def _register_routes(self):
        app = self.app

        def login_required(view):
            """Reject requests without a valid session."""

            @functools.wraps(view)
            def wrapped(*args, **kwargs):
                err = self._require_login()
                if err is not None:
                    return err
                return view(*args, **kwargs)

            return wrapped

        def admin_required(view):
            """Reject requests from users that are not administrators."""

            @functools.wraps(view)
            def wrapped(*args, **kwargs):
                err = self._require_admin()
                if err is not None:
                    return err
                return view(*args, **kwargs)

            return wrapped

        @app.get("/")
        def index():
            user = self._current_user()
            if user is None:
                return render_template("server_landing.html", hint=self._landing_context())
            if self.mode == "status" or user["role"] != "admin":
                # The startup-configuration page is administrator-only.
                return render_template(
                    "server_status.html", mode="server", **self._page_context(user)
                )
            return self._render_config(user)

        @app.get("/config")
        def config():
            """Startup-configuration page, reachable from the status page too."""
            user = self._current_user()
            if user is None or user["role"] != "admin":
                return redirect("/")
            return self._render_config(user)

        @app.get("/api/status")
        @login_required
        def api_status():
            return jsonify(
                {
                    "mode": self.mode,
                    "running": self.server is not None and self.server.running,
                    "server_info": self._server_info_payload() if self.server is not None else None,
                    "clients": self._client_list(),
                    "pid": os.getpid(),
                }
            )

        @app.post("/api/save_config")
        @admin_required
        def api_save_config():
            data = request.get_json(force=True)
            params = data.get("params", {})
            web_port = int(data.get("web_port", DEFAULT_WEB_PORT))
            os.makedirs(FLOW_WEB_DIR, exist_ok=True)
            config = {"servers": [params], "clients": [], "web": {"port": web_port}}
            with open(SERVER_CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=4, ensure_ascii=False)
            self.web_port = web_port
            if self.server is not None and web_port == self._bound_port:
                # Same web port: restart the TCP server in place; the Flask
                # app stays up, so there is no dead window.
                self._stop_server()
                try:
                    self._start_server(params)
                except Exception as e:
                    traceback.print_exc()
                    return jsonify({"ok": False, "error": f"failed to start TCP server: {e}"}), 500
                return jsonify({"ok": True, "server_info": self._server_info_payload()})
            if self.server is not None:
                # Web port changed: the Flask app cannot rebind, so restart
                # the whole process for the new port to take effect.
                threading.Thread(target=self._restart, daemon=True).start()
                return jsonify({"ok": True, "restarting": True})
            try:
                self._start_server(params)
            except Exception as e:
                traceback.print_exc()
                return jsonify({"ok": False, "error": f"failed to start TCP server: {e}"}), 500
            if self._bound_port is not None and web_port != self._bound_port:
                # the web port only takes effect on the next start
                threading.Thread(target=self._restart, daemon=True).start()
                return jsonify({"ok": True, "restarting": True})
            return jsonify({"ok": True, "server_info": self._server_info_payload()})

        @app.get("/api/server_info")
        def api_server_info():
            """TCP server address/port discovery for web clients."""
            if self.server is None or not self.server.running:
                return jsonify({"ok": False, "error": "TCP server is not running"}), 503
            return jsonify(self._server_info_payload())

        @app.get("/api/clients")
        @login_required
        def api_clients():
            return jsonify({"clients": self._client_list()})

        @app.get("/api/events")
        @login_required
        def api_events():
            since = request.args.get("since", 0, type=int)
            with self._events_lock:
                events = [e for e in self._events if e["id"] > since]
                latest = events[-1]["id"] if events else since
            return jsonify({"events": events, "latest": latest})

        @app.post("/api/send_msg")
        @login_required
        def api_send_msg():
            err = self._require_server()
            if err:
                return err
            data = request.get_json(force=True)
            target = data.get("target")
            message = data.get("message", "")
            if target == "server":
                return jsonify({"ok": False, "error": "the server cannot send to itself"}), 400
            info = self._target_info(target)
            if info is None:
                return jsonify({"ok": False, "error": "target client is not connected"}), 404
            try:
                self.server.send_message(info["socket"], message)
            except Exception as e:
                return jsonify({"ok": False, "error": str(e)}), 500
            return jsonify({"ok": True})

        @app.post("/api/send_file")
        @login_required
        def api_send_file():
            err = self._require_server()
            if err:
                return err
            target = json.loads(request.form.get("target", "null"))
            files = request.files.getlist("files")
            if not files:
                return jsonify({"ok": False, "error": "no files uploaded"}), 400
            if target == "server":
                return jsonify({"ok": False, "error": "the server cannot send to itself"}), 400
            info = self._target_info(target)
            if info is None:
                return jsonify({"ok": False, "error": "target client is not connected"}), 404
            destination = request.form.get("destination") or None
            os.makedirs(UPLOAD_DIR, exist_ok=True)
            saved = []
            for f in files:
                path = os.path.join(UPLOAD_DIR, os.path.basename(f.filename))
                f.save(path)
                saved.append(path)
            for path in saved:
                threading.Thread(
                    target=self._send_file_to_client,
                    args=(tuple(target), path, destination),
                    daemon=True,
                ).start()
            return jsonify({"ok": True, "paths": saved})

        @app.post("/api/send_folder")
        @login_required
        def api_send_folder():
            err = self._require_server()
            if err:
                return err
            target = json.loads(request.form.get("target", "null"))
            files = request.files.getlist("files")
            if not files:
                return jsonify({"ok": False, "error": "no files uploaded"}), 400
            if target == "server":
                return jsonify({"ok": False, "error": "the server cannot send to itself"}), 400
            info = self._target_info(target)
            if info is None:
                return jsonify({"ok": False, "error": "target client is not connected"}), 404
            destination = request.form.get("destination") or None
            os.makedirs(UPLOAD_DIR, exist_ok=True)
            root = None
            for f in files:
                rel = f.filename  # webkitRelativePath, e.g. "folder/sub/file.txt"
                path = os.path.join(UPLOAD_DIR, rel)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                f.save(path)
                if root is None:
                    root = os.path.join(UPLOAD_DIR, rel.split("/")[0])
            if root is None or not os.path.isdir(root):
                return jsonify({"ok": False, "error": "folder upload failed"}), 500
            threading.Thread(
                target=self._send_folder_to_client,
                args=(tuple(target), root, destination),
                daemon=True,
            ).start()
            return jsonify({"ok": True, "path": root})

        @app.post("/api/run_extension")
        @admin_required
        def api_run_extension():
            err = self._require_server()
            if err:
                return err
            data = request.get_json(force=True)
            command = data.get("command", "")
            parts = shlex.split(command)
            if not parts:
                return jsonify({"ok": False, "error": "empty command"}), 400
            handler = self.server._custom_handlers[1].get(parts[0].lower())
            if handler is None:
                return jsonify({"ok": False, "error": f"command {parts[0]} is not registered"}), 404
            threading.Thread(target=self._run_extension, args=(handler, command), daemon=True).start()
            return jsonify({"ok": True})

        @app.get("/api/available_commands")
        @admin_required
        def api_available_commands():
            err = self._require_server()
            if err:
                return err
            return jsonify({"commands": sorted(self.server._custom_handlers[1].keys())})

        @app.post("/api/sync_clients")
        @login_required
        def api_sync_clients():
            self._broadcast_clients()
            return jsonify({"ok": True})

        @app.get("/api/extensions_ui")
        @admin_required
        def api_get_extensions_ui():
            return jsonify({"extensions": _load_json_list(SERVER_EXTENSIONS_UI_FILE)})

        @app.get("/api/registered_extensions")
        @admin_required
        def api_registered_extensions():
            return jsonify({"extensions": _load_json_list(add_extension.added_extensions_log_file)})

        @app.post("/api/extensions_ui")
        @admin_required
        def api_save_extensions_ui():
            data = request.get_json(force=True)
            entries = data.get("extensions", [])
            os.makedirs(FLOW_WEB_DIR, exist_ok=True)
            with open(SERVER_EXTENSIONS_UI_FILE, "w", encoding="utf-8") as f:
                json.dump(entries, f, indent=4, ensure_ascii=False)
            return jsonify({"ok": True})

        @app.post("/api/add_extension")
        @admin_required
        def api_add_extension():
            data = request.get_json(force=True)
            paths = data.get("paths", [])
            try:
                add_extension.add_extension(paths)
            except Exception as e:
                return jsonify({"ok": False, "error": str(e)}), 400
            threading.Thread(target=self._restart, daemon=True).start()
            return jsonify({"ok": True, "restarting": True})

        @app.post("/api/remove_extension")
        @admin_required
        def api_remove_extension():
            data = request.get_json(force=True)
            paths = data.get("paths", [])
            try:
                add_extension.remove_extension(paths)
            except Exception as e:
                return jsonify({"ok": False, "error": str(e)}), 400
            threading.Thread(target=self._restart, daemon=True).start()
            return jsonify({"ok": True, "restarting": True})

        # ------------------------------------------------------------- accounts

        @app.post("/api/login")
        def api_login():
            data = request.get_json(silent=True) or {}
            username = str(data.get("username") or "").strip()
            password = str(data.get("password") or "")
            user = self.users.authenticate(username, password)
            if user is None:
                return jsonify({"ok": False, "error": "invalid username or password"}), 401
            session.clear()
            session["username"] = user["username"]
            session["role"] = user["role"]
            session["must_change_credentials"] = self._default_admin_credentials(user, password)
            return jsonify(
                {
                    "ok": True,
                    "username": user["username"],
                    "role": user["role"],
                    "must_change_credentials": session["must_change_credentials"],
                    "redirect": "/",
                }
            )

        @app.post("/api/logout")
        def api_logout():
            session.clear()
            return jsonify({"ok": True})

        @app.post("/api/account")
        @login_required
        def api_account():
            """Change own username/password; the current password is required."""
            user = self._current_user()
            data = request.get_json(silent=True) or {}
            current_password = str(data.get("current_password") or "")
            new_username = str(data.get("username") or "").strip()
            new_password = str(data.get("password") or "")
            if self.users.authenticate(user["username"], current_password) is None:
                return jsonify({"ok": False, "error": "current password is incorrect"}), 403
            try:
                self.users.change_credentials(user["username"], new_username, new_password)
            except ValueError as e:
                return jsonify({"ok": False, "error": str(e)}), 400
            session["username"] = new_username
            session["must_change_credentials"] = self._default_admin_credentials(
                {"username": new_username, "role": user["role"]}, new_password
            )
            return jsonify(
                {
                    "ok": True,
                    "username": new_username,
                    "must_change_credentials": session["must_change_credentials"],
                }
            )

        @app.get("/api/users")
        @admin_required
        def api_users():
            return jsonify({"users": self.users.list()})

        @app.post("/api/users")
        @admin_required
        def api_add_user():
            data = request.get_json(silent=True) or {}
            try:
                self.users.add(
                    str(data.get("username") or "").strip(),
                    str(data.get("password") or ""),
                    data.get("role"),
                )
            except ValueError as e:
                return jsonify({"ok": False, "error": str(e)}), 400
            return jsonify({"ok": True, "users": self.users.list()})

        @app.post("/api/users/delete")
        @admin_required
        def api_delete_user():
            user = self._current_user()
            data = request.get_json(silent=True) or {}
            username = str(data.get("username") or "").strip()
            if username == user["username"]:
                return jsonify({"ok": False, "error": "you cannot remove your own account"}), 400
            try:
                self.users.remove(username)
            except ValueError as e:
                return jsonify({"ok": False, "error": str(e)}), 400
            return jsonify({"ok": True, "users": self.users.list()})

    # ------------------------------------------------------------------- run

    def run(self):
        host = "127.0.0.1" if self.mode == "config" else self.server.host
        port = _find_free_port(self.web_port)
        if port != self.web_port:
            print(f"Web port {self.web_port} busy, using {port}")
        self._bound_port = port
        threading.Thread(target=self._open_browser, args=(port,), daemon=True).start()
        self.app.run(host=host, port=port, threaded=True, use_reloader=False)

    def _open_browser(self, port):
        time.sleep(1.5)
        try:
            import webbrowser

            webbrowser.open(f"http://127.0.0.1:{port}/")
        except Exception:
            pass
