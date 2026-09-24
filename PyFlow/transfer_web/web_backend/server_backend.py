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
addresses, a login button, a registration button and a password-reset button);
the configuration and status pages need a session.  Accounts live in the SQLite
store ``.Flow_Web/flow_web.db`` (see `user_database`); the first run seeds the
``admin``/``admin`` administrator and the frontend warns on every login until
those default credentials are changed.  Registration, password reset and
code-based client logins are delivered by the mailbox configured from the
startup-configuration page (see `mail_service`).

Client accounts: the client web frontend logs in through ``/api/client_login``
with a username/email, the account password *and* a mailed verification code —
both factors are required — and receives a session token.  Re-entering the
client web UI replays the saved credentials and token through
``/api/client_verify``, which only accepts them while they still open that
account.  The token binds the TCP connection the client opens (``/web_bind``) to
that account, and the instance list pushed to a client is limited to its
contacts, so accounts that never exchanged a contact request cannot see each
other.
"""

import functools
import json
import os
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
from PyFlow.transfer_web.web_backend.mail_service import MailService
from PyFlow.transfer_web.web_backend.user_database import (
    DEFAULT_ADMIN_PASSWORD,
    DEFAULT_ADMIN_USERNAME,
    UserDatabase,
    mask_email,
)

WEB_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FLOW_WEB_DIR = os.path.join(WEB_ROOT, ".Flow_Web")
SERVER_CONFIG_FILE = os.path.join(FLOW_WEB_DIR, "setup_server.json")
SERVER_EXTENSIONS_UI_FILE = os.path.join(FLOW_WEB_DIR, "server_extensions_ui.json")
UPLOAD_DIR = os.path.join(FLOW_WEB_DIR, "uploads")
TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
STATIC_DIR = os.path.join(WEB_ROOT, "static")

DEFAULT_WEB_PORT = 5000

# Account store and verification-mail service: both keep their state in
# .Flow_Web (flow_web.db and email_config.json).
SECRET_KEY_FILE = os.path.join(FLOW_WEB_DIR, "web_secret_key")

# TCP command a client sends to bind its connection to its account, and the
# acknowledgement carrying the address the server sees for that connection.
BIND_COMMAND = "/web_bind"
BIND_OK_COMMAND = "/web_bind_ok"

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
    (
        "is_asynic_clients_io",
        "Asyncio clients io",
        "bool",
        False,
        "Serve clients with asyncio coroutines; max_clients is then ignored.",
    ),
    ("is_debug", "Debug log", "bool", False, "Log execution-process lines as well."),
    ("is_print_log", "Print log", "bool", True, "Log at all; False silences the instance."),
]

# Web-only settings (not TCP_Server_Base parameters).
WEB_FIELDS = [
    ("web_port", "Web port", "number", DEFAULT_WEB_PORT, "Port of this web backend (clients query it)."),
]

# The web "ftp" share is not FTP: it browses one folder on the server host and
# hands selected entries to clients over the protocol's native /file and
# /file_folder transfers.  The shared folder is kept in the startup config
# (``web.ftp_root``), so a restart keeps serving it.
FTP_LIST_COMMAND = "/ftp_list"
FTP_GET_COMMAND = "/ftp_get"
FTP_LIST_OK_COMMAND = "/ftp_list_ok"
FTP_GET_OK_COMMAND = "/ftp_get_ok"
FTP_ERROR_COMMAND = "/ftp_error"


def _ftp_resolve(root, rel_path):
    """Resolve one share-relative path, refusing anything outside ``root``.

    Args:
        root (str): Absolute shared folder.
        rel_path (str): Path relative to the share; "" is the share itself.

    Returns:
        tuple: ``(absolute_path, clean_relative_path)``.

    Raises:
        ValueError: If the path is absolute or escapes the shared folder.
    """
    raw = (rel_path or "").strip()
    rel = raw.replace("\\", "/")
    # "/etc" is drive-relative on Windows (os.path.isabs is False there), so a
    # leading separator and any drive prefix are refused on top of isabs
    if os.path.isabs(raw) or rel.startswith("/") or os.path.splitdrive(raw)[0]:
        raise ValueError("path must be relative to the shared folder")
    rel = rel.strip("/")
    if ".." in rel.split("/"):
        raise ValueError("path must stay inside the shared folder")
    root_real = os.path.realpath(root)
    target = os.path.realpath(os.path.join(root_real, rel)) if rel else root_real
    if target != root_real and not target.startswith(root_real + os.sep):
        raise ValueError("path must stay inside the shared folder")
    return target, rel


def _ftp_listing(root, rel_path):
    """Build the listing of one folder inside the shared root.

    Args:
        root (str): Absolute shared folder.
        rel_path (str): Folder to list, relative to the root ("" is the root).

    Returns:
        dict: ``{"path", "parent", "entries"}``; every entry is
            ``{"name", "dir", "size", "mtime"}``, folders first, then files,
            each group sorted by name.

    Raises:
        ValueError: If the folder escapes the share or is not a folder.
        OSError: If the folder cannot be read.
    """
    target, rel = _ftp_resolve(root, rel_path)
    if not os.path.isdir(target):
        raise ValueError(f"not a folder: {rel or '/'}")
    entries = []
    for name in sorted(os.listdir(target), key=str.lower):
        full = os.path.join(target, name)
        try:
            is_dir = os.path.isdir(full)
            stat = os.stat(full)
        except OSError:
            continue
        entries.append(
            {
                "name": name,
                "dir": is_dir,
                "size": 0 if is_dir else stat.st_size,
                "mtime": int(stat.st_mtime),
            }
        )
    entries.sort(key=lambda entry: (not entry["dir"], entry["name"].lower()))
    parent = None if rel == "" else (os.path.dirname(rel) or "")
    return {"path": rel, "parent": parent, "entries": entries}

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


def _read_config_file():
    """Return the saved startup config, or ``{}`` when it is absent or unreadable."""
    if not os.path.exists(SERVER_CONFIG_FILE):
        return {}
    try:
        with open(SERVER_CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_config_file(config):
    """Persist the startup config as indented JSON."""
    os.makedirs(FLOW_WEB_DIR, exist_ok=True)
    with open(SERVER_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4, ensure_ascii=False)


def _config_display_value(key, value):
    """Render a saved config value for the config form input."""
    if value is None:
        return ""
    if key == "is_custom_keys" and isinstance(value, list):
        return json.dumps(value)
    return value


class ServerWebApp:
    """Flask app + TCP_Server_Base wrapper for the web tool."""

    def __init__(self, web_port=None, db_path=None, mail_config_path=None):
        """Create the Flask app and its stores.

        Args:
            web_port (int | None): Port of this web backend; defaults to
                ``DEFAULT_WEB_PORT``. A busy port falls back to the next free one.
            db_path (str | None): SQLite account database; defaults to
                ``.Flow_Web/flow_web.db``.
            mail_config_path (str | None): JSON file with the SMTP settings;
                defaults to ``.Flow_Web/email_config.json``.
        """
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
        self._addr_tokens = {}  # connected client address -> account session token
        self._bind_lock = threading.Lock()
        self.ftp_root = None  # shared folder of the web "ftp" server (host path)
        self._ftp_lock = threading.Lock()

        self.users = UserDatabase(db_path)
        self.mail = MailService(mail_config_path)
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
        """Read ``.Flow_Web/setup_server.json`` and start the TCP server.

        The saved "ftp" share is restored as well, so a folder shared before the
        restart keeps being served.
        """
        data = _read_config_file()
        servers = data.get("servers", [])
        if not servers:
            self.mode = "config"
            return
        web = data.get("web", {}) or {}
        self.web_port = int(web.get("port", DEFAULT_WEB_PORT))
        ftp_root = web.get("ftp_root")
        if isinstance(ftp_root, str) and os.path.isdir(ftp_root):
            with self._ftp_lock:
                self.ftp_root = ftp_root
        self._start_server(servers[0])

    def _start_server(self, config):
        """Create, register and start the TCP_Server_Base instance."""
        params = dict(config)
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
        self.server.register_command(
            BIND_COMMAND, self._on_web_bind, where_to_run="server", run_in_thread=True
        )
        self._register_ftp_commands()
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
            with self._bind_lock:
                for addr in [a for a in self._addr_tokens if a not in current]:
                    del self._addr_tokens[addr]
            if current != self._last_clients:
                self._last_clients = current
                self._broadcast_clients()
            if time.time() - last_check >= 60:
                last_check = time.time()
                self._broadcast_clients()

    def _connected_entries(self):
        """Return one entry per connected TCP client, as the sidebar shows them.

        Returns:
            list[dict]: ``{"ip", "port", "id"}`` per connection, in the order the
                server holds them.
        """
        if self.server is None:
            return []
        with self.server.client_lock:
            return [
                {"ip": addr[0], "port": addr[1], "id": info["id"]}
                for addr, info in self.server.clients.items()
            ]

    def _server_info_payload(self):
        """Return the TCP address/port web clients connect to.

        Returns:
            dict: ``{"host", "port", "is_enable_encrypto"}`` of the running TCP
                server; ``host`` is resolved when it binds a wildcard address.
        """
        return {
            "host": _public_host(self.server.host),
            "port": self.server.port,
            "is_enable_encrypto": self.server.is_enable_encrypto,
        }

    def _account_of(self, addr):
        """Resolve the account bound to a connected client address.

        Args:
            addr (tuple): Peer ``(ip, port)`` of the TCP connection.

        Returns:
            dict | None: Account without its password, or ``None`` while the
                connection has not bound a valid session token.
        """
        with self._bind_lock:
            token = self._addr_tokens.get(tuple(addr))
        return self.users.session_user(token) if token else None

    def _bound_addresses(self):
        """Map every bound account to the address its client connects from.

        Returns:
            dict: ``user_id`` -> ``{"ip", "port"}`` for the accounts with a live
                bound connection.
        """
        with self._bind_lock:
            bound = dict(self._addr_tokens)
        mapping = {}
        for addr, token in bound.items():
            user = self.users.session_user(token)
            if user is not None:
                mapping.setdefault(user["user_id"], {"ip": addr[0], "port": addr[1]})
        return mapping

    def _client_list(self):
        """List every connected instance with the account bound to it.

        This is the operator view used by the server console.

        Returns:
            list[dict]: One entry per connected client; bound entries also carry
                ``user_id``, ``username`` and ``email``.
        """
        entries = []
        for entry in self._connected_entries():
            account = self._account_of((entry["ip"], entry["port"]))
            if account is not None:
                entry.update(account)
            entries.append(entry)
        return entries

    def _client_list_for(self, addr):
        """List the connected instances one client is allowed to see.

        A client sees itself nowhere and sees another account only once the two
        are contacts; an unbound connection sees no other instance at all.

        Args:
            addr (tuple): Peer ``(ip, port)`` of the requesting connection.

        Returns:
            list[dict]: Contact entries, each with ``ip``, ``port``, ``id``,
                ``user_id``, ``username`` and ``email``.
        """
        user = self._account_of(addr)
        if user is None:
            return []
        contacts = {c["user_id"]: c for c in self.users.contacts(user["user_id"])}
        entries = []
        for entry in self._connected_entries():
            account = self._account_of((entry["ip"], entry["port"]))
            if account is None or account["user_id"] not in contacts:
                continue
            entry.update(account)
            entries.append(entry)
        return entries

    def _broadcast_clients(self):
        """Push every connected client the instance list it may see."""
        if self.server is None or not self.server.running:
            return
        with self.server.client_lock:
            targets = list(self.server.clients.items())
        for addr, info in targets:
            payload = json.dumps(self._client_list_for(addr), separators=(",", ":"))
            try:
                self.server.send_message(info["socket"], f"/web_clients_update {payload}")
            except Exception:
                pass

    def _on_sync_clients(self, sock, addr, cmd):
        """A client asked for a fresh instance list: broadcast it."""
        self._broadcast_clients()
        return None

    def _on_web_bind(self, sock, addr, cmd):
        """Bind a client connection to the account owning the session token.

        Server side of ``/web_bind <token>``: the client sends the token it got
        from ``/api/client_login`` right after connecting, which is what lets the
        server filter the instance list by contacts. The client is told which
        address the server sees for it, so it can recognise its own entry.
        """
        token = cmd[len(BIND_COMMAND) :].strip()
        user = self.users.session_user(token)
        if user is None:
            return None
        with self._bind_lock:
            self._addr_tokens[tuple(addr)] = token
        try:
            self.server.send_message(
                sock, f"{BIND_OK_COMMAND} {json.dumps({'ip': addr[0], 'port': addr[1]})}"
            )
        except Exception:
            traceback.print_exc()
        self._broadcast_clients()
        return None

    # ------------------------------------------------------------- "ftp" share

    def _register_ftp_commands(self):
        """Register the web "ftp" share commands on the running TCP server."""
        if self.server is None:
            return
        self.server.register_command(
            FTP_LIST_COMMAND, self._on_ftp_list, where_to_run="server", run_in_thread=True
        )
        self.server.register_command(
            FTP_GET_COMMAND, self._on_ftp_get, where_to_run="server", run_in_thread=True
        )

    def _ftp_shared_root(self):
        """Return the shared folder, or None while nothing is shared."""
        with self._ftp_lock:
            return self.ftp_root

    def _persist_ftp_root(self, root):
        """Remember the shared folder in the startup config so a restart restores it.

        Args:
            root (str | None): Folder now shared, or ``None`` once the share is
                taken away.
        """
        config = _read_config_file()
        web = dict(config.get("web") or {})
        if root:
            web["ftp_root"] = root
        else:
            web.pop("ftp_root", None)
        config["servers"] = config.get("servers") or []
        config.setdefault("clients", [])
        config["web"] = web
        try:
            _write_config_file(config)
        except OSError:
            traceback.print_exc()

    def _on_ftp_list(self, sock, addr, cmd):
        """Server side of ``/ftp_list``: answer with the folder listing.

        Not FTP: the answer is a protocol line (``/ftp_list_ok`` or
        ``/ftp_error``) carrying a JSON listing of one folder inside the
        shared root.
        """
        parts = cmd.split(" ", 2)
        request_id = parts[1] if len(parts) > 1 else "?"
        raw = parts[2].strip() if len(parts) > 2 else ""
        try:  # the web client sends the path as a JSON string; a raw path also works
            decoded = json.loads(raw)
        except ValueError:
            decoded = None
        rel_path = decoded if isinstance(decoded, str) else raw
        root = self._ftp_shared_root()
        if not root:
            return f"{FTP_ERROR_COMMAND} {request_id} no folder is shared"
        try:
            listing = _ftp_listing(root, rel_path)
        except (ValueError, OSError) as e:
            return f"{FTP_ERROR_COMMAND} {request_id} {e}"
        return f"{FTP_LIST_OK_COMMAND} {request_id} {json.dumps(listing)}"

    def _on_ftp_get(self, sock, addr, cmd):
        """Server side of ``/ftp_get``: push the selected share entries.

        Every entry is handed to the protocol's native transfer (``/file`` for
        a file, ``/file_folder`` for a folder) addressed to the asking client,
        carrying the receiver-side destination the client asked for (the
        receiver's default transfer folder when it asked for none).

        The request body is the list of share-relative entries; a client may
        also send ``{"paths": [...], "destination": "..."}`` to pick where the
        entries land on its own host.
        """
        parts = cmd.split(" ", 2)
        request_id = parts[1] if len(parts) > 1 else "?"
        try:
            request = json.loads(parts[2]) if len(parts) > 2 else []
        except ValueError:
            return f"{FTP_ERROR_COMMAND} {request_id} malformed request"
        destination = None
        if isinstance(request, dict):
            raw_destination = request.get("destination")
            destination = str(raw_destination).strip() if raw_destination else None
            request = request.get("paths")
        if not isinstance(request, list):
            return f"{FTP_ERROR_COMMAND} {request_id} malformed request"
        root = self._ftp_shared_root()
        if not root:
            return f"{FTP_ERROR_COMMAND} {request_id} no folder is shared"
        if self._target_info(addr) is None:
            return f"{FTP_ERROR_COMMAND} {request_id} client is not connected"
        started = 0
        skipped = 0
        for entry in request:
            try:
                target, _rel = _ftp_resolve(root, str(entry))
            except ValueError:
                skipped += 1
                continue
            if not os.path.exists(target):
                skipped += 1
                continue
            if os.path.isdir(target):
                self._send_folder_to_client(tuple(addr), target, destination)
            else:
                self._send_file_to_client(tuple(addr), target, destination)
            started += 1
        return f"{FTP_GET_OK_COMMAND} {request_id} {started} {skipped}"

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
        user = self.users.find(username)
        if user is None:
            session.clear()
            return None
        return user

    def _session_user(self):
        """Resolve the client session token carried by a request.

        Returns:
            dict | None: Account bound to the token, or ``None`` for a missing
                or unknown token.
        """
        token = request.args.get("token", "")
        data = request.get_json(silent=True)
        if isinstance(data, dict) and data.get("token"):
            token = str(data["token"])
        return self.users.session_user(token)

    def _send_code(self, purpose, target):
        """Issue a verification code for an email address and deliver it.

        Args:
            purpose (str): "register", "login" or "reset_password".
            target (str): Recipient email address.

        Returns:
            dict: ``{"expires_in", "resend_after", "masked_email"}`` describing
                the delivered code.

        Raises:
            ValueError: If a code was requested for that purpose and address too
                recently, or the mailbox is not configured or refuses the
                message. A code that could not be delivered is dropped.
        """
        issued = self.users.issue_code(purpose, target)
        try:
            self.mail.send_code(target, issued["code"], purpose, issued["expires_in"])
        except ValueError:
            self.users.discard_codes(purpose, target)
            raise
        return {
            "expires_in": issued["expires_in"],
            "resend_after": issued["resend_after"],
            "masked_email": mask_email(target),
        }

    def _account_with_email(self, identify):
        """Return the account matching ``identify`` together with its email.

        Args:
            identify (str): User id, username or email address.

        Returns:
            tuple: ``(account, email)`` for the matching account.

        Raises:
            ValueError: If no account matches, or it has no email address.
        """
        account = self.users.find(identify)
        if account is None:
            raise ValueError("no account matches that user name or email")
        if not account["email"]:
            raise ValueError("this account has no email address; ask an administrator")
        return account, account["email"]

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

    def _default_admin_credentials_in_use(self, username):
        """Report whether the seeded administrator still accepts the seeded password.

        Args:
            username (str): Account whose session is being opened.

        Returns:
            bool: True only while the seeded username still logs in with
                ``DEFAULT_ADMIN_PASSWORD``.
        """
        return username == DEFAULT_ADMIN_USERNAME and (
            self.users.authenticate(DEFAULT_ADMIN_USERNAME, DEFAULT_ADMIN_PASSWORD) is not None
        )

    def _page_context(self, user):
        """Render the account details every console page shows."""
        return {
            "role": user["role"],
            "username": user["username"],
            "user_id": user["user_id"],
            "email": user["email"] or "",
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

        def client_required(view):
            """Reject requests without a valid client session token."""

            @functools.wraps(view)
            def wrapped(*args, **kwargs):
                user = self._session_user()
                if user is None:
                    return jsonify({"ok": False, "error": "login required"}), 401
                return view(user, *args, **kwargs)

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
            # Keep every web-only setting (the shared "ftp" folder among them)
            # that this form does not own.
            web = dict(_read_config_file().get("web") or {})
            web["port"] = web_port
            config = {"servers": [params], "clients": [], "web": web}
            _write_config_file(config)
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

        @app.get("/api/ftp")
        @login_required
        def api_ftp_status():
            root = self._ftp_shared_root()
            return jsonify(
                {"ok": True, "root": root, "shared": bool(root and os.path.isdir(root))}
            )

        @app.post("/api/ftp/add")
        @admin_required
        def api_ftp_add():
            data = request.get_json(force=True)
            raw = (data.get("path") or "").strip()
            if not raw:
                return jsonify({"ok": False, "error": "a folder path is required"}), 400
            path = os.path.abspath(os.path.expanduser(raw))
            if not os.path.isdir(path):
                return jsonify({"ok": False, "error": f"not a folder: {path}"}), 400
            with self._ftp_lock:
                self.ftp_root = path
            self._persist_ftp_root(path)
            self._register_ftp_commands()
            return jsonify({"ok": True, "root": path})

        @app.post("/api/ftp/remove")
        @admin_required
        def api_ftp_remove():
            with self._ftp_lock:
                self.ftp_root = None
            self._persist_ftp_root(None)
            return jsonify({"ok": True})

        @app.get("/api/ftp/list")
        @login_required
        def api_ftp_list():
            root = self._ftp_shared_root()
            if not root:
                return jsonify({"ok": False, "error": "no folder is shared"}), 404
            try:
                listing = _ftp_listing(root, request.args.get("path", ""))
            except (ValueError, OSError) as e:
                return jsonify({"ok": False, "error": str(e)}), 400
            return jsonify({"ok": True, "root": root, "listing": listing})

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

        # Public flows of the landing page: registration, password reset and
        # the mailbox that delivers their verification codes.

        @app.post("/api/register/send_code")
        def api_register_send_code():
            """Mail a registration code to an address that is still free."""
            data = request.get_json(silent=True) or {}
            email = str(data.get("email") or "").strip()
            try:
                if self.users.email_registered(email):
                    return jsonify({"ok": False, "error": "this email is already registered"}), 400
                return jsonify({"ok": True, **self._send_code("register", email)})
            except ValueError as e:
                return jsonify({"ok": False, "error": str(e)}), 400

        @app.post("/api/register")
        def api_register():
            """Create an account once the mailed code checks out."""
            data = request.get_json(silent=True) or {}
            try:
                user = self.users.register_with_code(
                    str(data.get("username") or ""),
                    str(data.get("email") or ""),
                    str(data.get("password") or ""),
                    str(data.get("code") or ""),
                )
            except ValueError as e:
                return jsonify({"ok": False, "error": str(e)}), 400
            return jsonify({"ok": True, "user": user})

        @app.post("/api/login/send_code")
        def api_login_send_code():
            """Mail a login code to the address of an existing account."""
            data = request.get_json(silent=True) or {}
            try:
                _, email = self._account_with_email(str(data.get("identify") or ""))
                return jsonify({"ok": True, **self._send_code("login", email)})
            except ValueError as e:
                return jsonify({"ok": False, "error": str(e)}), 400

        @app.post("/api/password/send_code")
        def api_password_send_code():
            """Mail a password-reset code to the address of an existing account."""
            data = request.get_json(silent=True) or {}
            try:
                _, email = self._account_with_email(str(data.get("identify") or ""))
                return jsonify({"ok": True, **self._send_code("reset_password", email)})
            except ValueError as e:
                return jsonify({"ok": False, "error": str(e)}), 400

        @app.post("/api/password/reset")
        def api_password_reset():
            """Set a new password once the mailed reset code checks out."""
            data = request.get_json(silent=True) or {}
            try:
                self.users.reset_password_with_code(
                    str(data.get("identify") or ""),
                    str(data.get("code") or ""),
                    str(data.get("password") or ""),
                )
            except ValueError as e:
                return jsonify({"ok": False, "error": str(e)}), 400
            return jsonify({"ok": True})

        # Client accounts: a web client logs in here and receives the session
        # token that binds its TCP connection to the account.

        @app.post("/api/client_login")
        def api_client_login():
            """Log a web client in against its password and a mailed verification code."""
            data = request.get_json(silent=True) or {}
            identify = str(data.get("identify") or "").strip()
            password = str(data.get("password") or "")
            code = str(data.get("code") or "").strip()
            if not identify:
                return jsonify({"ok": False, "error": "enter your user name or email"}), 400
            if not password or not code:
                return (
                    jsonify(
                        {
                            "ok": False,
                            "error": "enter the account password and the mailed verification code",
                        }
                    ),
                    400,
                )
            user = self.users.authenticate(identify, password)
            if user is None:
                return jsonify({"ok": False, "error": "invalid account or password"}), 401
            try:
                _, email = self._account_with_email(identify)
                self.users.verify_code("login", email, code)
            except ValueError as e:
                return jsonify({"ok": False, "error": str(e)}), 401
            token = self.users.create_session(user["user_id"])
            return jsonify({"ok": True, "token": token, "user": user})

        @app.post("/api/client_verify")
        def api_client_verify():
            """Check a stored client session, optionally against its saved credentials.

            A client that logs itself in again from
            ``.Flow_Web/client_login.json`` sends the saved account and password
            along with the token; the token alone only proves the session is
            known, while the credentials prove they still open that account.
            """
            data = request.get_json(silent=True) or {}
            user = self._session_user()
            if user is None:
                return jsonify({"ok": False, "error": "login required"}), 401
            identify = str(data.get("identify") or "").strip()
            password = str(data.get("password") or "")
            if bool(identify) != bool(password):
                return (
                    jsonify(
                        {"ok": False, "error": "send the saved account and its password together"}
                    ),
                    400,
                )
            if identify:
                account = self.users.authenticate(identify, password)
                if account is None or account["user_id"] != user["user_id"]:
                    return (
                        jsonify(
                            {
                                "ok": False,
                                "error": "the saved credentials no longer open this account",
                            }
                        ),
                        401,
                    )
            return jsonify({"ok": True, "user": user})

        @app.post("/api/client_logout")
        def api_client_logout():
            """Invalidate a client session token."""
            data = request.get_json(silent=True) or {}
            self.users.drop_session(str(data.get("token") or ""))
            self._broadcast_clients()
            return jsonify({"ok": True})

        # Contacts: a client only ever sees the accounts it is a contact of.

        @app.post("/api/contacts/search")
        @client_required
        def api_search_contacts(user):
            """Search accounts by user id, username or email."""
            data = request.get_json(silent=True) or {}
            try:
                matches = self.users.search_users(
                    str(data.get("query") or ""), user["user_id"]
                )
            except ValueError as e:
                return jsonify({"ok": False, "error": str(e)}), 400
            contacts = {c["user_id"] for c in self.users.contacts(user["user_id"])}
            requests = self.users.contact_requests(user["user_id"])
            outgoing = {r["user"]["user_id"] for r in requests["outgoing"]}
            incoming = {r["user"]["user_id"] for r in requests["incoming"]}
            online = self._bound_addresses()
            results = []
            for match in matches:
                entry = dict(match)
                if match["user_id"] in contacts:
                    entry["relation"] = "contact"
                elif match["user_id"] in outgoing:
                    entry["relation"] = "outgoing"
                elif match["user_id"] in incoming:
                    entry["relation"] = "incoming"
                else:
                    entry["relation"] = "none"
                entry["online"] = match["user_id"] in online
                results.append(entry)
            return jsonify({"ok": True, "results": results})

        @app.post("/api/contacts/request")
        @client_required
        def api_request_contact(user):
            """Ask another account to become a contact."""
            data = request.get_json(silent=True) or {}
            try:
                target = self.users.request_contact(
                    user["user_id"], str(data.get("user_id") or "")
                )
            except ValueError as e:
                return jsonify({"ok": False, "error": str(e)}), 400
            return jsonify({"ok": True, "user": target})

        @app.get("/api/contact_requests")
        @client_required
        def api_contact_requests(user):
            """List the pending contact requests of the logged-in client."""
            return jsonify({"ok": True, **self.users.contact_requests(user["user_id"])})

        @app.post("/api/contacts/respond")
        @client_required
        def api_respond_contact(user):
            """Accept or reject one incoming contact request."""
            data = request.get_json(silent=True) or {}
            try:
                request_id = int(data.get("request_id"))
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "invalid contact request"}), 400
            accept = bool(data.get("accept"))
            try:
                requester = self.users.respond_request(user["user_id"], request_id, accept)
            except ValueError as e:
                return jsonify({"ok": False, "error": str(e)}), 400
            self._broadcast_clients()
            return jsonify({"ok": True, "accepted": accept, "user": requester})

        # Verification mailbox (administrators only): the settings are checked
        # against the real server before they are stored.

        @app.get("/api/email_config")
        @admin_required
        def api_get_email_config():
            """Return the stored SMTP settings of the verification mailbox."""
            config = self.mail.get_config() or {}
            config.pop("password", None)
            return jsonify({"ok": True, "enabled": self.mail.is_enabled(), "config": config})

        @app.post("/api/email_config")
        @admin_required
        def api_set_email_config():
            """Validate and store the SMTP settings, starting the mail service."""
            data = request.get_json(silent=True) or {}
            try:
                config = self.mail.configure(data.get("config") or data)
            except ValueError as e:
                return jsonify({"ok": False, "error": str(e)}), 400
            config.pop("password", None)
            return jsonify({"ok": True, "enabled": True, "config": config})

        # Server console session and user administration.

        @app.post("/api/login")
        def api_login():
            """Open the console session of a server user."""
            data = request.get_json(silent=True) or {}
            identify = str(data.get("identify") or "").strip()
            password = str(data.get("password") or "")
            user = self.users.authenticate(identify, password)
            if user is None:
                return jsonify({"ok": False, "error": "invalid account or password"}), 401
            session.clear()
            session["username"] = user["username"]
            session["role"] = user["role"]
            session["must_change_credentials"] = self._default_admin_credentials_in_use(
                user["username"]
            )
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
            """Close the console session."""
            session.clear()
            return jsonify({"ok": True})

        @app.post("/api/account")
        @login_required
        def api_account():
            """Change own username, email or password; the current password is required."""
            user = self._current_user()
            data = request.get_json(silent=True) or {}
            current_password = str(data.get("current_password") or "")
            new_username = str(data.get("username") or "").strip()
            new_password = str(data.get("password") or "") or None
            email = str(data.get("email") or "").strip() or None
            if self.users.authenticate(user["username"], current_password) is None:
                return jsonify({"ok": False, "error": "current password is incorrect"}), 403
            try:
                updated = self.users.update_credentials(
                    user["user_id"],
                    new_username=new_username,
                    new_password=new_password,
                    email=email,
                )
            except ValueError as e:
                return jsonify({"ok": False, "error": str(e)}), 400
            session["username"] = updated["username"]
            session["must_change_credentials"] = self._default_admin_credentials_in_use(
                updated["username"]
            )
            return jsonify(
                {
                    "ok": True,
                    "username": updated["username"],
                    "user": updated,
                    "must_change_credentials": session["must_change_credentials"],
                }
            )

        @app.get("/api/users")
        @admin_required
        def api_users():
            """List every account of this server."""
            return jsonify({"users": self.users.list_users()})

        @app.post("/api/users")
        @admin_required
        def api_add_user():
            """Create an account without the email registration flow."""
            data = request.get_json(silent=True) or {}
            try:
                self.users.add_user(
                    str(data.get("username") or "").strip(),
                    str(data.get("email") or "").strip(),
                    str(data.get("password") or ""),
                    data.get("role"),
                )
            except ValueError as e:
                return jsonify({"ok": False, "error": str(e)}), 400
            return jsonify({"ok": True, "users": self.users.list_users()})

        @app.post("/api/users/delete")
        @admin_required
        def api_delete_user():
            """Delete one account, keeping the last administrator."""
            user = self._current_user()
            data = request.get_json(silent=True) or {}
            username = str(data.get("username") or "").strip()
            if username == user["username"]:
                return jsonify({"ok": False, "error": "you cannot remove your own account"}), 400
            try:
                self.users.remove_user(username)
            except ValueError as e:
                return jsonify({"ok": False, "error": str(e)}), 400
            return jsonify({"ok": True, "users": self.users.list_users()})

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
