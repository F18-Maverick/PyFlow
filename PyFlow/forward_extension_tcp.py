"""Forward extension for the TCP protocol.

Lets a client forward the data it would normally send to the server
(strings, files, multiple files, folders, multiple folders) to a list of
destination clients instead. Every transfer family of the main TCP
protocol gets a matching ``/xxx_forward`` command:

  /send_msg_forward <msg1> <msg2> ... <(ip, port)> ...
      forward the messages to every listed destination
  /file_forward <file_path> <(ip, port)> ...
      forward one file to every listed destination
  /multiple_file_forward <file1> <file2> ... <(ip, port)> ...
      forward several files to every listed destination
  /folder_forward <folder_path> <(ip, port)> ...
      forward one folder (structure preserved) to every destination
  /multiple_folder_forward <folder1> <folder2> ... <(ip, port)> ...
      forward several folders to every listed destination

Items come first, destinations last; every destination is written as a
Python address tuple, e.g. ``"('127.0.0.1', 3000)"``. There is no limit
on the number or size of items or destinations.

The commands are only available on the client console: they are
registered in the "client" handler group, so typing them on the server
console is rejected as an unrecognized command. Forwarding goes through
the server - the client uploads the data over the normal transfer
channel (the server stores it in its transfer directory) and then asks
the server to push it to the destinations, which receive it through the
main protocol's own receive paths. Destinations that are unreachable
(not connected to the server, or the server itself, which is never in
the client table) are skipped and the remaining destinations are still
served.
"""

import ast
import functools
import os
import shlex

from .network_api import connect_tcp

server_instance = None
client_instance = None

_FORWARD_COMMANDS = (
    "/send_msg_forward",
    "/file_forward",
    "/multiple_file_forward",
    "/folder_forward",
    "/multiple_folder_forward",
)

_ADDRESS_LEN = 2  # (host, port) tuple shape

# client command kind -> the relay command the server receives
_RELAY_FOR_KIND = {
    "send_msg": "/forward_send_msg",
    "file": "/forward_file",
    "folder": "/forward_folder",
}


def _parse_items_and_addrs(tokens):
    """Split command tokens into (items, addresses).

    A token of the form ``('ip', port)`` is a destination; anything else
    is a forwarded item (message text or a path).
    """
    items = []
    addrs = []
    for token in tokens:
        if token.startswith("(") and token.endswith(")"):
            try:
                addr = ast.literal_eval(token)
            except (ValueError, SyntaxError):
                items.append(token)
                continue
            if isinstance(addr, tuple) and len(addr) == _ADDRESS_LEN and isinstance(addr[0], str):
                addrs.append(addr)
            else:
                items.append(token)
        else:
            items.append(token)
    return items, addrs


def _server_send(message):
    """Send one protocol request to the server through the client socket."""
    if client_instance is None or client_instance.client_socket is None:
        print("forward: client instance is not set up")
        return False
    return client_instance.send_message(client_instance.client_socket, message)


def _forward_request(command, names, addrs):
    """Ask the server to push ``names`` (messages or stored paths) to ``addrs``."""
    request = command + " " + " ".join(shlex.quote(n) for n in names)
    request += " " + " ".join(shlex.quote(str(a)) for a in addrs)
    return _server_send(request)


def _upload_files_sync(paths):
    """Upload every existing file to the server over the normal channel."""
    if client_instance is None:
        print("forward: client instance is not set up")
        return
    for path in paths:
        if not os.path.isfile(path):
            print(f"forward: {path} is not a valid file, skipped")
            continue
        client_instance.file_transfer_client_recv_client_start(
            f"/file {shlex.quote(path)}", None
        )


def _upload_folder_sync(folder_path):
    """Upload one folder to the server, preserving its structure.

    Mirrors the main protocol's folder transfer, but synchronous: every
    file transfer finishes before this returns, so the follow-up forward
    request can never overtake the upload.
    """
    base_path = os.path.dirname(folder_path)

    def get_relative_path(base, abs_path):
        rel = os.path.relpath(abs_path, base)
        if rel == ".":
            return ""
        return rel.replace(os.sep, "/")

    transfer_path = get_relative_path(base_path, folder_path)
    if client_instance is None:
        print("forward: client instance is not set up")
        return
    _server_send(f"/file_folder {shlex.quote(transfer_path)}")
    for root, dirs, files in os.walk(folder_path):
        rel_dir = get_relative_path(base_path, root)
        if root != folder_path:
            _server_send(f"/file_folder {shlex.quote(rel_dir)}")
        for file in files:
            client_instance.file_transfer_client_recv_client_start(
                f"/file_folder {shlex.quote(rel_dir)} {shlex.quote(file)}", root
            )


def _upload_folders_sync(paths):
    for path in paths:
        if not os.path.isdir(path):
            print(f"forward: {path} is not a valid folder, skipped")
            continue
        _upload_folder_sync(path)


def _client_forward_handler(kind, allow_multiple, sock, addr, cmd):
    """Console entry point for the /xxx_forward commands (client only).

    The command name never matters here: ``kind`` ("send_msg", "file" or
    "folder") and ``allow_multiple`` are bound at registration time with
    functools.partial. Registered with where_to_run="client", so it only
    fires from console input (interactive_mode), never from messages sent
    by other instances.
    """
    parts = shlex.split(cmd)
    items, addrs = _parse_items_and_addrs(parts[1:])
    if not items or not addrs:
        print(
            f"{kind}: need at least one item and one destination, "
            "e.g. /send_msg_forward \"msg\" \"('127.0.0.1', 3000)\""
        )
        return None
    if not allow_multiple and len(items) != 1:
        print(f"{kind}: expects exactly one item; use the multiple variant")
        return None
    relay = _RELAY_FOR_KIND[kind]
    if kind == "send_msg":
        _forward_request(relay, items, addrs)
    elif kind == "file":
        _upload_files_sync(items)
        _forward_request(relay, [os.path.basename(p) for p in items], addrs)
    elif kind == "folder":
        _upload_folders_sync(items)
        _forward_request(relay, [os.path.basename(p) for p in items], addrs)
    return None


def _server_skip_message(target):
    return f"forward: destination {target} is unreachable or is the server, skipped"


def _forward_send_msg_handler(sock, addr, cmd):
    """Server-side relay: push the messages to every reachable destination."""
    if server_instance is None:
        print("forward: server instance is not set up")
        return None
    parts = shlex.split(cmd)
    items, addrs = _parse_items_and_addrs(parts[1:])
    for target in addrs:
        client_info = server_instance.clients.get(target)
        if client_info is None:
            print(_server_skip_message(target))
            continue
        target_socket = client_info["socket"]
        for msg in items:
            server_instance.send_message(target_socket, msg)
    return None


def _forward_files_handler(sock, addr, cmd):
    """Server-side relay: push server-side files to every reachable destination."""
    if server_instance is None:
        print("forward: server instance is not set up")
        return None
    parts = shlex.split(cmd)
    names, addrs = _parse_items_and_addrs(parts[1:])
    for target in addrs:
        client_info = server_instance.clients.get(target)
        if client_info is None:
            print(_server_skip_message(target))
            continue
        for name in names:
            path = os.path.join(server_instance.file_transfer_dir, name)
            if not os.path.isfile(path):
                print(f"forward: {path} is not on the server, skipped")
                continue
            server_instance.file_transfer_server_recv_client_start(
                f"/file {shlex.quote(path)} {shlex.quote(str(target))}", None
            )
    return None


def _forward_folders_handler(sock, addr, cmd):
    """Server-side relay: push server-side folders to every reachable destination."""
    if server_instance is None:
        print("forward: server instance is not set up")
        return None
    parts = shlex.split(cmd)
    names, addrs = _parse_items_and_addrs(parts[1:])
    for target in addrs:
        client_info = server_instance.clients.get(target)
        if client_info is None:
            print(_server_skip_message(target))
            continue
        for name in names:
            path = os.path.join(server_instance.file_transfer_dir, name)
            if not os.path.isdir(path):
                print(f"forward: {path} is not on the server, skipped")
                continue
            server_instance.folder_file_transfer_server_recv_client_start(
                f"/file_folder {shlex.quote(path)} {shlex.quote(str(target))}"
            )
    return None


def setup_client_commands(client):  # noqa: PLW0603
    """Register the forward commands on a client instance (console use).

    Each command binds its transfer kind and single/multiple policy into
    the shared handler via functools.partial; where_to_run="client" makes
    them fire from console input only.
    """
    global client_instance  # noqa: PLW0603
    client_instance = client
    command_specs = [
        ("/send_msg_forward", "send_msg", True),
        ("/file_forward", "file", False),
        ("/multiple_file_forward", "file", True),
        ("/folder_forward", "folder", False),
        ("/multiple_folder_forward", "folder", True),
    ]
    for cmd_name, kind, allow_multiple in command_specs:
        client.register_command(
            cmd_name,
            functools.partial(_client_forward_handler, kind, allow_multiple),
            where_to_run="client",
            run_in_thread=True,
        )


def setup_server_commands(server):  # noqa: PLW0603
    """Register the internal forward relays on a server instance.

    These handlers are triggered by relay requests sent by clients, i.e.
    they live in the "server" group: messages coming in from other
    instances are dispatched there. The /xxx_forward commands themselves
    stay in the client group, so typing them on the server console is
    rejected as unrecognized.
    """
    global server_instance  # noqa: PLW0603
    server_instance = server
    server.register_command(
        "/forward_send_msg", _forward_send_msg_handler, where_to_run="server", run_in_thread=True
    )
    server.register_command(
        "/forward_file", _forward_files_handler, where_to_run="server", run_in_thread=True
    )
    server.register_command(
        "/forward_folder", _forward_folders_handler, where_to_run="server", run_in_thread=True
    )


def client_setup():
    """Create and start a forwarding-capable client (mirrors the control extension)."""
    global client_instance  # noqa: PLW0603
    client_instance = connect_tcp.TCP_Client_Base(
        host="127.0.0.1",
        port=65000,
        client_host="127.0.0.1",
        is_input_command_in_console=True,
        is_extend_command=True,
    )
    setup_client_commands(client_instance)
    client_instance.start_TCP_client()


def server_setup():
    """Create and start a forwarding-capable server (mirrors the control extension)."""
    global server_instance  # noqa: PLW0603
    server_instance = connect_tcp.TCP_Server_Base(
        host="127.0.0.1",
        port=65000,
        max_clients=10,
        is_input_command_in_console=True,
        is_extend_command=True,
    )
    setup_server_commands(server_instance)
    server_instance.start_TCP_Server()
