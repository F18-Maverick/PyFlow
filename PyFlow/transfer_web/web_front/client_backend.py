"""Flask backend wrapping the PyFlow TCP client for the web tool.

The launcher (``setup_client.py``) starts this backend and opens the
connect UI in the browser.  The user enters the server address (an
``http``/``https`` domain or a bare IP); the backend queries the
server's web backend ``/api/server_info`` for the TCP server address
and port, then starts the ``TCP_Client_Base`` instance.  The backend
stays up to relay the user's frontend actions:

- messages/files/folders to the server use the native transfer methods;
- messages to other clients use the native ``/forward_send_msg`` forwarding
  (a client-only command relayed by the server);
- files/folders to other clients are forwarded through the built-in
  ``forward_extension_tcp`` extension.

The sidebar instance list is kept fresh by the server's
``/web_clients_update`` broadcasts; a reload button re-requests the
list via ``/web_sync_clients``.

Inbound events (plain-text messages and files pushed by the server,
whether direct sends or client forwards) are captured on the TCP
client's receive threads through ``TCP_Client_Base``'s
``add_message_listener``/``add_file_listener`` APIs, queued here, and
polled by the frontend via ``/api/events``.

Accounts: a connected client logs in with a server account before the
instance list is usable.  A forced login needs both factors — the account
password and a verification code mailed to the account address — while a
client that starts again replays the saved credentials and session token
through ``/api/client_verify``.  ``/api/login`` opens the session, the
``/api/contacts/*`` and ``/api/contact_requests`` routes proxy the contact
management to the server, and the session token is sent over TCP with
``/web_bind`` so the server can push the contact list of that account.  The
credentials are remembered in ``.Flow_Web/client_login.json``
(owner-readable only) and the login window is shown whenever no session could
be restored.
"""

import json
import os
import shlex
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from urllib.parse import quote, urlparse

from flask import Flask, jsonify, render_template, request

from PyFlow import add_extension
from PyFlow import forward_extension_tcp
from PyFlow.network_api.connect_tcp import TCP_Client_Base, parse_forward_originator

WEB_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FLOW_WEB_DIR = os.path.join(WEB_ROOT, ".Flow_Web")
CLIENT_EXTENSIONS_UI_FILE = os.path.join(FLOW_WEB_DIR, "client_extensions_ui.json")
CLIENT_LAST_SERVER_FILE = os.path.join(FLOW_WEB_DIR, "client_last_server.json")
CLIENT_CONFIG_FILE = os.path.join(FLOW_WEB_DIR, "setup_client.json")
CLIENT_LOGIN_FILE = os.path.join(FLOW_WEB_DIR, "client_login.json")
UPLOAD_DIR = os.path.join(FLOW_WEB_DIR, "uploads")
TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
STATIC_DIR = os.path.join(WEB_ROOT, "static")

DEFAULT_CLIENT_WEB_PORT = 5001
DEFAULT_SERVER_WEB_PORT = 5000

# Account binding: the client tells the server which account its TCP
# connection belongs to, and waits for the acknowledgement carrying the
# address the server sees for it.
BIND_COMMAND = "/web_bind"
BIND_OK_COMMAND = "/web_bind_ok"
BIND_RETRY_LIMIT = 10
BIND_RETRY_INTERVAL = 1.0

# Timeout of one request to the server web backend, and the status its routes
# answer with when the session is gone.
REQUEST_TIMEOUT = 10

# Web "ftp" share (not FTP): the server answers ``/ftp_list`` with a folder
# listing of the folder it shares and pushes ``/ftp_get`` selections to this
# client over the protocol's native /file and /file_folder transfers.
FTP_LIST_COMMAND = "/ftp_list"
FTP_GET_COMMAND = "/ftp_get"
FTP_LIST_OK_COMMAND = "/ftp_list_ok"
FTP_GET_OK_COMMAND = "/ftp_get_ok"
FTP_ERROR_COMMAND = "/ftp_error"
UNAUTHORIZED = 401

# Ordered (key, label, type, default, help) for every TCP_Client_Base
# parameter shown in the startup-configuration UI.
CLIENT_PARAM_FIELDS = [
    ("host", "Server host", "text", "", "TCP server IP/host the client connects to."),
    ("port", "Server port", "number", 65432, "TCP port of the server."),
    ("client_host", "Client host", "text", "127.0.0.1", "Local address the client binds to."),
    ("client_port", "Client port", "number", "", "Local port (empty = auto-allocated)."),
    ("timeout", "Timeout (s)", "number", "", "Connection timeout in seconds (empty = none)."),
    ("port_add_step", "Port add step", "number", 1, "Step size for port allocation."),
    ("max_thread_num", "Max threads", "number", 10, "Maximum concurrent transfer threads."),
    (
        "is_input_command_in_console",
        "Console input",
        "bool",
        False,
        "Forced False by the web architecture (the web UI is the input).",
    ),
    (
        "is_wait_server",
        "Wait for server",
        "bool",
        True,
        "Wait for the server to be reachable before starting.",
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
    ("is_debug", "Debug log", "bool", False, "Log execution-process lines as well."),
    ("is_print_log", "Print log", "bool", True, "Log at all; False silences the instance."),
]


def _find_free_port(base):
    port = base
    while port < base + 100:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                port += 1
    return base


def _normalize_address(address):
    """Turn user input into ``scheme://host:port`` for the server web backend."""
    address = address.strip()
    if not address:
        raise ValueError("empty server address")
    if "://" not in address:
        address = "http://" + address
    parts = urlparse(address)
    if not parts.hostname:
        raise ValueError(f"invalid server address: {address}")
    port = parts.port or (443 if parts.scheme == "https" else DEFAULT_SERVER_WEB_PORT)
    return f"{parts.scheme}://{parts.hostname}:{port}"


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


def _last_server_address():
    """Return the address of the last server the client connected to."""
    if not os.path.exists(CLIENT_LAST_SERVER_FILE):
        return ""
    try:
        with open(CLIENT_LAST_SERVER_FILE, "r", encoding="utf-8") as f:
            return str(json.load(f).get("address", ""))
    except Exception:
        return ""


def _http_error_message(error):
    """Extract the server's error text from a refused HTTP response."""
    try:
        body = json.loads(error.read().decode("utf-8"))
        message = body.get("error") if isinstance(body, dict) else None
    except Exception:
        message = None
    return str(message) if message else f"HTTP {error.code}"


def _channel_ready(client):
    """Report whether a command may be written on the client connection.

    An encrypted connection must not carry a plaintext command before the key
    exchange flipped it, and the socket is only usable once the client is
    running.
    """
    if client is None or client.client_socket is None or not client.running:
        return False
    if not client.is_enable_encrypto:
        return True
    with client._crypto_lock:
        return client.client_socket in client._encrypted_sockets


class _ServerRequestError(ValueError):
    """Refusal of the server web backend, carrying its HTTP status."""

    def __init__(self, status, message):
        """Record the status code of the refused request.

        Args:
            status (int): HTTP status the server answered with.
            message (str): Error text reported by the server.
        """
        super().__init__(message)
        self.status = status


class ClientWebApp:
    """Flask app + TCP_Client_Base wrapper for the web tool."""

    def __init__(self, web_port=None):
        self.web_port = web_port or DEFAULT_CLIENT_WEB_PORT
        self.client = None
        self.server_info = None
        self.connected = False
        self._last_address = ""
        self._clients = []
        self._clients_lock = threading.Lock()
        self._events = []  # inbound events surfaced to the frontend (/api/events)
        self._events_lock = threading.Lock()
        self._event_seq = 0
        self._echo_expect = None  # plain text last sent to the server (echo suppression)
        self._echo_expect_at = 0.0

        # Account session of the connected server: the token/account pair the
        # TCP connection is bound to, plus the last login outcome.
        self._server_base = ""
        self.session = None
        self._login_error = ""
        self._saved_identify = ""
        self._bind_ack = False
        self._bind_pending = False
        self._bind_lock = threading.Lock()
        self._bound_address = None
        self._ftp_lock = threading.Lock()
        self._ftp_seq = 0  # request ids for the "ftp" listing/download round trips
        self._ftp_waiters = {}  # request id -> {"event": Event, "reply": tuple | None}
        self.app = Flask(
            __name__,
            template_folder=TEMPLATE_DIR,
            static_folder=STATIC_DIR,
            static_url_path="/static",
        )
        self._register_routes()

    # ---------------------------------------------------------------- helpers

    def _own_address(self):
        """Return this client's address as the server reports it."""
        if self._bound_address is not None:
            return dict(self._bound_address)
        if self.client is None or self.client.client_socket is None:
            return None
        try:
            ip, port = self.client.client_socket.getsockname()[:2]
            return {"ip": ip, "port": port, "id": f"{ip}:{port}"}
        except Exception:
            return None

    def _client_id(self):
        own = self._own_address()
        if own:
            return own["id"]
        return f"{self.client.client_host}:{self.client.client_port}"

    def _run_client_command(self, handler, command):
        try:
            self.client._execute_custom_handler(
                handler, command, self.client.client_socket, self._client_id()
            )
        except Exception:
            traceback.print_exc()

    def _send_file_to_server(self, path, destination=None):
        try:
            message = f"/file {shlex.quote(path)}"
            if destination:
                message += f" {shlex.quote(destination)}"
            self.client.file_transfer_client_recv_client_start(message, None)
        except Exception:
            traceback.print_exc()

    def _send_folder_to_server(self, path, destination=None):
        try:
            message = f"/file_folder {shlex.quote(path)}"
            if destination:
                message += f" {shlex.quote(destination)}"
            self.client.folder_file_transfer_client_recv_client_start(message)
        except Exception:
            traceback.print_exc()

    def _forward_file(self, path, addr, destination=None):
        try:
            message = f"/forward_file {shlex.quote(path)} {shlex.quote(str(addr))}"
            if destination:
                message += f" {shlex.quote(destination)}"
            self.client.forward_file_console(message)
        except Exception:
            traceback.print_exc()

    def _forward_folder(self, path, addr, destination=None):
        try:
            message = f"/forward_folder {shlex.quote(path)} {shlex.quote(str(addr))}"
            if destination:
                message += f" {shlex.quote(destination)}"
            self.client.forward_folder_console(message)
        except Exception:
            traceback.print_exc()

    def _ftp_request(self, command, payload, timeout=REQUEST_TIMEOUT):
        """Send one "ftp" command to the server and wait for its answer.

        Args:
            command (str): ``/ftp_list`` or ``/ftp_get``.
            payload (object): JSON-serializable request argument.
            timeout (float): Seconds to wait for the answer.

        Returns:
            tuple: ``(reply_command, reply_payload)`` as sent by the server.

        Raises:
            _ServerRequestError: If the client is not connected, the write
                fails, the server refuses the request or nothing arrives.
        """
        if not self.connected or self.client is None or not _channel_ready(self.client):
            raise _ServerRequestError(503, "not connected to the server")
        with self._ftp_lock:
            self._ftp_seq += 1
            request_id = str(self._ftp_seq)
            slot = {"event": threading.Event(), "reply": None}
            self._ftp_waiters[request_id] = slot
        line = f"{command} {request_id} {json.dumps(payload, separators=(',', ':'))}"
        try:
            self.client.send_message(self.client.client_socket, line)
        except Exception as e:
            with self._ftp_lock:
                self._ftp_waiters.pop(request_id, None)
            raise _ServerRequestError(502, f"cannot reach the server: {e}") from e
        try:
            if not slot["event"].wait(timeout):
                raise _ServerRequestError(504, "the server did not answer in time")
        finally:
            with self._ftp_lock:
                self._ftp_waiters.pop(request_id, None)
        reply_command, reply_payload = slot["reply"]
        if reply_command == FTP_ERROR_COMMAND:
            raise _ServerRequestError(502, str(reply_payload))
        return reply_command, reply_payload

    def _ftp_list(self, rel_path):
        """Ask the server for one folder of its shared folder.

        Args:
            rel_path (str): Folder relative to the share; "" is the share root.

        Returns:
            dict: The server's listing payload.
        """
        _command, payload = self._ftp_request(FTP_LIST_COMMAND, rel_path)
        return payload

    def _ftp_download(self, rel_paths, destination=None):
        """Ask the server to push the selected share entries to this client.

        Args:
            rel_paths (list): Share-relative files and folders to download.
            destination (str | None): Folder on this host the entries are saved
                into; ``None`` keeps the receiver's default transfer folder.

        Returns:
            dict: ``{"started": int, "skipped": int}``.
        """
        payload = {"paths": list(rel_paths), "destination": destination or ""}
        _command, payload = self._ftp_request(FTP_GET_COMMAND, payload)
        return payload

    def _register_ftp_commands(self):
        """Register the replies of the web "ftp" share on the TCP client."""
        if self.client is None:
            return
        for command in (FTP_LIST_OK_COMMAND, FTP_GET_OK_COMMAND, FTP_ERROR_COMMAND):
            self.client.register_command(
                command, self._on_ftp_reply, where_to_run="server", run_in_thread=True
            )

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

    def _on_clients_update(self, sock, addr, cmd):
        """Server broadcast: refresh the sidebar instance list."""
        payload = cmd[len("/web_clients_update") :].strip()
        try:
            clients = json.loads(payload)
        except Exception:
            return None
        if isinstance(clients, list):
            with self._clients_lock:
                self._clients = clients
        return None

    def _on_bind_ok(self, sock, addr, cmd):
        """Server ack: record the address the server sees for this client."""
        try:
            info = json.loads(cmd[len(BIND_OK_COMMAND) :].strip())
            address = {"ip": str(info["ip"]), "port": int(info["port"])}
        except Exception:
            return None
        address["id"] = f"{address['ip']}:{address['port']}"
        self._bound_address = address
        self._bind_ack = True
        return None

    def _on_ftp_reply(self, sock, addr, cmd):
        """Server answer to one "ftp" request: wake the waiting HTTP request."""
        parts = cmd.split(" ", 2)
        if len(parts) < 2:
            return None
        command = parts[0].lower()
        request_id = parts[1]
        body = parts[2].strip() if len(parts) > 2 else ""
        if command == FTP_LIST_OK_COMMAND:
            try:
                reply = (command, json.loads(body))
            except ValueError:
                reply = (FTP_ERROR_COMMAND, "malformed listing")
        elif command == FTP_GET_OK_COMMAND:
            started, _, skipped = body.partition(" ")
            reply = (command, {"started": int(started or 0), "skipped": int(skipped or 0)})
        else:
            reply = (FTP_ERROR_COMMAND, body or "the server refused the request")
        with self._ftp_lock:
            slot = self._ftp_waiters.get(request_id)
        if slot is not None:
            slot["reply"] = reply
            slot["event"].set()
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

    def _on_incoming_message(self, sender, text):
        """Client receive thread: an inbound plain-text message.

        ``sender`` is the author's ``"ip:port"`` when another client forwarded
        the message to us (the server relay envelope carries it), or ``None``
        for a direct push from the server. Direct pushes surface under the
        server entry; forwarded ones under the sender's own conversation.
        """
        text = (text or "").strip()
        if not text:
            return
        if text.startswith("Welcome!:"):  # connection greeting, not chat
            return
        if text == "Command received, processing in background.":  # server ack, not chat
            return
        if text.startswith("Unknown command"):  # server rejection notice, not chat
            return
        if text.startswith("msg send: "):  # echo of our own plain send to the server
            with self._events_lock:
                expect, at = self._echo_expect, self._echo_expect_at
            if expect is not None and time.time() - at <= 3 and text == "msg send: " + expect:
                return
        event = {"type": "msg", "text": text, "at": time.strftime("%H:%M:%S")}
        if sender:
            event["from"] = sender
        self._push_event(event)

    def _on_incoming_file(self, full_path, name, size, command):
        """Client receive thread: a file pushed by the server was saved."""
        try:
            cmd_name = (command or "").strip().split(" ", 1)[0].lower()
        except Exception:
            cmd_name = ""
        if cmd_name == "/crypto_pub_key":  # handshake keys are not user data
            return
        rel = full_path
        if self.client is not None:
            try:
                candidate = os.path.relpath(full_path, self.client.file_transfer_dir)
                if not candidate.startswith(".."):
                    rel = candidate
            except Exception:
                pass
        event = {
            "type": "file",
            "name": name,
            "path": rel,
            "size": size,
            "at": time.strftime("%H:%M:%S"),
        }
        # A forwarded file/folder carries the originator's address in the wire
        # command; direct pushes carry the receiver's own address and are
        # filtered out, so they keep surfacing under the server entry.
        own = self._own_address()
        originator = parse_forward_originator(command, own_address=own["id"] if own else None)
        if originator:
            event["from"] = originator
        self._push_event(event)

    # ---------------------------------------------------------------- connect

    def _start_client(self, host, port, is_enable_encrypto):
        params = self._load_client_params()
        params["host"] = host
        params["port"] = port
        params["is_enable_encrypto"] = is_enable_encrypto
        self._start_client_from_params(params)

    def _load_client_params(self):
        """Return the saved client startup params (``setup_client.json``), if any."""
        if os.path.exists(CLIENT_CONFIG_FILE):
            try:
                with open(CLIENT_CONFIG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return data if isinstance(data, dict) else {}
            except Exception:
                return {}
        return {}

    def start_from_config(self):
        """Start the TCP client from ``.Flow_Web/setup_client.json`` and log in."""
        params = self._load_client_params()
        if not params:
            return
        last = _last_server_address()
        if last:
            try:
                self._server_base = _normalize_address(last)
                self._last_address = last
            except ValueError:
                self._server_base = ""
        self._start_client_from_params(params)
        self._auto_login()

    def _normalize_client_params(self, params):
        """Normalize form values into TCP_Client_Base constructor arguments."""
        params = dict(params)
        # Web architecture constraints: extensions must be registered
        # before start, and the web UI replaces the console input.
        params["is_extend_command"] = True
        params["is_input_command_in_console"] = False
        if params.get("client_port") in (None, ""):
            params["client_port"] = None
        if params.get("timeout") in (None, ""):
            params["timeout"] = None
        if params.get("is_custom_keys") in (None, ""):
            params["is_custom_keys"] = None
        elif isinstance(params["is_custom_keys"], str):
            try:
                parsed = json.loads(params["is_custom_keys"])
                params["is_custom_keys"] = parsed if isinstance(parsed, list) else None
            except Exception:
                params["is_custom_keys"] = None
        return params

    def _start_client_from_params(self, params):
        """Create, register and start the TCP_Client_Base instance."""
        params = self._normalize_client_params(params)
        self.client = TCP_Client_Base(**params)
        forward_extension_tcp.setup_client_commands(self.client)
        self.client.register_command(
            "/web_clients_update", self._on_clients_update, where_to_run="server", run_in_thread=True
        )
        self.client.add_message_listener(self._on_incoming_message)
        self._register_ftp_commands()
        self.client.add_file_listener(self._on_incoming_file)
        try:
            add_extension.load_registered_extensions(self.client, "client")
        except ImportError as e:
            print(f"Failed to load registered extensions: {e}")
        # A fresh connection has to be bound to the account again.
        self._bind_ack = False
        self._bound_address = None
        threading.Thread(target=self.client.start_TCP_client, daemon=True).start()
        self.connected = True
        self._bind_account()
        self.server_info = {
            "host": params["host"],
            "port": params["port"],
            "is_enable_encrypto": params.get("is_enable_encrypto", True),
        }
        with self._clients_lock:
            self._clients = []
        os.makedirs(FLOW_WEB_DIR, exist_ok=True)
        with open(CLIENT_LAST_SERVER_FILE, "w", encoding="utf-8") as f:
            json.dump({"address": self._last_address}, f, indent=4, ensure_ascii=False)

    # ------------------------------------------------------- account / login

    def _server_request(self, path, payload=None):
        """Send one request to the connected server web backend.

        Args:
            path (str): Server route to call, e.g. ``"/api/client_login"``.
            payload (dict | None): JSON body of the request; ``None`` issues a
                GET without a body instead.

        Returns:
            dict: Parsed JSON reply of the server.

        Raises:
            ValueError: If no server address is known, the server cannot be
                reached, or its reply is not a JSON object. A refused request
                carries the server's own error message, or ``HTTP <status>``
                when the server sent none.
        """
        if not self._server_base:
            raise ValueError("not connected to a server")
        url = self._server_base + path
        if payload is None:
            req = urllib.request.Request(url)
        else:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            raise _ServerRequestError(e.code, _http_error_message(e)) from e
        except Exception as e:
            raise ValueError(
                f"cannot reach the server web backend at {self._server_base}: {e}"
            ) from e
        try:
            result = json.loads(body)
        except Exception as e:
            raise ValueError(f"invalid server reply: {e}") from e
        if not isinstance(result, dict):
            raise ValueError("invalid server reply")
        if result.get("ok") is False:
            raise ValueError(result.get("error") or "the server refused the request")
        return result

    def _save_login_file(self, identify, password, token):
        """Store the credentials used to log in to this server.

        Args:
            identify (str): Username or e-mail the user logged in with.
            password (str): Password the user typed; the account password.
            token (str): Session token the server issued for this login.
        """
        payload = {
            "server": self._server_base,
            "identify": identify,
            "password": password or "",
            "token": token or "",
        }
        os.makedirs(FLOW_WEB_DIR, exist_ok=True)
        with open(CLIENT_LOGIN_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=4, ensure_ascii=False)
        try:
            os.chmod(CLIENT_LOGIN_FILE, 0o600)
        except OSError:
            pass  # best effort: file modes are not portable

    def _load_login_file(self):
        """Return the saved login credentials, or ``None`` when absent."""
        if not os.path.exists(CLIENT_LOGIN_FILE):
            return None
        try:
            with open(CLIENT_LOGIN_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    def _clear_login_file(self):
        """Delete the saved login credentials, if any."""
        try:
            os.remove(CLIENT_LOGIN_FILE)
        except OSError:
            pass

    def _forget_session(self, message="", drop_credentials=False):
        """Drop the local session, keeping the saved credentials by default.

        Args:
            message (str): Login error the login window explains, empty for none.
            drop_credentials (bool): Delete the saved login file as well.
        """
        self.session = None
        self._bind_ack = False
        self._bound_address = None
        self._login_error = message
        if drop_credentials:
            self._clear_login_file()

    def _login(self, identify, password, code):
        """Log in to an account of the connected server.

        A forced login always needs both factors: the account password and a
        verification code mailed to the account address.

        Args:
            identify (str): Username or e-mail of the account.
            password (str): Password of the account.
            code (str): Mailed verification code of the ``login`` purpose.

        Returns:
            dict: The logged-in account without its password.

        Raises:
            ValueError: If the server refuses the credentials or answers
                without a session token.
        """
        result = self._server_request(
            "/api/client_login", {"identify": identify, "password": password, "code": code}
        )
        token = str(result.get("token") or "")
        if not token:
            raise ValueError("the server returned no session token")
        user = result.get("user") or {}
        self.session = {"token": token, "user": user}
        self._saved_identify = identify
        self._bind_ack = False
        self._bound_address = None
        self._login_error = ""
        self._save_login_file(identify, password, token)
        self._bind_account()
        return user

    def _auto_login(self):
        """Restore the saved session of this server when the server accepts it.

        The saved password and session token are replayed together: the token
        proves the session is known, the credentials prove they still open the
        account. A file without both, or one the server refuses, leaves the
        login window in charge.
        """
        if self.session is not None or not self._server_base:
            return
        saved = self._load_login_file()
        if not saved or saved.get("server") != self._server_base:
            return
        self._saved_identify = str(saved.get("identify") or "")
        password = str(saved.get("password") or "")
        token = str(saved.get("token") or "")
        if not (self._saved_identify and password and token):
            self._login_error = "the saved login is incomplete, sign in again"
            return
        payload = {"token": token, "identify": self._saved_identify, "password": password}
        try:
            result = self._server_request("/api/client_verify", payload)
        except ValueError as e:
            self._login_error = f"saved credentials were rejected: {e}"
            return
        self.session = {"token": token, "user": result.get("user") or {}}
        self._login_error = ""
        self._bind_ack = False
        self._bound_address = None
        self._bind_account()

    def _bind_account(self):
        """Bind the TCP connection to the logged-in account of the session."""
        if self.session is None or self._bind_ack:
            return
        with self._bind_lock:
            if self._bind_pending:
                return
            self._bind_pending = True
        threading.Thread(
            target=self._bind_loop, args=(self.session["token"],), daemon=True
        ).start()

    def _bind_loop(self, token):
        """Send ``/web_bind`` once a second until the server acknowledged it."""
        for attempt in range(BIND_RETRY_LIMIT):
            if self._bind_ack or self.session is None:
                break
            if attempt:
                time.sleep(BIND_RETRY_INTERVAL)
            client = self.client
            if not _channel_ready(client):
                continue
            try:
                client.send_message(client.client_socket, f"{BIND_COMMAND} {token}")
            except Exception:
                traceback.print_exc()
        with self._bind_lock:
            self._bind_pending = False

    def _logout(self):
        """Close the account session and forget the saved credentials."""
        token = self.session["token"] if self.session else ""
        if token:
            try:
                self._server_request("/api/client_logout", {"token": token})
            except ValueError:
                pass  # the local session goes away either way
        self._forget_session(drop_credentials=True)

    def _account_proxy(self, path, payload=None):
        """Forward one account request to the server and shape its reply.

        Args:
            path (str): Server route to call, e.g. ``"/api/contacts/search"``.
            payload (dict | None): Request body without the session token;
                ``None`` issues a GET carrying the token as a query argument.

        Returns:
            tuple: Flask response of the request.
        """
        if self.session is None:
            return jsonify({"ok": False, "error": "login required"}), 401
        token = self.session["token"]
        if payload is None:
            body = None
            path = f"{path}?token={quote(token)}"
        else:
            body = dict(payload)
            body["token"] = token
        try:
            return jsonify(self._server_request(path, body))
        except _ServerRequestError as e:
            status = e.status
            if status == UNAUTHORIZED:
                # The server dropped the session: return to the login window.
                self._forget_session("the session expired, please log in again")
            else:
                status = 400
            return jsonify({"ok": False, "error": str(e)}), status
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400

    # ------------------------------------------------------------------ routes

    def _register_routes(self):
        app = self.app

        @app.get("/")
        def index():
            if self.connected:
                if self.session is None:
                    self._auto_login()
                if self.session is not None:
                    return render_template(
                        "client_main.html", mode="client", user=self.session["user"]
                    )
                return render_template(
                    "client_login.html",
                    identify=self._saved_identify,
                    login_error=self._login_error,
                    server_address=self._server_base,
                )
            return render_template(
                "client_connect.html", last_address=_last_server_address()
            )

        @app.get("/config")
        def config():
            """Startup-configuration page, reachable from the main page too."""
            current = self._load_client_params()
            if not current and self.client is not None:
                c = self.client
                current = {
                    "host": c.host,
                    "port": c.port,
                    "client_host": c.client_host,
                    "client_port": c.client_port,
                    "timeout": c.timeout,
                    "port_add_step": c.port_add_step,
                    "max_thread_num": c.max_thread_num,
                    "is_input_command_in_console": c.is_input_command_in_console,
                    "is_wait_server": c.is_wait_server,
                    "is_extend_command": c.is_extend_command,
                    "is_enable_encrypto": c.is_enable_encrypto,
                    "is_custom_keys": c.is_custom_keys,
                    "max_mem_buff": c.max_mem_buff // (1024 * 1024),
                }
            fields = [
                (key, label, ftype, _config_display_value(key, current.get(key, default)), help)
                for key, label, ftype, default, help in CLIENT_PARAM_FIELDS
            ]
            return render_template("client_config.html", fields=fields)

        @app.post("/api/connect")
        def api_connect():
            data = request.get_json(force=True)
            address = data.get("address", "").strip()
            try:
                base = _normalize_address(address)
            except ValueError as e:
                return jsonify({"ok": False, "error": str(e)}), 400
            try:
                with urllib.request.urlopen(f"{base}/api/server_info", timeout=10) as resp:
                    info = json.loads(resp.read().decode("utf-8"))
            except Exception as e:
                return (
                    jsonify(
                        {
                            "ok": False,
                            "error": f"cannot reach the server web backend at {base}: {e}",
                        }
                    ),
                    502,
                )
            if not info.get("ok", True):
                return jsonify({"ok": False, "error": info.get("error", "server not ready")}), 503
            host = info.get("host")
            port = int(info.get("port"))
            is_enable_encrypto = bool(info.get("is_enable_encrypto", True))
            self._last_address = address
            self._server_base = base
            self._forget_session()
            try:
                self._start_client(host, port, is_enable_encrypto)
            except Exception as e:
                traceback.print_exc()
                return jsonify({"ok": False, "error": f"failed to start TCP client: {e}"}), 500
            self._auto_login()
            return jsonify({"ok": True, "server_info": self.server_info})

        @app.post("/api/save_config")
        def api_save_config():
            data = request.get_json(force=True)
            params = data.get("params", {})
            # Validate the params by constructing the client class before saving.
            try:
                TCP_Client_Base(**self._normalize_client_params(params))
            except Exception as e:
                return jsonify({"ok": False, "error": f"invalid configuration: {e}"}), 400
            os.makedirs(FLOW_WEB_DIR, exist_ok=True)
            with open(CLIENT_CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(params, f, indent=4, ensure_ascii=False)
            # Restart the TCP client in place; the Flask app stays up, so
            # there is no dead window and no dependency on process spawning.
            if self.client is not None:
                try:
                    self.client.close()
                except Exception:
                    traceback.print_exc()
            try:
                self._start_client_from_params(params)
            except Exception as e:
                traceback.print_exc()
                return jsonify({"ok": False, "error": f"failed to start TCP client: {e}"}), 500
            return jsonify({"ok": True, "server_info": self.server_info})

        @app.get("/api/status")
        def api_status():
            if self.session is not None and not self._bind_ack:
                self._bind_account()
            return jsonify(
                {
                    "connected": self.connected
                    and self.client is not None
                    and self.client.running,
                    "server_info": self.server_info,
                    "clients": self._clients_snapshot(),
                    "own_address": self._own_address(),
                    "pid": os.getpid(),
                    "logged_in": self.session is not None,
                    "user": self.session["user"] if self.session else None,
                    "server_address": self._server_base,
                }
            )

        @app.post("/api/login")
        def api_login():
            """Log the web client in with the account password and a mailed code."""
            data = request.get_json(force=True)
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
            try:
                user = self._login(identify, password, code)
            except _ServerRequestError as e:
                return jsonify({"ok": False, "error": str(e)}), e.status
            except ValueError as e:
                return jsonify({"ok": False, "error": str(e)}), 400
            return jsonify({"ok": True, "user": user})

        @app.post("/api/login/send_code")
        def api_login_send_code():
            """Ask the server to mail a login code to one account."""
            data = request.get_json(force=True)
            identify = str(data.get("identify") or "").strip()
            if not identify:
                return jsonify({"ok": False, "error": "enter your user name or email"}), 400
            try:
                payload = self._server_request("/api/login/send_code", {"identify": identify})
            except ValueError as e:
                return jsonify({"ok": False, "error": str(e)}), 400
            return jsonify(payload)

        @app.post("/api/logout")
        def api_logout():
            """Close the account session and forget the saved credentials."""
            self._logout()
            return jsonify({"ok": True})

        @app.post("/api/contacts/search")
        def api_contacts_search():
            """Search server accounts by user id, username or email."""
            data = request.get_json(force=True)
            return self._account_proxy("/api/contacts/search", {"query": data.get("query", "")})

        @app.post("/api/contacts/request")
        def api_contacts_request():
            """Ask another account of the server to become a contact."""
            data = request.get_json(force=True)
            return self._account_proxy(
                "/api/contacts/request", {"user_id": data.get("user_id", "")}
            )

        @app.get("/api/contact_requests")
        def api_contact_requests():
            """List the pending contact requests of the logged-in account."""
            return self._account_proxy("/api/contact_requests")

        @app.post("/api/contacts/respond")
        def api_contacts_respond():
            """Accept or reject one incoming contact request."""
            data = request.get_json(force=True)
            return self._account_proxy(
                "/api/contacts/respond",
                {"request_id": data.get("request_id"), "accept": bool(data.get("accept"))},
            )

        @app.get("/api/events")
        def api_events():
            since = request.args.get("since", 0, type=int)
            with self._events_lock:
                events = [e for e in self._events if e["id"] > since]
                latest = events[-1]["id"] if events else since
            return jsonify({"events": events, "latest": latest})

        @app.post("/api/send_msg")
        def api_send_msg():
            if not self.connected or self.client is None:
                return jsonify({"ok": False, "error": "not connected"}), 400
            data = request.get_json(force=True)
            target = data.get("target")
            message = data.get("message", "")
            if target == "server":
                ok = self.client.send_message(self.client.client_socket, message)
                if not ok:
                    return jsonify({"ok": False, "error": "send failed"}), 500
                with self._events_lock:
                    self._echo_expect = message
                    self._echo_expect_at = time.time()
                return jsonify({"ok": True})
            addr = (target[0], int(target[1]))
            threading.Thread(
                target=self.client.forward_messages, args=([message], [addr]), daemon=True
            ).start()
            return jsonify({"ok": True})

        @app.post("/api/send_file")
        def api_send_file():
            if not self.connected or self.client is None:
                return jsonify({"ok": False, "error": "not connected"}), 400
            try:
                target = json.loads(request.form.get("target"))
            except Exception:
                return jsonify({"ok": False, "error": "invalid target"}), 400
            files = request.files.getlist("files")
            if not files:
                return jsonify({"ok": False, "error": "no files uploaded"}), 400
            destination = request.form.get("destination") or None
            os.makedirs(UPLOAD_DIR, exist_ok=True)
            saved = []
            for f in files:
                path = os.path.join(UPLOAD_DIR, os.path.basename(f.filename))
                f.save(path)
                saved.append(path)
            if target == "server":
                for path in saved:
                    threading.Thread(
                        target=self._send_file_to_server, args=(path, destination), daemon=True
                    ).start()
            else:
                addr = tuple(target)
                for path in saved:
                    threading.Thread(
                        target=self._forward_file, args=(path, addr, destination), daemon=True
                    ).start()
            return jsonify({"ok": True})

        @app.post("/api/send_folder")
        def api_send_folder():
            if not self.connected or self.client is None:
                return jsonify({"ok": False, "error": "not connected"}), 400
            try:
                target = json.loads(request.form.get("target"))
            except Exception:
                return jsonify({"ok": False, "error": "invalid target"}), 400
            files = request.files.getlist("files")
            if not files:
                return jsonify({"ok": False, "error": "no files uploaded"}), 400
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
            if target == "server":
                threading.Thread(
                    target=self._send_folder_to_server, args=(root, destination), daemon=True
                ).start()
            else:
                addr = tuple(target)
                threading.Thread(
                    target=self._forward_folder, args=(root, addr, destination), daemon=True
                ).start()
            return jsonify({"ok": True})

        @app.post("/api/run_extension")
        def api_run_extension():
            if not self.connected or self.client is None:
                return jsonify({"ok": False, "error": "not connected"}), 400
            data = request.get_json(force=True)
            command = data.get("command", "")
            parts = shlex.split(command)
            if not parts:
                return jsonify({"ok": False, "error": "empty command"}), 400
            handler = self.client._custom_handlers[1].get(parts[0].lower())
            if handler is None:
                return jsonify({"ok": False, "error": f"command {parts[0]} is not registered"}), 404
            threading.Thread(
                target=self._run_client_command, args=(handler, command), daemon=True
            ).start()
            return jsonify({"ok": True})

        @app.get("/api/available_commands")
        def api_available_commands():
            if not self.connected or self.client is None:
                return jsonify({"ok": False, "error": "not connected"}), 400
            return jsonify({"commands": sorted(self.client._custom_handlers[1].keys())})

        @app.post("/api/sync_clients")
        def api_sync_clients():
            if not self.connected or self.client is None:
                return jsonify({"ok": False, "error": "not connected"}), 400
            self.client.send_message(self.client.client_socket, "/web_sync_clients")
            return jsonify({"ok": True})

        @app.post("/api/ftp/list")
        def api_ftp_list():
            payload = request.get_json(silent=True) or {}
            try:
                listing = self._ftp_list(str(payload.get("path") or ""))
            except _ServerRequestError as e:
                return jsonify({"ok": False, "error": str(e)}), e.status
            return jsonify({"ok": True, "listing": listing})

        @app.post("/api/ftp/download")
        def api_ftp_download():
            payload = request.get_json(silent=True) or {}
            wanted = payload.get("paths")
            if not isinstance(wanted, list) or not wanted:
                return jsonify({"ok": False, "error": "select at least one entry"}), 400
            destination = str(payload.get("destination") or "").strip() or None
            try:
                result = self._ftp_download([str(entry) for entry in wanted], destination)
            except _ServerRequestError as e:
                return jsonify({"ok": False, "error": str(e)}), e.status
            return jsonify({"ok": True, **result})

        @app.get("/api/extensions_ui")
        def api_get_extensions_ui():
            return jsonify({"extensions": _load_json_list(CLIENT_EXTENSIONS_UI_FILE)})

        @app.get("/api/registered_extensions")
        def api_registered_extensions():
            return jsonify({"extensions": _load_json_list(add_extension.added_extensions_log_file)})

        @app.post("/api/extensions_ui")
        def api_save_extensions_ui():
            data = request.get_json(force=True)
            entries = data.get("extensions", [])
            os.makedirs(FLOW_WEB_DIR, exist_ok=True)
            with open(CLIENT_EXTENSIONS_UI_FILE, "w", encoding="utf-8") as f:
                json.dump(entries, f, indent=4, ensure_ascii=False)
            return jsonify({"ok": True})

        @app.post("/api/add_extension")
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
        def api_remove_extension():
            data = request.get_json(force=True)
            paths = data.get("paths", [])
            try:
                add_extension.remove_extension(paths)
            except Exception as e:
                return jsonify({"ok": False, "error": str(e)}), 400
            threading.Thread(target=self._restart, daemon=True).start()
            return jsonify({"ok": True, "restarting": True})

    # ------------------------------------------------------------------- run

    def _clients_snapshot(self):
        with self._clients_lock:
            return list(self._clients)

    def run(self):
        port = _find_free_port(self.web_port)
        if port != self.web_port:
            print(f"Web port {self.web_port} busy, using {port}")
        threading.Thread(target=self._open_browser, args=(port,), daemon=True).start()
        self.app.run(host="127.0.0.1", port=port, threaded=True, use_reloader=False)

    def _open_browser(self, port):
        time.sleep(1.5)
        try:
            import webbrowser

            webbrowser.open(f"http://127.0.0.1:{port}/")
        except Exception:
            pass
