"""TCP transport for PyFlow: the server and client base classes and the wire parsers.

``TCP_Server_Base`` accepts connections and dispatches inbound lines;
``TCP_Client_Base`` connects, sends and reads on the same conventions:

- one message per line, terminated by a newline; every inbound line is
  reported to the registered message listeners (commands included), and a
  line that starts with ``/`` is then dispatched to the command handlers,
  anything else treated as a plain message;
- unless ``is_enable_encrypto`` is False, an encrypted channel is negotiated
  right after connect: the handshake lines and the public-key pushes stay
  plaintext, then every later message travels as a frame (RSA-OAEP for small
  text payloads, an ECDH-keyed AES-GCM seal for everything else, including
  file data), and each file/folder transfer sub-connection is framed the same
  way once both sides hold the session keys (see `_SecureChannel`);
- file/folder transfer, message forwarding and port allocation are layered on
  the same socket and share its command namespace;
- an interrupted transfer is resumed after a disconnect: both sides persist
  their transfer stores under the protocol's ``.Flow`` directory and the
  receiver asks the sender to continue the file from the bytes already on
  disk (``_request_resumes``; the wire lines are ``/resume_trans`` and
  ``/forward_resume``).

The forwarding extensions use the module-level parsers
`parse_forwarded_message`, `parse_forward_items_and_addrs`,
`parse_forward_originator` and `forward_skip_message`; the reconnect logic
uses `parse_resume_command`, `parse_forward_resume_command` and
`_resume_offset_for`.

Concepts live in ``docs/source/Network_APIs/TCP_Server_APIs.rst`` and
``TCP_Client_APIs.rst``; argument, return and exception contracts live in the
docstrings below.
"""

import asyncio
import collections
import os
import ast
import sys
import time
import copy
import shlex
import socket
import struct  # SO_LINGER's (l_onoff, l_linger) pair, for abortive closes
import selectors
import secrets
import traceback
import threading
import uuid
import errno
import queue
import json
from . import rsa_crypto
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor


def _process_is_alive(pid):
    """Report whether the process recorded in a port-range entry still runs.

    Args:
        pid (int | None): Process id from the record; ``None`` (a record written before pids were stored) counts as alive so it is never dropped just for lacking one.

    Returns:
    bool: True when the process exists, or when that cannot be determined
                (keeping an entry is harmless, dropping a live one is not).
    """
    if not isinstance(pid, int) or pid <= 0:
        return True
    if os.name == "nt":  # os.kill(pid, 0) would terminate the process on Windows
        import ctypes

        SYNCHRONIZE = 0x00100000
        WAIT_TIMEOUT = 0x00000102
        ERROR_INVALID_PARAMETER = 87
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not handle:
            # a nonexistent pid is reported as an invalid parameter; access
            # denied means the process is there but owned by someone else
            return kernel32.GetLastError() != ERROR_INVALID_PARAMETER
        try:
            return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by another user
    except OSError:
        return True  # unknown state: keep the entry
    return True


def _port_record_stale(entry):
    """Report whether a port-range entry belongs to a process that has exited.

    Args:
        entry (dict): One record entry (``server_port_info`` / ``client_port_info``).

    Returns:
    bool: True when the entry can be reclaimed.
    """
    pid = entry.get("pid")
    if pid is None:  # pre-pid record: keep the older rule
        return entry.get("is_running") is False
    return not _process_is_alive(pid)


def _is_closed_socket_error(exc):
    """Return true when the error means a socket that is already closed.

    True when the error means a socket that is already closed

    (EBADF / ENOTSOCK, Windows winsock WSAENOTSOCK 10038, ECONNRESET /
        EPIPE from a peer that dropped the connection, or the "connection
        error" RuntimeError raised by send_message after the instance
        stopped): the normal teardown signal between a closing thread and a
        receive thread, not a fault worth a traceback.

    Args:
        exc (object): Value used by the operation.

    Returns:
        bool | object: Result produced by the operation.
    """
    if isinstance(exc, RuntimeError) and str(exc) == "connection error":
        return True
    return isinstance(exc, OSError) and exc.errno in (
        errno.EBADF,
        errno.ENOTSOCK,
        errno.ECONNRESET,
        errno.EPIPE,
        10038,  # WSAENOTSOCK (Windows)
    )


MAX_DECODE_FAILURES = 3  # circuit breaker: close after N consecutive decode failures (re-exchange storm / garbage injection)
SEND_WAIT_TIMEOUT = 30.0  # seconds a non-blocking client write waits for buffer room
LOG_MERGE_ATTEMPTS = 8  # a log flush re-merges while other instances keep publishing

NETWORK_SPEED_WINDOW = 1.0  # sample window of the real-time speed, in seconds
NETWORK_SPEED_MIN_SPAN = 0.1  # seconds floor, so one short burst is not extrapolated

# Transfer reconnect: an interrupted transfer keeps its progress entry and its
# partial file, and both sides persist the stores to .Flow when a connection
# drops (see _persist_transfer_progress). After a reconnect the receiver asks
# the sender to continue from the bytes it already holds (/resume_trans).
RESUME_LINE = "/resume_trans"  # receiver -> sender: continue this transfer from an offset
FORWARD_RESUME_LINE = "/forward_resume"  # server -> forwarder: re-forward one item from an offset
RESUME_ACCEPT_TIMEOUT = 20.0  # seconds a resumed transfer waits for the sender to connect
RESUME_PORT_TIMEOUT = 20.0  # seconds a resumed sender waits for the receiver's transfer port
RESUME_SECURE_TIMEOUT = 10.0  # seconds a resume drive waits for the framed control channel
FILE_COMMAND_TOKENS = 3  # a ``/file <name> <fid>`` line carries this many tokens


def _wait_writable(client_socket, timeout):
    """Wait until a non-blocking socket accepts more data.

    Args:
        client_socket (socket.socket): Socket to wait on; must be non-blocking.
        timeout (float): Maximum seconds to wait. Must be > 0.

    Raises:
    TimeoutError: If the socket stays unwritable for ``timeout`` seconds.
            OSError: If the socket is closed or cannot be polled.
    """
    with selectors.DefaultSelector() as selector:
        selector.register(client_socket, selectors.EVENT_WRITE)
        if not selector.select(timeout):
            raise TimeoutError(f"peer did not accept data within {timeout} seconds")


_LOG_MERGE_LOCKS = {}  # log path -> RLock serialising that file's merges in this process
_LOG_MERGE_LOCKS_LOCK = threading.Lock()


def _log_merge_lock(path):
    """Return this process's lock for the log at ``path``.

    Shared by every merge of that file in this process (see
        `_publish_json_log`), so threads of one program never interleave their
        read-merge-publish.

    Args:
        path (str): Value used by the operation.

    Returns:
        object: Result produced by the operation.
    """
    with _LOG_MERGE_LOCKS_LOCK:
        return _LOG_MERGE_LOCKS.setdefault(os.path.abspath(path), threading.RLock())


def _log_content(path):
    """Read the exact bytes of the log at ``path``, or None when it is absent.

    `_publish_json_log` compares the content it read against this before it
        replaces the file: any publish by another process changes the bytes (an
        entry is always added), so a match means nothing was published in between.

    Args:
        path (str): Value used by the operation.

    Returns:
        NoneType | object: Result produced by the operation.
    """
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None


def _read_json_log(path):
    """Parse the JSON log at ``path``: ``(data, content)``.

    ``({}, None)`` while the file does not exist. A log that is empty (a
        publish cut short) or carries a trailing fragment (a crash, or a publish
        by the versions that shared one ``.tmp`` path) is read up to its first
        document: what is left is dropped, and the next publish rewrites the file
        without it, so a broken log cannot poison every later flush.

    Args:
        path (str): Value used by the operation.

    Returns:
        list | tuple: Result produced by the operation.
    """
    content = _log_content(path)
    if content is None:
        return {}, None
    text = content.decode("utf-8")
    if not text.strip():
        return {}, content  # nothing to keep: the publish repairs the file
    try:
        return json.loads(text), content
    except ValueError:
        data, _end = json.JSONDecoder().raw_decode(text)
        return data, content


def _publish_json_log(path, snapshot, record_key):
    """Merge one snapshot into the JSON log at ``path``.

    The file is read, merged and published back atomically (``os.replace``),
        so a reader never sees a truncated or partial log.

        Two writers must not lose each other's entries, which is what one shared
        ``<path>.tmp`` used to produce: one of them renamed a half-written file
        into place, the log no longer parsed (``Extra data``) and every later
        flush was dropped. Hence, per writer:

        * the temp file carries this writer's pid and thread id, so two writers
          can never truncate or rename each other's file into place;
        * ``_log_merge_lock`` serialises the merges of this process — several
          instances in one program (or one test session) sharing a log lose
          nothing;
        * a merge whose file changed under it — another *process* published since
          this writer read it — is redone from the new content instead of
          clobbering that process's entries.

    Args:
        path (str): JSON log file to merge into.
        snapshot (dict): Socket -> ``[[content, timestamp], ...]`` entries to add.
        record_key (callable): Maps one socket to its key in the log.

    Raises:
    RuntimeError: When every attempt lost the race to another writer.
    """
    with _log_merge_lock(path):
        for _ in range(LOG_MERGE_ATTEMPTS):
            existing, content = _read_json_log(path)
            for sock, entries in snapshot.items():
                key = record_key(sock)
                existing.setdefault(key, []).extend(entries)
            tmp_path = "{}.{}.{}.tmp".format(path, os.getpid(), threading.get_ident())
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(existing, f, ensure_ascii=False, indent=2)
            if _log_content(path) == content:  # no other process published meanwhile
                for _ in range(5):  # Windows: the log may be briefly open for reading
                    try:
                        os.replace(tmp_path, path)
                        return
                    except PermissionError:
                        time.sleep(0.05)
                os.replace(tmp_path, path)
                return
            os.remove(tmp_path)  # another process got there first: redo the merge
        raise RuntimeError("the log kept changing under the merge")


def _log_line(enabled, parts):
    """Print one command/result line when ``is_print_log`` allows it.

    Args:
        enabled (bool): Value used by the operation.
        parts (list | dict): Value used by the operation.
    """
    if enabled:
        print(*parts)


def _debug_line(enabled, is_debug, parts):
    """Print one execution-process line; needs ``is_print_log`` and ``is_debug``.

    Args:
        enabled (bool): Value used by the operation.
        is_debug (bool): Value used by the operation.
        parts (list | dict): Value used by the operation.
    """
    if enabled and is_debug:
        print(*parts)


def _parse_destination_path(command_part):
    """Extract the trailing destination directory from a received transfer

    command, or None when the command carries no destination.

        ``<id>`` is the file-client id appended by the sender; ``<addr>`` is a
        quoted client address tuple. The destination (when present) is the token
        before ``<id>``, except for folder-creation commands, which carry no id
        and put the destination last:

          /file <src> <id>
          /file <src> <dest> <id>
          /file <src> <addr> <id>
          /file <src> <addr> <dest> <id>
          /file_folder <rel>            folder creation, default location
          /file_folder <rel> <dest>     folder creation with destination
          /file_folder <rel> <file> <id>
          /file_folder <rel> <file> <dest> <id>

    Args:
        command_part (object): Value used by the operation.

    Returns:
        NoneType | object: Result produced by the operation.
    """
    if len(command_part) < 3:
        return None
    if command_part[0].lower() == "/file_folder":
        if len(command_part) == 3:
            return command_part[-1]  # creation + destination, no client id
        if len(command_part) >= 4 and command_part[-2] != command_part[2]:
            if not (command_part[-2].startswith("(") and command_part[-2].endswith(")")):
                return command_part[-2]
    elif len(command_part) >= 4:
        if not (command_part[-2].startswith("(") and command_part[-2].endswith(")")):
            return command_part[-2]
    return None


def _transfer_fid(command_part):
    """Return the fid a sender stamped on its transfer command, or None.

    Every user transfer command ends with the sender's transfer id
        (``/file <path> <id>``, ``/file_folder <rel> <file> [<dest>] <id>``); it
        keys that leg in ``file_recv_progress``/``file_upload_progress``. The
        protocol-internal ``/crypto_pub_key`` push reports None: it is no user
        file transfer.

    Args:
        command_part (object): Value used by the operation.

    Returns:
        NoneType | object: Result produced by the operation.
    """
    if command_part and command_part[0] == "/crypto_pub_key":
        return None
    try:
        return int(command_part[-1])
    except (IndexError, TypeError, ValueError):
        return None


def _peer_from_text(text):
    """Parse an ``"ip:port"`` peer string into an ``(ip, port)`` tuple.

    Args:
        text (object): Value used by the operation.

    Returns:
    tuple | None: The peer tuple, or None when the text carries none.
    """
    if not text:
        return None
    host, sep, port = str(text).rpartition(":")
    if not sep:
        return None
    try:
        return (host, int(port))
    except ValueError:
        return None


def _peer_from_json(value):
    """Parse a persisted ``[ip, port]`` peer into an ``(ip, port)`` tuple.

    Args:
        value (object): Value used by the operation.

    Returns:
    tuple | None: The peer tuple, or None when the value is not a peer.
    """
    if isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            return (str(value[0]), int(value[1]))
        except (TypeError, ValueError):
            return None
    return None


def _resume_offset_for(entry):
    """Bytes of an interrupted transfer's file already on the receiver's disk.

    The length of the partial file is what the sender is asked to continue
        from, so a missing (or unreadable) partial file restarts the transfer from
        its first byte.

    Args:
        entry (list): Progress entry ``[received, total, speed, path, peer]``.

    Returns:
    int: Byte offset to continue from.
    """
    try:
        size = os.path.getsize(entry[3])
    except (OSError, TypeError):
        return 0
    total = entry[1]
    if isinstance(total, int) and size > total:
        return 0  # not this transfer's partial file any more: start over
    return size


def parse_resume_command(command):
    """Split a ``/resume_trans <fid> <offset>`` line.

        Args:
            command (str): Wire line in ``/resume_trans <fid> <offset>`` format;
                ``fid`` and ``offset`` must be integers.

        Returns:
            tuple | None: ``(fid, offset)``, or None when the line is malformed.
        
    """
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    if len(parts) != 3 or parts[0].lower() != RESUME_LINE:
        return None
    try:
        return int(parts[1]), int(parts[2])
    except ValueError:
        return None


def _pause_command_fid(command):
    """Return the fid of a ``/pause_trans``/``/start_trans`` line, or None.

    Args:
        command (object): Value used by the operation.

    Returns:
        NoneType | object: Result produced by the operation.
    """
    try:
        return int(shlex.split(command)[1])
    except (IndexError, ValueError):
        return None


def _open_transfer_target(path, offset):
    """Open a transfer's destination file, ready to write from ``offset``.

    A resumed transfer continues a partial file: the file is opened for
        update and positioned at the byte the sender continues from, so a late or
        duplicated chunk overwrites its own place instead of corrupting the
        stream. A fresh transfer (offset 0) truncates the file.

    Args:
        path (str): Destination path of the transfer.
        offset (int): Byte offset the incoming stream starts at.

    Returns:
    file: Open binary file positioned at ``offset``.
    """
    if offset <= 0:
        return open(path, "wb")
    mode = "r+b" if os.path.exists(path) else "w+b"
    target = open(path, mode)
    target.seek(offset)
    return target


def _resumable_entry(progress, fid, peer, command_part):
    """Return the interrupted download a received ``/file`` continues, or None.

    A ``/file <name> <fid>`` line continues an entry only when the fid, the
    original sender (``peer``) and the file name all match; anything else is a
    new transfer and never touches an existing partial file.

    Args:
        progress (dict): ``file_recv_progress`` store, fid -> entry.
        fid (int | None): Transfer id the line carries.
        peer (tuple | None): Sender ``(ip, port)`` the line arrived from.
        command_part (list): Shell-split command tokens.

    Returns:
        list | None: The matching progress entry, or None.
    """
    if fid is None or peer is None or len(command_part) < FILE_COMMAND_TOKENS:
        return None
    if command_part[0].lower() != "/file":
        return None
    entry = progress.get(fid)
    if entry is None or entry[4] is None:
        return None
    if tuple(entry[4]) != tuple(peer):
        return None
    if os.path.basename(str(entry[3])) != os.path.basename(command_part[1]):
        return None
    return entry

def parse_forward_resume_command(command):
    """Split a ``/forward_resume <addr> <fid> <offset>`` line.

        Args:
            command (str): Wire line with a two-item tuple address, transfer ID,
                and byte offset; the address host must be a string and both values
                must be integers.

        Returns:
            tuple | None: ``(target, fid, offset)``, or None when the line is
                malformed.
        
    """
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    if len(parts) != 4 or parts[0].lower() != FORWARD_RESUME_LINE:
        return None
    try:
        target = ast.literal_eval(parts[1])
    except (ValueError, SyntaxError):
        return None
    if not (isinstance(target, tuple) and len(target) == 2 and isinstance(target[0], str)):
        return None
    try:
        return target, int(parts[2]), int(parts[3])
    except ValueError:
        return None


def parse_forwarded_message(command):
    """Split a ``/send_msg_from <addr> <payload>`` relay envelope.

    Args:
        command (str): Received line, e.g.
            ``/send_msg_from ('127.0.0.1', 3000) hello``.

    Returns:
        tuple | None: ``(sender_id, payload)`` where ``sender_id`` is the
            sender's ``"ip:port"``, or None when the line is not a well-formed
            envelope.
    """
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    if len(parts) < 3 or parts[0].lower() != "/send_msg_from":
        return None
    try:
        sender = ast.literal_eval(parts[1])
    except (ValueError, SyntaxError):
        return None
    if not (isinstance(sender, tuple) and len(sender) == 2 and isinstance(sender[0], str)):
        return None
    return f"{sender[0]}:{sender[1]}", " ".join(parts[2:])


def parse_forward_items_and_addrs(tokens):
    """Split forward-command tokens into items and destination addresses.

    A token of the form ``('ip', port)`` is a destination, everything else is a
    forwarded item (message text or a path). Used by the native message
    forwarding (``/forward_send_msg``) and by the file/folder forward extension.

    Args:
        tokens (list[str]): Tokens after the command name.

    Returns:
        tuple: ``(items, addrs)`` in the order given; ``items`` holds texts and
            paths, ``addrs`` holds ``(ip, port)`` tuples.
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
            if isinstance(addr, tuple) and len(addr) == 2 and isinstance(addr[0], str):
                addrs.append(addr)
            else:
                items.append(token)
        else:
            items.append(token)
    return items, addrs


def forward_skip_message(target):
    """Build the console notice for a forward destination that cannot be served.

    Args:
        target (tuple): Destination ``(ip, port)`` that is unreachable or is the
            server itself.

    Returns:
        str: One-line notice for the console.
    """
    return f"forward: destination {target} is unreachable or is the server, skipped"


def parse_forward_originator(command, own_address=None):
    """Extract the originator's ``"ip:port"`` from a received transfer command.

    The server's forward relay tags every pushed ``/file`` and ``/file_folder``
    command with the forwarding client's address tuple; a direct send carries the
    receiver's own address instead.

    Args:
        command (str): Received transfer command.
        own_address (str | None): This instance's own ``"ip:port"``; a command
            carrying it is a direct send and yields None.

    Returns:
        str | None: Originator ``"ip:port"``, or None when the command carries no
            originator (direct send or non-transfer command).
    """
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    if len(parts) < 4:
        return None
    for token in parts[1:-1]:  # skip the command name and the trailing id
        if token.startswith("(") and token.endswith(")"):
            try:
                addr = ast.literal_eval(token)
            except (ValueError, SyntaxError):
                continue
            if isinstance(addr, tuple) and len(addr) == 2 and isinstance(addr[0], str):
                originator = f"{addr[0]}:{addr[1]}"
                if own_address and originator == own_address:
                    return None  # direct send: the tuple is the receiver itself
                return originator
    return None


# ---- encrypted channel framing ---------------------------------------------
#
# On an encrypted socket every message travels as a frame:
#
#     magic "\\x00PF" | type (1) | body length (4, big endian) | body
#
# The body starts with the sender's session nonce and a per-socket sequence
# number (``<nonce>|<seq>|``) covering both frame types, so replay and
# cross-type reordering are detected. Two payload algorithms are used:
#
#   type 1 (RSA):   small text payloads up to the RSA-OAEP block limit; the
#                   body carries the chunked base64 ciphertext (same wire
#                   encoding the line-based control channel used before).
#   type 2 (ECDH):  everything else, including all binary payloads; the body
#                   carries a ``pf_ecdh_seal`` blob (salt|iv|ciphertext|tag)
#                   keyed by the session's ephemeral ECDH keypair, with
#                   ``<nonce>|<seq>`` bound as AAD.
#
# The receiver reads the frame type instead of inferring the algorithm from
# the payload length: a length can be ambiguous (an ECDH frame for a specific
# plaintext length has the same size as an RSA block) and inferring an
# algorithm from attacker-controlled bytes is a classic confusion oracle.
#
# Plaintext protocol lines (handshake, re-exchange and transfer coordination)
# may still arrive on an encrypted socket: they are passed through when they
# belong to the known protocol set and dropped otherwise, so an encrypted
# channel never dispatches injected plaintext commands.

FRAME_MAGIC = b"\x00PF"  # UTF-8 text lines never start with NUL, so a framed stream is unambiguous
FRAME_TYPE_RSA = 1
FRAME_TYPE_ECDH = 2
FRAME_HEADER_LEN = 8  # magic(3) + type(1) + length(4)
MAX_FRAME_PLAINTEXT = 1 << 20  # payload bytes per frame
MAX_FRAME_BODY = MAX_FRAME_PLAINTEXT + 8192  # sealed/rsa overhead slack
CRYPTO_FRAMES_START = "/crypto_frames_start"  # plaintext line: peer may send frames now
CRYPTO_ECDH_PUB = "/crypto_ecdh_pub"  # RSA-framed line carrying our ECDH public key
CRYPTO_FRAMES_TIMEOUT = 30.0  # seconds a framed send waits for the peer's frames-start
CRYPTO_PEER_KEY_TIMEOUT = 30.0  # seconds an ECDH frame waits for the peer's ECDH key
TRANSFER_KEY_TIMEOUT = 10.0  # seconds a transfer leg waits for its session key material

# Text payloads up to this size travel as a single RSA-OAEP block frame;
# 190 is pf_rsa_max_plaintext_len for the default 2048-bit key and the floor
# over the accepted key sizes (2048..16384), so the split is conservative.
RSA_SMALL_TEXT_LIMIT = 190

# Plaintext lines still tolerated on an encrypted socket: the crypto
# handshake/re-exchange lines, the frames-start announcement and the transfer
# port coordination messages. Anything else plaintext is dropped, so an
# injected line can neither run commands nor substitute key material
# (``/crypto_ecdh_pub`` is only ever accepted inside an RSA frame).
_PLAINTEXT_LINE_PREFIXES = (
    "/crypto_key_exchange",  # covers the ack too
    "/crypto_ready",
    "/crypto_pub_key",  # covers /crypto_pub_key_request
    "/crypto_reject",
    "/crypto_mode",
    CRYPTO_FRAMES_START,
    "/server_file_transfer_port",
    "/forward_upload",
)
# A plaintext handshake line means the peer reset its secure state
# (re-exchange): drop framing so the dance runs in raw mode again.
_PLAINTEXT_RESET_PREFIXES = _PLAINTEXT_LINE_PREFIXES[:-3]


class _FrameError(Exception):
    """A secure channel saw a malformed or undecryptable frame."""


def _pem_body_of(pem):
    """Return the one-line base64 body of a PEM text (armor lines dropped).

    Args:
        pem (object): Value used by the operation.

    Returns:
        object: Result produced by the operation.
    """
    body = "".join(
        line.strip() for line in pem.splitlines() if line and not line.startswith("-----")
    )
    return body


def _pem_text_of(body):
    """Rebuild a standard PEM text from its one-line base64 body.

    Args:
        body (object): Value used by the operation.

    Returns:
        object: Result produced by the operation.
    """
    lines = [body[i : i + 64] for i in range(0, len(body), 64)]
    return "-----BEGIN PUBLIC KEY-----\n" + "\n".join(lines) + "\n-----END PUBLIC KEY-----\n"


class _CryptoUnavailable(Exception):
    """A transfer leg was stamped encrypted but the session key is not ready."""


def _stamp_nonce(stamp):
    """Parse a transfer mode stamp ("plain" / "enc:<nonce>") -> nonce or None.

    Args:
        stamp (object): Value used by the operation.

    Returns:
        NoneType | object: Result produced by the operation.
    """
    if stamp and stamp.startswith("enc:"):
        nonce = stamp[4:].strip()
        return nonce or None
    return None


def _crypto_is_protocol_line(message):
    """Return true for the protocol's own handshake/coordination lines.

    True for the protocol's own handshake/coordination lines.

    These must not reset the decode-failure streak: a re-exchange (and the
        transfer-port coordination it needs) is not genuine peer traffic.

    Args:
        message (object): Value used by the operation.

    Returns:
        bool | object: Result produced by the operation.
    """
    if message.startswith("/crypto"):
        return True
    return message.startswith(("/server_file_transfer_port", "/forward_upload"))


class _CryptoSession:
    """Per control-session crypto context shared by that session's sockets.

    Holds the RSA identity and an ephemeral ECDH keypair; the public ECDH key
    travels as a small RSA-framed ``/crypto_ecdh_pub`` line after the channel
    flips, so the bulk (ECDH-sealed) key material is authenticated by the
    RSA/TOFU handshake that already authenticated the session.
    """

    def __init__(self, rsa_crypto_obj, role, log=None, debug=None):
        self.rsa = rsa_crypto_obj
        self.role = role
        self.log = log
        self.debug = debug
        self.lock = threading.RLock()
        self.ecdh = None
        self.peer_ecdh = None
        self.peer_ecdh_b64 = None
        self.peer_ecdh_event = threading.Event()

    def ensure_ecdh(self):
        """Handle the operation.

        Returns:
            object: Result produced by the operation.
        """
        with self.lock:
            if self.ecdh is None:
                self.ecdh = rsa_crypto.EcdhKeyPair()
            return self.ecdh

    def reset_ecdh(self):
        """Session was re-keyed: drop the old ECDH material."""
        with self.lock:
            self.ecdh = None
            self.peer_ecdh = None
            self.peer_ecdh_b64 = None
            self.peer_ecdh_event.clear()

    def set_peer_ecdh_b64(self, body):
        """Handle the operation.

        Args:
            body (object): Value used by the operation.

        Returns:
            bool: Result produced by the operation.
        """
        try:
            peer = rsa_crypto.EcdhPubKey.from_pem(_pem_text_of(body.strip()))
        except Exception:
            return False
        with self.lock:
            self.peer_ecdh = peer
            self.peer_ecdh_b64 = body.strip()
            self.peer_ecdh_event.set()
        return True

    def peer_ecdh_pub(self, timeout=0):
        """Return the peer's ECDH public key, or None (waits up to ``timeout``).

        The peer's ECDH public key, or None (waits up to ``timeout``).

        Args:
            timeout (float): Value used by the operation.

        Returns:
            NoneType | object: Result produced by the operation.
        """
        if not self.peer_ecdh_event.wait(timeout=max(0.0, timeout)):
            return None
        with self.lock:
            return self.peer_ecdh

    def publish_pub(self, channel):
        """Send our session ECDH public key once frames are allowed both ways.

        Args:
            channel (object): Value used by the operation.
        """
        if channel is None or not channel.claim_ecdh_pub_send():
            return
        try:
            body = _pem_body_of(self.ensure_ecdh().public_pem())
            channel.send(f"{CRYPTO_ECDH_PUB} {body}\n", force_rsa=True)
        except Exception:
            if self.log:
                self.log("crypto: publishing the session ECDH key failed")

    def transfer_stamp(self, channel, force_plain=False):
        """Mode+nonce announced to a transfer connector ("plain"/"enc:<nonce>").

        Encrypted only when this side's channel flipped and both sides hold
                the session ECDH material; key pushes force plaintext because they
                carry the public keys themselves.

        Args:
            channel (object): Value used by the operation.
            force_plain (bool): Value used by the operation.

        Returns:
            object | str: Result produced by the operation.
        """
        if force_plain or self.rsa is None or channel is None or not channel.secure:
            return "plain"
        self.ensure_ecdh()
        if self.peer_ecdh_pub(TRANSFER_KEY_TIMEOUT) is None:
            if self.log:
                self.log("crypto: peer ECDH key missing, transfer stays plaintext")
            return "plain"
        return "enc:" + secrets.token_hex(16)

    def attach_transfer(self, sock, control_channel, stamp):
        """Channel for a stamped transfer leg, or None when it stays plaintext.

        Raises `_CryptoUnavailable` when the stamp says encrypted but the
                session key material is not ready: the caller aborts the transfer
                instead of silently downgrading it.

        Args:
            sock (object): Value used by the operation.
            control_channel (object): Value used by the operation.
            stamp (object): Value used by the operation.

        Returns:
            NoneType | object: Result produced by the operation.

        Raises:
            _CryptoUnavailable: When the operation cannot complete.
        """
        nonce = _stamp_nonce(stamp)
        if nonce is None:
            return None
        self.ensure_ecdh()
        peer_pem_path = control_channel.peer_pem_path if control_channel is not None else None
        if peer_pem_path is None or self.peer_ecdh_pub(TRANSFER_KEY_TIMEOUT) is None:
            raise _CryptoUnavailable("transfer stamped encrypted but the session key is not ready")
        log = self.log or (lambda *parts: None)
        debug = self.debug or (lambda *parts: None)
        channel = _SecureChannel(sock, self, log, debug)
        channel.enable(nonce, nonce, peer_pem_path)
        channel.note_peer_frames_ready()  # the stamp fixes the mode: no frames-start dance on transfer legs
        return channel



def _connector_transfer_crypto(session, control_channel, stamp):
    """Return (session, peer_pem_path, nonce) for a connector-side transfer leg.

    (session, peer_pem_path, nonce) for a connector-side transfer leg.

    Returns None for a plaintext leg; raises `_CryptoUnavailable` when the
        stamp promises encryption but the session key material is missing, so the
        caller aborts the transfer instead of downgrading it silently.

    Args:
        session (object): Value used by the operation.
        control_channel (object): Value used by the operation.
        stamp (object): Value used by the operation.

    Returns:
        NoneType | list | tuple: Result produced by the operation.

    Raises:
        _CryptoUnavailable: When the operation cannot complete.
    """
    nonce = _stamp_nonce(stamp)
    if nonce is None:
        return None
    if session is None:
        raise _CryptoUnavailable("no crypto session for the transfer leg")
    session.ensure_ecdh()
    peer_pem_path = control_channel.peer_pem_path if control_channel is not None else None
    if peer_pem_path is None or session.peer_ecdh_pub(TRANSFER_KEY_TIMEOUT) is None:
        raise _CryptoUnavailable("transfer key material is not ready")
    return session, peer_pem_path, nonce


def _enable_connector_channel(sock, crypto):
    """Framing channel for a freshly connected transfer socket, or None.

    Args:
        sock (object): Value used by the operation.
        crypto (object): Value used by the operation.

    Returns:
        NoneType | object: Result produced by the operation.
    """
    if crypto is None:
        return None
    session, peer_pem_path, nonce = crypto
    channel = _SecureChannel(
        sock,
        session,
        session.log or (lambda *parts: None),
        session.debug or (lambda *parts: None),
    )
    channel.enable(nonce, nonce, peer_pem_path)
    channel.note_peer_frames_ready()  # the stamp fixes the mode: no frames-start dance
    return channel


def _magic_tail_len(buf):
    """Length of the proper ``FRAME_MAGIC`` prefix ``buf`` ends with (0..2).

    A magic split across reads is held, never served as plaintext: the bytes
        behind it belong to a frame, and frame bytes are not line traffic (see
        `_SecureChannel.feed`).

    Args:
        buf (object): Value used by the operation.

    Returns:
        int | object: Result produced by the operation.
    """
    for length in range(len(FRAME_MAGIC) - 1, 0, -1):
        if buf.endswith(FRAME_MAGIC[:length]):
            return length
    return 0


class _SecureChannel:
    """Framed encryption for one socket.

    Send: ``send`` chunks a payload into frames (RSA for a small text payload,
    ECDH-sealed otherwise). Receive: ``feed`` parses socket bytes into the
    plaintext buffer while the channel is framed (and reports the bytes
    unparsed while it is not), ``take`` serves the byte stream the callers
    read.

    Lifecycle: raw -> ``enable`` -> [``disable`` -> raw]. ``peer_frames_ready``
    must be set before frames may be sent (the peer announces it with a
    plaintext ``/crypto_frames_start`` after it flipped), so a frame can never
    reach a peer whose reader still expects raw bytes.
    """

    def __init__(
        self,
        sock,
        session,
        log,
        debug,
        send_lock=None,
        sendall=None,
        on_decode_failure=None,
        on_disable=None,
    ):
        self.socket = sock
        self.session = session
        self._log = log
        self._debug = debug
        self._on_decode_failure = on_decode_failure
        self._on_disable = on_disable  # keep the owner's secure-socket set truthful
        self._lock = threading.RLock()  # buffers + sequence counters
        self._send_lock = (
            send_lock if send_lock is not None else threading.Lock()
        )  # shared with the plaintext send path: frame and raw writes never interleave
        self._sendall = sendall if sendall is not None else sock.sendall
        self.secure = False
        self.peer_frames_ready = False
        self.frames_allowed = threading.Event()
        self.ecdh_pub_sent = False
        self.local_nonce = None
        self.peer_nonce = None
        self.peer_pem_path = None
        self.send_seq = 0
        self.recv_seq = 0
        self._raw = bytearray()  # socket bytes not yet parsed
        self._plain = bytearray()  # decrypted bytes ready for the reader

    def enable(self, local_nonce, peer_nonce, peer_pem_path=None):
        """Start speaking frames (flip completed on both sides).

        Args:
            local_nonce (object): Value used by the operation.
            peer_nonce (object): Value used by the operation.
            peer_pem_path (str): Value used by the operation.
        """
        with self._lock:
            self.local_nonce = local_nonce
            self.peer_nonce = peer_nonce
            self.peer_pem_path = peer_pem_path
            self.send_seq = 0
            self.recv_seq = 0
            self.ecdh_pub_sent = False  # a fresh session publishes its own key
            self.secure = True
            if self.peer_frames_ready:
                self.frames_allowed.set()

    def disable(self):
        """Drop back to raw mode (decode failure or peer re-exchange).

        The next handshake must announce frame readiness again, so the
                peer-ready flag is cleared with the mode, and the owner's secure-socket
                set is updated through ``on_disable``.
        """
        with self._lock:
            was_secure = self.secure
            self.secure = False
            self.peer_frames_ready = False
            self.ecdh_pub_sent = False
            self.frames_allowed.clear()
        if was_secure and self._on_disable is not None:
            try:
                self._on_disable()
            except Exception:
                pass

    def note_peer_frames_ready(self):
        """Return the peer announced it may receive frames.

        The peer announced it may receive frames.
        """
        with self._lock:
            self.peer_frames_ready = True
            if self.secure:
                self.frames_allowed.set()

    def claim_ecdh_pub_send(self):
        """Return true exactly once per session, when the peer can receive frames.

        True exactly once per session, when the peer can receive frames.

        Returns:
            bool: Result produced by the operation.
        """
        with self._lock:
            if self.ecdh_pub_sent or not self.frames_allowed.is_set():
                return False
            self.ecdh_pub_sent = True
            return True

    # -- send path -----------------------------------------------------------

    def send(self, payload, force_rsa=False):
        """Frame and send one payload (str or bytes) on this socket.

        ``force_rsa`` is used for the ECDH public-key line, which must travel
                in an RSA frame even if it is longer than the single-block limit (an
                ECDH frame would be circular: the peer key it needs is this payload).

                Raises on failure; the caller logs and reports the send failure.

        Args:
            payload (bytes | str): Value used by the operation.
            force_rsa (bool): Value used by the operation.

        Raises:
            RuntimeError: When the operation cannot complete.
        """
        if isinstance(payload, str):
            data = payload.encode("utf-8")
            small_text = True
        else:
            data = bytes(payload)
            small_text = False
        use_rsa = force_rsa or (small_text and len(data) <= RSA_SMALL_TEXT_LIMIT)
        peer = self.session.peer_ecdh_pub(0 if use_rsa else CRYPTO_PEER_KEY_TIMEOUT)
        if not use_rsa and peer is None:
            raise RuntimeError("encrypted channel has no peer ECDH key")
        if not self.frames_allowed.wait(timeout=CRYPTO_FRAMES_TIMEOUT):
            raise RuntimeError("encrypted channel not ready (peer never allowed frames)")
        if use_rsa:
            with self._send_lock:
                self._sendall(self._rsa_frame(data))
            return
        for start in range(0, max(len(data), 1), MAX_FRAME_PLAINTEXT):
            chunk = data[start : start + MAX_FRAME_PLAINTEXT]
            with self._send_lock:
                self._sendall(self._ecdh_frame(chunk, peer))

    def _rsa_frame(self, text_bytes):
        """Handle the operation.

        Args:
            text_bytes (bytes | str): Value used by the operation.

        Returns:
            object: Result produced by the operation.
        """
        with self._lock:
            nonce = self.local_nonce or ""
            seq = self.send_seq
            self.send_seq = seq + 1
        body = "{}|{}|{}".format(
            nonce,
            seq,
            self.session.rsa.encrypt_for_peer(self.peer_pem_path, text_bytes.decode("utf-8")),
        ).encode("ascii")
        return self._wrap(FRAME_TYPE_RSA, body)

    def _ecdh_frame(self, chunk, peer):
        """Handle the operation.

        Args:
            chunk (bytes | str): Value used by the operation.
            peer (tuple): Value used by the operation.

        Returns:
            object: Result produced by the operation.
        """
        with self._lock:
            nonce = self.local_nonce or ""
            seq = self.send_seq
            self.send_seq = seq + 1
        aad = "{}|{}".format(nonce, seq).encode("ascii")
        sealed = self.session.ecdh.seal(peer, chunk, aad)
        return self._wrap(FRAME_TYPE_ECDH, aad + b"|" + sealed)

    @staticmethod
    def _wrap(frame_type, body):
        """Handle the operation.

        Args:
            frame_type (object): Value used by the operation.
            body (object): Value used by the operation.

        Returns:
            object: Result produced by the operation.
        """
        return FRAME_MAGIC + bytes([frame_type]) + len(body).to_bytes(4, "big") + body

    # -- receive path --------------------------------------------------------

    def feed(self, data):
        """Parse received socket bytes; returns the bytes for the line reader.

        While the channel is framed every byte is parsed into the plaintext
                buffer and an empty result comes back; while it is not, plaintext
                bytes are returned for the line reader. Frame bytes are consumed in
                both modes: a peer that has not processed a re-exchange yet keeps
                sending frames after this side dropped framing, and such a stale
                frame cannot be authenticated any more — its bytes must never reach
                the line reader, which would echo them as a message and reset the
                decode-failure streak.

                The parse shares the channel lock with ``enable``/``disable``, so a
                flip that lands while the caller's read was in flight still
                classifies the bytes by the state of that moment.

        Args:
            data (bytes | str): Value used by the operation.

        Returns:
            object: Result produced by the operation.
        """
        with self._lock:
            self._raw += data
            return self._parse_buffered()

    def _parse_buffered(self):
        """Parse everything buffered; returns the bytes for the line reader.

        Must be called under the channel lock. A ``disable`` performed while
                parsing (the peer's re-exchange line) is honoured: the bytes behind
                it belong to the peer's raw stream and are returned as such.

        Returns:
            object: Result produced by the operation.
        """
        plain = bytearray()
        while self._raw:
            if (
                self._raw[: min(len(self._raw), len(FRAME_MAGIC))]
                == FRAME_MAGIC[: min(len(self._raw), len(FRAME_MAGIC))]
            ):
                if len(self._raw) < len(FRAME_MAGIC):
                    break  # a partial magic may still be a frame
                if not self._consume_frame():
                    break
            elif self.secure:
                if not self._consume_plaintext_line():
                    break
            else:
                # raw: only the bytes ahead of a stale frame are line traffic;
                # the frame behind them is consumed on the next turn — frame
                # bytes must never reach the line reader, which would echo
                # them as a message and reset the breaker (see feed)
                start = self._raw.find(FRAME_MAGIC)
                if start < 0:  # hold a magic a split may still complete
                    start = len(self._raw) - _magic_tail_len(self._raw)
                plain += self._raw[:start]
                del self._raw[:start]
        return bytes(plain)

    def _consume_frame(self):
        """Consume one whole frame; False when more bytes are needed.

        A framed channel decrypts the body; a raw one drops it, because the
                session keys were torn down with the framing.

        Returns:
            bool: Result produced by the operation.

        Raises:
            _FrameError: When the operation cannot complete.
        """
        if len(self._raw) < FRAME_HEADER_LEN:
            return False
        frame_type = self._raw[len(FRAME_MAGIC)]
        length = int.from_bytes(self._raw[len(FRAME_MAGIC) + 1 : FRAME_HEADER_LEN], "big")
        if frame_type not in (FRAME_TYPE_RSA, FRAME_TYPE_ECDH) or length > MAX_FRAME_BODY:
            self._raw.clear()  # a framed stream cannot be resynchronised
            raise _FrameError("malformed frame header")
        if len(self._raw) < FRAME_HEADER_LEN + length:
            return False
        body = bytes(self._raw[FRAME_HEADER_LEN : FRAME_HEADER_LEN + length])
        del self._raw[: FRAME_HEADER_LEN + length]
        if not self.secure:
            self._debug("crypto: dropped a stale frame")
            return True
        if frame_type == FRAME_TYPE_RSA:
            self._decode_rsa(body)
        else:
            self._decode_ecdh(body)
        return True

    def _consume_plaintext_line(self):
        """Pass known plaintext protocol lines through; drop anything else.

        Returns False when the line is not complete yet.

        Returns:
            bool: Result produced by the operation.

        Raises:
            _FrameError: When the operation cannot complete.
        """
        nl = self._raw.find(b"\n")
        if nl < 0:
            if len(self._raw) > MAX_FRAME_PLAINTEXT:
                self._raw.clear()
                raise _FrameError("unterminated plaintext line")
            return False
        line = bytes(self._raw[: nl + 1])
        del self._raw[: nl + 1]
        try:
            stripped = line.decode("utf-8").strip()
        except UnicodeDecodeError:
            stripped = ""
        if stripped.startswith(_PLAINTEXT_LINE_PREFIXES):
            if stripped.startswith(_PLAINTEXT_RESET_PREFIXES):
                # the peer restarted the handshake: framing is over until re-flip
                self._debug("crypto: peer re-exchange line, dropping framed mode")
                self.disable()
            self._plain += line
        else:
            self._debug("crypto: dropped plaintext line on an encrypted channel")
        return True

    def _split_prefix(self, body):
        """Split ``<nonce>|<seq>|`` off a frame body -> (nonce, seq) or None.

        Args:
            body (object): Value used by the operation.

        Returns:
            NoneType | list | tuple: Result produced by the operation.
        """
        first = body.find(b"|")
        if first < 0:
            return None
        second = body.find(b"|", first + 1)
        if second < 0:
            return None
        try:
            nonce = body[:first].decode("ascii")
            seq = int(body[first + 1 : second])
        except (UnicodeDecodeError, ValueError):
            return None
        return nonce, seq, body[second + 1 :]

    def _check_replay(self, nonce, seq):
        """Return true when the frame is fresh; a stale/duplicate frame is dropped.

        True when the frame is fresh; a stale/duplicate frame is dropped.

        Args:
            nonce (object): Value used by the operation.
            seq (object): Value used by the operation.

        Returns:
            bool: Result produced by the operation.
        """
        if nonce != self.peer_nonce:
            self._debug("crypto: replay dropped (nonce mismatch)")
            return False
        if seq != self.recv_seq:
            self._debug(
                "crypto: replay/out-of-order dropped (seq {}, expected {})".format(
                    seq, self.recv_seq
                )
            )
            return False
        return True

    def _decode_rsa(self, body):
        """Handle the operation.

        Args:
            body (object): Value used by the operation.

        Raises:
            _FrameError: When the operation cannot complete.
        """
        parts = self._split_prefix(body)
        if parts is None:
            raise _FrameError("malformed RSA frame")
        nonce, seq, cipher = parts
        if not self._check_replay(nonce, seq):
            return
        try:
            ok, plain = self.session.rsa.decrypt_with_own(cipher.decode("ascii"))
        except Exception:
            ok, plain = False, None
        if not ok:
            raise _FrameError("RSA decryption failed")
        self.recv_seq += 1
        self._plain += plain.encode("utf-8") if isinstance(plain, str) else bytes(plain)

    def _decode_ecdh(self, body):
        """Handle the operation.

        Args:
            body (object): Value used by the operation.

        Raises:
            _FrameError: When the operation cannot complete.
        """
        parts = self._split_prefix(body)
        if parts is None:
            raise _FrameError("malformed ECDH frame")
        nonce, seq, sealed = parts
        if not self._check_replay(nonce, seq):
            return
        peer = self.session.peer_ecdh_pub(0)
        if peer is None or self.session.ecdh is None:
            raise _FrameError("ECDH frame without session key material")
        aad = "{}|{}".format(nonce, seq).encode("ascii")
        try:
            plain = self.session.ecdh.open(peer, sealed, aad)
        except Exception:
            raise _FrameError("ECDH decryption failed")
        self.recv_seq += 1
        self._plain += plain

    def take(self, n):
        """Up to ``n`` plaintext bytes; empty when none are buffered.

        Args:
            n (int): Value used by the operation.

        Returns:
            bytes | object: Result produced by the operation.
        """
        with self._lock:
            if not self._plain:
                return b""
            out = bytes(self._plain[:n])
            del self._plain[: len(out)]
            return out

    def drain_plaintext(self):
        """Bytes the line reader still gets after framing failed.

        The decrypted buffer plus the plaintext already buffered with the
                frame that failed — the peer's re-exchange line, for example. Frame
                bytes stay in the channel, where the stale-frame drop consumes them:
                they must not be served as a line, and the channel may still need
                them to resynchronise.

        Returns:
            object: Result produced by the operation.
        """
        with self._lock:
            plain = bytearray(self._plain)
            self._plain.clear()
            if not self.secure:
                plain += self._parse_buffered()
            return bytes(plain)


class CommandNotFoundError(Exception):
    """A command passed to `call_extension_functions` is not registered.

    Raised when the first token of the command matches no handler registered
    with `register_command`, on either instance class.
    """


def _custom_handler_for(command, handlers):
    """Return the extension handler one command line selects.

    Args:
        command (str): Command line whose first token names the handler.
        handlers (list): The ``[{}, {}]`` registry, the console group and the inbound group.

    Returns:
    callable: The registered handler; with both groups registering the
                name, the console group wins.

    Raises:
    CommandNotFoundError: If neither group registers the first token.
            ValueError: If ``command`` cannot be split (unbalanced quotes).
    """
    cmd_parts = shlex.split(command.strip())
    cmd_name = cmd_parts[0].lower() if cmd_parts else ""
    handler = handlers[1].get(cmd_name)
    if handler is None:
        handler = handlers[0].get(cmd_name)
    if handler is None:
        raise CommandNotFoundError(f"Unknown extension command: {command}")
    return handler


class TCP_Server_Base:  # TCP server class
    """TCP server: accept clients, dispatch commands, relay messages and files.

    Each accepted connection is served by `handle_client`, either in its own
    thread (the default) or by a coroutine on an asyncio event loop when
    ``is_asynic_clients_io`` is True: every inbound line is delivered to the
    listeners registered with `add_message_listener` first; a line starting
    with ``/`` then goes to `handle_command` (built-in commands plus the
    handlers registered with `register_command`), any other line is a plain
    message stored in ``messages_dict``.

    Attributes:
        host (str): Address the server socket binds to.
        port (int): First port considered for binding and for allocation.
        clients (dict): Accepted connections keyed by ``(ip, port)``; each value
            holds ``socket``, ``address``, ``id`` and ``connected_time``.
        running (bool): True while the accept loop runs.
        is_asynic_clients_io (bool): Whether clients are served by coroutines on
            an event loop instead of one thread per client.
        is_enable_encrypto (bool): Whether the RSA channel is negotiated.
        is_debug (bool): Whether execution-process lines are logged as well.
        is_print_log (bool): Whether the instance logs anything at all.
        is_enable_file_transfer_progress_bar (bool): Whether every running file
            transfer is tracked in ``file_recv_progress``/``file_upload_progress``.
        is_enable_real_time_network_speed (bool): Whether the real-time network
            speed is measured into ``upload_network_speed``/``recv_network_speed``.
        file_recv_progress (dict): Running downloads keyed by fid; each value is
            ``[received bytes, total bytes, current speed, file path, peer]``
            (the speed is None until the transfer has moved its first chunk,
            and while speed measurement is off; ``peer`` is the sender's
            ``(ip, port)``).
        file_upload_progress (dict): Running uploads keyed by fid; each value is
            ``[sent bytes, total bytes, current speed, file path, peer]``, with
            ``peer`` the receiver's ``(ip, port)``. A forwarded transfer stores
            the original peer on both sides, never the relay.
        file_trans_progress_file (str): Path under the protocol's ``.Flow``
            directory where the two stores are written when a connection drops
            and read back on the next reconnect (``_request_resumes``).
        upload_network_speed (float | None): Bytes per second sent, or None when
            not measured.
        recv_network_speed (float | None): Bytes per second received, or None
            when not measured.
    """

    def __init__(
        self,
        host="127.0.0.1",
        port=65432,
        max_clients=10,
        port_add_step=1,
        port_range_num=100,
        max_file_transfer_thread_num=10,
        is_hand_alloc_port=False,
        is_input_command_in_console=True,
        max_custom_workers=10,
        is_extend_command=False,
        is_enable_encrypto=True,
        is_custom_keys=None,
        max_mem_buff=2048,
        recv_messages_chunk_len=4096,
        file_transfer_chunk_len=65536,
        is_asynic_clients_io=False,
        is_debug=False,
        is_print_log=True,
        is_enable_file_transfer_progress_bar=True,
        is_enable_real_time_network_speed=True,
    ):
        """Create the server and, unless extended, start accepting clients.

        Args:
            host (str): Address the server socket binds to. Defaults to
                "127.0.0.1".
            port (int): First port to bind; also the base of the allocation range.
            max_clients (int): Maximum concurrent clients served in thread mode;
                ignored when ``is_asynic_clients_io`` is True. Defaults to 10.
            port_add_step (int): Step between candidate ports. Defaults to 1.
            port_range_num (int): Number of ports per step. Defaults to 100.
            max_file_transfer_thread_num (int): Concurrent file transfers allowed.
                Defaults to 10.
            is_hand_alloc_port (bool): Reserve a port range across processes, so
                several instances on one host do not collide. Defaults to False.
            is_input_command_in_console (bool): Start the console command thread.
                Defaults to True.
            max_custom_workers (int): Worker slots for `submit_task` and threaded
                command handlers. Defaults to 10.
            is_extend_command (bool): When True, do not call `start_TCP_Server`;
                the caller starts the server when ready. Defaults to False.
            is_enable_encrypto (bool): Negotiate the RSA-encrypted channel for
                every connection. Defaults to True.
            is_custom_keys (list | None): ``[pub_key_path, pvt_key_path]`` pair used
                instead of the default key lookup; an invalid pair is ignored.
            max_mem_buff (int): Buffering ceiling in MiB for the in-memory forward
                pump; past it the uploader is told to pause. Defaults to 2048.
            recv_messages_chunk_len (int): Bytes read per receive on the control
                connection, covering commands and messages. Defaults to 4096.
            file_transfer_chunk_len (int): Chunk size in bytes streamed when
                sending or receiving a file or folder. Defaults to 65536 (64 KiB).
            is_asynic_clients_io (bool): Serve clients with asyncio coroutines on
                an event loop instead of one thread per client, so a single
                server can hold thousands of concurrent connections. Defaults to
                False.
            is_debug (bool): Log execution-process lines in addition to command
                and result lines. Defaults to False.
            is_print_log (bool): Log at all; False silences every line this
                instance would print. Defaults to True.
            is_enable_file_transfer_progress_bar (bool): Track every running
                file transfer in ``file_recv_progress`` (downloads) and
                ``file_upload_progress`` (uploads): each entry holds the bytes
                transferred so far, the total size and the current speed (None
                while ``is_enable_real_time_network_speed`` is False). An entry
                appears when a transfer starts and is dropped when it ends.
                Defaults to True.
            is_enable_real_time_network_speed (bool): Measure the real-time
                network speed of every send and receive — the control layer and
                every temporary connection (file transfers included) — into
                ``upload_network_speed`` and ``recv_network_speed`` (bytes per
                second, averaged over ``NETWORK_SPEED_WINDOW`` seconds). With
                False nothing is measured and every speed value is None.
                Defaults to True.

        Raises:
            OSError: If the ``.Flow`` directories or ``decode_command_table.json``
                cannot be created or read.
        """
        self.max_mem_buff = max_mem_buff * 1024 * 1024
        self.recv_messages_chunk_len = recv_messages_chunk_len
        self.file_transfer_chunk_len = file_transfer_chunk_len
        self.is_enable_file_transfer_progress_bar = is_enable_file_transfer_progress_bar
        self.is_enable_real_time_network_speed = is_enable_real_time_network_speed
        # Per-transfer progress stores: fid -> [transferred bytes, total bytes,
        # current speed, local file path, peer (ip, port)]. An entry appears
        # when a transfer starts and is removed when it ends, so the stores
        # hold exactly the transfers running now; iterate over a copy while
        # transfers are in flight. A transfer interrupted by a disconnect
        # keeps its entry (and its partial file) so it can be resumed once
        # the peer is back (see `_request_resumes`).
        self.file_recv_progress = {}  # downloads: fid -> [received, total, speed, path, peer]
        self.file_upload_progress = {}  # uploads: fid -> [sent, total, speed, path, peer]
        self.upload_network_speed = None  # bytes/s over the last NETWORK_SPEED_WINDOW seconds
        self.recv_network_speed = None
        self._network_speed_lock = threading.Lock()  # serialises the speed sample windows
        self._upload_speed_samples = collections.deque()  # (timestamp, bytes) samples
        self._recv_speed_samples = collections.deque()
        self._upload_speed_total = 0  # bytes held in the sample windows
        self._recv_speed_total = 0
        self._forward_fid = 0
        self._forward_fid_lock = threading.Lock()
        self._forward_relays = {}
        self._forward_relays_lock = threading.Lock()
        # Relay records kept after their relay died, so a disconnected
        # forward can be resumed: relay fid and every target tfid -> record.
        self._forward_resume_records = {}
        # Resume requests that arrived before the originator reconnected:
        # originator (ip, port) -> [(fid, target (ip, port), offset), ...].
        self._pending_relay_resumes = {}
        # Outbound-transfer pause switches (see `pause_trans`/`start_trans`):
        # fid -> bool. A leg waits while its own fid is marked paused.
        self._transfer_pause = {}
        self._transfer_pause_cond = threading.Condition()
        self._active_transfer_fids = set()  # fids of the legs running right now
        self._active_transfer_sockets = {}  # fid -> socket of the leg running right now
        self.project_dir = os.path.dirname(os.path.abspath(__file__))
        self.project_info_dir = os.path.join(self.project_dir, ".Flow")
        if os.path.exists(self.project_info_dir) == False:
            os.mkdir(self.project_info_dir)
        self.project_temp_info_dir = os.path.join(self.project_info_dir, "temp_info")
        if os.path.exists(self.project_temp_info_dir) == False:
            os.mkdir(self.project_temp_info_dir)
        self.server_port_lock_file = os.path.join(
            self.project_temp_info_dir, "server_port_lock.lock"
        )
        # Interrupted transfers are persisted here on a disconnect and read
        # back when a peer returns (see `_persist_transfer_progress`).
        self.file_trans_progress_file = os.path.join(
            self.project_info_dir, "server_file_trans_progress.json"
        )
        self._transfer_progress_lock = threading.RLock()  # serialises the persist/load pair
        self.decode_command_table_file_path = os.path.join(
            self.project_dir, "decode_command_table.json"
        )
        self.file_transfer_dir = os.path.join(self.project_dir, "received_files")
        self.host = host
        self.port = port
        self.all_allocated_ports_list = [self.port]
        self.max_clients = max_clients
        self.is_hand_alloc_port = is_hand_alloc_port
        self.is_input_command_in_console = is_input_command_in_console
        # `running` must exist before alloc_port(): the port-range record
        # reports it, and a manual-mode instance is live while it holds a range.
        self.running = False
        self._stopped = False  # a stop() before start_TCP_Server() must stick
        self.alloc_port(port_add_step, port_range_num)
        self.server_socket = None
        self.clients = {}  # store client info
        self.file_client_id_lock = threading.Lock()
        self.file_transfer_server_port_lock = threading.Lock()
        self.file_server_port_list = []
        self.file_client_id = 0
        self.client_lock = threading.Lock()  # add the threading lock
        self.max_file_transfer_thread_num = max_file_transfer_thread_num
        MAX_CONCURRENT_FILES = self.max_file_transfer_thread_num
        self.file_semaphore = threading.Semaphore(MAX_CONCURRENT_FILES)
        self.command_decode_table_str = None
        with open(self.decode_command_table_file_path, "r", encoding="utf-8") as f:
            self.command_decode_table_str = f.read()
        self.command_decode_table = ast.literal_eval(self.command_decode_table_str)
        self.send_file_header_sign = self.command_decode_table[0]["file_send_server_header"]
        self.send_file_data_sign = self.command_decode_table[0]["file_send_server_data"]
        self.server_received_file_header_sign = self.command_decode_table[0][
            "file_receive_client_header"
        ]
        self.server_received_file_data_sign = self.command_decode_table[0][
            "file_receive_client_data"
        ]
        self.server_start_file_transfer_sign = self.command_decode_table[0][
            "file_send_server_start_file_transfer"
        ]
        self.error_sign = self.command_decode_table[0]["file_send_receive_error"]
        self._custom_handlers = [{}, {}]
        self._custom_handler_threaded = [{}, {}]
        self._custom_executor = ThreadPoolExecutor(max_workers=max_custom_workers)
        self._task_semaphore = threading.Semaphore(max_custom_workers)
        # Inbound-event listeners (see add_message_listener/add_file_listener).
        # They run on the receive thread, so a listener must not block and must
        # never raise (exceptions are swallowed by the notify helpers).
        self._message_listeners = []
        self._file_listeners = []
        self._send_message_listeners = []
        # Sockets carrying the control protocol; only these are recorded.
        self._control_sockets = set()
        self._event_listeners_lock = threading.Lock()
        # Message/event stores: external code reads these instead of registering
        # listeners. Keyed by the socket; each value is a list of [content,
        # timestamp] pairs. messages_dict and sent_messages_dict hold plain text
        # (inbound / outbound); events_dict and sent_events_dict hold "/" commands.
        # When a store's total size reaches max_dict_size (64 KiB) it is flushed
        # to its JSON log and cleared (see _append_record, _flush_record_stores).
        self.messages_dict = {}
        self.events_dict = {}
        self.sent_messages_dict = {}
        self.sent_events_dict = {}
        self._messages_dict_lock = threading.Lock()
        self._events_dict_lock = threading.Lock()
        self._sent_messages_dict_lock = threading.Lock()
        self._sent_events_dict_lock = threading.Lock()
        self._socket_keys = {}  # record key -> "ip:port", captured while it was alive
        self._socket_keys_lock = threading.Lock()
        self._messages_dict_size = 0
        self._events_dict_size = 0
        self._sent_messages_dict_size = 0
        self._sent_events_dict_size = 0
        self.max_dict_size = 64 * 1024
        self.messages_log_file = os.path.join(self.project_info_dir, "messages_log.json")
        self.events_log_file = os.path.join(self.project_info_dir, "events_log.json")
        self.sent_messages_log_file = os.path.join(self.project_info_dir, "sent_messages_log.json")
        self.sent_events_log_file = os.path.join(self.project_info_dir, "sent_events_log.json")
        self.is_extend_command = is_extend_command
        self.is_enable_encrypto = is_enable_encrypto
        self.is_custom_keys = is_custom_keys
        self.is_asynic_clients_io = is_asynic_clients_io
        self.is_debug = is_debug
        self.is_print_log = is_print_log
        self._async_loop = None  # event loop driving the client coroutines (asynic clients io only)
        self._async_wakeup = None  # future `stop` completes to release the async accept loop
        self._crypto_lock = threading.RLock()  # serialises the crypto collections below (no-GIL safe); held only around short ops, never across I/O
        self._encrypted_sockets = set()  # sockets currently speaking frames (see _SecureChannel)
        self._channels = {}  # socket -> _SecureChannel (control and transfer connections)
        self._crypto_sessions = {}  # client_address -> _CryptoSession (shared by a session's sockets)
        self._crypto_peer = {}  # socket -> (peer_role, peer_pem_path)
        self._crypto_send_locks = {}  # socket -> Lock: wire order must match seq allocation order
        self._crypto_state = {}  # client_address -> crypto handshake state
        self._crypto_sock_addr = {}  # socket -> client_address
        self._crypto_push_active = set()  # sockets with an in-flight key push
        self.crypto = (
            rsa_crypto.RsaCrypto("server", self.project_dir, custom_keys=is_custom_keys)
            if is_enable_encrypto
            else None
        )
        if self.is_extend_command:
            pass
        else:
            self.start_TCP_Server()

    def _log(self, *parts):
        """Print a command/result line, unless ``is_print_log`` is False.

        Args:
            *parts (Any): Values forwarded to ``print``.
        """
        _log_line(self.is_print_log, parts)

    def _debug(self, *parts):
        """Print an execution-process line; needs ``is_print_log`` and ``is_debug``.

        Args:
            *parts (Any): Values forwarded to ``print``.
        """
        _debug_line(self.is_print_log, self.is_debug, parts)

    def _log_exc(self):
        """Print the traceback of the exception being handled, in debug mode only."""
        if self.is_print_log and self.is_debug:
            traceback.print_exc()

    def _measure_network_speed(self, payload, is_upload):
        """Fold just-sent/received traffic into the real-time speed estimate.

        Every send and receive sample feeds this — control messages, file data
                and relay traffic alike — so the speed attributes describe the
                instance's current traffic as a rolling average over
                ``NETWORK_SPEED_WINDOW`` seconds.

        Args:
            payload (bytes | str | int): Bytes just written or read; a str counts as its UTF-8 length, an int is a byte count already.
            is_upload (bool): True for sent bytes (``upload_network_speed``), False for received ones (``recv_network_speed``).

        Returns:
        float | None: The refreshed speed in bytes per second, or None when
                        ``is_enable_real_time_network_speed`` is False; both speed
                        attributes are None then.
        """
        if not self.is_enable_real_time_network_speed:
            self.upload_network_speed = None
            self.recv_network_speed = None
            return None
        if isinstance(payload, str):
            byte_count = len(payload.encode("utf-8"))
        elif isinstance(payload, int):
            byte_count = payload
        else:
            byte_count = len(payload)
        if not byte_count:
            return None
        now = time.monotonic()
        with self._network_speed_lock:
            if is_upload:
                samples = self._upload_speed_samples
                total = self._upload_speed_total + byte_count
            else:
                samples = self._recv_speed_samples
                total = self._recv_speed_total + byte_count
            samples.append((now, byte_count))
            cutoff = now - NETWORK_SPEED_WINDOW
            while samples[0][0] < cutoff:  # only the current window counts
                total -= samples.popleft()[1]
            if is_upload:
                self._upload_speed_total = total
            else:
                self._recv_speed_total = total
            span = now - samples[0][0]
            if span < NETWORK_SPEED_MIN_SPAN:  # a single burst is not extrapolated
                span = NETWORK_SPEED_MIN_SPAN
            speed = total / span
            if is_upload:
                self.upload_network_speed = speed
            else:
                self.recv_network_speed = speed
        return speed

    def _file_progress_start(self, progress_dict, fid, total_bytes, path, peer=None, initial=0):
        """Register one file transfer in a progress store.

        Args:
            progress_dict (dict): ``self.file_recv_progress`` for an inbound transfer, ``self.file_upload_progress`` for an outbound one.
            fid (int | None): The transfer's fid; None tracks nothing (a leg without a protocol transfer id, such as a key push).
            total_bytes (int): Size of the file being transferred.
            path (str): Local path of the file: the source of an outbound transfer, the file being written for an inbound one.
            peer (tuple | None): ``(ip, port)`` of the transfer's original peer — the sender for an inbound transfer, the receiver for an outbound one. A forwarded transfer stores the original peer on both sides, never the relay; None marks a leg that cannot be resumed (there is no peer to continue it with).
            initial (int): Bytes already transferred when the leg starts; a resumed transfer continues a partially transferred file.

        Returns:
        list | None: The ``[transferred, total, speed, path, peer]`` entry
                        the transfer updates, or None when tracking is off
                        (``is_enable_file_transfer_progress_bar`` False, or no fid).
        """
        if fid is None or not self.is_enable_file_transfer_progress_bar:
            return None
        entry = [initial, total_bytes, None, path, _peer_from_json(peer) if peer else None]
        progress_dict[fid] = entry
        with self._transfer_progress_lock:
            self._active_transfer_fids.add(fid)
        return entry

    def _file_progress_update(self, entry, byte_count, is_upload):
        """Fold one transferred chunk into a progress entry.

        Args:
            entry (list | None): Entry from `_file_progress_start`, or None when the transfer is not tracked.
            byte_count (int): Bytes of the chunk just sent or received.
            is_upload (bool): True for sent chunks, False for received ones. The speed slot mirrors the live measurement, so it stays None while ``is_enable_real_time_network_speed`` is False.
        """
        if entry is None:
            return
        entry[0] += byte_count
        entry[2] = self.upload_network_speed if is_upload else self.recv_network_speed

    def _file_progress_finish(self, progress_dict, fid, entry, keep=False):
        """Drop a finished transfer's progress entry, or keep it for a resume.

        A newer transfer may already hold the same fid (transfer ids are reused
                as transfers end), so only this transfer's own entry is dropped. With
                ``keep`` True an interrupted transfer keeps its entry — and the partial
                file that entry points at — so the reconnect logic can continue it
                (`_request_resumes`); the entry then lives until the transfer ends.

        Args:
            progress_dict (dict): Store the entry belongs to.
            fid (int | None): Transfer id the entry is keyed by.
            entry (list | None): Entry to drop; None tracks nothing.
            keep (bool): Keep the entry (and persist the stores) instead of dropping it. Defaults to False.
        """
        if entry is None:
            return
        with self._transfer_progress_lock:
            if progress_dict.get(fid) is entry:
                # a leg of an earlier session may finish after a newer leg
                # took the fid over: only the owner releases it
                self._active_transfer_fids.discard(fid)
        if keep:
            entry[2] = None  # a stopped transfer has no live speed any more
            self._persist_transfer_progress()
            return
        if progress_dict.get(fid) is entry:
            del progress_dict[fid]
        if os.path.exists(self.file_trans_progress_file):
            # the persisted stores are only kept up to date while they exist:
            # a transfer that ended must not be resumed on a later reconnect
            self._persist_transfer_progress()

    def _transfer_progress_snapshot(self):
        """Return the JSON-ready form of every transfer still in play.

        Returns:
        dict: ``{"upload": ..., "download": ..., "relays": ...}``; fids
                        are string keys and every peer is an ``[ip, port]`` list.
        """

        def entries(store):
            """Handle the operation.

            Args:
                store (list | dict): Value used by the operation.

            Returns:
                list | tuple: Result produced by the operation.
            """
            return {
                str(fid): [
                    entry[0],
                    entry[1],
                    entry[2],
                    entry[3],
                    list(entry[4]) if entry[4] else None,
                ]
                for fid, entry in list(store.items())
            }

        with self._forward_relays_lock:
            relays = {
                str(key): {
                    "fid": record.get("fid"),
                    "originator": list(record["originator"]) if record.get("originator") else None,
                    "targets": [list(target) for target in record.get("targets") or []],
                    "tfids": list(record.get("tfids") or []),
                    "kind": record.get("kind"),
                    "rel_dir": record.get("rel_dir"),
                    "fname": record.get("fname"),
                    "destination": record.get("destination"),
                }
                for key, record in list(self._forward_resume_records.items())
            }
        return {
            "upload": entries(self.file_upload_progress),
            "download": entries(self.file_recv_progress),
            "relays": relays,
        }

    def _persist_transfer_progress(self):
        """Write the transfer stores to the protocol's ``.Flow`` directory.

        Every in-flight transfer — uploads, downloads, and the relay records a
                disconnected forward is resumed from — is written to
                ``server_file_trans_progress.json`` when a connection drops, and the
                file is what the reconnect logic reads back. Once nothing is left to
                transfer the file is emptied; it is only rewritten while it exists, so
                a run without an interrupted transfer never creates it. A write
                failure is logged, never raised.
        """
        snapshot = self._transfer_progress_snapshot()
        with self._transfer_progress_lock:
            if not any(snapshot.values()) and not os.path.exists(self.file_trans_progress_file):
                return
            tmp_path = f"{self.file_trans_progress_file}.{os.getpid()}.tmp"
            try:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(snapshot, f, ensure_ascii=False, indent=2)
                os.replace(tmp_path, self.file_trans_progress_file)
            except OSError as e:
                self._log(f"transfer progress file update failed: {e}")
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    def _load_transfer_progress(self):
        """Read the persisted interrupted transfers back into the stores.

        Every entry read back keeps the bytes it had already transferred, so
                the reconnect logic can ask the sender to continue from there; every
                relay record comes back so a disconnected forward can be routed again.
                Entries a live transfer (or a relay) already owns are left alone.

        Returns:
        int: Number of transfer entries and relay records restored.
        """
        with self._transfer_progress_lock:
            try:
                with open(self.file_trans_progress_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except FileNotFoundError:
                return 0
            except (OSError, ValueError) as e:
                self._log(f"transfer progress file unreadable ({e}), ignored")
                return 0
            if not isinstance(data, dict):
                return 0
            restored = 0
            with self._forward_relays_lock:
                for key_text, record in (data.get("relays") or {}).items():
                    try:
                        key = int(key_text)
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(record, dict):
                        continue
                    record = dict(record)
                    record["originator"] = _peer_from_json(record.get("originator"))
                    record["targets"] = [
                        target
                        for target in (_peer_from_json(t) for t in record.get("targets") or [])
                        if target
                    ]
                    self._forward_resume_records.setdefault(key, record)
                    restored += 1
            for key, store in (
                ("download", self.file_recv_progress),
                ("upload", self.file_upload_progress),
            ):
                for fid_text, row in (data.get(key) or {}).items():
                    try:
                        fid = int(fid_text)
                        entry = [int(row[0]), int(row[1]), None, row[3], _peer_from_json(row[4])]
                    except (TypeError, ValueError, IndexError, KeyError):
                        continue
                    if fid in store or fid in self._forward_resume_records:
                        continue  # a live transfer owns it, or a relay drives it
                    store[fid] = entry
                    restored += 1
            return restored

    def _forward_relay_targets(self, fid, peer=None):
        """Live relay writers a pause/resume for ``fid`` addresses.

        Args:
            fid (int): Relay fid, or one of its per-target transfer ids.
            peer (tuple | None): Restrict to the writer serving this peer; None addresses every writer of the relay.

        Returns:
        tuple: ``(relay, [targets])``; ``(None, [])`` when no live relay
                        writer matches.
        """
        with self._forward_relays_lock:
            relay = self._forward_relays.get(fid)
        if relay is None or not relay.get("writer_pause"):
            return None, []
        if peer is None:
            return relay, list(relay["writer_pause"])
        for target in relay["writer_pause"]:
            if tuple(target) == tuple(peer):
                return relay, [target]
        return None, []

    def _mark_transfer_pause(self, fid, paused, peer=None):
        """Set or clear one transfer's pause switch (see `pause_trans`).

        Args:
            fid (int): Value used by the operation.
            paused (bool): Value used by the operation.
            peer (tuple): Value used by the operation.

        Returns:
        bool: True when a live relay writer or outbound leg was found.
        """
        relay, targets = self._forward_relay_targets(fid, peer)
        if targets:
            with relay["cond"]:
                for target in targets:
                    relay["writer_pause"][target] = paused
                if not paused:
                    relay["cond"].notify_all()
            return True
        with self._transfer_progress_lock:
            live = fid in self._active_transfer_fids
        if live and peer is not None:
            entry = self.file_upload_progress.get(fid)
            if entry is not None and entry[4] is not None and tuple(entry[4]) != tuple(peer):
                return False  # that leg belongs to another peer
        with self._transfer_pause_cond:
            self._transfer_pause[fid] = paused
            self._transfer_pause_cond.notify_all()
        return live

    def pause_trans(self, fid, peer=None):
        """Pause an outbound transfer of this instance.

        The sending leg with this fid stops at its next chunk and stays open
        until `start_trans` (or the peer's ``/start_trans``) resumes it. A
        forward relay's writer toward ``peer`` is paused the same way the
        ``/pause_trans`` line pauses it.

        Args:
            fid (int): Transfer id the outbound leg is keyed by.
            peer (tuple | None): Restrict the pause to the leg or relay writer
                toward this ``(ip, port)``; None addresses every match.

        Returns:
            bool: True when a running leg (or relay writer) was marked paused;
                False when nothing matched — the switch is still armed, so a
                leg later started under this fid starts paused.
        """
        return self._mark_transfer_pause(fid, True, peer)

    def start_trans(self, fid, peer=None):
        """Resume an outbound transfer paused with `pause_trans`.

        Args:
            fid (int): Transfer id the outbound leg is keyed by.
            peer (tuple | None): Restrict the resume to the leg or relay
                writer toward this ``(ip, port)``; None addresses every match.

        Returns:
            bool: True when a live leg (or relay writer) was resumed.
        """
        return self._mark_transfer_pause(fid, False, peer)

    def _register_transfer_socket(self, fid, sock):
        """Record the socket of a leg, so a dead session can be cleared out.

        Args:
            fid (int): Value used by the operation.
            sock (object): Value used by the operation.
        """
        if fid is None:
            return
        with self._transfer_progress_lock:
            self._active_transfer_sockets[fid] = sock

    def _release_transfer_socket(self, fid, sock):
        """Forget a leg's socket once the leg is over (its own, not a newer one's).

        Args:
            fid (int): Value used by the operation.
            sock (object): Value used by the operation.
        """
        if fid is None:
            return
        with self._transfer_progress_lock:
            if self._active_transfer_sockets.get(fid) is sock:
                del self._active_transfer_sockets[fid]

    def _drop_stale_transfers(self, peer=None):
        """Close the legs a peer's previous session left behind.

        A peer that (re)connects proves that the legs of its earlier session
                are over: their sockets are shut down so the stored transfers can be
                resumed without two legs writing the same transfer.

        Args:
            peer (tuple | None): Restrict to the legs whose progress entry names this ``(ip, port)``; None clears every leg of the instance (a fresh session replaces all of them).

        Returns:
        int: Number of legs closed.
        """
        stale = []
        for store in (self.file_upload_progress, self.file_recv_progress):
            for fid, entry in list(store.items()):
                if peer is not None and (entry[4] is None or tuple(entry[4]) != tuple(peer)):
                    continue
                with self._transfer_progress_lock:
                    sock = self._active_transfer_sockets.pop(fid, None)
                    if sock is not None:
                        self._active_transfer_fids.discard(fid)
                if sock is not None:
                    stale.append(sock)
        for sock in stale:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass  # already gone
            try:
                sock.close()
            except OSError:
                pass
        return len(stale)

    def pause_trans_remote(self, peer, fid):
        """Ask a connected client to pause its transfer toward this server.

        Sends the ``/pause_trans <fid>`` line the peers use for transfer flow
        control (see `pause_trans` on the receiving side).

        Args:
            peer (tuple): Connected client ``(ip, port)``.
            fid (int): Transfer id of that client's outbound leg.

        Returns:
            bool: True when the line was written; False when ``peer`` is not
                connected.
        """
        info = self.clients.get(peer) if peer is not None else None
        if info is None:
            self._log(f"pause: {peer} is not a connected client")
            return False
        return self.send_message(info["socket"], f"/pause_trans {fid}")

    def start_trans_remote(self, peer, fid):
        """Ask a connected client to resume a transfer paused earlier.

        Args:
            peer (tuple): Connected client ``(ip, port)``.
            fid (int): Transfer id of that client's outbound leg.

        Returns:
            bool: True when the line was written; False when ``peer`` is not
                connected.
        """
        info = self.clients.get(peer) if peer is not None else None
        if info is None:
            self._log(f"start: {peer} is not a connected client")
            return False
        return self.send_message(info["socket"], f"/start_trans {fid}")

    def _wait_transfer_paused(self, fid):
        """Block a sending leg while its fid is marked paused.

        Runs before every chunk so ``/pause_trans`` stops the stream without
                closing the transfer; the wait ends when the leg is resumed, when the
                instance stops, or when the fid is not paused at all.

        Args:
            fid (int): Value used by the operation.
        """
        if fid is None:
            return
        with self._transfer_pause_cond:
            while self._transfer_pause.get(fid, False):
                if not self.running:
                    return
                self._transfer_pause_cond.wait(timeout=0.5)

    def _wait_transfer_server_port(self, fid, timeout=RESUME_PORT_TIMEOUT):
        """Wait for the peer's ``/server_file_transfer_port`` of one fid.

        Args:
            fid (int): Transfer id the peer announces its listener under.
            timeout (float): Seconds to wait.

        Returns:
        tuple | None: ``(port, stamp)`` announced for this fid, or None
                        when the announcement does not arrive in time.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.file_transfer_server_port_lock:
                for info in reversed(list(self.file_server_port_list)):
                    if info[1] == fid:
                        # the newest announcement wins: an older one may belong
                        # to a leg of a session that is already gone
                        self.file_server_port_list.remove(info)
                        stamp = info[2] if len(info) > 2 else "plain"
                        return info[0], stamp
            time.sleep(0.1)
        return None

    def _forget_transfer_port(self, fid):
        """Drop pending port announcements of a fid an older session left behind.

        Args:
            fid (int): Value used by the operation.
        """
        if fid is None:
            return
        with self.file_transfer_server_port_lock:
            self.file_server_port_list[:] = [
                info for info in self.file_server_port_list if info[1] != fid
            ]

    def _wait_control_secure(self, control_socket, timeout=RESUME_SECURE_TIMEOUT):
        """Wait until a control channel may carry framed protocol lines.

        The reconnect flows run right after a session is established, when the
                crypto handshake may still be completing; a resume line sent then would
                travel plaintext and be dropped by the peer's framing guard. Without
                encryption there is nothing to wait for.

        Args:
            control_socket (socket.socket): Value used by the operation.
            timeout (float): Value used by the operation.

        Returns:
        bool: True when the channel speaks frames (or encryption is off).
        """
        if not self.is_enable_encrypto or self.crypto is None:
            return True
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._crypto_lock:
                channel = self._channels.get(control_socket)
            if channel is not None and channel.secure:
                return True
            time.sleep(0.05)
        self._log("resume: control channel not framed yet, transfers kept for the next reconnect")
        return False

    def alloc_port(self, port_add_step, port_range_num):
        """Reserve this server's port range under the cross-process lock.

        No-op unless ``is_hand_alloc_port`` is True.

        Args:
            port_add_step (int): Step between candidate ports.
            port_range_num (int): Number of ports per step.
        """
        if self.is_hand_alloc_port == True:
            while self.is_server_port_temp_info_file_locked():
                time.sleep(0.1)
            self.server_port_temp_info_file_lock()
            try:
                self.hand_alloc_port(port_add_step, port_range_num)
            finally:
                # a failing allocation must not leave the lock behind: every
                # later instance would wait for it forever
                self.server_port_temp_info_file_unlock()

    def free_port(self):
        """Release this server's reserved port range.

        No-op unless ``is_hand_alloc_port`` is True.
        """
        if self.is_hand_alloc_port == True:
            while self.is_server_port_temp_info_file_locked():
                time.sleep(0.1)
            self.server_port_temp_info_file_lock()
            try:
                self.hand_free_port()
            finally:
                self.server_port_temp_info_file_unlock()

    def server_port_temp_info_file_lock(self):
        """Create the lock file that reserves the server port range for this process."""
        with open(self.server_port_lock_file, "w", encoding="utf-8") as f:
            f.write("locked")

    def is_server_port_temp_info_file_locked(self):
        """Report whether the server port range is reserved by some process.

        Returns:
            bool: True while the lock file exists.
        """
        if os.path.exists(self.server_port_lock_file):
            return True
        else:
            return False

    def server_port_temp_info_file_unlock(self):
        """Remove the lock file that reserves the server port range."""
        if os.path.exists(self.server_port_lock_file):
            os.remove(self.server_port_lock_file)

    def hand_alloc_port(self, port_add_step, port_range_num):
        """Allocate the next free server port range and record it on disk.

        ``port`` is moved past the ranges already recorded by other servers, so the
        instance ends up with a range of its own.

        Args:
            port_add_step (int): Step between candidate ports.
            port_range_num (int): Number of ports per step.

        Raises:
            OSError: If the server port info file cannot be read or written.
        """
        self.port_temp_info_path = os.path.join(self.project_temp_info_dir, "server_port_info.log")
        client_port_temp_info_file_path = os.path.join(
            self.project_temp_info_dir, "clients_port_info.log"
        )
        if os.path.exists(client_port_temp_info_file_path):
            self._log(
                "Warning: client port info file exists, means the client has already allocated a port, may cause port conflict!"
            )
        self.port_add_step = port_add_step
        self.port_range_num = port_range_num
        self.add_latest_port = self.port + 1
        self.minus_latest_port = self.port
        self.alloc_add_port_lock = threading.Lock()
        self.alloc_minus_port_lock = threading.Lock()
        self.each_client_port_range = int(self.port_range_num / self.max_clients)
        if os.path.exists(self.port_temp_info_path) == False:
            self.server_num = 0
            self.server_port_info = []
            self.min_port = self.port - self.port_add_step * self.port_range_num
            self.max_port = self.port + 1 + self.port_add_step * self.port_range_num
            each_server_info = {
                "server_id": self.server_num,
                "host": self.host,
                "port": self.port,
                "min_port": self.min_port,
                "max_port": self.max_port,
                "is_running": True,
                "pid": os.getpid(),  # the sweep reclaims the range once it is gone
                "started_at": time.time(),
            }
            self.server_port_info.append(each_server_info)
            with open(self.port_temp_info_path, "w", encoding="utf-8") as f:
                f.write(str(self.server_port_info))
        else:
            with open(self.port_temp_info_path, "r", encoding="utf-8") as f:
                self.server_port_info = ast.literal_eval(f.read())
            # drop the entries whose owning process is gone: a killed instance
            # never reaches hand_free_port, and its range is free again
            self.server_port_info = [
                entry for entry in self.server_port_info if not _port_record_stale(entry)
            ]
            if not self.server_port_info:  # nobody holds a range: start over
                os.remove(self.port_temp_info_path)
                self.hand_alloc_port(port_add_step, port_range_num)
                return
            self.server_num = self.server_port_info[len(self.server_port_info) - 1]["server_id"] + 1
            auto_port_add = (
                self.server_port_info[len(self.server_port_info) - 1]["max_port"]
                + self.port_add_step * self.port_range_num
                + 1
            )
            auto_port_minus = (
                self.server_port_info[len(self.server_port_info) - 1]["min_port"]
                - self.port_add_step * self.port_range_num
                - 1
            )
            if self.port > auto_port_minus and self.port < auto_port_add:
                self.port = auto_port_add
            self.min_port = self.port - self.port_add_step * self.port_range_num
            self.max_port = self.port + 1 + self.port_add_step * self.port_range_num
            each_server_info = {
                "server_id": self.server_num,
                "host": self.host,
                "port": self.port,
                "min_port": self.min_port,
                "max_port": self.max_port,
                # this instance is live while it holds the range: hand_free_port()
                # drops the entry on a clean stop, and the pid lets a later
                # instance reclaim the range of one that was killed
                "is_running": True,
                "pid": os.getpid(),
                "started_at": time.time(),
            }
            self.server_port_info.append(each_server_info)
            with open(self.port_temp_info_path, "w", encoding="utf-8") as f:
                f.write(str(self.server_port_info))

    def hand_free_port(self):
        """Drop this server's entry from the on-disk port range record."""
        self.port_temp_info_path = os.path.join(self.project_temp_info_dir, "server_port_info.log")
        if os.path.exists(self.port_temp_info_path):
            with open(self.port_temp_info_path, "r", encoding="utf-8") as f:
                self.server_port_info = ast.literal_eval(f.read())
            # rebuild instead of deleting by index: other live instances keep
            # their entries, so this one is not necessarily the last, and a
            # shrinking list would make the next index run past its end
            self.server_port_info = [
                entry for entry in self.server_port_info if entry["server_id"] != self.server_num
            ]
            if len(self.server_port_info) == 0:
                os.remove(self.port_temp_info_path)
            else:
                with open(self.port_temp_info_path, "w", encoding="utf-8") as f:
                    f.write(str(self.server_port_info))

    def palloc(self):
        """Allocate a transfer port, waiting until one is free.

        Returns:
            int: Allocated port, or 0 when allocation is disabled
                (``is_hand_alloc_port`` False).
        """
        alloc_port = 0
        while True:
            alloc_port = self.file_palloc()
            if alloc_port is not None:
                return alloc_port
            alloc_port = self.spy_palloc()
            if alloc_port is not None:
                return alloc_port
            # both directions are exhausted: wait for a port to come back
            time.sleep(0.1)

    def pfree(self, port):
        """Release a port obtained from `palloc`.

        Args:
            port (int): Port to release.
        """
        self.file_pfree(port)
        self.spy_pfree(port)

    def file_palloc(self):
        """Allocate the next port above the base, or the first free one in range.

        Returns:
            int: Allocated port; None when the upward range is exhausted; 0 when
                allocation is disabled (``is_hand_alloc_port`` False).
        """
        if self.is_hand_alloc_port:
            with self.alloc_add_port_lock:
                if self.add_latest_port + self.port_add_step > self.max_port:
                    for step in range(self.port + 1, self.max_port, self.port_add_step):
                        if step in self.all_allocated_ports_list:
                            pass
                        else:
                            allocated_port = step
                            self.all_allocated_ports_list.append(allocated_port)
                            return allocated_port
                    return None
                self.add_latest_port += self.port_add_step
                self.all_allocated_ports_list.append(self.add_latest_port)
                return self.add_latest_port
        else:
            return 0

    def file_pfree(self, port):
        """Release a port obtained from `file_palloc` and step the cursor back.

        Args:
            port (int): Port to release. Ignored when allocation is disabled.
        """
        if self.is_hand_alloc_port:
            with self.alloc_add_port_lock:
                if port in self.all_allocated_ports_list:
                    self.all_allocated_ports_list.remove(port)
                    self._debug("releasing file transfer port, current latest port:", port)
                self.add_latest_port -= self.port_add_step
        else:
            pass

    def spy_palloc(self):
        """Allocate the next port below the base, or the first free one in range.

        Returns:
            int: Allocated port; None when the downward range is exhausted; 0 when
                allocation is disabled (``is_hand_alloc_port`` False).
        """
        if self.is_hand_alloc_port:
            with self.alloc_minus_port_lock:
                if self.minus_latest_port - self.port_add_step < self.min_port:
                    for step in range(self.port, self.min_port, -self.port_add_step):
                        if step in self.all_allocated_ports_list:
                            pass
                        else:
                            allocated_port = step
                            self.all_allocated_ports_list.append(allocated_port)
                            return allocated_port
                    return None
                self.minus_latest_port -= self.port_add_step
                self.all_allocated_ports_list.append(self.minus_latest_port)
                return self.minus_latest_port
        else:
            return 0

    def spy_pfree(self, port):
        """Release a port obtained from `spy_palloc` and step the cursor back.

        Args:
            port (int): Port to release. Ignored when allocation is disabled.
        """
        if self.is_hand_alloc_port:
            with self.alloc_minus_port_lock:
                if port in self.all_allocated_ports_list:
                    self.all_allocated_ports_list.remove(port)
                self.minus_latest_port += self.port_add_step
        else:
            pass

    def register_command(self, command_name, handler, where_to_run, run_in_thread=False):
        """Register a custom command handler.

        Args:
            command_name (str): Command to intercept, e.g. "/my_command"; matched
                case-insensitively against the first token.
            handler (callable): ``handler(client_socket, client_address, command)``
                called with the raw line; a non-None return value is sent back to
                the sender as the response.
            where_to_run (str): "server" for commands arriving from clients,
                "client" for commands typed on this instance's console.
            run_in_thread (bool): Run the handler on the worker pool instead of the
                reader thread. Defaults to False.

        Returns:
            bool | None: False when ``where_to_run`` is neither "server" nor
                "client"; the handler is then not registered.
        """
        registe_index = None
        if where_to_run == "server":
            registe_index = 0
        elif where_to_run == "client":
            registe_index = 1
        else:
            self._log(f"Invalid where_to_run value: {where_to_run}, must be 'server' or 'client'")
            return False
        self._custom_handlers[registe_index][command_name] = handler
        self._custom_handler_threaded[registe_index][command_name] = run_in_thread


    def call_extension_functions(self, command):
        """Call one registered extension command handler directly from code.

        The handler is selected by the first token of ``command``, matched
        case-insensitively against the names given to `register_command`: the
        console group is searched first, then the group fed by the lines
        clients send. The handler runs on the calling thread — ``run_in_thread``
        only affects the console and inbound dispatch paths — and is called
        with ``None`` for both ``client_socket`` and ``client_address``, since
        a direct call names no peer.

        Args:
            command (str): Command line to run, e.g. ``"/my_command arg"``; the
                handler receives it unchanged.

        Returns:
            object: Whatever the handler returns.

        Raises:
            CommandNotFoundError: If no registered handler matches the first
                token of ``command``.
            ValueError: If ``command`` cannot be split (unbalanced quotes).
        """
        handler = _custom_handler_for(command, self._custom_handlers)
        return handler(None, None, command)

    def add_message_listener(self, listener):
        """Register ``listener(client_id, message)`` for every inbound line.

        Every line received from a client is reported, commands included
        (``/help``, ``/file ...``, the crypto handshake lines, ...), so the
        listener observes all inbound traffic; dispatch is unaffected, a
        command still goes through the command handlers.

        Args:
            listener (callable): ``listener(client_id, message)`` where
                ``client_id`` is the sender's ``"ip:port"``. It runs on the receive
                thread, so it must not block, and exceptions raised inside it are
                swallowed.
        """
        with self._event_listeners_lock:
            if listener not in self._message_listeners:
                self._message_listeners.append(listener)

    def remove_message_listener(self, listener):
        """Unregister a listener previously added by `add_message_listener`.

        Args:
            listener (callable): Listener to remove; an unknown one is ignored.
        """
        with self._event_listeners_lock:
            try:
                self._message_listeners.remove(listener)
            except ValueError:
                pass

    def add_send_message_listener(self, listener):
        """Register ``listener(target_id, message)`` for every payload written by `send_message`.

        Fired after a successful write, for text and bytes alike (so a byte stream
        reaches it), never for ``is_send_raw`` protocol lines. ``target_id`` is the
        receiving client's ``"ip:port"``. It runs on the sending thread, so it must
        not block, and exceptions raised inside it are swallowed.

        Args:
            listener (callable): ``listener(target_id, message)``.
        """
        with self._event_listeners_lock:
            if listener not in self._send_message_listeners:
                self._send_message_listeners.append(listener)

    def remove_send_message_listener(self, listener):
        """Unregister a listener previously added by `add_send_message_listener`.

        Args:
            listener (callable): Listener to remove; an unknown one is ignored.
        """
        with self._event_listeners_lock:
            try:
                self._send_message_listeners.remove(listener)
            except ValueError:
                pass

    def add_file_listener(self, listener):
        """Register ``listener(client_id, full_path, name, size, command)`` per saved file.

        Fired after a file uploaded by a client (a direct send, or a forwarded
        file/folder item staged on the server) has been fully written to
        ``file_transfer_dir``.

        Args:
            listener (callable): ``listener(client_id, full_path, name, size,
                command)``; ``client_id`` is the uploader's ``"ip:port"`` and
                ``command`` the wire command that triggered the transfer, so a
                listener can recognise protocol pushes such as ``/crypto_pub_key``.
                It runs on the transfer thread, so it must not block.
        """
        with self._event_listeners_lock:
            if listener not in self._file_listeners:
                self._file_listeners.append(listener)

    def remove_file_listener(self, listener):
        """Unregister a listener previously added by `add_file_listener`.

        Args:
            listener (callable): Listener to remove; an unknown one is ignored.
        """
        with self._event_listeners_lock:
            try:
                self._file_listeners.remove(listener)
            except ValueError:
                pass

    def _notify_message_received(self, client_id, message):
        """Handle the operation.

        Args:
            client_id (object): Value used by the operation.
            message (object): Value used by the operation.
        """
        with self._event_listeners_lock:
            listeners = list(self._message_listeners)
        for listener in listeners:
            try:
                listener(client_id, message)
            except Exception as e:
                self._log(f"message listener error: {e}")
                self._log_exc()

    def _notify_transfer_chunk(self, client_socket, data):
        """Report one chunk read from a non-control socket to the message listeners.

        Side-channel sockets (file/folder transfers, forwards) carry raw bytes and
        ``sign`` tokens, which belong to the transfer layer, so they are reported
        to the listeners but never recorded in a store.
        """
        if not data or client_socket in self._control_sockets:
            return
        with self._event_listeners_lock:
            listeners = list(self._message_listeners)
        for listener in listeners:
            try:
                listener(self._socket_key(client_socket), data)
            except Exception as e:
                self._log(f"message listener error: {e}")
                self._log_exc()

    def _notify_message_sent(self, client_socket, message):
        """Report one written payload to the send listeners."""
        with self._event_listeners_lock:
            listeners = list(self._send_message_listeners)
        if not listeners:
            return
        target_id = self._socket_key(client_socket)
        for listener in listeners:
            try:
                listener(target_id, message)
            except Exception as e:
                self._log(f"send message listener error: {e}")
                self._log_exc()

    def _notify_file_received(self, client_id, full_path, name, size, command):
        """Handle the operation.

        Args:
            client_id (object): Value used by the operation.
            full_path (str): Value used by the operation.
            name (object): Value used by the operation.
            size (int): Value used by the operation.
            command (object): Value used by the operation.
        """
        with self._event_listeners_lock:
            listeners = list(self._file_listeners)
        for listener in listeners:
            try:
                listener(client_id, full_path, name, size, command)
            except Exception as e:
                self._log(f"file listener error: {e}")
                self._log_exc()

    def _socket_key(self, sock):
        """Serializable key for a sender socket (its peer address).

        Forwarded records already carry the originator's address as a
                string and are used as-is; a socket that cannot answer
                ``getpeername`` (already closed) falls back to its repr.

        Args:
            sock (object): Value used by the operation.

        Returns:
            object: Result produced by the operation.
        """
        if isinstance(sock, str):
            return sock
        try:
            ip, port = sock.getpeername()[:2]
            return f"{ip}:{port}"
        except Exception:
            return str(sock)

    def _remember_socket_key(self, sock):
        """Capture the log key of ``sock`` while it is known to be alive.

        A record stays buffered after its connection goes away, and the
                flush that persists it must still file it under the sender's
                address: by then the closed socket no longer answers
                ``getpeername`` and would be logged as ``str(sock)``. The key is
                forgotten once nothing is buffered under it any more (see
                ``_flush_dict_locked``).

        Args:
            sock (object): Value used by the operation.
        """
        with self._socket_keys_lock:
            if sock not in self._socket_keys:
                self._socket_keys[sock] = self._socket_key(sock)

    def _record_key(self, sock):
        """Return the JSON-log key for ``sock``: the address captured when it was.

        The JSON-log key for ``sock``: the address captured when it was

        recorded, or the live peer address for a socket inserted without a
                record.

        Args:
            sock (object): Value used by the operation.

        Returns:
            object: Result produced by the operation.
        """
        key = self._socket_keys.get(sock)
        if key is not None:
            return key
        return self._socket_key(sock)

    def _append_record(self, store, log_path, sock, content):
        """Append one ``[content, timestamp]`` pair under ``sock``; flush when full.

        Args:
            store (tuple): ``(dict, lock, size_attr)``; the dict maps socket to
                a list of pairs, and ``size_attr`` names the byte-size attribute.
            log_path (str): JSON log the store flushes into.
            sock (object): Key socket.
            content (str): Text to record.
        """
        d, lock, size_attr = store
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with lock:
            self._remember_socket_key(sock)
            d.setdefault(sock, []).append([content, timestamp])
            size = getattr(self, size_attr) + len(content.encode("utf-8", "replace"))
            size += len(timestamp)
            setattr(self, size_attr, size)
            if size >= self.max_dict_size:
                self._flush_dict_locked(d, size_attr, log_path)

    def _record_message(self, sock, content):
        """Store one inbound plain-text message under the sender's socket."""
        self._append_record(
            (self.messages_dict, self._messages_dict_lock, "_messages_dict_size"),
            self.messages_log_file, sock, content,
        )

    def _record_event(self, sock, command):
        """Store one inbound "/" command under the sender's socket."""
        self._append_record(
            (self.events_dict, self._events_dict_lock, "_events_dict_size"),
            self.events_log_file, sock, command,
        )

    def _record_sent(self, sock, content):
        """Store one outbound text: a "/" line as a sent event, other text as a sent message."""
        if content.startswith("/"):
            self._append_record(
                (self.sent_events_dict, self._sent_events_dict_lock, "_sent_events_dict_size"),
                self.sent_events_log_file, sock, content,
            )
        else:
            self._append_record(
                (
                    self.sent_messages_dict,
                    self._sent_messages_dict_lock,
                    "_sent_messages_dict_size",
                ),
                self.sent_messages_log_file, sock, content,
            )

    def _update_event_timestamp(self, sock, command, timestamp):
        """Stamp the completion time onto the recorded event for ``command``.

        File transfers finish on a worker thread after the receive thread
                recorded the command, so the event's timestamp is refreshed here with
                the moment the transfer actually completed.

        Args:
            sock (object): Value used by the operation.
            command (object): Value used by the operation.
            timestamp (object): Value used by the operation.
        """
        with self._events_dict_lock:
            entries = self.events_dict.get(sock)
            if entries:
                for entry in reversed(entries):
                    if entry[0] == command:
                        entry[1] = timestamp
                        return

    def _splice_event_command(self, command, **parts):
        """Return the command to record as an event.

        The wire command is recorded verbatim whenever it is available. When
                this end cannot see the original command (a transfer relayed by the
                server, or a protocol-internal control line), splice a readable
                command from the available parts so the event still identifies the
                transfer.

        Args:
            command (object): Value used by the operation.
            **parts (list | dict): Value used by the operation.

        Returns:
            object: Result produced by the operation.
        """
        if command:
            return command
        kind = parts.get("kind")
        fname = parts.get("fname")
        rel_dir = parts.get("rel_dir")
        if kind == "folder" and fname:
            if rel_dir:
                return "/file_folder {} {}".format(shlex.quote(rel_dir), shlex.quote(fname))
            return "/file_folder {}".format(shlex.quote(fname))
        if fname:
            return "/file {}".format(shlex.quote(fname))
        return parts.get("fallback") or "/unknown"

    def _flush_dict_locked(self, d, size_attr, path):
        """Flush ``d`` (socket -> [[content, ts], ...]) into its JSON log and

        clear it. The caller must hold the dict's lock.

        Args:
            d (list | dict): Value used by the operation.
            size_attr (object): Value used by the operation.
            path (str): Value used by the operation.
        """
        if not d:
            return
        snapshot = dict(d)
        d.clear()
        setattr(self, size_attr, 0)
        self._merge_json_log(path, snapshot)
        with self._socket_keys_lock:  # a key is only kept while some store buffers it
            for sock in snapshot:
                if not any(sock in store for store in self._record_dicts()):
                    self._socket_keys.pop(sock, None)

    def _record_dicts(self):
        """Return the four record stores, in flush order."""
        return (
            self.messages_dict,
            self.events_dict,
            self.sent_messages_dict,
            self.sent_events_dict,
        )

    def _flush_messages_dict(self):
        """Flush the inbound messages store into its JSON log."""
        with self._messages_dict_lock:
            self._flush_dict_locked(
                self.messages_dict, "_messages_dict_size", self.messages_log_file
            )

    def _flush_events_dict(self):
        """Flush the inbound events store into its JSON log."""
        with self._events_dict_lock:
            self._flush_dict_locked(self.events_dict, "_events_dict_size", self.events_log_file)

    def _flush_record_stores(self):
        """Flush every record store into its JSON log (called on stop)."""
        with self._messages_dict_lock:
            self._flush_dict_locked(
                self.messages_dict, "_messages_dict_size", self.messages_log_file
            )
        with self._events_dict_lock:
            self._flush_dict_locked(self.events_dict, "_events_dict_size", self.events_log_file)
        with self._sent_messages_dict_lock:
            self._flush_dict_locked(
                self.sent_messages_dict, "_sent_messages_dict_size", self.sent_messages_log_file
            )
        with self._sent_events_dict_lock:
            self._flush_dict_locked(
                self.sent_events_dict, "_sent_events_dict_size", self.sent_events_log_file
            )

    def _merge_json_log(self, path, snapshot):
        """Merge ``snapshot`` into the JSON log at ``path`` (append per socket).

        Publishes through `_publish_json_log`: a temp file named after this
                writer, and an optimistic re-merge when another instance published in
                between, so concurrent flushes of several instances sharing one log
                neither corrupt nor lose each other. A failure is logged, never raised
                into the caller.

        Args:
            path (str): Value used by the operation.
            snapshot (list | dict): Value used by the operation.
        """
        try:
            _publish_json_log(path, snapshot, self._record_key)
        except Exception as e:
            self._log(f"log file update failed: {e}")
            self._log_exc()

    def submit_task(self, func, *args, **kwargs):
        """Run a callable on the instance's worker pool.

        Args:
            func (callable): Callable to run.
            *args (Any): Positional arguments forwarded to ``func``.
            **kwargs (Any): Keyword arguments forwarded to ``func``.

        Returns:
            concurrent.futures.Future: Handle for the submitted call; its worker
                slot is released when the call finishes.
        """
        self._task_semaphore.acquire()
        future = self._custom_executor.submit(func, *args, **kwargs)
        future.add_done_callback(lambda f: self._task_semaphore.release())
        return future

    def create_temporary_server(self, handler, port=None, max_connections=1):
        """Start a temporary listener for a side channel (not the main protocol).

        Args:
            handler (callable): ``handler(client_socket, address)`` started in its
                own thread for every accepted connection.
            port (int | None): Port to bind; None allocates one with `palloc`.
            max_connections (int): Listen backlog. Defaults to 1.

        Returns:
            tuple: ``(port, thread, stop_event)``; setting ``stop_event`` ends the
                loop, which closes the socket and frees the port.

        Raises:
            RuntimeError: If ``port`` is None and no port can be allocated.
        """
        if port is None:
            port = self.palloc()
            if port is None:
                raise RuntimeError("No available port for temporary server")
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind((self.host, port))
        server_socket.listen(max_connections)
        stop_event = threading.Event()

        def server_loop():
            """Handle the operation."""
            while not stop_event.is_set():
                try:
                    server_socket.settimeout(1.0)
                    client_sock, addr = server_socket.accept()
                    threading.Thread(target=handler, args=(client_sock, addr), daemon=True).start()
                except socket.timeout:
                    continue
                except Exception as e:
                    if not stop_event.is_set():
                        self._log(f"Temporary server error: {e}")
                    break
            server_socket.close()
            self.pfree(port)

        server_thread = threading.Thread(target=server_loop, daemon=True)
        server_thread.start()
        return port, server_thread, stop_event

    def create_temporary_client(self, server_host, server_port, bind_port=None, on_data=None):
        """Open a temporary outbound connection for a side channel.

        Args:
            server_host (str): Host to connect to.
            server_port (int): Port to connect to.
            bind_port (int | None): Local port to bind; None lets the OS choose.
            on_data (callable | None): ``on_data(data, client_socket)`` called for
                every received chunk.

        Returns:
            tuple: ``(client_socket, thread, stop_event)``; setting ``stop_event``
                ends the receiver thread.
        """
        client_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if bind_port is not None:
            client_sock.bind((self.host, bind_port))
        client_sock.connect((server_host, server_port))
        stop_event = threading.Event()

        def receiver():
            """Handle the operation."""
            while not stop_event.is_set():
                try:
                    client_sock.settimeout(1.0)
                    data = self.receive_message(client_sock, self.recv_messages_chunk_len)
                    if not data:
                        break
                    if on_data:
                        on_data(data, client_sock)
                except socket.timeout:
                    continue
                except:
                    break
            client_sock.close()

        recv_thread = threading.Thread(target=receiver, daemon=True)
        recv_thread.start()
        return client_sock, recv_thread, stop_event

    def broadcast(
        self, message, exclude_client=None
    ):  # broadcast message to all clients except exclude_client
        """Send one message to every connected client.

        Clients whose send fails are disconnected and removed from ``clients``.

        Args:
            message (str | bytes): Payload passed to `send_message`.
            exclude_client (tuple | None): ``(ip, port)`` to leave out, typically
                the client the message came from.
        """
        with self.client_lock:
            disconnected_clients = []
            for addr, client_info in self.clients.items():
                if exclude_client and addr == exclude_client:
                    continue
                try:
                    self.send_message(client_info["socket"], message)
                except:
                    disconnected_clients.append(addr)
                    self._log_exc()
            for addr in disconnected_clients:  # del disconnected clients
                if addr in self.clients:
                    self._debug(f"deleting the disconnected client: {addr}")
                    self.clients[addr]["socket"].close()
                    del self.clients[addr]

    def send_msg_to_specific_client(
        self, message
    ):  # send message to specific client by client address
        """Send the messages of a console line to the clients named in it.

        Args:
            message (str): ``/send_msg`` line as typed: message text followed by one
                or more ``(ip, port)`` identifiers; each message is delivered to the
                identifiers that follow it. Addresses that are not connected are
                skipped with a console notice.
        """
        command_part = shlex.split(message)
        del command_part[0]
        client_message_pair_list = []
        client_list = []
        msg_list = []
        client_addr_times = 0
        for command_part_index in range(len(command_part)):
            part = command_part[command_part_index]
            if part.startswith("(") and part.endswith(")"):
                try:
                    client_addr = ast.literal_eval(part)
                    client_list.append(client_addr)
                    client_addr_times += 1
                    if command_part_index == len(command_part) - 1:
                        client_message_pair = [client_list, msg_list]
                        client_message_pair_list.append(client_message_pair)
                except:
                    self._log_exc()
                    self._log(
                        f"ErrorWhileParsingClientAddress: {part} is not a valid client address, skipped"
                    )
            else:
                if client_addr_times != 0:
                    client_message_pair = [client_list, msg_list]
                    client_message_pair_list.append(client_message_pair)
                    client_list = []
                    msg_list = []
                    msg_list.append(part)
                    client_addr_times = 0
                else:
                    msg_list.append(part)
        for each_client_message_pair in client_message_pair_list:
            for client_addr in each_client_message_pair[0]:
                for msg in each_client_message_pair[1]:
                    if client_addr in self.clients:
                        client_socket = self.clients[client_addr]["socket"]
                        self.send_message(client_socket, msg)
                    else:
                        self._log(f"Client {client_addr} not found, cannot send message: {msg}")

    def _crypto_get_send_lock(self, client_socket):
        """Per-socket lock serialising encrypt+send for one connection.

        A control socket's `_SecureChannel` shares this exact lock object, so
                a frame write can never interleave with a raw write on the same
                socket (and vice versa) during the flip.

        Args:
            client_socket (socket.socket): Value used by the operation.

        Returns:
            object: Result produced by the operation.
        """
        with self._crypto_lock:
            lock = self._crypto_send_locks.get(client_socket)
            if lock is None:
                lock = threading.Lock()
                self._crypto_send_locks[client_socket] = lock
            return lock

    def send_message(self, client_socket, message, is_send_raw=False):
        # send message to specific client
        """Write one message to a client socket, framing it when the channel is up.

        Args:
            client_socket (socket.socket): Target connection.
            message (str | bytes): Payload; a str is stripped and newline
                terminated, bytes are sent as they are.
            is_send_raw (bool): Write the payload as plaintext even when the channel
                is encrypted; only for the crypto handshake and transfer coordination lines.

        Returns:
            bool: True when the payload was written, False for an unsupported
                payload type.

        Raises:
            RuntimeError: If the server is not running or no socket was passed.
            OSError: If the socket write fails (the original error is re-raised).
        """
        if is_send_raw:
            deal_msg = message.strip()
            if not deal_msg.endswith("\n"):
                deal_msg += "\n"
            encoded = deal_msg.encode("utf-8")
            self._sendall(client_socket, encoded)
            self._measure_network_speed(encoded, is_upload=True)
            return True
        written = self._write_message(client_socket, message)
        if written:
            self._after_send(client_socket, message)
        return written

    def _write_message(self, client_socket, message):
        """Write one payload: encrypted when the channel is up, plaintext otherwise.

        Returns:
            bool: True when written, False for an unsupported payload type.
        """
        if not self.running or not client_socket:
            self._log("disable the connect to server")
            raise RuntimeError("connection error")
        with self._crypto_lock:
            channel = self._channels.get(client_socket)
        if channel is not None and channel.secure:
            try:
                if not isinstance(message, (str, bytes)):
                    self._log(f"Unsupported message type: {type(message)}")
                    return False
                payload = message
                if isinstance(message, str) and not message.strip().endswith("\n"):
                    payload = message.strip() + "\n"  # line framing for the control protocol
                channel.send(payload)
                self._measure_network_speed(payload, is_upload=True)
                return True
            except Exception as e:
                if not _is_closed_socket_error(e):
                    self._log(f"send msg error: {e}")
                    self._log_exc()
                raise
        with self._crypto_get_send_lock(client_socket):  # plaintext writes stay ordered per socket
            try:  # add newline character for server to distinguish messages
                if isinstance(message, str):
                    deal_msg = message.strip()
                    if not deal_msg.endswith("\n"):
                        deal_msg += "\n"
                    data = deal_msg.encode("utf-8")
                elif isinstance(message, bytes):
                    data = message
                else:
                    self._log(f"Unsupported message type: {type(message)}")
                    return False
                self._sendall(client_socket, data)
                self._measure_network_speed(data, is_upload=True)
                return True
            except Exception as e:
                if not _is_closed_socket_error(e):
                    self._log(f"send msg error: {e}")
                    self._log_exc()
                raise

    def _after_send(self, client_socket, message):
        """Record and report one payload that `_write_message` has written."""
        if isinstance(message, str):
            message = message.strip()
            if message and client_socket in self._control_sockets:
                self._record_sent(client_socket, message)
        self._notify_message_sent(client_socket, message)

    def _sendall(self, client_socket, data):
        """Write ``data`` in full, waiting for room on a non-blocking socket.

        Args:
            client_socket (socket.socket): Target connection.
            data (bytes): Payload to write in full.

        Raises:
        OSError: If the socket write fails or the peer closed the
                        connection.
                    TimeoutError: If a non-blocking socket stays unwritable for
                        ``SEND_WAIT_TIMEOUT`` seconds.
        """
        if not self.is_asynic_clients_io:  # blocking socket: a single sendall is enough
            client_socket.sendall(data)
            return
        view = memoryview(data)
        while view:
            try:
                sent = client_socket.send(view)
            except BlockingIOError:  # non-blocking socket with a full send buffer
                _wait_writable(client_socket, SEND_WAIT_TIMEOUT)
                continue
            if sent == 0:  # the peer closed the connection
                raise BrokenPipeError(errno.EPIPE, "socket closed while sending")
            view = view[sent:]

    def _crypto_mark_encrypted(self, client_socket, peer_role, peer_pem_path):
        """Handle the operation.

        Args:
            client_socket (socket.socket): Value used by the operation.
            peer_role (object): Value used by the operation.
            peer_pem_path (str): Value used by the operation.
        """
        with self._crypto_lock:
            self._encrypted_sockets.add(client_socket)
            self._crypto_peer[client_socket] = (peer_role, peer_pem_path)

    def _crypto_session_for(self, client_address):
        """Get or create the crypto session for a client address.

        The crypto session of one client address (created on demand).

        Args:
            client_address (tuple): Value used by the operation.

        Returns:
            object: Result produced by the operation.
        """
        with self._crypto_lock:
            session = self._crypto_sessions.get(client_address)
            if session is None and self.crypto is not None:
                session = _CryptoSession(self.crypto, "server", self._log, self._debug)
                self._crypto_sessions[client_address] = session
            return session

    def _crypto_control_state(self, client_socket):
        """Return (session, channel) of one control socket; (None, None) when unknown.

        (session, channel) of one control socket; (None, None) when unknown.

        Args:
            client_socket (socket.socket): Value used by the operation.

        Returns:
            list | tuple: Result produced by the operation.
        """
        with self._crypto_lock:
            addr = self._crypto_sock_addr.get(client_socket)
            session = self._crypto_sessions.get(addr) if addr is not None else None
            channel = self._channels.get(client_socket)
        return session, channel

    def _crypto_after_flip(self, client_socket):
        """Announce frame readiness (plaintext) and publish the session ECDH key.

        Args:
            client_socket (socket.socket): Value used by the operation.
        """
        _session, channel = self._crypto_control_state(client_socket)
        if channel is None:
            return
        try:
            # plaintext by design: the peer needs this line before it may frame
            self.send_message(client_socket, CRYPTO_FRAMES_START, is_send_raw=True)
        except Exception:
            self._log_exc()
        self._crypto_publish_ecdh_pub(client_socket)

    def _crypto_publish_ecdh_pub(self, client_socket):
        """Send our session ECDH public key once frames are allowed both ways.

        Args:
            client_socket (socket.socket): Value used by the operation.
        """
        session, channel = self._crypto_control_state(client_socket)
        if session is not None:
            session.publish_pub(channel)

    def _crypto_transfer_stamp(self, control_socket, force_plain=False):
        """Mode+nonce a listener announces to a transfer connector.

        Args:
            control_socket (socket.socket): Value used by the operation.
            force_plain (bool): Value used by the operation.

        Returns:
            object | str: Result produced by the operation.
        """
        session, channel = self._crypto_control_state(control_socket)
        if session is None:
            return "plain"
        return session.transfer_stamp(channel, force_plain)

    def _crypto_attach_transfer_in(self, sock, control_socket, stamp):
        """Framing channel for an accepted transfer socket (listener side).

        Args:
            sock (object): Value used by the operation.
            control_socket (socket.socket): Value used by the operation.
            stamp (object): Value used by the operation.

        Returns:
            NoneType | object: Result produced by the operation.
        """
        session, control_channel = self._crypto_control_state(control_socket)
        if session is None:
            return None
        channel = session.attach_transfer(sock, control_channel, stamp)
        if channel is not None:
            with self._crypto_lock:
                self._channels[sock] = channel
                self._encrypted_sockets.add(sock)
        return channel

    def _crypto_attach_transfer_out(self, sock, control_socket, stamp):
        """Framing channel for a connected transfer socket (connector side).

        Args:
            sock (object): Value used by the operation.
            control_socket (socket.socket): Value used by the operation.
            stamp (object): Value used by the operation.

        Returns:
            object: Result produced by the operation.
        """
        session, control_channel = self._crypto_control_state(control_socket)
        crypto = _connector_transfer_crypto(session, control_channel, stamp)
        channel = _enable_connector_channel(sock, crypto)
        if channel is not None:
            with self._crypto_lock:
                self._channels[sock] = channel
                self._encrypted_sockets.add(sock)
        return channel

    def _crypto_drop_channel(self, sock):
        """Forget a finished transfer socket's channel.

        Args:
            sock (object): Value used by the operation.
        """
        with self._crypto_lock:
            self._channels.pop(sock, None)
            self._encrypted_sockets.discard(sock)

    @staticmethod
    def _new_crypto_state(force=False):
        """Fresh per-connection crypto handshake state.

        Args:
            force (bool): Value used by the operation.

        Returns:
            dict: Result produced by the operation.
        """
        return {
            "force": force,
            "peer_nonce": None,
            "my_nonce": None,
            "peer_pub_event": threading.Event(),
            "peer_pub_ok": None,
            "peer_pem_path": None,
            "ready_sent": False,
            "peer_ready": False,
            "decode_failures": 0,
        }

    def receive_message(self, client_socket, msg_length):  # receive message
        """Read up to ``msg_length`` bytes from a client socket.

        A socket speaking frames (see `_SecureChannel`) returns decrypted
        plaintext, served from the channel's buffer; a socket without one is
        read raw. A read that started while the channel was raw is classified
        by the channel when it returns, so bytes of a flip that landed during
        the read never land in the line reader. Reads from a non-control socket
        are also reported to the message listeners (see `_notify_transfer_chunk`).

        Args:
            client_socket (socket.socket): Connection to read from.
            msg_length (int): Maximum number of bytes to read.

        Returns:
            bytes: Received bytes, empty when the peer closed the connection.
        """
        while True:
            with self._crypto_lock:
                channel = self._channels.get(client_socket)
            if channel is None:
                data = client_socket.recv(msg_length)
                self._measure_network_speed(data, is_upload=False)
                self._notify_transfer_chunk(client_socket, data)
                return data
            data = channel.take(msg_length)
            if data:
                self._measure_network_speed(data, is_upload=False)
                self._notify_transfer_chunk(client_socket, data)
                return data  # buffered plaintext is served even after a disable
            raw = client_socket.recv(self.recv_messages_chunk_len if channel.secure else msg_length)
            if not raw:
                return b""
            try:
                plain = channel.feed(raw)
            except _FrameError as exc:
                self._log(f"crypto: {exc}")
                self._crypto_channel_error(client_socket)
                leftover = channel.drain_plaintext()
                if leftover:
                    self._measure_network_speed(leftover, is_upload=False)
                    self._notify_transfer_chunk(client_socket, leftover)
                    return leftover  # hand back whatever was already read
                continue  # framing is off now: keep serving in raw mode
            if plain:
                self._measure_network_speed(plain, is_upload=False)
                self._notify_transfer_chunk(client_socket, plain)
                return plain  # plaintext mode: the line reader gets the bytes

    def _crypto_channel_error(self, client_socket):
        """Handle a frame decode failure on a control or transfer socket.

        A frame failed to decode: re-exchange on a control socket, close a

        transfer socket (its leg has no handshake to recover with).

        Args:
            client_socket (socket.socket): Value used by the operation.
        """
        with self._crypto_lock:
            is_control = client_socket in self._crypto_sock_addr
        if is_control:
            self._crypto_on_decode_failure(client_socket)
            return
        self._log("crypto: transfer frame error, closing the transfer connection")
        try:
            client_socket.close()
        except Exception:
            pass

    def _crypto_note_healthy(self, client_socket):
        """Reset the decode-failure count after a valid frame.

        A frame decoded cleanly: reset the streak that feeds the breaker.

        Args:
            client_socket (socket.socket): Value used by the operation.
        """
        with self._crypto_lock:
            addr = self._crypto_sock_addr.get(client_socket)
            state = self._crypto_state.get(addr) if addr is not None else None
            if state is not None:
                state["decode_failures"] = 0

    def _crypto_on_decode_failure(self, client_socket):
        """Decryption failed (stale/wrong key): drop encryption, reload

        our own key and ask the peer to re-exchange public keys.

                A circuit breaker closes the connection after
                ``MAX_DECODE_FAILURES`` consecutive failures on one connection,
                so a garbage-injection or persistent-mismatch storm cannot spin
                the re-exchange loop forever.

        Args:
            client_socket (socket.socket): Value used by the operation.
        """
        with self._crypto_lock:
            channel = self._channels.get(client_socket)
        if channel is not None:
            channel.disable()
        with self._crypto_lock:
            self._encrypted_sockets.discard(client_socket)
            self._crypto_peer.pop(client_socket, None)
            addr = self._crypto_sock_addr.get(client_socket)
            state = self._crypto_state.get(addr) if addr is not None else None
            if state is None:
                state = self._new_crypto_state(force=True)
                if addr is not None:
                    self._crypto_state[addr] = state
            state["decode_failures"] = state.get("decode_failures", 0) + 1
            if state["decode_failures"] >= MAX_DECODE_FAILURES:
                do_close = True
                do_reload = False
                do_exchange = False
            else:
                do_close = False
                do_reload = True
                do_exchange = True
                state["force"] = True
                state["my_nonce"] = self._crypto_fresh_nonce()
                state["peer_pub_event"] = threading.Event()
                state["peer_pub_ok"] = None
        if do_close:
            self._log("crypto: too many decode failures, closing connection")
            try:
                client_socket.close()
            except Exception:
                self._log_exc()
            return
        if do_reload and self.crypto is not None:
            try:
                self.crypto.reload_own_key()
            except Exception:
                self._log_exc()
        self._debug("crypto: decode failure, re-exchanging public keys")
        if do_exchange:
            if addr is not None:
                threading.Thread(
                    target=self._crypto_wait_server_ready,
                    args=(client_socket, addr, state),
                    daemon=True,
                ).start()
            try:
                with self._crypto_lock:
                    nonce = state.get("my_nonce") or self._crypto_fresh_nonce()
                self.send_message(
                    client_socket, f"/crypto_key_exchange {nonce} 1", is_send_raw=True
                )
            except Exception:
                self._log_exc()

    def _crypto_drop_secure(self, client_socket):
        """Framing stopped: keep the secure-socket set and peer map truthful.

        Args:
            client_socket (socket.socket): Value used by the operation.
        """
        with self._crypto_lock:
            self._encrypted_sockets.discard(client_socket)
            self._crypto_peer.pop(client_socket, None)

    @staticmethod
    def _crypto_fresh_nonce():
        """Handle the operation.

        Returns:
            object: Result produced by the operation.
        """
        return secrets.token_hex(16)

    def _crypto_on_client_hello(self, client_socket, client_address, client_nonce, force):
        """Server side of /crypto_key_exchange: record the client's

        session nonce, reply that we need its public key (every
                connection re-exchanges), then wait for it and announce
                readiness.

        Args:
            client_socket (socket.socket): Value used by the operation.
            client_address (tuple): Value used by the operation.
            client_nonce (object): Value used by the operation.
            force (bool): Value used by the operation.
        """
        self.crypto.ensure_keys()
        my_nonce = self._crypto_fresh_nonce()
        with self._crypto_lock:
            state = self._crypto_state.setdefault(client_address, self._new_crypto_state(force))
            state["force"] = force
            state["peer_nonce"] = client_nonce
            state["my_nonce"] = my_nonce
            state["peer_pub_event"] = threading.Event()
            state["peer_pub_ok"] = None
        session = self._crypto_sessions.get(client_address)
        if session is not None:
            session.reset_ecdh()  # a fresh handshake is a fresh session key
        self.send_message(
            client_socket,
            f"/crypto_key_exchange_ack {my_nonce} 1 {1 if force else 0}",
            is_send_raw=True,
        )
        threading.Thread(
            target=self._crypto_wait_server_ready,
            args=(client_socket, client_address, state),
            daemon=True,
        ).start()

    def _crypto_try_server_flip(self, client_socket, state):
        """Flip the socket to framed encryption once the peer key passed the

        TOFU check and both sides announced readiness (no plaintext/framed
                interleaving).

        Args:
            client_socket (socket.socket): Value used by the operation.
            state (object): Value used by the operation.
        """
        # no send may interleave with the flip
        with self._crypto_get_send_lock(client_socket):
            with self._crypto_lock:
                if not (
                    state.get("peer_pub_ok") is True
                    and state.get("peer_pem_path")
                    and state.get("ready_sent")
                    and state.get("peer_ready")
                    and client_socket not in self._encrypted_sockets
                ):
                    return
                self._crypto_mark_encrypted(client_socket, "client", state["peer_pem_path"])
                channel = self._channels.get(client_socket)
                my_nonce = state.get("my_nonce")
                peer_nonce = state.get("peer_nonce")
                peer_pem_path = state.get("peer_pem_path")
        if channel is not None:
            channel.enable(my_nonce, peer_nonce, peer_pem_path)
        threading.Thread(target=self._crypto_after_flip, args=(client_socket,), daemon=True).start()

    def _crypto_wait_server_ready(self, client_socket, client_address, state):
        """Wait until the client's public key arrived and passed the

        TOFU check, then announce readiness. The socket flips to
                encrypted mode only when the client's /crypto_ready arrives too.

        Args:
            client_socket (socket.socket): Value used by the operation.
            client_address (tuple): Value used by the operation.
            state (object): Value used by the operation.
        """
        with self._crypto_lock:
            peer_pub_event = state.get("peer_pub_event")
        if peer_pub_event is None or not peer_pub_event.wait(timeout=60):
            self._log("crypto: waiting for client public key timed out")
            return
        with self._crypto_lock:
            pub_ok = state.get("peer_pub_ok")
        if pub_ok is not True:
            self._log("crypto: client public key rejected, connection dropped")
            return
        with self._crypto_lock:
            state["ready_sent"] = True
        try:
            self.send_message(client_socket, "/crypto_ready", is_send_raw=True)
        except Exception:
            self._log_exc()
        self._crypto_try_server_flip(client_socket, state)

    def _crypto_push_pub_to_client(self, client_socket, client_address):
        """Push our public key file to the client via the file-transfer

        mechanism (server is the sender).

        Args:
            client_socket (socket.socket): Value used by the operation.
            client_address (tuple): Value used by the operation.

        Returns:
            bool: Result produced by the operation.
        """
        while True:  # serialise pushes: wait for an in-flight push instead of silently skipping it
            with self._crypto_lock:
                if client_socket not in self._crypto_push_active:
                    self._crypto_push_active.add(client_socket)
                    break
            time.sleep(0.05)
        try:
            with self.file_client_id_lock:
                client_id = copy.copy(self.file_client_id)
                self.file_client_id += 1
            send_msg = f"/crypto_pub_key {client_id}"
            self.send_message(client_socket, send_msg, is_send_raw=True)
            file_transfer_client_port = self.palloc()
            file_server_port = None
            waiting_time = 0
            while True:
                time.sleep(0.05)
                with self.file_transfer_server_port_lock:
                    for port_info in list(self.file_server_port_list):
                        if port_info[1] == client_id:
                            file_server_port = port_info[0]
                            self.file_server_port_list.remove(port_info)
                            break
                if file_server_port is not None:
                    break
                waiting_time += 1
                if waiting_time >= 200:
                    self._log("crypto: transfer port waiting timeout, public key push failed")
                    return False
            self.file_transfer_mode(  # a key push carries the keys themselves: always plaintext
                self.crypto.pub_path,
                client_address[0],
                file_server_port,
                file_transfer_client_port,
                control_socket=client_socket,
                crypto="plain",
            )
            self.pfree(file_transfer_client_port)
        except Exception:
            self._log_exc()
            return False
        finally:
            with self._crypto_lock:
                self._crypto_push_active.discard(client_socket)
        return True

    def _crypto_store_received_pub(self, full_path, peer_role, client_address):
        """TOFU-check a public key received via the file-transfer

        mechanism and cache it under the peer endpoint.

                The check runs ``verify_peer_pub`` against ``pub_key.json``; on
                failure the peer connection is rejected and dropped.

        Args:
            full_path (str): Value used by the operation.
            peer_role (object): Value used by the operation.
            client_address (tuple): Value used by the operation.
        """
        if self.crypto is None:
            return
        peer_ip, peer_port = client_address
        try:
            with open(full_path, "r", encoding="utf-8") as f:
                pem = f.read()
        except OSError:
            pem = ""
        ok, reason = self.crypto.verify_peer_pub(peer_ip, peer_port, pem, peer_role)
        with self._crypto_lock:
            state = self._crypto_state.get(client_address)
            if ok:
                self.crypto.store_peer_pem(peer_role, peer_ip, peer_port, full_path)
                if state is not None:
                    state["peer_pem_path"] = self.crypto.peer_pem_path(
                        peer_role, peer_ip, peer_port
                    )
                    state["peer_pub_ok"] = True
                    state["peer_pub_event"].set()
                self._log(f"crypto: accepted public key from {peer_ip}:{peer_port} ({reason})")
            else:
                if state is not None:
                    state["peer_pub_ok"] = False
                    state["peer_pub_event"].set()
                self._log(f"crypto: REJECTED public key from {peer_ip}:{peer_port}: {reason}")
                try:
                    os.remove(full_path)  # rejected key: do not leave it in received_files/
                except OSError:
                    pass
                peer_socket = self.clients.get(client_address, {}).get("socket")
        if not ok and peer_socket is not None:
            try:
                self.send_message(peer_socket, f"/crypto_reject {reason}", is_send_raw=True)
            except Exception:
                self._log_exc()
            try:
                peer_socket.close()
            except Exception:
                self._log_exc()

    def handle_client(self, client_socket, client_address):  # deal with each client
        """Serve one accepted client until it disconnects.

        Registers the client, greets it, announces the encryption mode and reads
        lines until the peer closes: commands go to `handle_command`, plain
        messages go to the message listeners and to ``messages_dict``. The client
        is removed from ``clients`` and its socket closed when the read loop ends
        for any reason.

        With ``is_asynic_clients_io`` False (the default) the call blocks and owns
        its thread; with it True the client is served by a coroutine on the
        server's event loop instead, and the call returns as soon as that
        coroutine is scheduled. A connection arriving while that mode is on but
        no event loop runs is closed with a console notice.

        Args:
            client_socket (socket.socket): Accepted connection.
            client_address (tuple): Peer ``(ip, port)``; used as the client id and
                as the key in ``clients``.
        """
        if self.is_asynic_clients_io:
            self._schedule_client_coroutine(client_socket, client_address)
            return
        client_id = self._register_client(client_socket, client_address)
        try:
            if not self._announce_client(client_socket, client_id):
                return
            buffer = ""
            while True:
                data = self.receive_message(  # get msg from client
                    client_socket, self.recv_messages_chunk_len
                )
                self._debug(data)
                if not data:
                    break
                buffer += data.decode("utf-8")
                while "\n" in buffer:  # deal with multiple messages in buffer
                    line, buffer = buffer.split("\n", 1)
                    message = line.strip()
                    if not message:
                        continue
                    response = self._process_client_line(
                        client_socket, client_address, client_id, message
                    )
                    if response:  # send response to client
                        self.send_message(client_socket, response)
        except ConnectionResetError:
            pass  # peer dropped with RST; the finally block reports the disconnect once
        except Exception as e:
            if not _is_closed_socket_error(e):
                self._log(f"error while deal with client {client_id} : {e}")
                self._log_exc()
        finally:
            self._unregister_client(client_socket, client_address, client_id)

    def _schedule_client_coroutine(self, client_socket, client_address):
        """Schedule one accepted client on the server's asyncio event loop.

        Args:
            client_socket (socket.socket): Accepted connection.
            client_address (tuple): Peer ``(ip, port)``.
        """
        loop = self._async_loop
        if loop is None or loop.is_closed():
            self._log(
                "asynic clients io is enabled but no event loop is running, closing the connection"
            )
            try:
                client_socket.close()
            except Exception:
                self._log_exc()
            return
        loop.call_soon_threadsafe(
            loop.create_task, self._handle_client_async(client_socket, client_address)
        )

    async def _async_receive(self, client_socket, loop):
        """Read up to ``recv_messages_chunk_len`` plaintext bytes from one accepted client.

        Mirrors `receive_message` for the asyncio path: framed bytes are
                decrypted off the event loop (RSA/ECDH are CPU-bound) and served from
                the channel's buffer; a socket without a channel is read raw. As in
                `receive_message`, the channel classifies what the read returned, so a
                flip that landed during the read never leaks frame bytes into the line
                reader.

        Args:
            client_socket (socket.socket): Value used by the operation.
            loop (object): Value used by the operation.

        Returns:
            bytes | object: Result produced by the operation.
        """
        while True:
            with self._crypto_lock:
                channel = self._channels.get(client_socket)
            if channel is None:
                data = await loop.sock_recv(client_socket, self.recv_messages_chunk_len)
                self._notify_transfer_chunk(client_socket, data)
                return data
            data = channel.take(self.recv_messages_chunk_len)
            if data:
                self._notify_transfer_chunk(client_socket, data)
                return data  # buffered plaintext is served even after a disable
            raw = await loop.sock_recv(client_socket, self.recv_messages_chunk_len)
            if not raw:
                return b""
            try:
                plain = await asyncio.to_thread(channel.feed, raw)
            except _FrameError as exc:
                self._log(f"crypto: {exc}")
                await asyncio.to_thread(self._crypto_channel_error, client_socket)
                leftover = channel.drain_plaintext()
                if leftover:
                    self._notify_transfer_chunk(client_socket, leftover)
                    return leftover
                continue  # framing is off now: keep serving in raw mode
            if plain:
                self._notify_transfer_chunk(client_socket, plain)
                return plain  # plaintext mode: the line reader gets the bytes

    async def _handle_client_async(self, client_socket, client_address):
        """Serve one accepted client until it disconnects, as a coroutine.

        The socket is read through the event loop while the line dispatch and
                every write run on worker threads, so a blocking command handler,
                listener or peer cannot stall the other connections.

        Args:
            client_socket (socket.socket): Value used by the operation.
            client_address (tuple): Value used by the operation.
        """
        loop = asyncio.get_running_loop()
        client_id = self._register_client(client_socket, client_address)
        try:
            if not await asyncio.to_thread(self._announce_client, client_socket, client_id):
                return
            buffer = ""
            while self.running:
                data = await self._async_receive(client_socket, loop)  # get msg from client
                self._debug(data)
                if not data:
                    break
                buffer += data.decode("utf-8", "replace")
                while "\n" in buffer:  # deal with multiple messages in buffer
                    line, buffer = buffer.split("\n", 1)
                    message = line.strip()
                    if not message:
                        continue
                    response = await asyncio.to_thread(
                        self._process_client_line,
                        client_socket,
                        client_address,
                        client_id,
                        message,
                    )
                    if response:  # send response to client
                        await asyncio.to_thread(self.send_message, client_socket, response)
        except ConnectionResetError:
            pass  # peer dropped with RST; the finally block reports the disconnect once
        except Exception as e:
            if not _is_closed_socket_error(e):
                self._log(f"error while deal with client {client_id} : {e}")
                self._log_exc()
        finally:
            try:
                # a cancelled read can leave its poll callback registered on the fd
                loop.remove_reader(client_socket.fileno())
            except Exception:
                pass
            self._unregister_client(client_socket, client_address, client_id)

    def _register_client(self, client_socket, client_address):
        """Add one accepted connection to ``clients``.

        Args:
            client_socket (socket.socket): Accepted connection.
            client_address (tuple): Peer ``(ip, port)``.

        Returns:
        str: Client id, ``"<ip>:<port>"``.
        """
        client_id = f"{client_address[0]}:{client_address[1]}"
        with self.client_lock:  # add new client
            self._control_sockets.add(client_socket)
            self.clients[client_address] = {
                "socket": client_socket,
                "address": client_address,
                "id": client_id,
                "connected_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        if self.is_enable_encrypto and self.crypto is not None:
            session = self._crypto_session_for(client_address)
            channel = _SecureChannel(
                client_socket,
                session,
                self._log,
                self._debug,
                send_lock=self._crypto_get_send_lock(client_socket),
                sendall=lambda data, sock=client_socket: self._sendall(sock, data),
                on_disable=lambda: self._crypto_drop_secure(client_socket),
            )
            with self._crypto_lock:
                self._crypto_sock_addr[client_socket] = client_address
                self._channels[client_socket] = channel
        self._debug(f"new connection: {client_id}")
        self._debug(f"connection count mount: {len(self.clients)}")
        # a peer that comes back carries interrupted transfers: continue them
        # (and wake the forwarded-resume requests that waited for it) off the
        # accept path, because the resume may need this peer's handshake first
        threading.Thread(
            target=self._resume_after_connect, args=(client_address,), daemon=True
        ).start()
        return client_id

    def _announce_client(self, client_socket, client_id):
        """Greet one client and announce the encryption mode and port range.

        Args:
            client_socket (socket.socket): Connection to greet.
            client_id (str): Client id used in the console log.

        Returns:
        bool: True when the peer was greeted, False when it was already gone
                        (the caller closes the connection either way).
        """
        welcome_msg = f"Welcome!: {client_id}\n"  # send welcome message
        try:
            self.send_message(client_socket, welcome_msg)
        except Exception as e:
            # the server is stopping (or the peer vanished): the caller's
            # cleanup closes the connection; never let this escape
            if not _is_closed_socket_error(e):
                self._log(f"error while welcoming client {client_id} : {e}")
            return False
        # announce our encryption mode; a mismatched peer is disconnected in handle_command
        try:
            self.send_message(
                client_socket,
                f"/crypto_mode {1 if self.is_enable_encrypto else 0}",
                is_send_raw=True,
            )
        except Exception as e:
            # the peer vanished right after the welcome: the caller's cleanup
            # closes the connection; never let this escape
            if not _is_closed_socket_error(e):
                self._log(f"error while announcing crypto mode to {client_id} : {e}")
            return False
        if self.is_hand_alloc_port == True:
            port_alloc_range_msg = "/client_alloc_port_range {}".format(self.each_client_port_range)
        else:
            port_alloc_range_msg = "/client_alloc_port_range NO_LIMIT"
        # the range is fixed for the server's lifetime, so only the connection
        # that just joined needs it (announcing it to every client is O(N) per
        # accepted connection)
        try:
            self.send_message(client_socket, port_alloc_range_msg)
        except Exception as e:
            # the peer vanished right after the welcome: the caller's cleanup
            # closes the connection; never let this escape
            if not _is_closed_socket_error(e):
                self._log(f"error while announcing the port range to {client_id} : {e}")
            return False
        self._debug(self.clients)
        return True

    def _process_client_line(self, client_socket, client_address, client_id, message):
        """Report, log and dispatch one complete (already decrypted) line from a client.

        Args:
            client_socket (socket.socket): Connection the line came from.
            client_address (tuple): Peer ``(ip, port)``.
            client_id (str): Client id used in the console log.
            message (str): One line with its newline removed.

        Returns:
        str | None: Response for that client, or None when none is due.
        """
        if not _crypto_is_protocol_line(message):
            # genuine peer traffic: reset the streak that feeds the breaker
            self._crypto_note_healthy(client_socket)
        self._log(message)
        # every inbound line is an event for the message listeners (commands
        # included); dispatch below is unaffected
        self._notify_message_received(client_id, message)
        if message.startswith("/"):  # deal with special command
            self._record_event(client_socket, message)
            return self.handle_command(client_socket, client_address, message)
        self._record_message(client_socket, message)
        timestamp = datetime.now().strftime("%H:%M:%S")  # deal with normal message
        log_msg = f"[{timestamp}] {client_id}: {message}"
        self._log(log_msg)
        return f"msg send: {message}"

    def _unregister_client(self, client_socket, client_address, client_id):
        """Drop one client's state and close its socket.

        Args:
            client_socket (socket.socket): Connection to close.
            client_address (tuple): Peer ``(ip, port)``.
            client_id (str): Client id used in the console log.
        """
        with self.client_lock:
            if client_address in self.clients:
                del self.clients[client_address]
        with self._crypto_lock:
            self._crypto_state.pop(client_address, None)
            self._crypto_sessions.pop(client_address, None)
            self._crypto_sock_addr.pop(client_socket, None)
            self._crypto_peer.pop(client_socket, None)
            self._crypto_push_active.discard(client_socket)
            self._encrypted_sockets.discard(client_socket)
            self._control_sockets.discard(client_socket)
            self._channels.pop(client_socket, None)
            self._crypto_send_locks.pop(client_socket, None)
        client_socket.close()
        # the transfers that died with this connection are written to the
        # progress file so a reconnect can continue them (_request_resumes)
        self._persist_transfer_progress()
        self._debug(f"client disconnected: {client_id}")
        self._debug(f"current connection count: {len(self.clients)}")

    def handle_command(
        self, client_socket, client_address, command
    ):  # deal with special commands from client
        """Dispatch one command line received from a client.

        Built-in commands (``/help``, ``/time``, ``/clients``, ``/quit``,
        ``/crypto_mode``, ``/file``, ``/file_folder``,
        ``/server_file_transfer_port`` and the crypto exchange lines) are handled
        here; any other name goes to the handlers registered for the "server" side
        via `register_command`. An encryption-mode mismatch and ``/quit`` close the
        connection; an unknown command is only reported on the console.

        Args:
            client_socket (socket.socket): Connection the line came from.
            client_address (tuple): Peer ``(ip, port)``.
            command (str): Line including its leading ``/``.

        Returns:
            str | None: Response for that client, or None when no response is due
                (crypto lines, file transfers, ``/quit``, which answers on the
                socket before shutting it down, and custom handlers that run in the
                background).
        """
        self._debug(client_socket, client_address, command)
        client_id = f"{client_address[0]}:{client_address[1]}"
        send_str = None
        if command == "/help":
            help_text = [
                "avalable commands:",
                "/help - print help meg\n",
                "\t/time - display server time\n",
                "\t/clients - display connected clients\n",
                "\t/file <file_path> [destination_file_path] - send file to server\n",
                "\t/multiple_file <file1> <file2> ... [destination_file_path] ",
                "- send multiple files to server\n",
                "\t/file_folder <folder_path> [destination_file_path] - send folder to server\n",
                "\t/multiple_file_folder <folder1> <folder2> ... [destination_file_path] ",
                "- send multiple folders to server\n",
                "\t/quit - disconnect",
            ]
            send_str = " ".join(help_text) + "\n"
            return send_str
        elif command == "/time":
            send_str = f"server time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}" + "\n"
            return send_str
        elif command == "/clients":
            with self.client_lock:
                client_list = [info["id"] for info in self.clients.values()]
                send_str = f"online clients ({len(client_list)}): {', '.join(client_list)}" + "\n"
                return send_str
        elif command == "/quit":
            # the goodbye is written here (not returned) so the connection can
            # be shut down right after it: a client that stays connected after
            # /quit must not keep the server reading from the session
            try:
                self.send_message(client_socket, "Bye!\n")
            except Exception as e:
                if not _is_closed_socket_error(e):
                    self._log(f"error while sending goodbye to {client_id} : {e}")
            try:
                client_socket.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass  # already gone or not connected any more
            return None
        elif command.lower().split(" ")[0] == "/crypto_mode":
            try:
                client_crypto = int(command.split(" ")[1])
            except (IndexError, ValueError):
                client_crypto = -1
            if client_crypto != (1 if self.is_enable_encrypto else 0):
                self._log(
                    f"crypto: encryption mode mismatch with client {client_id} "
                    f"(client={client_crypto}, server={1 if self.is_enable_encrypto else 0}), "
                    f"disconnecting"
                )
                try:
                    client_socket.close()
                except Exception:
                    self._log_exc()
            return None
        elif shlex.split(command.lower())[0] == "/file":
            self.file_transfer_server_recv_server_start_thread(client_id, client_socket, command)
        elif shlex.split(command.lower())[0] == "/file_folder":
            self.file_folder_transfer_server_recv_server_start_thread(
                command, client_id, client_socket
            )
        elif shlex.split(command.lower())[0] == "/server_file_transfer_port":
            parts = command.split(" ")
            with self.file_transfer_server_port_lock:
                self.file_transfer_server_port = int(parts[1])
                try:
                    file_client_id = int(parts[2])
                    stamp = parts[3] if len(parts) > 3 else "plain"  # listener mode+nonce
                    self.file_server_port_list.append(
                        [self.file_transfer_server_port, file_client_id, stamp]
                    )
                except:
                    self._log_exc()
                    pass
        elif shlex.split(command.lower())[0] == "/forward_item":
            threading.Thread(
                target=self._forward_item_handler,
                args=(client_socket, client_address, command),
                daemon=True,
            ).start()
            return None
        elif shlex.split(command.lower())[0] == "/forward_send_msg":
            # Internal relay request typed on a client console (client-only
            # command, same name on the wire): push the messages to the
            # listed destinations.
            threading.Thread(
                target=self._handle_forward_send_msg,
                args=(client_socket, client_address, command),
                daemon=True,
            ).start()
            return "Command received, processing in background.\n"
        elif shlex.split(command.lower())[0] == "/pause_trans":
            self._handle_pause_command(client_address, command, True)
            return None
        elif shlex.split(command.lower())[0] == "/start_trans":
            self._handle_pause_command(client_address, command, False)
            return None
        elif shlex.split(command.lower())[0] == RESUME_LINE:
            parsed = parse_resume_command(command)
            if parsed is not None:
                threading.Thread(
                    target=self._resume_request,
                    args=(client_address, parsed[0], parsed[1]),
                    daemon=True,
                ).start()
            return None
        elif (
            self.is_enable_encrypto
            and self.crypto is not None
            and command.lower().split(" ")[0] == "/crypto_key_exchange"
        ):
            parts = command.split(" ")
            client_nonce = parts[1] if len(parts) > 1 else ""
            force = len(parts) > 2 and parts[2] == "1"
            self._crypto_on_client_hello(client_socket, client_address, client_nonce, force)
            return None
        elif (
            self.is_enable_encrypto
            and self.crypto is not None
            and command.lower().split(" ")[0] == "/crypto_pub_key"
        ):
            with self._crypto_lock:  # only accept pushes from a connection that started the handshake (else the TOFU registry could be poisoned)
                handshaking = client_address in self._crypto_state
            if not handshaking:
                self._log(f"crypto: ignoring /crypto_pub_key from non-handshaking peer {client_id}")
                return None
            self.file_transfer_server_recv_server_start_thread(client_id, client_socket, command)
            return None
        elif (
            self.is_enable_encrypto
            and self.crypto is not None
            and command.lower().split(" ")[0] == "/crypto_pub_key_request"
        ):
            threading.Thread(  # client asks the server to push its public key
                target=self._crypto_push_pub_to_client,
                args=(client_socket, client_address),
                daemon=True,
            ).start()
            return None
        elif (
            self.is_enable_encrypto
            and self.crypto is not None
            and command.lower().split(" ")[0] == "/crypto_key_exchange_ack"
        ):
            parts = command.split(" ")
            if len(parts) < 3:
                return None
            client_nonce = parts[1]
            client_needs_server_pub = parts[2]
            force = len(parts) > 3 and parts[3] == "1"
            with self._crypto_lock:
                state = self._crypto_state.get(client_address)
                if state is not None:
                    state["peer_nonce"] = (
                        client_nonce  # reply to a server-initiated hello: fresh session nonce from the client's ack
                    )
            if force or client_needs_server_pub == "1":
                threading.Thread(
                    target=self._crypto_push_pub_to_client,
                    args=(client_socket, client_address),
                    daemon=True,
                ).start()
            return None
        elif (
            self.is_enable_encrypto
            and self.crypto is not None
            and command.lower().split(" ")[0] == "/crypto_ready"
        ):
            with self._crypto_lock:
                state = self._crypto_state.setdefault(
                    client_address, self._new_crypto_state(force=False)
                )
                state["peer_ready"] = True
            self._crypto_try_server_flip(client_socket, state)
            return None
        elif (
            self.is_enable_encrypto
            and self.crypto is not None
            and command.lower().split(" ")[0] == "/crypto_reject"
        ):
            reason = command[
                len("/crypto_reject") :
            ].strip()  # the peer rejected our public key (TOFU mismatch)
            self._log(f"crypto: connection rejected by peer: {reason}")
            try:
                client_socket.close()
            except Exception:
                self._log_exc()
            return None
        elif (
            self.is_enable_encrypto
            and self.crypto is not None
            and command.lower().split(" ")[0] == CRYPTO_FRAMES_START
        ):
            _session, channel = self._crypto_control_state(client_socket)
            if channel is not None:
                channel.note_peer_frames_ready()
                self._crypto_publish_ecdh_pub(client_socket)
            return None
        elif (
            self.is_enable_encrypto
            and self.crypto is not None
            and command.lower().split(" ")[0] == CRYPTO_ECDH_PUB
        ):
            # only ever accepted inside an RSA frame (see _PLAINTEXT_LINE_PREFIXES)
            parts = command.split(" ", 1)
            session = self._crypto_sessions.get(client_address)
            if session is not None and len(parts) > 1:
                if not session.set_peer_ecdh_b64(parts[1]):
                    self._log(f"crypto: rejected the ECDH key of {client_id}")
            return None
        else:
            cmd_parts = shlex.split(command.strip())
            if not cmd_parts:
                return None
            cmd_name = cmd_parts[0].lower()
            if cmd_name in self._custom_handlers[0]:
                handler = self._custom_handlers[0][cmd_name]
                run_in_thread = self._custom_handler_threaded[0].get(cmd_name, False)
                if run_in_thread:
                    self.submit_task(
                        self._execute_custom_handler,
                        handler,
                        command,
                        client_socket,
                        client_address,
                    )
                    return "Command received, processing in background.\n"
                else:
                    response = self._execute_custom_handler(
                        handler, command, client_socket, client_address
                    )
                    return response
            else:
                self._log(f"Unknown command: {command}")

    def _handle_forward_send_msg(self, sock, addr, cmd):
        """Relay plain messages to every reachable destination client.

        Server side of the client-only ``/forward_send_msg`` command (typed on
                a client console and relayed here over the wire). Every message is
                wrapped in a ``/send_msg_from <addr> <payload>`` envelope carrying the
                originator's address so receivers can attribute it. The messages are
                recorded under the originator's socket (``sock``), exactly as if the
                originator had sent them to the server. Destinations that are
                unreachable -- or the server itself, which is never in the client
                table -- are skipped and the rest are still served.

        Args:
            sock (object): Value used by the operation.
            addr (tuple): Value used by the operation.
            cmd (object): Value used by the operation.

        Returns:
            NoneType: Result produced by the operation.
        """
        parts = shlex.split(cmd)
        items, addrs = parse_forward_items_and_addrs(parts[1:])
        reached = False
        for target in addrs:
            for msg in items:
                if self.forward_message_to(target, msg, addr):
                    reached = True
        if reached:
            for msg in items:
                self._record_message(sock, msg)
        return None

    def forward_message_to(self, target, message, originator_addr):
        """Send one plain message to a connected target, tagged with its origin.

        Public API for forward extensions: the message is wrapped in a
        ``/send_msg_from <addr> <payload>`` envelope so the receiver can attribute
        it to the originator (see `parse_forwarded_message`).

        Args:
            target (tuple): Destination ``(ip, port)``.
            message (str): Payload to deliver.
            originator_addr (tuple): Originating client ``(ip, port)``.

        Returns:
            bool: False when ``target`` is not connected (a console notice is
                printed); True when the envelope was sent.
        """
        client_info = self.clients.get(target)
        if client_info is None:
            self._log(forward_skip_message(target))
            return False
        self.send_message(
            client_info["socket"],
            f"/send_msg_from {shlex.quote(repr(originator_addr))} {shlex.quote(message)}",
        )
        return True

    def forward_target_command(
        self, kind, rel_dir, fname, originator_addr, tfid, destination_path=None
    ):
        """Build the tagged wire command that pushes one forwarded item.

        Public API for forward extensions. The originator tuple sits before the
        trailing transfer id, where the receiver's existing parsers ignore it and
        `parse_forward_originator` recovers it for attribution.

        Args:
            kind (str): "file" or "file_folder".
            rel_dir (str): Relative folder path (folders only).
            fname (str): File or folder name.
            originator_addr (tuple): Originating client ``(ip, port)``.
            tfid (int): Transfer id shared by the pushed item.
            destination_path (str | None): Receiver-side destination directory.

        Returns:
            str: Command line to hand to `send_message`.
        """
        originator = shlex.quote(repr(originator_addr))
        if kind == "file":
            if destination_path:
                return (
                    f"/file {shlex.quote(fname)} {originator} "
                    f"{shlex.quote(destination_path)} {tfid}"
                )
            return f"/file {shlex.quote(fname)} {originator} {tfid}"
        if destination_path:
            return (
                f"/file_folder {shlex.quote(rel_dir)} {shlex.quote(fname)} "
                f"{originator} {shlex.quote(destination_path)} {tfid}"
            )
        return f"/file_folder {shlex.quote(rel_dir)} {shlex.quote(fname)} {originator} {tfid}"

    def forward_item_to(
        self, target, kind, rel_dir, fname, originator_addr, tfid, destination_path=None
    ):
        """Push one forwarded file or folder item to a connected target.

        Public API for forward extensions: sends the line built by
        `forward_target_command`, which the receiver attributes with
        `parse_forward_originator`.

        Args:
            target (tuple): Destination ``(ip, port)``.
            kind (str): "file" or "file_folder".
            rel_dir (str): Relative folder path (folders only).
            fname (str): File or folder name.
            originator_addr (tuple): Originating client ``(ip, port)``.
            tfid (int): Transfer id shared by the pushed item.
            destination_path (str | None): Receiver-side destination directory.

        Returns:
            bool: False when ``target`` is not connected (a console notice is
                printed); True when the command was sent.
        """
        client_info = self.clients.get(target)
        if client_info is None:
            self._log(forward_skip_message(target))
            return False
        self.send_message(
            client_info["socket"],
            self.forward_target_command(
                kind, rel_dir, fname, originator_addr, tfid, destination_path
            ),
        )
        return True

    def _execute_custom_handler(self, handler, command, client_socket=None, client_address=None):
        """Handle the operation.

        Args:
            handler (callable): Value used by the operation.
            command (object): Value used by the operation.
            client_socket (socket.socket): Value used by the operation.
            client_address (tuple): Value used by the operation.

        Returns:
            NoneType | object: Result produced by the operation.
        """
        try:
            result = handler(client_socket, client_address, command)
            if result is not None:
                if isinstance(result, str) and not result.endswith("\n"):
                    result += "\n"
                try:
                    self.send_message(client_socket, result)
                except Exception as e:
                    self._log(f"Error sending message: {e}")
                return result
            return None
        except Exception as e:
            error_msg = f"Error in custom command handler: {e}\n"
            self._log_exc()
            try:
                self.send_message(client_socket, error_msg)
            except Exception as e:
                self._log(f"Error sending error message: {e}")
            return error_msg

    def file_folder_transfer_server_recv_server_start_thread(  # start a file folder server thread on server
        self, command, client_id, client_socket
    ):
        """Start a server-side folder receive operation.

        Args:
            command (object): Value used by the operation.
            client_id (object): Value used by the operation.
            client_socket (socket.socket): Value used by the operation.
        """
        command_part = shlex.split(command)
        relative_folder_path = command_part[1]
        # >= 4 tokens means a file transfer (/file_folder <rel> <file> [<dest>] <id>);
        # 2-3 tokens is folder creation (optional destination, no client id)
        file_name = command_part[2] if len(command_part) >= 4 else None
        folder_transfer_server_recv_server_start_thread = threading.Thread(
            target=self.file_transfer_server_recv_server_start,
            args=(client_id, client_socket, command, relative_folder_path, file_name),
            daemon=True,
        )
        folder_transfer_server_recv_server_start_thread.start()

    def file_transfer_server_recv_server_start_thread(  # start a file server thread on server
        self, client_id, client_socket, command
    ):
        """Start a server-side file receive operation.

        Args:
            client_id (object): Value used by the operation.
            client_socket (socket.socket): Value used by the operation.
            command (object): Value used by the operation.
        """
        file_transfer_server_recv_server_start_thread = threading.Thread(
            target=self.file_transfer_server_recv_server_start,
            args=(client_id, client_socket, command),
            daemon=True,
        )
        file_transfer_server_recv_server_start_thread.start()

    def file_transfer_server_recv_server_start(  # deal with file transfer request server on server from client and receive file from client
        self, client_id, client_socket, command, new_save_path=None, file_name=None
    ):
        """Receive a file sent by a connected client.

        Args:
            client_id (object): Value used by the operation.
            client_socket (socket.socket): Value used by the operation.
            command (object): Value used by the operation.
            new_save_path (str): Value used by the operation.
            file_name (str): Value used by the operation.
        """
        file_transfer_server_port = self.palloc()
        self.file_transfer_mode_recv(
            self.host,
            file_transfer_server_port,
            client_socket,
            client_id,
            new_save_path,
            file_name,
            command,
            peer=_peer_from_text(client_id),  # the sender a resume is asked to continue from
        )
        self.pfree(file_transfer_server_port)

    def file_transfer_mode_recv(
        self,
        server_file_address,
        server_file_port,
        client_socket,
        client_id,
        new_save_path,
        file_name,
        command,
        peer=None,
    ):
        """Receive one file over a transfer sub-connection.

        Args:
            server_file_address (str): Local address for the transfer listener.
            server_file_port (int): Local port for the transfer listener.
            client_socket (socket.socket): Sender's control socket.
            client_id (str): Sender identifier in ``"ip:port"`` form.
            new_save_path (str | None): Relative directory for folder contents.
            file_name (str | None): Destination filename, when specified.
            command (str): Wire command that initiated the transfer.
            peer (tuple | None): Original sender ``(ip, port)`` for resuming.

        Returns:
            None: after the leg starts or finishes.
        """
        file_running = True
        client_file_socket = None
        server_file_socket = None
        save_path = None

        def close_socket():
            """Handle the operation."""
            nonlocal file_running
            nonlocal client_file_socket
            nonlocal server_file_socket
            file_running = False
            self._crypto_drop_channel(client_file_socket)
            client_file_socket.close()
            server_file_socket.close()

        def setting_file_save_path():
            """Handle the operation."""
            nonlocal save_path
            save_path = self.file_transfer_dir
            if new_save_path or destination_path:
                if destination_path:
                    if os.path.isabs(destination_path):
                        save_path = destination_path
                    else:
                        save_path = os.path.join(save_path, destination_path)
                if new_save_path:
                    path_list = new_save_path.split("/")
                    if path_list and path_list[0] == "":
                        del path_list[0]
                    for node in path_list:
                        save_path = os.path.join(save_path, node)
                os.makedirs(save_path, exist_ok=True)
            if file_name or new_save_path == None:
                return save_path
            close_socket()
            return None

        def file_transfer_client_recv(client_id):
            """Handle the operation.

            Args:
                client_id (object): Value used by the operation.
            """
            nonlocal file_running
            nonlocal client_file_socket
            nonlocal server_file_socket
            nonlocal save_path
            filename = None
            full_path = None
            progress = None  # this leg's file_recv_progress entry, when tracked
            finished_ok = False
            try:
                self.send_message(client_file_socket, self.server_start_file_transfer_sign)
            except Exception as e:
                # the instance stopped (or the peer vanished) between the accept
                # and this start sign: the transfer is over. A closing instance
                # raises RuntimeError("connection error") by design — the normal
                # teardown signal — and it must never kill this thread (pytest
                # reports any escape as an unhandled thread exception).
                if not _is_closed_socket_error(e):
                    self._log(f"error while starting the file transfer: {e}")
                    self._log_exc()
                close_socket()
                return False
            try:
                name_len_bytes = b""
                while len(name_len_bytes) < 4:
                    chunk = self.receive_message(client_file_socket, 4 - len(name_len_bytes))
                    self._debug(chunk)
                    if not chunk:
                        try:
                            self.send_message(client_file_socket, self.error_sign)
                        except Exception:
                            pass  # send_message already logged real errors; a dead peer is expected
                        close_socket()
                        raise ConnectionError(
                            "ErrorWhileReceivingFileNameLength: client disconnected"
                        )
                    name_len_bytes += chunk
                    if name_len_bytes.strip() == self.error_sign.encode("utf-8"):
                        close_socket()
                        raise ConnectionError(
                            "ErrorSignReceivedWhileReceivingFileNameLength: client reported error and disconnected"
                        )
                name_len = int.from_bytes(name_len_bytes, "big")
                file_name_encoded = b""
                while len(file_name_encoded) < name_len:
                    chunk = self.receive_message(
                        client_file_socket, name_len - len(file_name_encoded)
                    )
                    if not chunk:
                        try:
                            self.send_message(client_file_socket, self.error_sign)
                        except Exception:
                            pass  # send_message already logged real errors; a dead peer is expected
                        close_socket()
                        raise ConnectionError("ErrorWhileReceivingFileName: client disconnected")
                    file_name_encoded += chunk
                    if file_name_encoded.strip() == self.error_sign.encode("utf-8"):
                        close_socket()
                        raise ConnectionError(
                            "ErrorSignReceivedWhileReceivingFileName: client reported error and disconnected"
                        )
                filename = file_name_encoded.decode("utf-8")
                filename = filename.strip()
                filename = os.path.basename(filename)
                size_bytes = b""
                while len(size_bytes) < 8:
                    chunk = self.receive_message(client_file_socket, 8 - len(size_bytes))
                    if not chunk:
                        try:
                            self.send_message(client_file_socket, self.error_sign)
                        except Exception:
                            pass  # send_message already logged real errors; a dead peer is expected
                        close_socket()
                        raise ConnectionError("ErrorWhileReceivingFileSize: client disconnected")
                    size_bytes += chunk
                    if size_bytes.strip() == self.error_sign.encode("utf-8"):
                        close_socket()
                        raise ConnectionError(
                            "ErrorSignReceivedWhileReceivingFileSize: client reported error and disconnected"
                        )
                file_size = int.from_bytes(size_bytes, "big")
                if resume_entry is not None and resume_entry[1] != file_size:
                    raise ConnectionError(
                        f"resume rejected: {filename} is {file_size} bytes, "
                        f"the partial file expects {resume_entry[1]}"
                    )
                self.send_message(client_file_socket, self.server_received_file_header_sign)
                original_filename = filename
                if resume_entry is not None:
                    # continued transfer: the receiver's partial file keeps the
                    # bytes that are already on disk and the sender continues
                    full_path = resume_entry[3]
                    final_filename = os.path.basename(full_path)
                    resume_from = resume_entry[0]  # the offset the sender was asked for
                    if resume_from <= 0 or resume_from > file_size:
                        resume_from = 0  # not this file's offset: start it over
                else:
                    if file_name:
                        final_filename = file_name.strip()
                    elif command_part[0] == "/crypto_pub_key":
                        # concurrent key pushes share received_files/: a fixed name would be
                        # overwritten/moved by another connection before this one consumes it
                        final_filename = "{}.pem".format(uuid.uuid4().hex)
                    else:
                        final_filename = os.path.basename(original_filename)
                    full_path = os.path.join(save_path, final_filename).strip()
                    resume_from = 0
                os.makedirs(os.path.dirname(full_path), exist_ok=True)
                progress = self._file_progress_start(
                    self.file_recv_progress,
                    progress_fid,
                    file_size,
                    full_path,
                    peer=peer,
                    initial=resume_from,
                )
                with _open_transfer_target(full_path, resume_from) as f:
                    remaining = file_size - resume_from
                    while remaining > 0:
                        chunk = self.receive_message(
                            client_file_socket, min(self.file_transfer_chunk_len, remaining)
                        )
                        if not chunk:
                            try:
                                self.send_message(client_file_socket, self.error_sign)
                            except:
                                self._log_exc()
                                pass
                            close_socket()
                            raise ConnectionError(
                                "ErrorWhileReceivingFileData: client disconnected"
                            )
                        f.write(chunk)
                        remaining -= len(chunk)
                        self._file_progress_update(progress, len(chunk), is_upload=False)
                    f.truncate()  # a longer partial file drops its stale tail
                finished_ok = True
                # TOFU first: the ack is only a notification, and a failed
                # ack send must not skip the key registration (the server
                # would never announce readiness and the handshake hangs)
                self._notify_file_received(client_id, full_path, final_filename, file_size, command)
                self._update_event_timestamp(
                    client_socket,
                    self._splice_event_command(command, fname=final_filename),
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                )
                self._log(f"file {filename} received from {client_id}, size {file_size} bytes")
                if command_part[0] == "/crypto_pub_key":
                    with self._crypto_lock:
                        peer_addr = self._crypto_sock_addr.get(client_socket)
                    if peer_addr:
                        self._crypto_store_received_pub(full_path, "client", peer_addr)
                    else:
                        # the peer mapping is already gone (connection torn
                        # down mid-handshake): store nothing, leave nothing
                        try:
                            os.remove(full_path)
                        except OSError:
                            pass
                try:
                    self.send_message(client_file_socket, self.server_received_file_data_sign)
                except Exception:
                    self._log_exc()
                close_socket()
            except Exception as e:
                self._log_exc()
                if full_path is not None and os.path.exists(full_path) and progress is None:
                    try:
                        os.remove(full_path)  # untracked partial transfer: nothing can resume it
                    except OSError:
                        pass
                try:
                    self.send_message(client_file_socket, self.error_sign)
                except Exception:
                    pass  # send_message already logged real errors; a dead peer is expected
                close_socket()
                self._log(f"ErrorWhileReceiveFile: {e}")
                return False
            else:
                close_socket()
                return None
            finally:
                self._file_progress_finish(
                    self.file_recv_progress,
                    progress_fid,
                    progress,
                    keep=(
                        not finished_ok
                        and progress is not None
                        and progress[4] is not None  # resumable: it has a peer
                    ),
                )
                self._release_transfer_socket(progress_fid, client_file_socket)

        server_file_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_file_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_file_socket.bind((server_file_address, server_file_port))
        file_transfer_server_port = server_file_socket.getsockname()[1]
        command_part = shlex.split(command)
        file_client_id = command_part[len(command_part) - 1]
        progress_fid = _transfer_fid(command_part)  # keys this leg in file_recv_progress
        resume_entry = _resumable_entry(self.file_recv_progress, progress_fid, peer, command_part)
        destination_path = _parse_destination_path(command_part)
        stamp = self._crypto_transfer_stamp(
            client_socket, force_plain=command_part[0] == "/crypto_pub_key"
        )
        self._forget_transfer_port(progress_fid)  # an older session's entry is stale
        transfer_server_port_msg = "/server_file_transfer_port {} {} {}\n".format(
            file_transfer_server_port, file_client_id, stamp
        )
        self.send_message(
            client_socket, transfer_server_port_msg, is_send_raw=True
        )  # coordination line: always plaintext (like the handshake)
        server_file_socket.listen(1)
        if resume_entry is not None:
            # the sender answers this resume with /file; its connection is the continued leg
            server_file_socket.settimeout(RESUME_ACCEPT_TIMEOUT)
        try:
            client_file_socket, client_file_address = server_file_socket.accept()
            if resume_entry is not None:
                client_file_socket.settimeout(None)  # the accept timeout is the listener's only
            self._crypto_attach_transfer_in(client_file_socket, client_socket, stamp)
            self._register_transfer_socket(progress_fid, client_file_socket)
            if resume_entry is not None:
                threading.Thread(
                    target=file_transfer_client_recv, args=(client_id,), daemon=True
                ).start()
            else:
                is_open_file_transfer = setting_file_save_path()
                if is_open_file_transfer == None:
                    pass
                else:
                    threading.Thread(
                        target=file_transfer_client_recv, args=(client_id,), daemon=True
                    ).start()
        except socket.timeout:
            self._log(
                f"resume: {client_id} did not continue transfer {progress_fid}, "
                f"it stays in the progress stores for the next reconnect"
            )
        except Exception as e:
            self._log(f"\nget file transfer msg error: {e}")
            self._log_exc()
            close_socket()
        finally:
            server_file_socket.close()

    def diff_multiple_file_diff_multiple_client_transfer_server_recv_client_start(self, message):
        """Send grouped files and folders to their listed clients.

        Args:
            message (object): Value used by the operation.
        """
        command_part = shlex.split(message)
        del command_part[0]
        # optional destination directory is the last argument, after the
        # client addresses (which are always tuples)
        destination_path = None
        if command_part and not (
            command_part[-1].startswith("(") and command_part[-1].endswith(")")
        ):
            destination_path = command_part[-1]
            del command_part[-1]
        file_client_pair_list = []
        file_list = []
        client_list = []
        command_part_addr_times = 0
        for command_part_index in range(len(command_part)):
            part = command_part[command_part_index]
            if part.startswith("(") and part.endswith(")"):
                try:
                    client_addr = ast.literal_eval(part)
                    client_list.append(client_addr)
                    command_part_addr_times += 1
                    if command_part_index == len(command_part) - 1:
                        file_client_pair = [client_list, file_list]
                        file_client_pair_list.append(file_client_pair)
                except:
                    self._log_exc()
            else:
                if command_part_addr_times != 0:
                    file_client_pair = [client_list, file_list]
                    file_client_pair_list.append(file_client_pair)
                    file_list = []
                    client_list = []
                    file_list.append(part)
                    command_part_addr_times = 0
                else:
                    file_list.append(part)
        for each_file_client_pair in file_client_pair_list:
            file_transfer_command_message = ""
            file_folder_transfer_command_message = ""
            for file in each_file_client_pair[1]:
                try:
                    if os.path.isfile(file):
                        if file_transfer_command_message == "":
                            file_transfer_command_message = "/file {}".format(shlex.quote(file))
                        else:
                            file_transfer_command_message += " {}".format(shlex.quote(file))
                    elif os.path.isdir(file):
                        if file_folder_transfer_command_message == "":
                            file_folder_transfer_command_message = "/file_folder {}".format(
                                shlex.quote(file)
                            )
                        else:
                            file_folder_transfer_command_message += " {}".format(shlex.quote(file))
                except:
                    self._log_exc()
                    self._log(
                        f"ErrorWhileParsingFilePath: {file} is not a valid file or folder path, skipped"
                    )
                    pass
            for client_addr in each_file_client_pair[0]:
                if file_transfer_command_message != "":
                    file_transfer_command_message += " {}".format(shlex.quote(str(client_addr)))
                if file_folder_transfer_command_message != "":
                    file_folder_transfer_command_message += " {}".format(
                        shlex.quote(str(client_addr))
                    )
            if destination_path:
                if file_transfer_command_message != "":
                    file_transfer_command_message += " {}".format(shlex.quote(destination_path))
                if file_folder_transfer_command_message != "":
                    file_folder_transfer_command_message += " {}".format(
                        shlex.quote(destination_path)
                    )
            if file_transfer_command_message != "":
                self.multiple_file_multiple_client_transfer_server_recv_client_start(
                    file_transfer_command_message
                )
            if file_folder_transfer_command_message != "":
                self.multiple_file_multiple_client_transfer_server_recv_client_start(
                    file_folder_transfer_command_message
                )

    def multiple_file_multiple_client_transfer_server_recv_client_start(self, message):
        """Send requested files or folders to each listed client.

        Args:
            message (object): Value used by the operation.
        """
        command_part = shlex.split(message)
        command_type = command_part[0]
        del command_part[0]
        # optional destination directory is the last argument, after the
        # client addresses (which are always tuples)
        destination_path = None
        if command_part and not (
            command_part[-1].startswith("(") and command_part[-1].endswith(")")
        ):
            destination_path = command_part[-1]
            del command_part[-1]
        transfer_file_list = []
        client_addr_list = []
        for part in command_part:
            if part.startswith("(") and part.endswith(")"):
                try:
                    client_addr = ast.literal_eval(part)
                    client_addr_list.append(client_addr)
                except:
                    self._log_exc()
            else:
                transfer_file_list.append(part)
        for client_addr in client_addr_list:
            for transfer_file in transfer_file_list:
                if command_type == "/multiple_file_multiple_client":
                    # console form: classify each item by its own type
                    if os.path.isfile(transfer_file):
                        item_type = "/file"
                    elif os.path.isdir(transfer_file):
                        item_type = "/file_folder"
                    else:
                        self._log(
                            f"ErrorWhileParsingFilePath: {transfer_file} is not "
                            "a valid file or folder path, skipped"
                        )
                        continue
                else:
                    item_type = command_type
                if item_type == "/file":
                    file_transfer_command_message = "/file {} {}".format(
                        shlex.quote(transfer_file), shlex.quote(str(client_addr))
                    )
                    if destination_path:
                        file_transfer_command_message += " {}".format(shlex.quote(destination_path))
                    self.file_transfer_server_recv_client_start_thread(
                        file_transfer_command_message
                    )
                    self._log(f"start to send file command: {file_transfer_command_message}")
                elif item_type == "/file_folder":
                    folder_transfer_command_message = "/file_folder {} {}".format(
                        shlex.quote(transfer_file), shlex.quote(str(client_addr))
                    )
                    if destination_path:
                        folder_transfer_command_message += " {}".format(
                            shlex.quote(destination_path)
                        )
                    self.folder_file_transfer_server_recv_client_start(
                        folder_transfer_command_message
                    )
                    self._log(f"start to send folder command: {folder_transfer_command_message}")

    def folder_file_transfer_server_recv_client_start(self, message):
        """Send a folder's contents to one connected client.

        Args:
            message (object): Value used by the operation.
        """
        command_part = shlex.split(message)
        folder_path = command_part[1]
        if command_part[-1].startswith("(") and command_part[-1].endswith(")"):
            destination_path = None
            client_addr = ast.literal_eval(command_part[-1])
        else:
            destination_path = command_part[-1]
            client_addr = ast.literal_eval(command_part[-2])
        client_socket = self.clients[client_addr]["socket"]
        if os.path.isdir(folder_path) == False:
            self._log(f"{folder_path} is not a valid folder path")
            return False
        base_path = os.path.dirname(folder_path)

        def get_relative_path(base_path, abs_path):
            """Handle the operation.

            Args:
                base_path (str): Value used by the operation.
                abs_path (str): Value used by the operation.
            """
            base = os.path.normpath(base_path)
            abs_ = os.path.normpath(abs_path)
            common = os.path.commonpath([base, abs_])
            if common != base:
                raise ValueError(f"'{abs_path}' is not a subpath of '{base_path}'")
            rel = os.path.relpath(abs_, base)
            if rel == ".":
                return ""
            rel = rel.replace(os.sep, "/")
            return "/" + rel

        def send_folder_transfer_command(folder_path, file_name=None, abspath=None):
            """Handle the operation.

            Args:
                folder_path (str): Value used by the operation.
                file_name (str): Value used by the operation.
                abspath (str): Value used by the operation.
            """
            folder_transfer_command_message = "/file_folder {}".format(shlex.quote(folder_path))
            if file_name:
                each_file_transfer_command_message = "/file_folder {} {} {}".format(
                    shlex.quote(folder_path), shlex.quote(file_name), shlex.quote(str(client_addr))
                )
                if destination_path:
                    each_file_transfer_command_message += " {}".format(
                        shlex.quote(destination_path)
                    )
                self.file_transfer_server_recv_client_start_thread(
                    each_file_transfer_command_message, abspath
                )
                self._log(f"start to send folder command: {each_file_transfer_command_message}")
            else:
                if destination_path:
                    folder_transfer_command_message += " {}".format(shlex.quote(destination_path))
                self.send_message(client_socket, folder_transfer_command_message.strip())
                self._log(f"start to send folder command: {folder_transfer_command_message}")

        def start_file_transfer_with_limit(rel_dir, file, root):
            """Handle the operation.

            Args:
                rel_dir (str): Value used by the operation.
                file (str): Value used by the operation.
                root (object): Value used by the operation.
            """
            cmd = "/file_folder {} {} {}".format(
                shlex.quote(rel_dir), shlex.quote(file), shlex.quote(str(client_addr))
            )
            if destination_path:
                cmd += " {}".format(shlex.quote(destination_path))

            def limited_transfer():
                """Handle the operation."""
                self.file_semaphore.acquire()
                try:
                    self.file_transfer_server_recv_client_start(cmd, root)
                finally:
                    self.file_semaphore.release()

            thread = threading.Thread(target=limited_transfer, daemon=True)
            thread.start()
            self._log(f"start to send file: {cmd} (limit {self.max_file_transfer_thread_num})")

        def get_all_files_in_folder():
            """Handle the operation."""
            for root, dirs, files in os.walk(folder_path):
                rel_dir = get_relative_path(base_path, root)
                if root != folder_path:
                    send_folder_transfer_command(rel_dir)
                for file in files:
                    start_file_transfer_with_limit(rel_dir, file, root)
            self._log(f"finished sending all files in folder {folder_path}")

        transfer_path = get_relative_path(base_path, folder_path)
        send_folder_transfer_command(transfer_path)
        get_all_files_in_folder()

    def file_transfer_server_recv_client_start_thread(self, message, file_folder_abspath=None):
        """Start a thread to send a file or folder item to a client.

        Args:
            message (object): Value used by the operation.
            file_folder_abspath (str): Value used by the operation.
        """
        file_transfer_server_recv_client_start_thread = threading.Thread(
            target=self.file_transfer_server_recv_client_start,
            args=(message, file_folder_abspath),
            daemon=True,
        )
        file_transfer_server_recv_client_start_thread.start()

    def file_transfer_server_recv_client_start(self, message, file_folder_abspath):
        """Send a file or folder item to the addressed connected client.

        Args:
            message (object): Value used by the operation.
            file_folder_abspath (str): Value used by the operation.
        """
        client_id = None
        command_part = shlex.split(message)
        # optional destination directory comes after the client address tuple
        if command_part[-1].startswith("(") and command_part[-1].endswith(")"):
            client_ip = ast.literal_eval(command_part[-1])
        else:
            client_ip = ast.literal_eval(command_part[-2])
        client_address = client_ip[0]
        try:
            client_socket = self.clients[client_ip]["socket"]
        except:
            self._log(
                "ErrorWhileSearchingClientSocket: can not find the client socket, file sending failed"
            )
            self._log_exc()
            return False
        self._debug(client_socket, client_address, message)
        with self.file_client_id_lock:
            client_id = copy.copy(self.file_client_id)
            send_msg = message.strip() + " " + str(self.file_client_id) + "\n"
            self.file_client_id += 1
        try:
            waiting_time = 0
            if shlex.split(message.lower())[0] == "/file_folder":
                filename = os.path.join(file_folder_abspath, shlex.split(message)[2])
            else:
                filename = shlex.split(message)[1]
            self.send_message(client_socket, send_msg)
            file_transfer_client_port = self.palloc()
            file_server_port = None
            stamp = "plain"
            is_find_port = True
            while is_find_port:
                time.sleep(1)
                if len(self.file_server_port_list) > 0:
                    with self.file_transfer_server_port_lock:
                        for port_info in self.file_server_port_list:
                            if port_info[1] == client_id:
                                file_server_port = port_info[0]
                                stamp = port_info[2]  # the listener's mode+nonce
                                self.file_server_port_list.remove(port_info)
                                is_find_port = False
                                break
                    pass
                waiting_time += 1
                if waiting_time >= 20:
                    self._log(
                        "ErrorWhileReceiveFileServerPort: transfer port waiting timeout, file sending failed"
                    )
                    return False
            self.file_transfer_mode(
                filename,
                client_address,
                file_server_port,
                file_transfer_client_port,
                control_socket=client_socket,
                crypto=stamp,
                fid=client_id,
                peer=client_ip,  # the receiver a resume is asked to continue from
            )
            self.pfree(file_transfer_client_port)
        except IndexError:
            self._log("invalid command, please use '/file <filename>'")
            self._log_exc()

    def file_transfer_mode(  # noqa: PLR0911 - peer-close and timeout exits are distinct outcomes
        self,
        filename,
        server_address,
        server_port,
        client_port,
        control_socket=None,
        crypto=None,
        fid=None,
        peer=None,
        resume_offset=0,
    ):
        """Send one file over a transfer sub-connection.

        Args:
            filename (str): Source file path.
            server_address (str): Transfer listener address.
            server_port (int): Transfer listener port.
            client_port (int): Local port to bind for the transfer.
            control_socket (socket.socket | None): Control socket carrying the mode stamp.
            crypto (str | None): ``"plain"`` or encrypted transfer stamp.
            fid (int | None): Transfer ID used for progress and pause tracking.
            peer (tuple | None): Original receiver ``(ip, port)`` for resuming.
            resume_offset (int): Byte offset to resume from; defaults to zero.

        Returns:
            bool: True when the receiver acknowledges the file; otherwise False.
        """
        self._log(f"start to send file: {filename}")
        client_file_socket = None
        reset_time = 0

        def close_socket():
            """Handle the operation."""
            nonlocal file_running
            nonlocal client_file_socket
            file_running = False
            self._crypto_drop_channel(client_file_socket)
            client_file_socket.close()
            with self.file_client_id_lock:
                self.file_client_id -= 1

        while True:
            try:
                client_file_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                client_file_socket.bind((self.host, client_port))
                client_file_socket.connect((server_address, server_port))
                break
            except Exception as e:
                self._log(f"file transfer connection error: {e}")
                self._log_exc()
                if reset_time >= 20:
                    close_socket()
                    self._log("unable to connect to file transfer server, file sending failed")
                    return False
                reset_time += 1
                time.sleep(1)
        try:
            if crypto and crypto != "plain":
                self._crypto_attach_transfer_out(client_file_socket, control_socket, crypto)
        except _CryptoUnavailable as exc:
            self._log(f"crypto: file transfer aborted: {exc}")
            close_socket()
            return False
        file_running = True
        self._register_transfer_socket(fid, client_file_socket)
        file_receive_data_from_server = ""

        def receive_file_transfer_messages():
            """Handle the operation."""
            nonlocal file_running
            nonlocal client_file_socket
            nonlocal file_receive_data_from_server
            while file_running:
                try:
                    data = self.receive_message(client_file_socket, self.recv_messages_chunk_len)
                    if not data:
                        self._log("\nbreak the file transfer connection from server")
                        try:
                            self.send_message(client_file_socket, self.error_sign)
                        except Exception:
                            pass  # send_message already logged real errors; a dead peer is expected
                        close_socket()
                        break
                    file_receive_data_from_server = data.decode("utf-8").strip()
                    if file_receive_data_from_server == self.error_sign:
                        self._log(
                            "\nError sign received from server, file transfer may have failed"
                        )
                        close_socket()
                        break
                except Exception as e:
                    self._log(f"\nget file transfer msg error: {e}")
                    if not _is_closed_socket_error(e):
                        self._log_exc()
                    try:
                        self.send_message(client_file_socket, self.error_sign)
                    except Exception:
                        pass  # send_message already logged real errors; a dead peer is expected
                    close_socket()
                    break

        receive_thread = threading.Thread(target=receive_file_transfer_messages, daemon=True)
        receive_thread.start()
        waiting_time = 0
        progress = None  # this leg's file_upload_progress entry, when tracked
        file_sent = False  # True once the receiver acknowledged the whole file
        source_missing = False  # True when the source file no longer exists
        try:
            while True:
                if file_receive_data_from_server == self.server_start_file_transfer_sign:
                    break
                if file_receive_data_from_server == self.error_sign:
                    close_socket()
                    break
                if not file_running:
                    # peer closed the file connection: the receive thread
                    # already cleaned up; nothing more to wait for
                    return False
                time.sleep(1)
                waiting_time += 1
                if waiting_time >= 10:
                    try:
                        self.send_message(client_file_socket, self.error_sign)
                    except Exception:
                        pass  # send_message already logged real errors; a dead peer is expected
                    self._log(
                        f"ErrorWhileSendFile: \
                          Wait file transfer function start sign timeout, \
                          file {filename} sending failed"
                    )
                    close_socket()
                    return False
            waiting_time = 0
            file_size = os.path.getsize(filename)
            file_name_encoded = os.path.basename(filename).encode("utf-8")
            name_len = len(file_name_encoded)
            self.send_message(client_file_socket, name_len.to_bytes(4, "big"))
            self.send_message(client_file_socket, file_name_encoded)
            self.send_message(client_file_socket, file_size.to_bytes(8, "big"))
            if resume_offset < 0 or resume_offset > file_size:
                resume_offset = 0  # not this file's offset any more: send it whole
            progress = self._file_progress_start(
                self.file_upload_progress,
                fid,
                file_size,
                filename,
                peer=peer,
                initial=resume_offset,
            )
            with open(filename, "rb") as f:
                if resume_offset:
                    f.seek(resume_offset)
                while True:
                    self._wait_transfer_paused(fid)
                    file_data = f.read(self.file_transfer_chunk_len)
                    if not file_data:
                        break
                    self.send_message(client_file_socket, file_data)
                    self._file_progress_update(progress, len(file_data), is_upload=True)
            extra_time = (file_size // (100 * 1024 * 1024)) * 10
            timeout = int(30 + extra_time)
            while True:
                if file_receive_data_from_server == self.server_received_file_data_sign:
                    break
                if file_receive_data_from_server == self.error_sign:
                    close_socket()
                    break
                if not file_running:
                    # peer closed the file connection: the receive thread
                    # already cleaned up; nothing more to wait for
                    return False
                time.sleep(1)
                waiting_time += 1
                if waiting_time >= timeout:
                    try:
                        self.send_message(client_file_socket, self.error_sign)
                    except Exception:
                        pass  # send_message already logged real errors; a dead peer is expected
                    close_socket()
                    self._log(
                        f"ErrorWhileSendFileData: \
                          wait file transfer confirmation sign timeout, \
                          file {filename} sending may have failed"
                    )
                    return False
            file_sent = True
            self._log(f"Success: file {filename} sent successfully")
            close_socket()
            return True
        except FileNotFoundError:
            # the source is gone: this transfer cannot be resumed later
            source_missing = True
            self._log_exc()
            try:
                self.send_message(client_file_socket, self.error_sign)
            except Exception:
                pass  # send_message already logged real errors; a dead peer is expected
            close_socket()
            self._log(f"file {filename} not exist")
            return False
        except Exception as e:
            if not _is_closed_socket_error(e):
                self._log_exc()
            try:
                self.send_message(client_file_socket, self.error_sign)
            except Exception:
                pass  # send_message already logged real errors; a dead peer is expected
            close_socket()
            self._log(f"send error: {e}")
            return False
        finally:
            self._file_progress_finish(
                self.file_upload_progress,
                fid,
                progress,
                keep=(
                    not file_sent
                    and not source_missing
                    and progress is not None
                    and progress[4] is not None  # resumable: it has a peer
                ),
            )
            self._release_transfer_socket(fid, client_file_socket)
            with self._transfer_pause_cond:
                self._transfer_pause.pop(fid, None)

    # ---- transfer reconnect (server side) ----------------------------------

    def _request_resumes(self, peer):
        """Ask a peer that just connected to continue this side's interrupted downloads.

        Reads the persisted stores back (`_load_transfer_progress`). Every download
        this instance was receiving from ``peer`` gets its offset set to the bytes
        already on disk, and ``/resume_trans <fid> <offset>`` is sent; the sender
        answers with ``/file <name> <fid>`` and the continued leg is received by
        `file_transfer_mode_recv`. Uploads need no action here.

        Args:
            peer (tuple): Peer ``(ip, port)`` that just connected.

        Returns:
            int: Number of resume requests sent.
        """
        restored = self._load_transfer_progress()
        if restored:
            self._log(f"transfer reconnect: restored {restored} interrupted transfer(s)")
        peer = tuple(peer)
        with self.client_lock:
            info = self.clients.get(peer)
        if info is None or not self._wait_control_secure(info["socket"]):
            return 0
        started = 0
        for fid, entry in list(self.file_recv_progress.items()):
            if entry[4] is None or tuple(entry[4]) != peer:
                continue  # not a download from this peer
            with self._forward_relays_lock:
                if fid in self._forward_resume_records:
                    continue  # a forwarded transfer: its target drives the resume
            with self._transfer_progress_lock:
                if fid in self._active_transfer_fids:
                    continue  # a leg is already running for this fid
            entry[0] = _resume_offset_for(entry)
            if self.send_message(info["socket"], f"{RESUME_LINE} {fid} {entry[0]}"):
                started += 1
        return started

    def resume_trans(self, peer_addr, fid, offset):
        """Continue one interrupted transfer this instance was sending.

        Runs when a receiver asks to continue a transfer (``/resume_trans``,
        see `_request_resumes` on the receiving side): ``/file <name> <fid>``
        is sent so the receiver opens a listener, and the source file is read
        from the byte offset the receiver already holds. Also the public API
        for resuming one transfer on demand.

        Args:
            peer_addr (tuple | None): Receiver ``(ip, port)`` the request came
                from; None skips the peer check.
            fid (int): Transfer id of the interrupted transfer.
            offset (int): Byte offset the receiver already holds.

        Returns:
            bool: True when the resume leg was started.
        """
        entry = self.file_upload_progress.get(fid)
        if entry is None:
            self._log(f"resume: no outgoing transfer with fid {fid} to continue")
            return False
        if peer_addr is not None and entry[4] is not None and tuple(entry[4]) != tuple(peer_addr):
            self._log(f"resume: transfer {fid} is for {entry[4]}, not {peer_addr}, ignored")
            return False
        with self.client_lock:
            info = self.clients.get(tuple(peer_addr)) if peer_addr is not None else None
        if info is None:
            self._log(f"resume: {peer_addr} is not connected, transfer {fid} kept")
            return False
        self._forget_transfer_port(fid)  # drop a dead session's stale announcement
        if not self.send_message(
            info["socket"], f"/file {shlex.quote(os.path.basename(str(entry[3])))} {fid}"
        ):
            self._log(f"resume: cannot send /file for transfer {fid} to {peer_addr}")
            return False
        port_info = self._wait_transfer_server_port(fid)
        if port_info is None:
            self._log(f"resume: {peer_addr} did not announce a transfer port for {fid}")
            return False
        port, stamp = port_info
        def run_resume_send_leg():
            client_port = self.palloc()
            try:
                self.file_transfer_mode(
                    entry[3],
                    peer_addr[0],
                    port,
                    client_port,
                    control_socket=info["socket"],
                    crypto=stamp,
                    fid=fid,
                    peer=tuple(peer_addr),
                    resume_offset=offset,
                )
            finally:
                self.pfree(client_port)

        threading.Thread(target=run_resume_send_leg, daemon=True).start()
        return True


    def _resume_request(self, client_address, fid, offset):
        """Route one client's ``/resume_trans`` to the transfer it names.

        The fid identifies either this server's own outbound leg to that
                client, or a target leg of a forward relay — the relay record then
                names the originator, which is asked to re-forward to this client.

        Args:
            client_address (tuple): Value used by the operation.
            fid (int): Value used by the operation.
            offset (int): Value used by the operation.

        Returns:
            object: Result produced by the operation.
        """
        with self._forward_relays_lock:
            record = self._forward_resume_records.get(fid)
        if record is not None:
            originator = record.get("originator")
            targets = [tuple(target) for target in record.get("targets") or []]
            if (
                originator is not None
                and tuple(client_address) in targets
                and tuple(originator) != tuple(client_address)
            ):
                return self._route_forward_resume(record, client_address, offset)
        return self.resume_trans(client_address, fid, offset)

    def _route_forward_resume(self, record, target_addr, offset):
        """Ask a forwarded transfer's originator to continue it for one target.

        The server only relays: the record of the dead relay names the
                originating client, which re-drives the item to this target from the
                offset the target already holds (``/forward_resume``). An originator
                that is not back yet gets the request as soon as it reconnects.

        Args:
            record (object): Value used by the operation.
            target_addr (object): Value used by the operation.
            offset (int): Value used by the operation.

        Returns:
            bool | object: Result produced by the operation.
        """
        originator = record.get("originator")
        originator_fid = record.get("fid")
        if originator is None or originator_fid is None:
            return False
        request = (
            f"{FORWARD_RESUME_LINE} {shlex.quote(repr(tuple(target_addr)))} "
            f"{originator_fid} {offset}"
        )
        with self.client_lock:
            info = self.clients.get(tuple(originator))
        if info is None:
            with self._forward_relays_lock:
                self._pending_relay_resumes.setdefault(tuple(originator), []).append(
                    (originator_fid, tuple(target_addr), offset)
                )
            self._log(
                f"resume: originator {tuple(originator)} is not connected, "
                f"request for {tuple(target_addr)} kept"
            )
            return False
        self._log(
            f"resume: asking {tuple(originator)} to continue forwarded transfer "
            f"{originator_fid} for {tuple(target_addr)}"
        )
        return self.send_message(info["socket"], request)

    def _flush_pending_relay_resumes(self, client_address):
        """Send the forwarded-resume requests that waited for this originator.

        Args:
            client_address (tuple): Value used by the operation.
        """
        with self._forward_relays_lock:
            pending = self._pending_relay_resumes.pop(tuple(client_address), None)
        if not pending:
            return
        with self.client_lock:
            info = self.clients.get(tuple(client_address))
        if info is None:
            return
        for fid, target, offset in pending:
            self.send_message(
                info["socket"],
                f"{FORWARD_RESUME_LINE} {shlex.quote(repr(tuple(target)))} {fid} {offset}",
            )

    def _resume_after_connect(self, client_address):
        """Continue this peer's interrupted transfers after it (re)connected.

        Args:
            client_address (tuple): Value used by the operation.
        """
        self._drop_stale_transfers(peer=tuple(client_address))
        self._flush_pending_relay_resumes(tuple(client_address))
        self._request_resumes(tuple(client_address))

    # ---- native in-memory forward relay (server side) ----------------------

    def _forward_alloc_fid(self):
        """Handle the operation.

        Returns:
            object: Result produced by the operation.
        """
        with self._forward_fid_lock:
            self._forward_fid += 1
            return self._forward_fid

    def _forward_notify_error(self, sock, msg):
        """Handle the operation.

        Args:
            sock (object): Value used by the operation.
            msg (object): Value used by the operation.
        """
        try:
            self.send_message(sock, f"/forward_error {msg}")
        except Exception:
            pass

    def _forward_parse_items_addrs(self, tokens):
        """Handle the operation.

        Args:
            tokens (list | dict): Value used by the operation.

        Returns:
            list | tuple: Result produced by the operation.
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
                if isinstance(addr, tuple) and len(addr) == 2 and isinstance(addr[0], str):
                    addrs.append(addr)
                else:
                    items.append(token)
            else:
                items.append(token)
        return items, addrs

    def _forward_item_handler(self, sock, addr, cmd):
        """Server-side entry for /forward_item <kind> [resume=<offset>] <...> <addr...>.

        The server never touches disk: it only relays the uploaded byte
                stream to every reachable target client from memory. ``resume=<offset>``
                marks a repeated forward of a file the target already holds partially:
                the uploader starts at that offset, so the relay expects
                ``size - offset`` bytes of stream.

        Args:
            sock (object): Value used by the operation.
            addr (tuple): Value used by the operation.
            cmd (object): Value used by the operation.
        """
        try:
            parts = shlex.split(cmd)
        except Exception:
            self._forward_notify_error(sock, "invalid forward command")
            return
        if len(parts) < 3:
            self._forward_notify_error(sock, "invalid forward command")
            return
        kind = parts[1].lower()
        rest = parts[2:]
        resume_offset = 0
        if rest and rest[0].startswith("resume="):
            try:
                resume_offset = max(0, int(rest[0][len("resume=") :]))
            except ValueError:
                self._forward_notify_error(sock, "invalid resume offset")
                return
            rest = rest[1:]
        # optional destination directory is the very last argument, after the
        # client address tuples
        destination_path = None
        if rest and not (rest[-1].startswith("(") and rest[-1].endswith(")")):
            destination_path = rest[-1]
            rest = rest[:-1]
        items, addrs = self._forward_parse_items_addrs(rest)
        valid_targets = []
        for target in addrs:
            if target == (self.host, self.port):
                self._log(f"forward: target {target} is the server itself, skipped")
                continue
            if target not in self.clients:
                self._log(f"forward: target {target} is unreachable, skipped")
                continue
            valid_targets.append(target)
        if not valid_targets:
            self._forward_notify_error(sock, "no reachable targets to forward to")
            return
        if kind == "file":
            if not items:
                self._forward_notify_error(sock, "missing file name")
                return
            rel_dir = ""
            fname = items[0]
        elif kind == "folder":
            if len(items) < 2:
                self._forward_notify_error(sock, "missing folder file")
                return
            rel_dir = items[0]
            fname = items[1]
        else:
            self._forward_notify_error(sock, f"unknown forward kind: {kind}")
            return
        threading.Thread(
            target=self._forward_relay,
            args=(sock, addr, kind, rel_dir, fname, valid_targets, destination_path, resume_offset),
            daemon=True,
        ).start()

    def _forward_read_sign(self, sock, sign, timeout=15):
        """Read from a target transfer socket until ``sign`` is seen.

        Reads through `receive_message`, so a framed transfer connection
                yields decrypted bytes here like any other plaintext leg.

        Args:
            sock (object): Value used by the operation.
            sign (object): Value used by the operation.
            timeout (float): Value used by the operation.

        Returns:
            bool: Result produced by the operation.
        """
        buf = b""
        target = sign.encode("utf-8")
        err = self.error_sign.encode("utf-8")
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data = self.receive_message(sock, self.recv_messages_chunk_len)
            except Exception:
                return False
            if not data:
                return False
            buf += data
            if target in buf:
                return True
            if err in buf:
                return False
        return False

    def _forward_recv_exact(self, sock, n):
        """Handle the operation.

        Args:
            sock (object): Value used by the operation.
            n (int): Value used by the operation.

        Returns:
            object: Result produced by the operation.

        Raises:
            ConnectionError: When the operation cannot complete.
        """
        buf = b""
        while len(buf) < n:
            data = self.receive_message(sock, n - len(buf))
            if not data:
                raise ConnectionError("forward: uploader closed the stream")
            buf += data
        return buf

    def _handle_pause_command(self, client_address, command, paused):
        """Apply a client's ``/pause_trans``/``/start_trans`` on this side.

        A client throttles the writer that feeds it (a forward relay's writer
                for its target) or, without a relay, this server's own outbound leg to
                that client (see `pause_trans`).

        Args:
            client_address (tuple): Value used by the operation.
            command (object): Value used by the operation.
            paused (bool): Value used by the operation.

        Returns:
            NoneType | object: Result produced by the operation.
        """
        fid = _pause_command_fid(command)
        if fid is None:
            return None
        return self._mark_transfer_pause(fid, paused, tuple(client_address))

    def _forward_relay(
        self,
        forwarder_sock,
        originator_addr,
        kind,
        rel_dir,
        fname,
        targets,
        destination_path=None,
        resume_offset=0,
    ):
        """Relay one file/folder item to every target, streaming from the

        uploader's transfer connection with bounded in-memory buffering.

                Every pushed command is tagged with the originator's address (see
                ``forward_item_to``) so the receivers can attribute the transfer.

        Args:
            forwarder_sock (object): Value used by the operation.
            originator_addr (tuple): Value used by the operation.
            kind (object): Value used by the operation.
            rel_dir (str): Value used by the operation.
            fname (object): Value used by the operation.
            targets (object): Value used by the operation.
            destination_path (str): Value used by the operation.
            resume_offset (object): Value used by the operation.
        """
        fid = self._forward_alloc_fid()
        tfids = [self._forward_alloc_fid() for _ in targets]
        relay = {
            "cond": threading.Condition(),
            "writer_pause": {target: False for target in targets},
        }
        with self._forward_relays_lock:
            for key in [fid] + tfids:
                self._forward_relays[key] = relay
        record = {
            "fid": fid,
            "originator": [originator_addr[0], originator_addr[1]],
            "targets": [[target[0], target[1]] for target in targets],
            "tfids": list(tfids),
            "kind": kind,
            "rel_dir": rel_dir,
            "fname": fname,
            "destination": destination_path,
        }
        # the record survives the relay so an interrupted forward can be
        # routed back to its originator (see `_route_forward_resume`)
        with self._forward_relays_lock:
            for key in [fid] + tfids:
                self._forward_resume_records[key] = record
        try:
            # ask every target to open a receive transfer socket; the
            # destination directory (if any) goes before the target id so
            # the receiver's own destination parsing sees it
            for target, tfid in zip(targets, tfids):
                self.forward_item_to(
                    target, kind, rel_dir, fname, originator_addr, tfid, destination_path
                )
            # collect every target's advertised transfer port and mode stamp
            ports = {}
            deadline = time.time() + 20
            while len(ports) < len(tfids) and time.time() < deadline:
                with self.file_transfer_server_port_lock:
                    for info in list(self.file_server_port_list):
                        if info[1] in tfids and info[1] not in ports:
                            ports[info[1]] = (info[0], info[2])
                            self.file_server_port_list.remove(info)
                if len(ports) < len(tfids):
                    time.sleep(0.05)
            if len(ports) < len(tfids):
                self._forward_notify_error(forwarder_sock, "target port setup timed out")
                return
            # connect to each target and wait for its start sign
            target_conns = []
            for target, tfid in zip(targets, tfids):
                tport, stamp = ports[tfid]
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                try:
                    sock.connect((target[0], tport))
                except Exception as e:
                    self._log(f"forward: cannot connect to target {target}: {e}")
                    sock.close()
                    continue
                try:
                    target_control = self.clients.get(target, {}).get("socket")
                    self._crypto_attach_transfer_out(sock, target_control, stamp)
                except _CryptoUnavailable as exc:
                    self._log(f"forward: target {target} transfer aborted: {exc}")
                    sock.close()
                    continue
                if not self._forward_read_sign(sock, self.server_start_file_transfer_sign):
                    self._log(f"forward: target {target} did not start, skipped")
                    sock.close()
                    continue
                target_conns.append((tfid, target, sock))
            if not target_conns:
                self._forward_notify_error(forwarder_sock, "no target accepted the transfer")
                return
            # open the upload listener and hand it to the forwarder
            up_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            up_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            up_sock.bind((self.host, self.palloc()))
            sport = up_sock.getsockname()[1]
            up_stamp = self._crypto_transfer_stamp(forwarder_sock)
            up_sock.listen(1)
            # coordination line: always plaintext, so it also reaches a peer
            # that has not finished flipping to framed mode
            self.send_message(
                forwarder_sock, f"/forward_upload {fid} {sport} {up_stamp}", is_send_raw=True
            )
            up_client, _ = up_sock.accept()
            self._crypto_attach_transfer_in(up_client, forwarder_sock, up_stamp)
            self.send_message(up_client, self.server_start_file_transfer_sign)
            delivered = self._forward_pump(
                up_client, target_conns, forwarder_sock, fid, fname, relay, resume_offset
            )
            self._crypto_drop_channel(up_client)
            up_client.close()
            up_sock.close()
            self.pfree(sport)
            if delivered >= len(target_conns):
                # every target got the whole item: nothing is left to resume
                with self._forward_relays_lock:
                    for key in [fid] + tfids:
                        self._forward_resume_records.pop(key, None)
        finally:
            with self._forward_relays_lock:
                for key in [fid] + tfids:
                    self._forward_relays.pop(key, None)

    def _forward_pump(
        self, up_sock, target_conns, forwarder_sock, fid, fname, relay, resume_offset=0
    ):
        """Pump the uploader's byte stream to every target from memory.

        The total bytes held in the per-target queues never exceeds
                ``max_mem_buff``: once it does, the uploader is told to pause
                (``/pause_trans``) and the reader stops pulling; as the writers
                drain below the low-water mark the uploader is resumed
                (``/start_trans``).

        Args:
            up_sock (object): Value used by the operation.
            target_conns (object): Value used by the operation.
            forwarder_sock (object): Value used by the operation.
            fid (int): Value used by the operation.
            fname (object): Value used by the operation.
            relay (object): Value used by the operation.
            resume_offset (object): Value used by the operation.

        Returns:
        int: Targets that acknowledged the whole item (they are the ones
                        the relay's resume record can be dropped for).
        """
        max_bytes = max(1, self.max_mem_buff)
        low_water = max(1, max_bytes // 2)
        cond = relay["cond"]
        writer_pause = relay["writer_pause"]
        state = {"buffered": 0, "paused": False}
        queues = {tfid: queue.Queue() for tfid, _, _ in target_conns}
        END = object()
        result = {"ok": 0}

        def enqueue(chunk):
            """Handle the operation.

            Args:
                chunk (bytes | str): Value used by the operation.
            """
            with cond:
                state["buffered"] += len(chunk) * len(target_conns)
                for q in queues.values():
                    q.put(chunk)
                if not state["paused"] and state["buffered"] > max_bytes:
                    state["paused"] = True
                    try:
                        self.send_message(forwarder_sock, f"/pause_trans {fid}")
                    except Exception:
                        pass

        def writer(tfid, target, sock):
            """Handle the operation.

            Args:
                tfid (int): Value used by the operation.
                target (tuple): Value used by the operation.
                sock (object): Value used by the operation.
            """
            q = queues[tfid]
            while True:
                with cond:
                    while writer_pause.get(target, False):
                        cond.wait()
                item = q.get()
                if item is END:
                    break
                try:
                    self.send_message(sock, item)
                except Exception:
                    with cond:
                        while not q.empty():
                            state["buffered"] -= len(q.get())
                    break
                with cond:
                    state["buffered"] -= len(item)
                    if state["paused"] and state["buffered"] <= low_water:
                        state["paused"] = False
                        try:
                            self.send_message(forwarder_sock, f"/start_trans {fid}")
                        except Exception:
                            pass
                        cond.notify_all()
            if self._forward_read_sign(sock, self.server_received_file_data_sign):
                result["ok"] += 1
            self._crypto_drop_channel(sock)
            try:
                sock.close()
            except Exception:
                pass

        def reader():
            """Handle the operation."""
            try:
                name_len_b = self._forward_recv_exact(up_sock, 4)
                name_len = int.from_bytes(name_len_b, "big")
                name_b = self._forward_recv_exact(up_sock, name_len)
                size_b = self._forward_recv_exact(up_sock, 8)
                file_size = int.from_bytes(size_b, "big")
            except Exception as e:
                self._log(f"forward: uploader header read failed: {e}")
                for q in queues.values():
                    q.put(END)
                return
            for chunk in (name_len_b, name_b, size_b):
                enqueue(chunk)
            # a repeated forward uploads only the remainder of the file: the
            # offset the command carried says how much of the header's size
            # does not travel any more
            remaining = max(0, file_size - resume_offset)
            while remaining > 0:
                with cond:
                    while state["paused"]:
                        cond.wait()
                chunk = self._forward_recv_exact(
                    up_sock, min(self.file_transfer_chunk_len, remaining)
                )
                remaining -= len(chunk)
                enqueue(chunk)
            for q in queues.values():
                q.put(END)

        threads = [
            threading.Thread(target=writer, args=(tfid, target, sock), daemon=True)
            for tfid, target, sock in target_conns
        ]
        for t in threads:
            t.start()
        reader()
        for t in threads:
            t.join(timeout=120)
        try:
            self.send_message(up_sock, self.server_received_file_data_sign)
        except Exception:
            pass
        self._log(f"forward: relayed {fname} to {result['ok']}/{len(target_conns)} targets")
        return result["ok"]

    def start_TCP_Server(self):  # set up server socket
        """Bind the server socket, then accept clients until `stop` runs.

        Blocks the calling thread. A console command thread is started when
        ``is_input_command_in_console`` is True. Every accepted connection is
        served by `handle_client`: in its own thread by default, or by a
        coroutine on an asyncio event loop when ``is_asynic_clients_io`` is True,
        in which case ``max_clients`` no longer limits the connection count.
        Socket errors and the end of the accept loop both end in `stop`.
        """
        try:
            self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server_socket.bind((self.host, self.port))
            self.server_socket.listen(
                socket.SOMAXCONN if self.is_asynic_clients_io else self.max_clients
            )
            if self._stopped:
                # stop() arrived while the socket was being bound: stay stopped
                # instead of reviving the server (its listener would leak)
                self.server_socket.close()
                return
            self.running = True
            self._log(f"TCP server deployed on {self.host}:{self.port}")
            if self.is_asynic_clients_io:
                self._debug("clients io mode: asyncio, max clients mount: no limit")
            else:
                self._debug(f"max clients mount: {self.max_clients}")
            self._log("input '/stop' to stop the server\n")
            if self.is_input_command_in_console:
                input_thread = threading.Thread(
                    target=self.console_input, daemon=True
                )  # set up console input thread
                input_thread.start()
            else:
                pass
            if self.is_asynic_clients_io:
                asyncio.run(self._accept_clients_async())
            else:
                self._accept_clients_threaded()
        except Exception as e:
            self._log(f"Server error: {e}")
            self._log_exc()
        finally:
            self.stop()

    def _accept_clients_threaded(self):
        """Accept clients until the server stops, one thread per client."""
        while self.running:  # main loop to accept clients
            try:
                client_socket, client_address = self.server_socket.accept()
                if len(self.clients) >= self.max_clients:
                    self.send_message(client_socket, "Max connection mount, try latter")
                    client_socket.close()
                    continue
                client_thread = threading.Thread(  # set up client handling thread
                    target=self.handle_client, args=(client_socket, client_address), daemon=True
                )
                client_thread.start()
            except OSError as e:
                # an accept error while stopping is the expected wake-up from
                # stop()'s shutdown(), not a fault worth a traceback
                if self.running and not _is_closed_socket_error(e):
                    self._log(f"accept failed: {e}")
                    self._log_exc()
                break  # server socket closed, exit loop

    async def _accept_clients_async(self):
        """Accept clients until the server stops, one coroutine per client.

        Runs the whole client side on one event loop: the listening socket is
                used in non-blocking mode, and every accepted connection is handled by
                `handle_client` on this loop, so the connection count is bounded only by
                the file-descriptor limit. `stop` completes ``_async_wakeup`` to release
                the loop; its remaining client coroutines are cancelled when it ends.
        """
        loop = asyncio.get_running_loop()
        self._async_loop = loop
        wakeup = loop.create_future()
        self._async_wakeup = wakeup
        try:
            self.server_socket.setblocking(False)
            while self.running:
                accept = loop.create_task(loop.sock_accept(self.server_socket))
                done, _ = await asyncio.wait({accept, wakeup}, return_when=asyncio.FIRST_COMPLETED)
                if wakeup in done:
                    if accept.done() and not accept.cancelled() and accept.exception() is None:
                        accept.result()[0].close()  # accepted while stopping: dropped
                    else:
                        accept.cancel()
                        await asyncio.gather(accept, return_exceptions=True)
                    break
                try:
                    client_socket, client_address = accept.result()
                except OSError as e:
                    if not _is_closed_socket_error(e):
                        self._log(f"accept failed: {e}")
                        self._log_exc()
                    break  # server socket closed, exit loop
                except Exception as e:
                    self._log(f"accept loop error: {e}")
                    self._log_exc()
                    break
                client_socket.setblocking(False)
                self.handle_client(client_socket, client_address)
        finally:
            self._async_loop = None
            self._async_wakeup = None

    def console_input(self):  # deal consule input
        """Read console commands until the server stops.

        Handles ``/stop``, ``/status``, ``/clients``, ``/send_msg``, ``/file``,
        ``/file_folder``, ``/multiple_file_multiple_client``,
        ``/diff_multiple_file_diff_multiple_client`` and ``/help``; the forward
        commands are client-only and are refused here. Any other name goes to the
        handlers registered with ``where_to_run="client"``. Ctrl-C and EOF stop the
        server.
        """
        while self.running:
            try:
                cmd = input()
                deal_cmd = cmd.strip()
                if deal_cmd.lower() == "/stop":
                    self._log("shutting down...")
                    self.running = False
                    self.stop()
                elif deal_cmd.lower() == "/status":
                    self._log(f"current connection count: {len(self.clients)}")
                    self._log(f"server running: {self.running}")
                elif deal_cmd.lower() == "/clients":
                    with self.client_lock:
                        for addr, info in self.clients.items():
                            self._log(f"  {info['id']} - connection time: {info['connected_time']}")
                elif shlex.split(deal_cmd)[0].lower() == "/send_msg":
                    self.send_msg_to_specific_client(deal_cmd)
                elif shlex.split(deal_cmd)[0].lower() == "/file":
                    self.file_transfer_server_recv_client_start_thread(deal_cmd)
                elif shlex.split(deal_cmd)[0].lower() == "/file_folder":
                    self.folder_file_transfer_server_recv_client_start(deal_cmd)
                elif shlex.split(deal_cmd)[0].lower() == "/multiple_file_multiple_client":
                    self.multiple_file_multiple_client_transfer_server_recv_client_start(deal_cmd)
                elif shlex.split(deal_cmd)[0].lower() == "/diff_multiple_file_diff_multiple_client":
                    self.diff_multiple_file_diff_multiple_client_transfer_server_recv_client_start(
                        deal_cmd
                    )
                elif shlex.split(deal_cmd)[0].lower() in (
                    "/forward_send_msg",
                    "/forward_file",
                    "/forward_folder",
                ):
                    self._log(
                        "forward commands are client-only; "
                        "run them on a client console, not on the server"
                    )
                elif shlex.split(deal_cmd)[0].lower() == "/help":
                    help_text = [
                        "avalable commands:",
                        "/stop - stop the server\n",
                        "\t/status - display server status\n",
                        "\t/clients - display connected clients\n",
                        "\t/send_msg <message1> <message2> ... ",
                        "<client_id1> <client_id2> ... <messageN>",
                        " ... <client_idN> ... - send message or ",
                        "messages to specific client or clients\n",
                        "\t/file <file_path> <client_id> [destination_file_path] - ",
                        "send file to specific client\n",
                        "\t/file_folder <folder_path> <client_id> [destination_file_path] - ",
                        "send folder to specific client\n",
                        "\t/multiple_file_multiple_client <file1> <file2>",
                        " ... <client_id1> <client_id2> ... <fileN> ...",
                        " <client_idN> ... - send multiple files to multiple",
                        " clients, files should be before clients, ",
                        "and clients should be in format of (ip, port)\n",
                        "\t/diff_multiple_file_diff_multiple_client <file1> <file2>",
                        " ... <client_id1> <client_id2> ... <fileN> ...",
                        " <client_idN> ... - send multiple files to multiple clients",
                        " with different file list for each client, files and clients",
                        " should be in pairs, and clients should be in format of (ip, port)",
                    ]
                    self._log("\n" + " ".join(help_text) + "\n")
                else:
                    cmd_parts = shlex.split(deal_cmd)
                    if not cmd_parts:
                        return None
                    cmd_name = cmd_parts[0].lower()
                    if cmd_name in self._custom_handlers[1]:
                        handler = self._custom_handlers[1][cmd_name]
                        run_in_thread = self._custom_handler_threaded[1].get(cmd_name, False)
                        if run_in_thread:
                            self.submit_task(self._execute_custom_handler, handler, deal_cmd)
                            pass
                        else:
                            response = self._execute_custom_handler(handler, deal_cmd)
                            pass
                    else:
                        self._log("Unrecognized command, input '/help' for available commands")
            except KeyboardInterrupt:
                self._log("\nKeyboardInterrupt received, shutting down...")
                self.running = False
                self.stop()
                break
            except EOFError:
                self._log("EOF received, shutting down...")
                self.running = False
                self.stop()
                break
            except Exception as e:
                self._log(f"console command error: {e}")
                self._log_exc()
                pass

    def stop(self):  # shutting down the server
        """Stop the server and release everything it owns.

        Closes the server socket and every client connection, flushes the message
        and event stores, releases the allocated port range, clears ``running``
        and releases the client event loop when ``is_asynic_clients_io`` is True.
        Safe to call more than once.
        """
        self.running = False
        self._stopped = True  # also cancels a start_TCP_Server() still binding
        self.free_port()
        self._flush_record_stores()
        # in-flight transfers die with the server: persist them for the next run
        self._persist_transfer_progress()
        with self.client_lock:  # close all clients connections
            for client_info in self.clients.values():
                try:
                    # a client thread blocked in recv() keeps the connection half
                    # open past close(): the shutdown sends its FIN and wakes it
                    client_info["socket"].shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass  # already disconnected
                try:
                    client_info["socket"].close()
                except:
                    self._log_exc()
                    pass
            self.clients.clear()
        if self.server_socket:  # close server socket
            try:
                # close() alone does not release the port: a thread blocked in
                # accept() keeps the descriptor open until the syscall returns,
                # so the listener would stay up. shutdown() ends that accept
                # immediately (the loop treats the error as the stop signal).
                self.server_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass  # not connected / already shut down
            self.server_socket.close()
            self._log("server stopped")
        self._wake_async_accept_loop()

    def _wake_async_accept_loop(self):
        """Release the accept coroutine, if any, so its event loop can end."""
        loop = self._async_loop
        wakeup = self._async_wakeup
        if loop is None or loop.is_closed() or wakeup is None or wakeup.done():
            return
        try:
            loop.call_soon_threadsafe(wakeup.set_result, None)
        except RuntimeError:
            self._log_exc()


class TCP_Client_Base:  # TCP client class
    """TCP client: connect to a server, dispatch commands, send and receive messages.

    Lines received from the server go through `receive_messages`: every line is
    delivered to the listeners registered with `add_message_listener` first
    (commands included; a forwarded ``/send_msg_from`` envelope is delivered as
    its payload), then a line starting with ``/`` is handled by
    `handle_server_command` (protocol commands plus the handlers registered for
    the "server" side), any other line is stored in ``messages_dict`` as a plain
    message. With ``is_input_command_in_console`` the console thread
    `interactive_mode` sends typed lines to the server.

    Attributes:
        host (str): Server address this client connects to.
        port (int): Server port this client connects to.
        client_host (str): Local address the socket binds to.
        client_port (int | None): Local port, None when the OS chose one.
        running (bool): True while the connection is up.
        is_enable_encrypto (bool): Whether the RSA channel is negotiated.
        is_debug (bool): Whether execution-process lines are logged as well.
        is_print_log (bool): Whether the instance logs anything at all.
        is_enable_file_transfer_progress_bar (bool): Whether every running file
            transfer is tracked in ``file_recv_progress``/``file_upload_progress``.
        is_enable_real_time_network_speed (bool): Whether the real-time network
            speed is measured into ``upload_network_speed``/``recv_network_speed``.
        is_auto_reconnect (bool): Whether `start_TCP_client` reconnects after
            the connection drops (and resumes the interrupted transfers).
        file_recv_progress (dict): Running downloads keyed by fid; each value is
            ``[received bytes, total bytes, current speed, file path, peer]``
            (the speed is None until the transfer has moved its first chunk,
            and while speed measurement is off; ``peer`` is the sender's
            ``(ip, port)``).
        file_upload_progress (dict): Running uploads keyed by fid; each value is
            ``[sent bytes, total bytes, current speed, file path, peer]``, with
            ``peer`` the receiver's ``(ip, port)``. A forwarded transfer stores
            the original peer on both sides, never the relay.
        file_trans_progress_file (str): Path under the protocol's ``.Flow``
            directory where the two stores are written when the connection
            drops and read back on the next reconnect (`_request_resumes`).
        upload_network_speed (float | None): Bytes per second sent, or None when
            not measured.
        recv_network_speed (float | None): Bytes per second received, or None
            when not measured.
    """

    def __init__(
        self,
        host=None,
        client_host="127.0.0.1",
        port=65432,
        client_port=None,
        timeout=None,
        port_add_step=1,
        max_thread_num=10,
        is_input_command_in_console=True,
        is_wait_server=True,
        max_custom_workers=10,
        is_extend_command=False,
        is_enable_encrypto=True,
        is_custom_keys=None,
        max_mem_buff=2048,
        recv_messages_chunk_len=4096,
        file_transfer_chunk_len=65536,
        is_debug=False,
        is_print_log=True,
        is_enable_file_transfer_progress_bar=True,
        is_enable_real_time_network_speed=True,
        is_auto_reconnect=False,
    ):
        """Create the client and, unless extended, connect and start reading.

        Args:
            host (str | None): Server address to connect to; required before
                `connect` is called.
            client_host (str): Local address the socket binds to. Defaults to
                "127.0.0.1".
            port (int): Server port. Defaults to 65432.
            client_port (int | None): Local port to bind; None lets the OS choose
                an ephemeral port.
            timeout (float | None): Socket timeout in seconds for connect and
                receive. Must be None when ``is_wait_server`` is True.
            port_add_step (int): Step between candidate ports in the allocation
                range. Defaults to 1.
            max_thread_num (int): Concurrent file transfers allowed. Defaults to 10.
            is_input_command_in_console (bool): Enter interactive mode after
                connecting. Defaults to True.
            is_wait_server (bool): Keep retrying while the server is not reachable.
                Defaults to True.
            max_custom_workers (int): Worker slots for `submit_task` and threaded
                command handlers. Defaults to 10.
            is_extend_command (bool): When True, do not call `start_TCP_client`; the
                caller connects when ready. Defaults to False.
            is_enable_encrypto (bool): Negotiate the RSA-encrypted channel with the
                server. Defaults to True.
            is_custom_keys (list | None): ``[pub_key_path, pvt_key_path]`` pair used
                instead of the default key lookup; an invalid pair is ignored.
            max_mem_buff (int): Buffer ceiling in MiB, kept for parity with the
                server class; the client's forward path does not read it today.
                Defaults to 2048.
            recv_messages_chunk_len (int): Bytes read per receive on the control
                connection, covering commands and messages. Defaults to 4096.
            file_transfer_chunk_len (int): Chunk size in bytes streamed when
                sending or receiving a file or folder. Defaults to 65536 (64 KiB).
            is_debug (bool): Log execution-process lines in addition to command
                and result lines. Defaults to False.
            is_print_log (bool): Log at all; False silences every line this
                instance would print. Defaults to True.
            is_enable_file_transfer_progress_bar (bool): Track every running
                file transfer in ``file_recv_progress`` (downloads) and
                ``file_upload_progress`` (uploads): each entry holds the bytes
                transferred so far, the total size and the current speed (None
                while ``is_enable_real_time_network_speed`` is False). An entry
                appears when a transfer starts and is dropped when it ends.
                Defaults to True.
            is_enable_real_time_network_speed (bool): Measure the real-time
                network speed of every send and receive — the control layer and
                every temporary connection (file transfers included) — into
                ``upload_network_speed`` and ``recv_network_speed`` (bytes per
                second, averaged over ``NETWORK_SPEED_WINDOW`` seconds). With
                False nothing is measured and every speed value is None.
                Defaults to True.
            is_auto_reconnect (bool): Reconnect (and resume the interrupted
                transfers, see `_request_resumes`) when the connection drops
                while `start_TCP_client` runs. The transfers that died with
                the connection are persisted to the protocol's ``.Flow``
                directory and read back on the next connect. Defaults to
                False: a dropped connection ends the client.

        Raises:
            ValueError: If ``is_wait_server`` is True and ``timeout`` is not None.
            OSError: If the ``.Flow`` directories or ``decode_command_table.json``
                cannot be created or read.
        """
        self.max_mem_buff = max_mem_buff * 1024 * 1024
        self.recv_messages_chunk_len = recv_messages_chunk_len
        self.file_transfer_chunk_len = file_transfer_chunk_len
        self.is_enable_file_transfer_progress_bar = is_enable_file_transfer_progress_bar
        self.is_enable_real_time_network_speed = is_enable_real_time_network_speed
        # Per-transfer progress stores: fid -> [transferred bytes, total bytes,
        # current speed, local file path, peer (ip, port)]. An entry appears
        # when a transfer starts and is removed when it ends; a transfer
        # interrupted by a disconnect keeps its entry (and its partial file)
        # so it can be resumed once the peer is back (see `_request_resumes`).
        self.file_recv_progress = {}  # downloads: fid -> [received, total, speed, path, peer]
        self.file_upload_progress = {}  # uploads: fid -> [sent, total, speed, path, peer]
        self.upload_network_speed = None  # bytes/s over the last NETWORK_SPEED_WINDOW seconds
        self.recv_network_speed = None
        self._network_speed_lock = threading.Lock()  # serialises the speed sample windows
        self._upload_speed_samples = collections.deque()  # (timestamp, bytes) samples
        self._recv_speed_samples = collections.deque()
        self._upload_speed_total = 0  # bytes held in the sample windows
        self._recv_speed_total = 0
        self._forward_upload_queue = queue.Queue()
        # Relay records of the forwards this client started, kept after their
        # leg died so a target's resume request can re-drive them: relay fid
        # and every target tfid -> record (see `_resume_forward`).
        self._forward_resume_records = {}
        # Resumes this client asked a forward's originator for, keyed by the
        # local path of the partial file: the fresh relay leg lands on that
        # path and must append to what is already there (see
        # `_take_pending_resume`).
        self._resume_pending_paths = {}  # path -> (old fid, byte offset)
        self._forward_resume_lock = threading.Lock()
        self._forward_error = None
        # Outbound-transfer pause switches (see `pause_trans`/`start_trans`):
        # fid -> bool. A leg waits while its own fid is marked paused.
        self._transfer_pause = {}
        self._transfer_pause_cond = threading.Condition()
        self._active_transfer_fids = set()  # fids of the legs running right now
        self._active_transfer_sockets = {}  # fid -> socket of the leg running right now
        self.project_dir = os.path.dirname(os.path.abspath(__file__))
        self.file_transfer_dir = os.path.join(self.project_dir, "received_files")
        self.decode_command_table_file_path = os.path.join(
            self.project_dir, "decode_command_table.json"
        )
        self.project_info_dir = os.path.join(self.project_dir, ".Flow")
        if os.path.exists(self.project_info_dir) == False:
            os.mkdir(self.project_info_dir)
        self.project_temp_info_dir = os.path.join(self.project_info_dir, "temp_info")
        self.client_port_lock_file = os.path.join(
            self.project_temp_info_dir, "client_port_lock.lock"
        )
        # Interrupted transfers are persisted here on a disconnect and read
        # back when the client reconnects (see `_persist_transfer_progress`).
        self.file_trans_progress_file = os.path.join(
            self.project_info_dir, "client_file_trans_progress.json"
        )
        self._transfer_progress_lock = threading.RLock()  # serialises the persist/load pair
        if os.path.exists(self.project_temp_info_dir) == False:
            os.mkdir(self.project_temp_info_dir)
        self.all_allocated_ports_list = []
        self.is_hand_alloc_port = None
        self.file_transfer_server_port = None
        self.file_server_port_list = []
        self.file_transfer_server_port_lock = threading.Lock()
        self.client_ports_list = []
        self.client_port = client_port
        self.host = host
        self.client_host = client_host
        self.port = port
        self.port_add_step = port_add_step
        self.latest_port = None
        self.timeout = timeout
        self.client_socket = None
        self.running = False
        self._stopped = False  # a close() before connect() must stick
        self.receive_thread = None
        self.command_decode_table_str = None
        self.max_thread_num = max_thread_num
        self.is_input_command_in_console = is_input_command_in_console
        self.is_wait_server = is_wait_server
        self.is_auto_reconnect = is_auto_reconnect
        if self.is_wait_server and self.timeout != None:
            error_msg = [
                "wait_server_timeout is not applicable when is_wait_server is True,",
                "please set wait_server_timeout to None or set is_wait_server to False",
            ]
            raise ValueError(" ".join(error_msg))
        self.file_client_id = 0
        self.file_client_id_lock = threading.Lock()
        MAX_CONCURRENT_FILES = self.max_thread_num
        self.file_semaphore = threading.Semaphore(MAX_CONCURRENT_FILES)
        with open(self.decode_command_table_file_path, "r", encoding="utf-8") as f:
            self.command_decode_table_str = f.read()
        self.command_decode_table = ast.literal_eval(self.command_decode_table_str)
        self.send_file_header_sign = self.command_decode_table[0]["file_send_server_header"]
        self.send_file_data_sign = self.command_decode_table[0]["file_send_server_data"]
        self.server_received_file_header_sign = self.command_decode_table[0][
            "file_receive_client_header"
        ]
        self.server_received_file_data_sign = self.command_decode_table[0][
            "file_receive_client_data"
        ]
        self.server_start_file_transfer_sign = self.command_decode_table[0][
            "file_send_server_start_file_transfer"
        ]
        self.error_sign = self.command_decode_table[0]["file_send_receive_error"]
        self._custom_handlers = [{}, {}]
        self._custom_handler_threaded = [{}, {}]
        self._custom_executor = ThreadPoolExecutor(max_workers=max_custom_workers)
        self._task_semaphore = threading.Semaphore(max_custom_workers)
        # Inbound-event listeners (see add_message_listener/add_file_listener).
        # They run on the receive thread, so a listener must not block and must
        # never raise (exceptions are swallowed by the notify helpers).
        self._message_listeners = []
        self._file_listeners = []
        self._send_message_listeners = []
        self._control_sockets = set()  # the control link to the server; only it is recorded
        self._event_listeners_lock = threading.Lock()
        # Message/event stores: external code reads these instead of registering
        # listeners. Keyed by the socket; each value is a list of [content,
        # timestamp] pairs. messages_dict and sent_messages_dict hold plain text
        # (inbound / outbound); events_dict and sent_events_dict hold "/" commands.
        # When a store's total size reaches max_dict_size (64 KiB) it is flushed
        # to its JSON log and cleared (see _append_record, _flush_record_stores).
        self.messages_dict = {}
        self.events_dict = {}
        self.sent_messages_dict = {}
        self.sent_events_dict = {}
        self._messages_dict_lock = threading.Lock()
        self._events_dict_lock = threading.Lock()
        self._sent_messages_dict_lock = threading.Lock()
        self._sent_events_dict_lock = threading.Lock()
        self._socket_keys = {}  # record key -> "ip:port", captured while it was alive
        self._socket_keys_lock = threading.Lock()
        self._messages_dict_size = 0
        self._events_dict_size = 0
        self._sent_messages_dict_size = 0
        self._sent_events_dict_size = 0
        self.max_dict_size = 64 * 1024
        self.messages_log_file = os.path.join(self.project_info_dir, "messages_log.json")
        self.events_log_file = os.path.join(self.project_info_dir, "events_log.json")
        self.sent_messages_log_file = os.path.join(self.project_info_dir, "sent_messages_log.json")
        self.sent_events_log_file = os.path.join(self.project_info_dir, "sent_events_log.json")
        self.is_extend_command = is_extend_command
        self.is_enable_encrypto = is_enable_encrypto
        self.is_custom_keys = is_custom_keys
        self.is_debug = is_debug
        self.is_print_log = is_print_log
        self._crypto_lock = threading.RLock()  # serialises the crypto collections (no-GIL safe); held only around short ops, never across I/O
        self._encrypted_sockets = set()  # sockets currently speaking frames (see _SecureChannel)
        self._channels = {}  # socket -> _SecureChannel (control and transfer connections)
        self._crypto_session = None  # the session context, created when crypto is used
        self._crypto_peer = {}  # socket -> (peer_role, peer_pem_path)
        self.crypto = (
            rsa_crypto.RsaCrypto("client", self.project_dir, custom_keys=is_custom_keys)
            if is_enable_encrypto
            else None
        )
        self._crypto_ack_event = threading.Event()
        self._crypto_exchange_done = threading.Event()
        self._crypto_server_pub_event = threading.Event()
        self._crypto_force = False
        self._crypto_push_active = set()
        self._crypto_send_lock = threading.Lock()  # send+encrypt ordered: wire order == seq order
        self._crypto_handshake_gen = 0  # bumped on handshake start; stale flows act/close no-op
        self._crypto_mode_event = threading.Event()  # set once the server announced its crypto mode
        self._crypto_mode_ok = False  # server mode matches ours (negotiated at connect)
        self._crypto_mode_timeout = 10  # seconds to wait for the server's mode announcement

        self._crypto_requested_server_pub = False
        self._crypto_server_ready = False
        self._crypto_own_ready_sent = False
        self._crypto_server_pub_ok = None  # None/True/False: TOFU check of server key
        self._crypto_server_pem_path = None  # cached server public key file
        self._crypto_handshake_started = False  # a hello was sent/received
        self._crypto_my_nonce = None  # session nonce we advertise
        self._crypto_server_nonce = None  # session nonce the server advertises
        self._crypto_decode_failures = 0  # consecutive decode failures
        if self.is_extend_command:
            pass
        else:
            self.start_TCP_client()

    def register_command(self, command_name, handler, where_to_run, run_in_thread=False):
        """Register a custom command handler.

        Args:
            command_name (str): Command to intercept, e.g. "/my_command"; matched
                case-insensitively against the first token.
            handler (callable): ``handler(client_socket, client_address, command)``
                called with the raw line; a non-None return value is sent back as
                the response.
            where_to_run (str): "server" for commands pushed by the server, "client"
                for commands typed on this instance's console.
            run_in_thread (bool): Run the handler on the worker pool instead of the
                reader thread. Defaults to False.

        Returns:
            bool | None: False when ``where_to_run`` is neither "server" nor
                "client"; the handler is then not registered.
        """
        registe_index = None
        if where_to_run == "server":
            registe_index = 0
        elif where_to_run == "client":
            registe_index = 1
        else:
            self._log(f"Invalid where_to_run value: {where_to_run}, must be 'server' or 'client'")
            return False
        self._custom_handlers[registe_index][command_name] = handler
        self._custom_handler_threaded[registe_index][command_name] = run_in_thread

    def call_extension_functions(self, command):
        """Call one registered extension command handler directly from code.

        The handler is selected by the first token of ``command``, matched
        case-insensitively against the names given to `register_command`: the
        console group is searched first, then the group fed by the lines the
        server sends. The handler runs on the calling thread — ``run_in_thread``
        only affects the console and inbound dispatch paths — and is called
        with this client's socket and id, the context its dispatch paths give
        it.

        Args:
            command (str): Command line to run, e.g. ``"/my_command arg"``; the
                handler receives it unchanged.

        Returns:
            object: Whatever the handler returns.

        Raises:
            CommandNotFoundError: If no registered handler matches the first
                token of ``command``.
            ValueError: If ``command`` cannot be split (unbalanced quotes).
        """
        handler = _custom_handler_for(command, self._custom_handlers)
        client_id = f"{self.client_host}:{self.client_port}"
        return handler(self.client_socket, client_id, command)

    def add_message_listener(self, listener):
        """Register ``listener(sender_id, message)`` for every inbound line.

        Every line received from the server is reported, commands included, so
        the listener observes all inbound traffic; a message another client
        forwarded here is reported as its payload, attributed to the forwarder.

        Args:
            listener (callable): ``listener(sender_id, message)``; ``sender_id`` is
                the author's ``"ip:port"`` — the forwarding client for a message
                another client forwarded here (``/send_msg_from`` envelope), or None
                for a direct push from the server, which names no client author. It
                runs on the receive thread, so it must not block, and exceptions
                raised inside it are swallowed.

        """
        with self._event_listeners_lock:
            if listener not in self._message_listeners:
                self._message_listeners.append(listener)

    def remove_message_listener(self, listener):
        """Unregister a listener previously added by `add_message_listener`.

        Args:
            listener (callable): Listener to remove; an unknown one is ignored.
        """
        with self._event_listeners_lock:
            try:
                self._message_listeners.remove(listener)
            except ValueError:
                pass

    def add_send_message_listener(self, listener):
        """Register ``listener(target_id, message)`` for every payload written by `send_message`.

        Fired after a successful write, for text and bytes alike (so a byte stream
        reaches it), never for ``is_send_raw`` protocol lines. ``target_id`` is the
        server's ``"ip:port"``. It runs on the sending thread, so it must not block,
        and exceptions raised inside it are swallowed.

        Args:
            listener (callable): ``listener(target_id, message)``.
        """
        with self._event_listeners_lock:
            if listener not in self._send_message_listeners:
                self._send_message_listeners.append(listener)

    def remove_send_message_listener(self, listener):
        """Unregister a listener previously added by `add_send_message_listener`.

        Args:
            listener (callable): Listener to remove; an unknown one is ignored.
        """
        with self._event_listeners_lock:
            try:
                self._send_message_listeners.remove(listener)
            except ValueError:
                pass

    def add_file_listener(self, listener):
        """Register ``listener(full_path, name, size, command)`` per saved inbound file.

        Fired after a file pushed by the server (a direct send, or a forwarded
        file/folder item) has been fully written to ``file_transfer_dir``.

        Args:
            listener (callable): ``listener(full_path, name, size, command)``;
                ``command`` is the wire command that triggered the transfer, so a
                listener can recognise protocol pushes such as ``/crypto_pub_key``.
                It runs on the transfer thread, so it must not block.
        """
        with self._event_listeners_lock:
            if listener not in self._file_listeners:
                self._file_listeners.append(listener)

    def remove_file_listener(self, listener):
        """Unregister a listener previously added by `add_file_listener`.

        Args:
            listener (callable): Listener to remove; an unknown one is ignored.
        """
        with self._event_listeners_lock:
            try:
                self._file_listeners.remove(listener)
            except ValueError:
                pass

    def _notify_message_received(self, sender, message):
        """Handle the operation.

        Args:
            sender (object): Value used by the operation.
            message (object): Value used by the operation.
        """
        with self._event_listeners_lock:
            listeners = list(self._message_listeners)
        for listener in listeners:
            try:
                listener(sender, message)
            except Exception as e:
                self._log(f"message listener error: {e}")
                self._log_exc()

    def _notify_transfer_chunk(self, client_socket, data):
        """Report one chunk read from a non-control socket to the message listeners.

        Side-channel sockets (file/folder transfers, forwards) carry raw bytes and
        ``sign`` tokens, which belong to the transfer layer, so they are reported
        to the listeners but never recorded in a store.
        """
        if not data or client_socket in self._control_sockets:
            return
        with self._event_listeners_lock:
            listeners = list(self._message_listeners)
        for listener in listeners:
            try:
                listener(self._socket_key(client_socket), data)
            except Exception as e:
                self._log(f"message listener error: {e}")
                self._log_exc()

    def _notify_message_sent(self, client_socket, message):
        """Report one written payload to the send listeners."""
        with self._event_listeners_lock:
            listeners = list(self._send_message_listeners)
        if not listeners:
            return
        target_id = self._socket_key(client_socket)
        for listener in listeners:
            try:
                listener(target_id, message)
            except Exception as e:
                self._log(f"send message listener error: {e}")
                self._log_exc()

    def _notify_file_received(self, full_path, name, size, command):
        """Handle the operation.

        Args:
            full_path (str): Value used by the operation.
            name (object): Value used by the operation.
            size (int): Value used by the operation.
            command (object): Value used by the operation.
        """
        with self._event_listeners_lock:
            listeners = list(self._file_listeners)
        for listener in listeners:
            try:
                listener(full_path, name, size, command)
            except Exception as e:
                self._log(f"file listener error: {e}")
                self._log_exc()

    def _socket_key(self, sock):
        """Serializable key for a sender socket (its peer address).

        Forwarded records already carry the originator's address as a
                string and are used as-is; a socket that cannot answer
                ``getpeername`` (already closed) falls back to its repr.

        Args:
            sock (object): Value used by the operation.

        Returns:
            object: Result produced by the operation.
        """
        if isinstance(sock, str):
            return sock
        try:
            ip, port = sock.getpeername()[:2]
            return f"{ip}:{port}"
        except Exception:
            return str(sock)

    def _remember_socket_key(self, sock):
        """Capture the log key of ``sock`` while it is known to be alive.

        A record stays buffered after its connection goes away, and the
                flush that persists it must still file it under the sender's
                address: by then the closed socket no longer answers
                ``getpeername`` and would be logged as ``str(sock)``. The key is
                forgotten once nothing is buffered under it any more (see
                ``_flush_dict_locked``).

        Args:
            sock (object): Value used by the operation.
        """
        with self._socket_keys_lock:
            if sock not in self._socket_keys:
                self._socket_keys[sock] = self._socket_key(sock)

    def _record_key(self, sock):
        """Return the JSON-log key for ``sock``: the address captured when it was.

        The JSON-log key for ``sock``: the address captured when it was

        recorded, or the live peer address for a socket inserted without a
                record.

        Args:
            sock (object): Value used by the operation.

        Returns:
            object: Result produced by the operation.
        """
        key = self._socket_keys.get(sock)
        if key is not None:
            return key
        return self._socket_key(sock)

    def _append_record(self, store, log_path, sock, content):
        """Append one ``[content, timestamp]`` pair under ``sock``; flush when full.

        Args:
            store (tuple): ``(dict, lock, size_attr)``; the dict maps socket to
                a list of pairs, and ``size_attr`` names the byte-size attribute.
            log_path (str): JSON log the store flushes into.
            sock (object): Key socket.
            content (str): Text to record.
        """
        d, lock, size_attr = store
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with lock:
            self._remember_socket_key(sock)
            d.setdefault(sock, []).append([content, timestamp])
            size = getattr(self, size_attr) + len(content.encode("utf-8", "replace"))
            size += len(timestamp)
            setattr(self, size_attr, size)
            if size >= self.max_dict_size:
                self._flush_dict_locked(d, size_attr, log_path)

    def _record_message(self, sock, content):
        """Store one inbound plain-text message under the sender's socket."""
        self._append_record(
            (self.messages_dict, self._messages_dict_lock, "_messages_dict_size"),
            self.messages_log_file, sock, content,
        )

    def _record_event(self, sock, command):
        """Store one inbound "/" command under the sender's socket."""
        self._append_record(
            (self.events_dict, self._events_dict_lock, "_events_dict_size"),
            self.events_log_file, sock, command,
        )

    def _record_sent(self, sock, content):
        """Store one outbound text: a "/" line as a sent event, other text as a sent message."""
        if content.startswith("/"):
            self._append_record(
                (self.sent_events_dict, self._sent_events_dict_lock, "_sent_events_dict_size"),
                self.sent_events_log_file, sock, content,
            )
        else:
            self._append_record(
                (
                    self.sent_messages_dict,
                    self._sent_messages_dict_lock,
                    "_sent_messages_dict_size",
                ),
                self.sent_messages_log_file, sock, content,
            )

    def _record_dicts(self):
        """Return the four record stores, in flush order."""
        return (
            self.messages_dict,
            self.events_dict,
            self.sent_messages_dict,
            self.sent_events_dict,
        )

    def _flush_messages_dict(self):
        """Flush the inbound messages store into its JSON log."""
        with self._messages_dict_lock:
            self._flush_dict_locked(
                self.messages_dict, "_messages_dict_size", self.messages_log_file
            )

    def _flush_events_dict(self):
        """Flush the inbound events store into its JSON log."""
        with self._events_dict_lock:
            self._flush_dict_locked(self.events_dict, "_events_dict_size", self.events_log_file)

    def _flush_record_stores(self):
        """Flush every record store into its JSON log (called on stop)."""
        with self._messages_dict_lock:
            self._flush_dict_locked(
                self.messages_dict, "_messages_dict_size", self.messages_log_file
            )
        with self._events_dict_lock:
            self._flush_dict_locked(self.events_dict, "_events_dict_size", self.events_log_file)
        with self._sent_messages_dict_lock:
            self._flush_dict_locked(
                self.sent_messages_dict, "_sent_messages_dict_size", self.sent_messages_log_file
            )
        with self._sent_events_dict_lock:
            self._flush_dict_locked(
                self.sent_events_dict, "_sent_events_dict_size", self.sent_events_log_file
            )

    def _update_event_timestamp(self, sock, command, timestamp):
        """Stamp the completion time onto the recorded event for ``command``.

        File transfers finish on a worker thread after the receive thread
                recorded the command, so the event's timestamp is refreshed here with
                the moment the transfer actually completed.

        Args:
            sock (object): Value used by the operation.
            command (object): Value used by the operation.
            timestamp (object): Value used by the operation.
        """
        with self._events_dict_lock:
            entries = self.events_dict.get(sock)
            if entries:
                for entry in reversed(entries):
                    if entry[0] == command:
                        entry[1] = timestamp
                        return

    def _splice_event_command(self, command, **parts):
        """Return the command to record as an event.

        The wire command is recorded verbatim whenever it is available. When
                this end cannot see the original command (a transfer relayed by the
                server, or a protocol-internal control line), splice a readable
                command from the available parts so the event still identifies the
                transfer.

        Args:
            command (object): Value used by the operation.
            **parts (list | dict): Value used by the operation.

        Returns:
            object: Result produced by the operation.
        """
        if command:
            return command
        kind = parts.get("kind")
        fname = parts.get("fname")
        rel_dir = parts.get("rel_dir")
        if kind == "folder" and fname:
            if rel_dir:
                return "/file_folder {} {}".format(shlex.quote(rel_dir), shlex.quote(fname))
            return "/file_folder {}".format(shlex.quote(fname))
        if fname:
            return "/file {}".format(shlex.quote(fname))
        return parts.get("fallback") or "/unknown"

    def _flush_dict_locked(self, d, size_attr, path):
        """Flush ``d`` (socket -> [[content, ts], ...]) into its JSON log and

        clear it. The caller must hold the dict's lock.

        Args:
            d (list | dict): Value used by the operation.
            size_attr (object): Value used by the operation.
            path (str): Value used by the operation.
        """
        if not d:
            return
        snapshot = dict(d)
        d.clear()
        setattr(self, size_attr, 0)
        self._merge_json_log(path, snapshot)
        with self._socket_keys_lock:  # a key is only kept while some store buffers it
            for sock in snapshot:
                if not any(sock in store for store in self._record_dicts()):
                    self._socket_keys.pop(sock, None)

    def _merge_json_log(self, path, snapshot):
        """Merge ``snapshot`` into the JSON log at ``path`` (append per socket).

        Publishes through `_publish_json_log`: a temp file named after this
                writer, and an optimistic re-merge when another instance published in
                between, so concurrent flushes of several instances sharing one log
                neither corrupt nor lose each other. A failure is logged, never raised
                into the caller.

        Args:
            path (str): Value used by the operation.
            snapshot (list | dict): Value used by the operation.
        """
        try:
            _publish_json_log(path, snapshot, self._record_key)
        except Exception as e:
            self._log(f"log file update failed: {e}")
            self._log_exc()

    def submit_task(self, func, *args, **kwargs):
        """Run a callable on the instance's worker pool.

        Args:
            func (callable): Callable to run.
            *args (Any): Positional arguments forwarded to ``func``.
            **kwargs (Any): Keyword arguments forwarded to ``func``.

        Returns:
            concurrent.futures.Future: Handle for the submitted call; its worker
                slot is released when the call finishes.
        """
        self._task_semaphore.acquire()
        future = self._custom_executor.submit(func, *args, **kwargs)
        future.add_done_callback(lambda f: self._task_semaphore.release())
        return future

    def _log(self, *parts):
        """Print a command/result line, unless ``is_print_log`` is False.

        Args:
            *parts (Any): Values forwarded to ``print``.
        """
        _log_line(self.is_print_log, parts)

    def _debug(self, *parts):
        """Print an execution-process line; needs ``is_print_log`` and ``is_debug``.

        Args:
            *parts (Any): Values forwarded to ``print``.
        """
        _debug_line(self.is_print_log, self.is_debug, parts)

    def _log_exc(self):
        """Print the traceback of the exception being handled, in debug mode only."""
        if self.is_print_log and self.is_debug:
            traceback.print_exc()

    def _measure_network_speed(self, payload, is_upload):
        """Fold just-sent/received traffic into the real-time speed estimate.

        Every send and receive sample feeds this — control messages, file data
                and relay traffic alike — so the speed attributes describe the
                instance's current traffic as a rolling average over
                ``NETWORK_SPEED_WINDOW`` seconds.

        Args:
            payload (bytes | str | int): Bytes just written or read; a str counts as its UTF-8 length, an int is a byte count already.
            is_upload (bool): True for sent bytes (``upload_network_speed``), False for received ones (``recv_network_speed``).

        Returns:
        float | None: The refreshed speed in bytes per second, or None when
                        ``is_enable_real_time_network_speed`` is False; both speed
                        attributes are None then.
        """
        if not self.is_enable_real_time_network_speed:
            self.upload_network_speed = None
            self.recv_network_speed = None
            return None
        if isinstance(payload, str):
            byte_count = len(payload.encode("utf-8"))
        elif isinstance(payload, int):
            byte_count = payload
        else:
            byte_count = len(payload)
        if not byte_count:
            return None
        now = time.monotonic()
        with self._network_speed_lock:
            if is_upload:
                samples = self._upload_speed_samples
                total = self._upload_speed_total + byte_count
            else:
                samples = self._recv_speed_samples
                total = self._recv_speed_total + byte_count
            samples.append((now, byte_count))
            cutoff = now - NETWORK_SPEED_WINDOW
            while samples[0][0] < cutoff:  # only the current window counts
                total -= samples.popleft()[1]
            if is_upload:
                self._upload_speed_total = total
            else:
                self._recv_speed_total = total
            span = now - samples[0][0]
            if span < NETWORK_SPEED_MIN_SPAN:  # a single burst is not extrapolated
                span = NETWORK_SPEED_MIN_SPAN
            speed = total / span
            if is_upload:
                self.upload_network_speed = speed
            else:
                self.recv_network_speed = speed
        return speed

    def _file_progress_start(self, progress_dict, fid, total_bytes, path, peer=None, initial=0):
        """Register one file transfer in a progress store.

        Args:
            progress_dict (dict): ``self.file_recv_progress`` for an inbound transfer, ``self.file_upload_progress`` for an outbound one.
            fid (int | None): The transfer's fid; None tracks nothing (a leg without a protocol transfer id, such as a key push).
            total_bytes (int): Size of the file being transferred.
            path (str): Local path of the file: the source of an outbound transfer, the file being written for an inbound one.
            peer (tuple | None): ``(ip, port)`` of the transfer's original peer — the sender for an inbound transfer, the receiver for an outbound one. A forwarded transfer stores the original peer on both sides, never the relay; None marks a leg that cannot be resumed (there is no peer to continue it with).
            initial (int): Bytes already transferred when the leg starts; a resumed transfer continues a partially transferred file.

        Returns:
        list | None: The ``[transferred, total, speed, path, peer]`` entry
                        the transfer updates, or None when tracking is off
                        (``is_enable_file_transfer_progress_bar`` False, or no fid).
        """
        if fid is None or not self.is_enable_file_transfer_progress_bar:
            return None
        entry = [initial, total_bytes, None, path, _peer_from_json(peer) if peer else None]
        progress_dict[fid] = entry
        with self._transfer_progress_lock:
            self._active_transfer_fids.add(fid)
        return entry

    def _file_progress_update(self, entry, byte_count, is_upload):
        """Fold one transferred chunk into a progress entry.

        Args:
            entry (list | None): Entry from `_file_progress_start`, or None when the transfer is not tracked.
            byte_count (int): Bytes of the chunk just sent or received.
            is_upload (bool): True for sent chunks, False for received ones. The speed slot mirrors the live measurement, so it stays None while ``is_enable_real_time_network_speed`` is False.
        """
        if entry is None:
            return
        entry[0] += byte_count
        entry[2] = self.upload_network_speed if is_upload else self.recv_network_speed

    def _file_progress_finish(self, progress_dict, fid, entry, keep=False):
        """Drop a finished transfer's progress entry, or keep it for a resume.

        A newer transfer may already hold the same fid (transfer ids are reused
                as transfers end), so only this transfer's own entry is dropped. With
                ``keep`` True an interrupted transfer keeps its entry — and the partial
                file that entry points at — so the reconnect logic can continue it
                (`_request_resumes`); the entry then lives until the transfer ends.

        Args:
            progress_dict (dict): Store the entry belongs to.
            fid (int | None): Transfer id the entry is keyed by.
            entry (list | None): Entry to drop; None tracks nothing.
            keep (bool): Keep the entry (and persist the stores) instead of dropping it. Defaults to False.
        """
        if entry is None:
            return
        with self._transfer_progress_lock:
            if progress_dict.get(fid) is entry:
                # a leg of an earlier session may finish after a newer leg
                # took the fid over: only the owner releases it
                self._active_transfer_fids.discard(fid)
        if keep:
            entry[2] = None  # a stopped transfer has no live speed any more
            self._persist_transfer_progress()
            return
        if progress_dict.get(fid) is entry:
            del progress_dict[fid]
        if os.path.exists(self.file_trans_progress_file):
            # the persisted stores are only kept up to date while they exist:
            # a transfer that ended must not be resumed on a later reconnect
            self._persist_transfer_progress()

    def _transfer_progress_snapshot(self):
        """Return the JSON-ready form of every transfer still in play.

        Returns:
        dict: ``{"upload": ..., "download": ..., "relays": ...}``; fids
                        are string keys and every peer is an ``[ip, port]`` list.
        """

        def entries(store):
            """Handle the operation.

            Args:
                store (list | dict): Value used by the operation.

            Returns:
                list | tuple: Result produced by the operation.
            """
            return {
                str(fid): [
                    entry[0],
                    entry[1],
                    entry[2],
                    entry[3],
                    list(entry[4]) if entry[4] else None,
                ]
                for fid, entry in list(store.items())
            }

        with self._forward_resume_lock:
            relays = {
                str(key): {
                    "fid": record.get("fid"),
                    "kind": record.get("kind"),
                    "rel_dir": record.get("rel_dir"),
                    "fname": record.get("fname"),
                    "path": record.get("path"),
                    "destination": record.get("destination"),
                    "targets": [list(target) for target in record.get("targets") or []],
                }
                for key, record in list(self._forward_resume_records.items())
            }
        return {
            "upload": entries(self.file_upload_progress),
            "download": entries(self.file_recv_progress),
            "relays": relays,
        }

    def _persist_transfer_progress(self):
        """Write the transfer stores to the protocol's ``.Flow`` directory.

        Every in-flight transfer — uploads, downloads, and the relay records
                of the forwards this client started — is written to
                ``client_file_trans_progress.json`` when a connection drops, and the
                file is what the reconnect logic reads back. Once nothing is left to
                transfer the file is emptied; it is only rewritten while it exists, so
                a run without an interrupted transfer never creates it. A write
                failure is logged, never raised.
        """
        snapshot = self._transfer_progress_snapshot()
        with self._transfer_progress_lock:
            if not any(snapshot.values()) and not os.path.exists(self.file_trans_progress_file):
                return
            tmp_path = f"{self.file_trans_progress_file}.{os.getpid()}.tmp"
            try:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(snapshot, f, ensure_ascii=False, indent=2)
                os.replace(tmp_path, self.file_trans_progress_file)
            except OSError as e:
                self._log(f"transfer progress file update failed: {e}")
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    def _load_transfer_progress(self):
        """Read the persisted interrupted transfers back into the stores.

        Every entry read back keeps the bytes it had already transferred, so
                the reconnect logic can ask the sender to continue from there; every
                relay record comes back so an interrupted forward can be re-driven.
                Entries a live transfer (or a relay) already owns are left alone.

        Returns:
        int: Number of transfer entries and relay records restored.
        """
        with self._transfer_progress_lock:
            try:
                with open(self.file_trans_progress_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except FileNotFoundError:
                return 0
            except (OSError, ValueError) as e:
                self._log(f"transfer progress file unreadable ({e}), ignored")
                return 0
            if not isinstance(data, dict):
                return 0
            restored = 0
            with self._forward_resume_lock:
                for key_text, record in (data.get("relays") or {}).items():
                    try:
                        key = int(key_text)
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(record, dict):
                        continue
                    record = dict(record)
                    record["targets"] = [
                        target
                        for target in (_peer_from_json(t) for t in record.get("targets") or [])
                        if target
                    ]
                    self._forward_resume_records.setdefault(key, record)
                    restored += 1
            for key, store in (
                ("download", self.file_recv_progress),
                ("upload", self.file_upload_progress),
            ):
                for fid_text, row in (data.get(key) or {}).items():
                    try:
                        fid = int(fid_text)
                        entry = [int(row[0]), int(row[1]), None, row[3], _peer_from_json(row[4])]
                    except (TypeError, ValueError, IndexError, KeyError):
                        continue
                    if fid in store or fid in self._forward_resume_records:
                        continue  # a live transfer owns it, or a relay drives it
                    store[fid] = entry
                    restored += 1
            return restored

    def _mark_transfer_pause(self, fid, paused, peer=None):
        """Set or clear one transfer's pause switch (see `pause_trans`).

        Args:
            fid (int): Value used by the operation.
            paused (bool): Value used by the operation.
            peer (tuple): Value used by the operation.

        Returns:
        bool: True when a running outbound leg was found.
        """
        with self._transfer_progress_lock:
            live = fid in self._active_transfer_fids
        if live and peer is not None:
            entry = self.file_upload_progress.get(fid)
            if entry is not None and entry[4] is not None and tuple(entry[4]) != tuple(peer):
                return False  # that leg belongs to another peer
        with self._transfer_pause_cond:
            self._transfer_pause[fid] = paused
            self._transfer_pause_cond.notify_all()
        return live

    def pause_trans(self, fid, peer=None):
        """Pause an outbound transfer of this instance.

        The sending leg with this fid stops at its next chunk and stays open
        until `start_trans` (or the peer's ``/start_trans``) resumes it. This
        covers every outgoing transfer — a plain upload as much as a forward
        upload — and is what the ``/pause_trans`` line does when it arrives
        from a peer.

        Args:
            fid (int): Transfer id the outbound leg is keyed by.
            peer (tuple | None): Restrict the pause to the leg whose original
                receiver is this ``(ip, port)``; None pauses whichever leg
                holds the fid.

        Returns:
            bool: True when a running leg was marked paused; False when
                nothing matched — the switch is still armed, so a leg started
                later under this fid starts paused.
        """
        return self._mark_transfer_pause(fid, True, peer)

    def start_trans(self, fid, peer=None):
        """Resume an outbound transfer paused with `pause_trans`.

        Args:
            fid (int): Transfer id the outbound leg is keyed by.
            peer (tuple | None): Restrict the resume to the leg whose original
                receiver is this ``(ip, port)``; None addresses every match.

        Returns:
            bool: True when a running leg was resumed.
        """
        return self._mark_transfer_pause(fid, False, peer)

    def pause_trans_remote(self, peer, fid):
        """Ask the server to pause its transfer toward this client.

        Sends the ``/pause_trans <fid>`` line the peers use for transfer flow
        control (a forwarded upload this client relays is throttled this way).

        Args:
            peer (tuple | None): The server's ``(ip, port)``; None accepts the
                connected server.
            fid (int): Transfer id of the server's outbound leg.

        Returns:
            bool: True when the line was written.
        """
        if peer is not None and tuple(peer) != (self.host, self.port):
            self._log(f"pause: {peer} is not the server this client is connected to")
            return False
        return self.send_message(self.client_socket, f"/pause_trans {fid}")

    def start_trans_remote(self, peer, fid):
        """Ask the server to resume a transfer paused earlier.

        Args:
            peer (tuple | None): The server's ``(ip, port)``; None accepts the
                connected server.
            fid (int): Transfer id of the server's outbound leg.

        Returns:
            bool: True when the line was written.
        """
        if peer is not None and tuple(peer) != (self.host, self.port):
            self._log(f"start: {peer} is not the server this client is connected to")
            return False
        return self.send_message(self.client_socket, f"/start_trans {fid}")

    def _register_transfer_socket(self, fid, sock):
        """Record the socket of a leg, so a dead session can be cleared out.

        Args:
            fid (int): Value used by the operation.
            sock (object): Value used by the operation.
        """
        if fid is None:
            return
        with self._transfer_progress_lock:
            self._active_transfer_sockets[fid] = sock

    def _release_transfer_socket(self, fid, sock):
        """Forget a leg's socket once the leg is over (its own, not a newer one's).

        Args:
            fid (int): Value used by the operation.
            sock (object): Value used by the operation.
        """
        if fid is None:
            return
        with self._transfer_progress_lock:
            if self._active_transfer_sockets.get(fid) is sock:
                del self._active_transfer_sockets[fid]

    def _drop_stale_transfers(self, peer=None):
        """Close the legs a peer's previous session left behind.

        A peer that (re)connects proves that the legs of its earlier session
                are over: their sockets are shut down so the stored transfers can be
                resumed without two legs writing the same transfer.

        Args:
            peer (tuple | None): Restrict to the legs whose progress entry names this ``(ip, port)``; None clears every leg of the instance (a fresh session replaces all of them).

        Returns:
        int: Number of legs closed.
        """
        stale = []
        for store in (self.file_upload_progress, self.file_recv_progress):
            for fid, entry in list(store.items()):
                if peer is not None and (entry[4] is None or tuple(entry[4]) != tuple(peer)):
                    continue
                with self._transfer_progress_lock:
                    sock = self._active_transfer_sockets.pop(fid, None)
                    if sock is not None:
                        self._active_transfer_fids.discard(fid)
                if sock is not None:
                    stale.append(sock)
        for sock in stale:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass  # already gone
            try:
                sock.close()
            except OSError:
                pass
        return len(stale)

    def _wait_transfer_paused(self, fid):
        """Block a sending leg while its fid is marked paused.

        Runs before every chunk so ``/pause_trans`` stops the stream without
                closing the transfer; the wait ends when the leg is resumed, when the
                instance stops, or when the fid is not paused at all.

        Args:
            fid (int): Value used by the operation.
        """
        if fid is None:
            return
        with self._transfer_pause_cond:
            while self._transfer_pause.get(fid, False):
                if not self.running:
                    return
                self._transfer_pause_cond.wait(timeout=0.5)

    def _wait_transfer_server_port(self, fid, timeout=RESUME_PORT_TIMEOUT):
        """Wait for the peer's ``/server_file_transfer_port`` of one fid.

        Args:
            fid (int): Transfer id the peer announces its listener under.
            timeout (float): Seconds to wait.

        Returns:
        tuple | None: ``(port, stamp)`` announced for this fid, or None
                        when the announcement does not arrive in time.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.file_transfer_server_port_lock:
                for info in reversed(list(self.file_server_port_list)):
                    if info[1] == fid:
                        # the newest announcement wins: an older one may belong
                        # to a leg of a session that is already gone
                        self.file_server_port_list.remove(info)
                        stamp = info[2] if len(info) > 2 else "plain"
                        return info[0], stamp
            time.sleep(0.1)
        return None

    def _forget_transfer_port(self, fid):
        """Drop pending port announcements of a fid an older session left behind.

        Args:
            fid (int): Value used by the operation.
        """
        if fid is None:
            return
        with self.file_transfer_server_port_lock:
            self.file_server_port_list[:] = [
                info for info in self.file_server_port_list if info[1] != fid
            ]

    def _wait_control_secure(self, control_socket, timeout=RESUME_SECURE_TIMEOUT):
        """Wait until a control channel may carry framed protocol lines.

        The reconnect flows run right after a session is established, when the
                crypto handshake may still be completing; a resume line sent then would
                travel plaintext and be dropped by the peer's framing guard. Without
                encryption there is nothing to wait for.

        Args:
            control_socket (socket.socket): Value used by the operation.
            timeout (float): Value used by the operation.

        Returns:
        bool: True when the channel speaks frames (or encryption is off).
        """
        if not self.is_enable_encrypto or self.crypto is None:
            return True
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._crypto_lock:
                channel = self._channels.get(control_socket)
            if channel is not None and channel.secure:
                return True
            time.sleep(0.05)
        self._log("resume: control channel not framed yet, transfers kept for the next reconnect")
        return False

    def alloc_port(self, port_add_step, port_range_num):
        """Reserve this client's port range under the cross-process lock.

        No-op until the server assigns a range (see ``/client_alloc_port_range``).

        Args:
            port_add_step (int): Step between candidate ports.
            port_range_num (int): Number of ports per step.
        """
        if self.is_hand_alloc_port == True:
            while self.is_client_port_temp_info_file_locked():
                time.sleep(0.1)
            self.client_port_temp_info_file_lock()
            try:
                self.hand_alloc_port(port_add_step, port_range_num)
            finally:
                # a failing allocation must not leave the lock behind: every
                # later instance would wait for it forever
                self.client_port_temp_info_file_unlock()

    def free_port(self):
        """Release this client's reserved port range.

        No-op unless a range was assigned (``is_hand_alloc_port`` True).
        """
        if self.is_hand_alloc_port == True:
            while self.is_client_port_temp_info_file_locked():
                time.sleep(0.1)
            self.client_port_temp_info_file_lock()
            try:
                self.hand_free_port()
            finally:
                self.client_port_temp_info_file_unlock()

    def client_port_temp_info_file_lock(self):
        """Create the lock file that reserves the client port range for this process."""
        with open(self.client_port_lock_file, "w", encoding="utf-8") as f:
            f.write("locked")

    def is_client_port_temp_info_file_locked(self):
        """Report whether the client port range is reserved by some process.

        Returns:
            bool: True while the lock file exists.
        """
        if os.path.exists(self.client_port_lock_file):
            return True
        else:
            return False

    def client_port_temp_info_file_unlock(self):
        """Remove the lock file that reserves the client port range."""
        if os.path.exists(self.client_port_lock_file):
            os.remove(self.client_port_lock_file)

    def hand_alloc_port(self, port_add_step, port_range_num):
        """Allocate the next free client port range and record it on disk.

        ``port`` is moved past the ranges already recorded by other clients on this
        host, so each instance ends up with a range of its own.

        Args:
            port_add_step (int): Step between candidate ports.
            port_range_num (int): Number of ports per step.

        Raises:
            OSError: If the client port info file cannot be read or written.
        """
        self.port_temp_info_path = os.path.join(self.project_temp_info_dir, "clients_port_info.log")
        server_port_temp_info_file_path = os.path.join(
            self.project_temp_info_dir, "server_port_info.log"
        )
        if os.path.exists(server_port_temp_info_file_path) == True:
            self._log(
                "Warning: server port info file exists, means the server has already allocated a port, may cause port conflict!"
            )
        self.port_add_step = port_add_step
        self.port_range_num = port_range_num
        self.add_latest_port = self.port + 1
        self.minus_latest_port = self.port
        self.alloc_add_port_lock = threading.Lock()
        self.alloc_minus_port_lock = threading.Lock()
        if os.path.exists(self.port_temp_info_path) == False:
            self.client_num = 0
            self.client_port_info = []
            self.min_port = self.port - self.port_add_step * self.port_range_num
            self.max_port = self.port + 1 + self.port_add_step * self.port_range_num
            each_client_info = {
                "client_id": self.client_num,
                "host": self.host,
                "port": self.port,
                "min_port": self.min_port,
                "max_port": self.max_port,
                "is_running": True,
                "pid": os.getpid(),  # the sweep reclaims the range once it is gone
                "started_at": time.time(),
            }
            self.client_port_info.append(each_client_info)
            with open(self.port_temp_info_path, "w", encoding="utf-8") as f:
                f.write(str(self.client_port_info))
        else:
            with open(self.port_temp_info_path, "r", encoding="utf-8") as f:
                self.client_port_info = ast.literal_eval(f.read())
            # drop the entries whose owning process is gone (see the server twin)
            self.client_port_info = [
                entry for entry in self.client_port_info if not _port_record_stale(entry)
            ]
            if not self.client_port_info:  # nobody holds a range: start over
                os.remove(self.port_temp_info_path)
                self.hand_alloc_port(port_add_step, port_range_num)
                return
            self.client_num = self.client_port_info[len(self.client_port_info) - 1]["client_id"] + 1
            auto_port_add = (
                self.client_port_info[len(self.client_port_info) - 1]["max_port"]
                + self.port_add_step * self.port_range_num
                + 1
            )
            auto_port_minus = (
                self.client_port_info[len(self.client_port_info) - 1]["min_port"]
                - self.port_add_step * self.port_range_num
                - 1
            )
            if self.port > auto_port_minus and self.port < auto_port_add:
                self.port = auto_port_add
            self.min_port = self.port - self.port_add_step * self.port_range_num
            self.max_port = self.port + 1 + self.port_add_step * self.port_range_num
            each_client_info = {
                "client_id": self.client_num,
                "host": self.host,
                "port": self.port,
                "min_port": self.min_port,
                "max_port": self.max_port,
                # the client is connected while it holds the range: hand_free_port()
                # drops the entry on disconnect, and the pid lets a later
                # instance reclaim the range of one that was killed
                "is_running": True,
                "pid": os.getpid(),
                "started_at": time.time(),
            }
            self.client_port_info.append(each_client_info)
            with open(self.port_temp_info_path, "w", encoding="utf-8") as f:
                f.write(str(self.client_port_info))

    def hand_free_port(self):
        """Drop this client's entry from the on-disk port range record."""
        self.port_temp_info_path = os.path.join(self.project_temp_info_dir, "clients_port_info.log")
        if os.path.exists(self.port_temp_info_path):
            with open(self.port_temp_info_path, "r", encoding="utf-8") as f:
                self.client_port_info = ast.literal_eval(f.read())
            # rebuild instead of deleting by index: other live instances keep
            # their entries, so this one is not necessarily the last, and a
            # shrinking list would make the next index run past its end
            self.client_port_info = [
                entry for entry in self.client_port_info if entry["client_id"] != self.client_num
            ]
            if len(self.client_port_info) == 0:
                os.remove(self.port_temp_info_path)
            else:
                with open(self.port_temp_info_path, "w", encoding="utf-8") as f:
                    f.write(str(self.client_port_info))

    def palloc(self):
        """Allocate a port, waiting until one is free.

        Returns:
            int: Allocated port, or 0 when no allocation range was assigned.
        """
        alloc_port = 0
        while True:
            alloc_port = self.file_palloc()
            if alloc_port is not None:
                return alloc_port
            alloc_port = self.spy_palloc()
            if alloc_port is not None:
                return alloc_port
            # both directions are exhausted: wait for a port to come back
            time.sleep(0.1)

    def pfree(self, port):
        """Release a port obtained from `palloc`.

        Args:
            port (int): Port to release.
        """
        self.file_pfree(port)
        self.spy_pfree(port)

    def file_palloc(self):
        """Allocate the next port above the base, or the first free one in range.

        Returns:
            int: Allocated port; None when the upward range is exhausted; 0 when no
                allocation range was assigned.
        """
        if self.is_hand_alloc_port:
            with self.alloc_add_port_lock:
                if self.add_latest_port + self.port_add_step > self.max_port:
                    for step in range(self.port + 1, self.max_port, self.port_add_step):
                        if step in self.all_allocated_ports_list:
                            pass
                        else:
                            allocated_port = step
                            self.all_allocated_ports_list.append(allocated_port)
                            return allocated_port
                    return None
                self.add_latest_port += self.port_add_step
                self.all_allocated_ports_list.append(self.add_latest_port)
                return self.add_latest_port
        else:
            return 0

    def file_pfree(self, port):
        """Release a port obtained from `file_palloc` and step the cursor back.

        Args:
            port (int): Port to release. Ignored when allocation is disabled.
        """
        if self.is_hand_alloc_port:
            with self.alloc_add_port_lock:
                if port in self.all_allocated_ports_list:
                    self.all_allocated_ports_list.remove(port)
                    self._debug("releasing file transfer port, current latest port:", port)
                self.add_latest_port -= self.port_add_step
        else:
            pass

    def spy_palloc(self):
        """Allocate the next port below the base, or the first free one in range.

        Returns:
            int: Allocated port; None when the downward range is exhausted; 0 when
                no allocation range was assigned.
        """
        if self.is_hand_alloc_port:
            with self.alloc_minus_port_lock:
                if self.minus_latest_port - self.port_add_step < self.min_port:
                    for step in range(self.port, self.min_port, -self.port_add_step):
                        if step in self.all_allocated_ports_list:
                            pass
                        else:
                            allocated_port = step
                            self.all_allocated_ports_list.append(allocated_port)
                            return allocated_port
                    return None
                self.minus_latest_port -= self.port_add_step
                self.all_allocated_ports_list.append(self.minus_latest_port)
                return self.minus_latest_port
        else:
            return 0

    def spy_pfree(self, port):
        """Release a port obtained from `spy_palloc` and step the cursor back.

        Args:
            port (int): Port to release. Ignored when allocation is disabled.
        """
        if self.is_hand_alloc_port:
            with self.alloc_minus_port_lock:
                if port in self.all_allocated_ports_list:
                    self.all_allocated_ports_list.remove(port)
                    self._debug("releasing file transfer port, current latest port:", port)
                self.minus_latest_port += self.port_add_step
        else:
            pass

    def create_temporary_server(self, handler, port=None, max_connections=1):
        """Start a temporary listener for a side channel (not the main protocol).

        Args:
            handler (callable): ``handler(client_socket, address)`` started in its
                own thread for every accepted connection.
            port (int | None): Port to bind; None allocates one with `palloc`.
            max_connections (int): Listen backlog. Defaults to 1.

        Returns:
            tuple: ``(port, thread, stop_event)``; setting ``stop_event`` ends the
                loop, which closes the socket and frees the port.

        Raises:
            RuntimeError: If ``port`` is None and no port can be allocated.
        """
        if port is None:
            port = self.palloc()
            if port is None:
                raise RuntimeError("No available port for temporary server")
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind((self.client_host, port))
        server_socket.listen(max_connections)
        stop_event = threading.Event()

        def server_loop():
            """Handle the operation."""
            while not stop_event.is_set():
                try:
                    server_socket.settimeout(1.0)
                    client_sock, addr = server_socket.accept()
                    threading.Thread(target=handler, args=(client_sock, addr), daemon=True).start()
                except socket.timeout:
                    continue
                except Exception as e:
                    if not stop_event.is_set():
                        self._log(f"Temporary server error: {e}")
                    break
            server_socket.close()
            self.pfree(port)

        server_thread = threading.Thread(target=server_loop, daemon=True)
        server_thread.start()
        return port, server_thread, stop_event

    def create_temporary_client(self, server_host, server_port, bind_port=None, on_data=None):
        """Open a temporary outbound connection for a side channel.

        Args:
            server_host (str): Host to connect to.
            server_port (int): Port to connect to.
            bind_port (int | None): Local port to bind; None allocates one with
                `palloc`.
            on_data (callable | None): ``on_data(data, client_socket)`` called for
                every received chunk.

        Returns:
            tuple: ``(client_socket, thread, stop_event)``; setting ``stop_event``
                ends the receiver thread.
        """
        client_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if bind_port is not None:
            client_sock.bind((self.client_host, bind_port))
        else:
            bind_port = self.palloc()
            client_sock.bind((self.client_host, bind_port))
        client_sock.connect((server_host, server_port))
        stop_event = threading.Event()

        def receiver():
            """Handle the operation."""
            while not stop_event.is_set():
                try:
                    client_sock.settimeout(1.0)
                    data = self.receive_message(client_sock, self.recv_messages_chunk_len)
                    if not data:
                        break
                    if on_data:
                        on_data(data, client_sock)
                except socket.timeout:
                    continue
                except:
                    break
            client_sock.close()

        recv_thread = threading.Thread(target=receiver, daemon=True)
        recv_thread.start()
        return client_sock, recv_thread, stop_event

    def _crypto_negotiate_mode(self):
        """Announce our encryption mode and wait for the server's.

        Returns True only when both sides agree on ``is_enable_encrypto``.
                On a mismatch (or a missing announcement) the connection is
                closed and False is returned.

        Returns:
            bool: Result produced by the operation.
        """
        self.send_message(
            self.client_socket,
            f"/crypto_mode {1 if self.is_enable_encrypto else 0}",
            is_send_raw=True,
        )
        if not self._crypto_mode_event.wait(timeout=self._crypto_mode_timeout):
            self._log("crypto: server did not announce its encryption mode, disconnecting")
            self.close()
            return False
        if not self._crypto_mode_ok:
            self.close()  # mismatch: the receive thread signalled the refusal; close idempotently
            return False
        return True

    def connect(self):  # connect to server
        """Connect to the server and start reading from it.

        Binds ``client_port`` when one was configured, then retries while
        ``is_wait_server`` is True and the server is not reachable yet. Once the
        socket is up the receive thread is started and the encryption mode is
        negotiated, which closes the connection when the two sides disagree.

        Returns:
            bool: True when the connection is established (and, if encryption is
                enabled, the key exchange has been started); False when the attempt
                failed or the mode negotiation closed the connection.
        """
        while True:
            try:
                self.client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.client_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if self.timeout != None:
                    self.client_socket.settimeout(self.timeout)  # connect over 5 seconds timeout
                else:
                    self.client_socket.settimeout(5)
                self._log(f"connecting to {self.host}:{self.port}...")
                if self.client_port == None:
                    pass
                else:
                    self.local_address = (self.client_host, self.client_port)
                    try:
                        self.client_socket.bind(self.local_address)
                    except OSError as e:
                        # the configured port is not ours to take right now (a
                        # session that just ended can keep it, in TIME_WAIT on
                        # Windows): connect anyway, from an OS-assigned port.
                        # The interrupted transfers of the previous session
                        # are matched by address, so they stay stored until a
                        # reconnect that can use the configured port again.
                        self._log(
                            f"client port {self.client_port} is not available ({e}); "
                            f"connecting from an OS-assigned port instead"
                        )
                        self.local_address = None
                self.client_socket.connect((self.host, self.port))
                if self._stopped:
                    # close() arrived while the socket was connecting: stay
                    # closed instead of reviving the connection
                    self.client_socket.close()
                    self.client_socket = None
                    return False
                self.running = True
                self._control_sockets.add(self.client_socket)
                # this client's only control link is the server: the socket is
                # up, so every leg still registered belongs to the previous
                # session and is over. The sweep runs before the receive thread
                # starts, so a resume request of the new session cannot be
                # affected by it.
                self._drop_stale_transfers()
                self.receive_thread = threading.Thread(
                    target=self.receive_messages
                )  # set up get msg thread
                with self._crypto_lock:  # reset before the recv thread starts
                    self._crypto_mode_event.clear()
                    self._crypto_mode_ok = False
                self.receive_thread.daemon = True
                if self.is_enable_encrypto and self.crypto is not None:
                    session = self._crypto_session_ensure()
                    channel = _SecureChannel(
                        self.client_socket,
                        session,
                        self._log,
                        self._debug,
                        send_lock=self._crypto_send_lock,
                        on_disable=lambda: self._crypto_drop_secure(self.client_socket),
                    )
                    with self._crypto_lock:
                        self._channels[self.client_socket] = channel
                self.receive_thread.start()
                if not self._crypto_negotiate_mode():
                    return False
                if self.is_enable_encrypto and self.crypto is not None:
                    with self._crypto_lock:
                        self._crypto_decode_failures = 0  # fresh connection, fresh breaker
                    self._crypto_start_exchange()
                self._log("connect success! type '/help' to get help.\n")
                # a (re)connected session carries the transfers that died with
                # the previous one: continue them from what is already on disk
                threading.Thread(
                    target=self._request_resumes, daemon=True
                ).start()
                return True
            except socket.timeout:
                if self.is_wait_server:
                    self._log("waiting for server to start...")
                    pass
                else:
                    self._log("timeout, unable to connect to server")
                    self._log_exc()
                    return False
            except ConnectionRefusedError:
                if self.is_wait_server:
                    self._log("waiting for server to start...")
                    pass
                else:
                    self._log("connection rejected by server, please ensure the server is running")
                    self._log_exc()
                    return False
            except Exception as e:
                self._log(f"connection error: {e}")
                self._log_exc()
                return False

    def receive_messages(self):  # get server msg
        """Read from the server until the connection ends.

        Runs on the receive thread: every inbound line is reported to the message
        listeners (commands included); a plain message is also stored in
        ``messages_dict``, while a ``/send_msg_from`` envelope is reported and
        stored as its payload attributed to the forwarding client, and other ``/``
        lines additionally go to `handle_server_command`. Any end of the
        connection clears ``running`` and releases the port range.
        """
        buffer = ""
        while self.running:
            try:
                data = self.receive_message(self.client_socket, self.recv_messages_chunk_len)
                if not data:
                    self._log("\nbreak the connection from server")
                    self.running = False
                    self.free_port()
                    # the transfers running on this session died with it: keep
                    # them on disk for the next connect (_request_resumes)
                    self._persist_transfer_progress()
                    break
                buffer += data.decode("utf-8")
                while "\n" in buffer:  # deal with multiple messages in buffer
                    line, buffer = buffer.split("\n", 1)
                    message = line.strip()
                    if not message:
                        continue
                    if not _crypto_is_protocol_line(message):
                        # genuine peer traffic: reset the streak that feeds the breaker
                        self._crypto_note_healthy(self.client_socket)
                    sender = None  # the author's "ip:port"; None for direct server pushes
                    if message.startswith("/"):
                        # A /send_msg_from envelope is a message that another
                        # client forwarded to us: its records belong to the
                        # originator, whose socket exists only on the server, so
                        # the originator's address is the store key here. The
                        # payload is reported and stored below like any inbound
                        # message; the envelope itself is not dispatched.
                        unwrapped = parse_forwarded_message(message)
                        if unwrapped is not None:
                            raw = message
                            sender, message = unwrapped
                            self._record_event(sender, raw)
                            if not message:
                                continue
                        else:
                            # a command from the server: reported to the listeners
                            # like every inbound line, then handled
                            self._record_event(self.client_socket, message)
                            self._notify_message_received(sender, message)
                            self.handle_server_command(message)
                            self._log(f"\n[server] {message}")
                            continue
                    self._notify_message_received(sender, message)
                    if not message.startswith("/"):
                        self._record_message(sender or self.client_socket, message)
                    self._log(f"\n[server] {message}")
            except socket.timeout:
                continue
            except ConnectionResetError:
                self._log("\nReset by server, connection closed")
                self._log_exc()
                self.running = False
                self.free_port()
                self._persist_transfer_progress()
                break
            except Exception as e:
                if not _is_closed_socket_error(e):
                    self._log(f"\nget msg error: {e}")
                    self._log_exc()
                self.running = False
                self.free_port()
                self._persist_transfer_progress()
                break

    def send_message(self, client_socket, message, is_send_raw=False):
        # send msg to server
        """Write one line to a socket, encrypting when the channel is up.

        Args:
            client_socket (socket.socket): Target connection; the client passes
                ``self.client_socket``.
            message (str | bytes): Payload; a str is stripped and newline
                terminated, bytes are sent as they are.
            is_send_raw (bool): Write the payload as plaintext even when the channel
                is encrypted; only for the crypto handshake and transfer coordination lines.

        Returns:
            bool: True when the payload was written; False when the client is not
                running, no socket was passed, or the payload type is unsupported.
        """
        if is_send_raw:
            deal_msg = message.strip()
            if not deal_msg.endswith("\n"):
                deal_msg += "\n"
            encoded = deal_msg.encode("utf-8")
            client_socket.sendall(encoded)
            self._measure_network_speed(encoded, is_upload=True)
            return True
        written = self._write_message(client_socket, message)
        if written:
            self._after_send(client_socket, message)
        return written

    def _write_message(self, client_socket, message):  # noqa: PLR0911 - same exits as the old send_message body
        """Write one line: encrypted when the channel is up, plaintext otherwise.

        Returns:
            bool: True when written; False when the client is not running, no
                socket was passed, or the payload type is unsupported.
        """
        if not self.running or not self.client_socket:
            self._log("disable the connect to server")
            return False
        with self._crypto_lock:
            channel = self._channels.get(client_socket)
        if channel is not None and channel.secure:
            try:
                if not isinstance(message, (str, bytes)):
                    self._log(f"Unsupported message type: {type(message)}")
                    return False
                payload = message.strip() + "\n" if isinstance(message, str) else message
                channel.send(payload)
                self._measure_network_speed(payload, is_upload=True)
                return True
            except Exception as e:
                if not _is_closed_socket_error(e):
                    self._log(f"send msg error: {e}")
                    self._log_exc()
                return False
        with self._crypto_send_lock:  # plaintext writes stay ordered (no-GIL safe)
            try:  # add newline character for server to distinguish messages
                if isinstance(message, str):
                    deal_msg = message.strip()
                    if not deal_msg.endswith("\n"):
                        deal_msg += "\n"
                    data = deal_msg.encode("utf-8")
                elif isinstance(message, bytes):
                    data = message
                else:
                    self._log(f"Unsupported message type: {type(message)}")
                    return False
                client_socket.sendall(data)
                self._measure_network_speed(data, is_upload=True)
                return True
            except Exception as e:
                if not _is_closed_socket_error(e):
                    self._log(f"send msg error: {e}")
                    self._log_exc()
                return False

    def _after_send(self, client_socket, message):
        """Record and report one payload that `_write_message` has written."""
        if isinstance(message, str):
            message = message.strip()
            if message and client_socket in self._control_sockets:
                self._record_sent(client_socket, message)
        self._notify_message_sent(client_socket, message)

    def send_message_to_server(self, message):
        """Send the payload of a console line to the server.

        Args:
            message (str): Console line such as ``/send_msg hello``; the first token
                (the command name) is dropped and the second one is sent.

        Raises:
            IndexError: If the line has fewer than two tokens.
        """
        self.send_message(self.client_socket, shlex.split(message)[1])

    def receive_message(self, client_socket, msg_length):  # receive msg
        """Read up to ``msg_length`` bytes from a socket.

        A socket speaking frames (see `_SecureChannel`) returns decrypted
        plaintext, served from the channel's buffer; a socket without one is
        read raw. A read that started while the channel was raw is classified
        by the channel when it returns, so bytes of a flip that landed during
        the read never land in the line reader.

        Args:
            client_socket (socket.socket): Connection to read from.
            msg_length (int): Maximum number of bytes to read.

        Returns:
            bytes: Received bytes, empty when the peer closed the connection.
        """
        while True:
            with self._crypto_lock:
                channel = self._channels.get(client_socket)
            if channel is None:
                data = client_socket.recv(msg_length)
                self._measure_network_speed(data, is_upload=False)
                self._notify_transfer_chunk(client_socket, data)
                return data
            data = channel.take(msg_length)
            if data:
                self._measure_network_speed(data, is_upload=False)
                self._notify_transfer_chunk(client_socket, data)
                return data  # buffered plaintext is served even after a disable
            raw = client_socket.recv(self.recv_messages_chunk_len if channel.secure else msg_length)
            if not raw:
                return b""
            try:
                plain = channel.feed(raw)
            except _FrameError as exc:
                self._log(f"crypto: {exc}")
                self._crypto_channel_error(client_socket)
                leftover = channel.drain_plaintext()
                if leftover:
                    self._measure_network_speed(leftover, is_upload=False)
                    self._notify_transfer_chunk(client_socket, leftover)
                    return leftover  # hand back whatever was already read
                continue  # framing is off now: keep serving in raw mode
            if plain:
                self._measure_network_speed(plain, is_upload=False)
                self._notify_transfer_chunk(client_socket, plain)
                return plain  # plaintext mode: the line reader gets the bytes

    def _crypto_mark_encrypted(self, client_socket, peer_role, peer_pem_path):
        """Handle the operation.

        Args:
            client_socket (socket.socket): Value used by the operation.
            peer_role (object): Value used by the operation.
            peer_pem_path (str): Value used by the operation.
        """
        with self._crypto_lock:
            self._encrypted_sockets.add(client_socket)
            self._crypto_peer[client_socket] = (peer_role, peer_pem_path)

    def _crypto_session_ensure(self):
        """Get or create the client's crypto session.

        The client's crypto session context (created on demand).

        Returns:
            object: Result produced by the operation.
        """
        with self._crypto_lock:
            if self._crypto_session is None and self.crypto is not None:
                self._crypto_session = _CryptoSession(self.crypto, "client", self._log, self._debug)
            return self._crypto_session

    def _crypto_control_state(self, client_socket):
        """Return (session, channel) of one socket; (None, None) when unknown.

        (session, channel) of one socket; (None, None) when unknown.

        Args:
            client_socket (socket.socket): Value used by the operation.

        Returns:
            list | tuple: Result produced by the operation.
        """
        with self._crypto_lock:
            return self._crypto_session, self._channels.get(client_socket)

    def _crypto_channel_error(self, client_socket):
        """Handle a frame decode failure on a control or transfer socket.

        A frame failed to decode: re-exchange on the control socket, close a

        transfer socket (its leg has no handshake to recover with).

        Args:
            client_socket (socket.socket): Value used by the operation.
        """
        if client_socket is self.client_socket:
            self._crypto_on_decode_failure(client_socket)
            return
        self._log("crypto: transfer frame error, closing the transfer connection")
        try:
            client_socket.close()
        except Exception:
            pass

    def _crypto_after_flip(self, client_socket):
        """Announce frame readiness (plaintext) and publish the session ECDH key.

        Args:
            client_socket (socket.socket): Value used by the operation.
        """
        _session, channel = self._crypto_control_state(client_socket)
        if channel is None:
            return
        try:
            # plaintext by design: the peer needs this line before it may frame
            self.send_message(client_socket, CRYPTO_FRAMES_START, is_send_raw=True)
        except Exception:
            self._log_exc()
        self._crypto_publish_ecdh_pub(client_socket)

    def _crypto_publish_ecdh_pub(self, client_socket):
        """Send our session ECDH public key once frames are allowed both ways.

        Args:
            client_socket (socket.socket): Value used by the operation.
        """
        session, channel = self._crypto_control_state(client_socket)
        if session is not None:
            session.publish_pub(channel)

    def _crypto_transfer_stamp(self, control_socket, force_plain=False):
        """Mode+nonce a listener announces to a transfer connector.

        Args:
            control_socket (socket.socket): Value used by the operation.
            force_plain (bool): Value used by the operation.

        Returns:
            object | str: Result produced by the operation.
        """
        session, channel = self._crypto_control_state(control_socket)
        if session is None:
            return "plain"
        return session.transfer_stamp(channel, force_plain)

    def _crypto_attach_transfer_in(self, sock, control_socket, stamp):
        """Framing channel for an accepted transfer socket (listener side).

        Args:
            sock (object): Value used by the operation.
            control_socket (socket.socket): Value used by the operation.
            stamp (object): Value used by the operation.

        Returns:
            NoneType | object: Result produced by the operation.
        """
        session, control_channel = self._crypto_control_state(control_socket)
        if session is None:
            return None
        channel = session.attach_transfer(sock, control_channel, stamp)
        if channel is not None:
            with self._crypto_lock:
                self._channels[sock] = channel
                self._encrypted_sockets.add(sock)
        return channel

    def _crypto_drop_secure(self, client_socket):
        """Framing stopped: keep the secure-socket set and peer map truthful.

        Args:
            client_socket (socket.socket): Value used by the operation.
        """
        with self._crypto_lock:
            self._encrypted_sockets.discard(client_socket)
            self._crypto_peer.pop(client_socket, None)

    def _crypto_note_healthy(self, client_socket):
        """Reset the decode-failure count after a valid frame.

        A frame decoded cleanly: reset the streak that feeds the breaker.

        Args:
            client_socket (socket.socket): Value used by the operation.
        """
        with self._crypto_lock:
            self._crypto_decode_failures = 0

    def _crypto_attach_transfer_out(self, sock, control_socket, stamp):
        """Framing channel for a connected transfer socket (connector side).

        Args:
            sock (object): Value used by the operation.
            control_socket (socket.socket): Value used by the operation.
            stamp (object): Value used by the operation.

        Returns:
            object: Result produced by the operation.
        """
        session, control_channel = self._crypto_control_state(control_socket)
        crypto = _connector_transfer_crypto(session, control_channel, stamp)
        channel = _enable_connector_channel(sock, crypto)
        if channel is not None:
            with self._crypto_lock:
                self._channels[sock] = channel
                self._encrypted_sockets.add(sock)
        return channel

    def _crypto_drop_channel(self, sock):
        """Forget a finished transfer socket's channel.

        Args:
            sock (object): Value used by the operation.
        """
        with self._crypto_lock:
            self._channels.pop(sock, None)
            self._encrypted_sockets.discard(sock)

    def _crypto_on_decode_failure(self, client_socket):
        """Decryption failed (stale/wrong key): drop encryption, reload

        our own key and re-run the public-key exchange (forced).

                A circuit breaker closes the connection after
                ``MAX_DECODE_FAILURES`` consecutive failures, so a garbage or
                persistent-mismatch storm cannot spin the loop forever.

        Args:
            client_socket (socket.socket): Value used by the operation.
        """
        with self._crypto_lock:
            channel = self._channels.get(client_socket)
        if channel is not None:
            channel.disable()
        with self._crypto_lock:
            self._encrypted_sockets.discard(client_socket)
            self._crypto_peer.pop(client_socket, None)
            self._crypto_decode_failures += 1
            if self._crypto_decode_failures >= MAX_DECODE_FAILURES:
                do_close = True
                do_exchange = False
            else:
                do_close = False
                do_exchange = True
                self._crypto_my_nonce = self._crypto_fresh_nonce()
        if do_close:
            self._log("crypto: too many decode failures, closing connection")
            try:
                self.close()
            except Exception:
                self._log_exc()
            return
        if self.crypto is not None:
            try:
                self.crypto.reload_own_key()
            except Exception:
                self._log_exc()
        self._debug("crypto: decode failure, re-exchanging public keys")
        if do_exchange:
            try:
                threading.Thread(
                    target=self._crypto_exchange_thread, args=(True,), daemon=True
                ).start()
            except Exception:
                self._log_exc()

    def _crypto_start_exchange(self):
        """Start the client side of the public-key handshake (daemon)."""
        threading.Thread(target=self._crypto_exchange_thread, daemon=True).start()

    def _crypto_still_current(self, gen):
        """Return true while ``gen`` is still the latest handshake generation.

        True while ``gen`` is still the latest handshake generation.

        Args:
            gen (object): Value used by the operation.

        Returns:
            object: Result produced by the operation.
        """
        with self._crypto_lock:
            return self._crypto_handshake_gen == gen

    def _crypto_exchange_thread(self, force=False):
        """Drive the client half of the handshake to completion.

        Args:
            force (bool): Value used by the operation.

        Raises:
            TimeoutError: When the operation cannot complete.
        """
        try:
            self.crypto.ensure_keys()
            session = self._crypto_session_ensure()
            if session is not None:
                session.reset_ecdh()  # a fresh handshake is a fresh session key
            with self._crypto_lock:
                ack_event = threading.Event()
                done_event = threading.Event()
                pub_event = threading.Event()
                self._crypto_ack_event = ack_event
                self._crypto_exchange_done = done_event
                self._crypto_server_pub_event = pub_event
                self._crypto_requested_server_pub = False
                self._crypto_server_pub_ok = None
                self._crypto_server_pem_path = None
                self._crypto_my_nonce = self._crypto_fresh_nonce()
                self._crypto_handshake_started = True
                self._crypto_handshake_gen += 1
                gen = self._crypto_handshake_gen
                hello = f"/crypto_key_exchange {self._crypto_my_nonce}"
                if force:
                    hello += " 1"
            self.send_message(self.client_socket, hello, is_send_raw=True)
            if not ack_event.wait(timeout=30):
                if not self._crypto_still_current(gen):
                    return  # superseded by a newer handshake; that flow owns the connection
                raise TimeoutError("crypto handshake: no ack from server")
            if not done_event.wait(timeout=90):
                if not self._crypto_still_current(gen):
                    return
                raise TimeoutError("crypto handshake: key exchange did not complete")
            self._crypto_try_flip()  # readiness was announced by _wait_done; wait here until the socket actually flipped
            for _ in range(600):
                with self._crypto_lock:
                    flipped = self.client_socket in self._encrypted_sockets
                if flipped:
                    break
                time.sleep(0.1)
            else:
                if self._crypto_still_current(gen):
                    raise TimeoutError("crypto handshake: server /crypto_ready never arrived")
        except Exception as e:
            self._log(f"crypto handshake failed: {e}")
            self._log_exc()
            try:
                self.close()
            except Exception:
                pass

    def _crypto_on_server_hello(self, server_nonce, force):
        """Server initiated the handshake (re-exchange after a decode

        failure): record its session nonce, ack that we need its key
                (every connection re-exchanges), then run our half.

        Args:
            server_nonce (object): Value used by the operation.
            force (bool): Value used by the operation.
        """
        with self._crypto_lock:
            self._crypto_force = force
            self._crypto_server_nonce = server_nonce
            self._crypto_handshake_started = True
            self._crypto_my_nonce = self._crypto_fresh_nonce()
            self._crypto_handshake_gen += 1
            my_nonce = self._crypto_my_nonce
        session = self._crypto_session_ensure()
        if session is not None:
            session.reset_ecdh()  # a re-exchange is a fresh session key
        ack = f"/crypto_key_exchange_ack {my_nonce} 1 {1 if force else 0}"
        self.send_message(self.client_socket, ack, is_send_raw=True)
        self._crypto_after_ack("1" if force else "0", force)

    def _crypto_try_flip(self):
        """Flip to framed encryption only when the server key passed the

        TOFU check and both sides announced readiness (no plaintext/framed
                interleaving).
        """
        with self._crypto_send_lock:  # no send may interleave with the flip
            with self._crypto_lock:
                if not (
                    self._crypto_server_ready
                    and self._crypto_own_ready_sent
                    and self._crypto_server_pub_ok is True
                    and self._crypto_server_pem_path
                    and self.client_socket not in self._encrypted_sockets
                ):
                    return
                self._crypto_mark_encrypted(
                    self.client_socket, "server", self._crypto_server_pem_path
                )
                channel = self._channels.get(self.client_socket)
                my_nonce = self._crypto_my_nonce
                peer_nonce = self._crypto_server_nonce
                peer_pem_path = self._crypto_server_pem_path
        if channel is not None:
            channel.enable(my_nonce, peer_nonce, peer_pem_path)
        threading.Thread(
            target=self._crypto_after_flip, args=(self.client_socket,), daemon=True
        ).start()

    @staticmethod
    def _crypto_fresh_nonce():
        """Handle the operation.

        Returns:
            object: Result produced by the operation.
        """
        return secrets.token_hex(16)

    def _crypto_after_ack(self, server_needs, force):
        """Server replied to our hello: push our key and request the

        server's key (if it needs us to), then signal the exchange
                thread. Runs under ``_crypto_lock`` so a concurrent handshake
                cannot interleave (no-GIL safe).

        Args:
            server_needs (object): Value used by the operation.
            force (bool): Value used by the operation.
        """
        with self._crypto_lock:
            gen = self._crypto_handshake_gen
            need_push = force or server_needs == "1"
            push_thread = None
            if need_push:
                push_thread = threading.Thread(target=self._crypto_push_pub_key, daemon=True)
                push_thread.start()
            if not self._crypto_requested_server_pub:
                self._crypto_requested_server_pub = True
                request_pub = True
            else:
                request_pub = False
        if request_pub:
            self.send_message(self.client_socket, "/crypto_pub_key_request", is_send_raw=True)

        def _wait_done():
            """Handle the operation."""
            with self._crypto_lock:
                pub_event = self._crypto_server_pub_event
            if not pub_event.wait(timeout=90):
                self._log("crypto: waiting for server public key timed out")
                return
            if push_thread is not None:
                # the push is best-effort (its ack can lag behind a slow file
                # transfer): never gate our readiness on it
                push_thread.join(timeout=5)
            with self._crypto_lock:
                if self._crypto_handshake_gen != gen or self._crypto_server_pub_ok is not True:
                    stale = True
                else:
                    stale = False
                    self._crypto_own_ready_sent = True  # announce readiness: flip only once the server's /crypto_ready arrived too; sent on every handshake path
            if stale:
                return
            try:
                self.send_message(self.client_socket, "/crypto_ready", is_send_raw=True)
            except OSError:
                pass  # connection already closed (reject/breaker): nothing to announce
            with self._crypto_lock:
                self._crypto_exchange_done.set()

        threading.Thread(target=_wait_done, daemon=True).start()
        with self._crypto_lock:
            self._crypto_ack_event.set()

    def _crypto_push_pub_key(self):
        """Push our public key file to the server via the file-transfer

        mechanism (client is the sender).

        Returns:
            bool: Result produced by the operation.
        """
        while (
            True
        ):  # serialise pushes: wait for a concurrent push (double handshake) instead of skipping it
            with self._crypto_lock:
                if self.client_socket not in self._crypto_push_active:
                    self._crypto_push_active.add(self.client_socket)
                    break
            time.sleep(0.05)
        try:
            with self.file_client_id_lock:
                client_id = copy.copy(self.file_client_id)
                self.file_client_id += 1
            send_msg = f"/crypto_pub_key {client_id}"
            self.send_message(self.client_socket, send_msg, is_send_raw=True)
            file_transfer_client_port = self.palloc()
            file_server_port = None
            waiting_time = 0
            while True:
                time.sleep(0.05)
                with self.file_transfer_server_port_lock:
                    for port_info in list(self.file_server_port_list):
                        if port_info[1] == client_id:
                            file_server_port = port_info[0]
                            self.file_server_port_list.remove(port_info)
                            break
                if file_server_port is not None:
                    break
                waiting_time += 1
                if waiting_time >= 200:
                    self._log("crypto: transfer port waiting timeout, public key push failed")
                    return False
            self.file_transfer_mode(  # a key push carries the keys themselves: always plaintext
                self.crypto.pub_path,
                self.host,
                file_server_port,
                file_transfer_client_port,
                control_socket=self.client_socket,
                crypto="plain",
            )
            self.pfree(file_transfer_client_port)
        except Exception:
            self._log_exc()
            return False
        finally:
            with self._crypto_lock:
                self._crypto_push_active.discard(self.client_socket)
        return True

    def _crypto_store_received_pub(self, full_path, peer_role, server_addr):
        """TOFU-check the server's public key received via the file

        transfer mechanism and cache it under the server endpoint.

                On failure the connection is rejected and closed.

        Args:
            full_path (str): Value used by the operation.
            peer_role (object): Value used by the operation.
            server_addr (tuple): Value used by the operation.
        """
        if self.crypto is None:
            return
        peer_ip, peer_port = server_addr
        try:
            with open(full_path, "r", encoding="utf-8") as f:
                pem = f.read()
        except OSError:
            pem = ""
        ok, reason = self.crypto.verify_peer_pub(peer_ip, peer_port, pem, peer_role)
        if ok:
            self.crypto.store_peer_pem(peer_role, peer_ip, peer_port, full_path)
            with self._crypto_lock:
                self._crypto_server_pem_path = self.crypto.peer_pem_path(
                    peer_role, peer_ip, peer_port
                )
                self._crypto_server_pub_ok = True
            self._log(f"crypto: accepted server public key ({reason})")
        else:
            with self._crypto_lock:
                self._crypto_server_pub_ok = False
            self._log(f"crypto: REJECTED server public key: {reason}")
            try:
                os.remove(full_path)  # rejected key: do not leave it in received_files/
            except OSError:
                pass
            try:
                self.send_message(self.client_socket, f"/crypto_reject {reason}", is_send_raw=True)
            except Exception:
                self._log_exc()
            try:
                self.close()
            except Exception:
                self._log_exc()
            return
        with self._crypto_lock:
            self._crypto_server_pub_event.set()

    def handle_server_command(self, command):  # deal with special command from server
        """Dispatch one command line pushed by the server.

        Handles the protocol's own lines: ``/crypto_mode`` (a mismatch closes the
        connection), ``/client_alloc_port_range``, the ``/crypto_*`` exchange lines,
        and the transfer lines ``/file``, ``/file_folder``,
        ``/server_file_transfer_port``, ``/forward_upload``, ``/pause_trans``,
        ``/start_trans``, ``/resume_trans``, ``/forward_resume`` and
        ``/forward_error``. Any other name goes to the handlers registered for
        the "server" side via `register_command`; an unknown command is only
        reported on the console.

        Args:
            command (str): Line including its leading ``/``.
        """
        client_id = f"{self.client_host}:{self.client_port}"
        if command.lower().split(" ")[0] == "/crypto_mode":
            try:
                peer_mode = int(command.split(" ")[1])
            except (IndexError, ValueError):
                peer_mode = -1
            expected = 1 if self.is_enable_encrypto else 0
            with self._crypto_lock:
                self._crypto_mode_ok = peer_mode == expected
                self._crypto_mode_event.set()
            if not self._crypto_mode_ok:
                self._log(
                    f"crypto: encryption mode mismatch with server "
                    f"(server={peer_mode}, client={expected}), disconnecting"
                )
                self.close()
            return
        if command.lower().split(" ")[0] == "/client_alloc_port_range":
            if command.lower().split(" ")[1] == "no_limit":
                self.is_hand_alloc_port = False
                self._log("server has no limit on client port allocation")
            else:
                self.is_hand_alloc_port = True
                self.each_client_port_range = int(command.split(" ")[1])
                self.alloc_port(self.port_add_step, self.each_client_port_range)
                self._log(
                    f"server allocated port range for each client: {self.each_client_port_range}"
                )
        elif shlex.split(command.lower())[0] == "/server_file_transfer_port":
            parts = command.split(" ")
            with self.file_transfer_server_port_lock:
                self.file_transfer_server_port = int(parts[1])
                try:
                    file_client_id = int(parts[2])
                    stamp = parts[3] if len(parts) > 3 else "plain"  # listener mode+nonce
                    self.file_server_port_list.append(
                        [self.file_transfer_server_port, file_client_id, stamp]
                    )
                except:
                    self._log_exc()
                    pass
        elif (
            self.is_enable_encrypto
            and self.crypto is not None
            and command.lower().split(" ")[0] == "/crypto_key_exchange"
        ):
            parts = command.split(" ")
            server_nonce = parts[1] if len(parts) > 1 else ""
            force = len(parts) > 2 and parts[2] == "1"
            self._crypto_on_server_hello(server_nonce, force)
        elif (
            self.is_enable_encrypto
            and self.crypto is not None
            and command.lower().split(" ")[0] == "/crypto_key_exchange_ack"
        ):
            parts = command.split(" ")
            if len(parts) < 3:
                return
            with self._crypto_lock:
                self._crypto_server_nonce = parts[1]
            server_needs = parts[2]
            force = len(parts) > 3 and parts[3] == "1"
            self._crypto_after_ack(server_needs, force)
        elif (
            self.is_enable_encrypto
            and self.crypto is not None
            and command.lower().split(" ")[0] == "/crypto_reject"
        ):
            reason = command[len("/crypto_reject") :].strip()
            self._log(f"crypto: connection rejected by server: {reason}")
            self.close()
        elif (
            self.is_enable_encrypto
            and self.crypto is not None
            and command.lower().split(" ")[0] == "/crypto_pub_key"
        ):
            with self._crypto_lock:
                handshake_started = self._crypto_handshake_started
            if not handshake_started:  # only accept key pushes while a handshake is in progress
                self._debug("crypto: ignoring /crypto_pub_key outside a handshake")
                return
            self.file_transfer_client_recv_server_start_thread(  # server pushes its key file; the receive hook TOFU-checks it
                client_id, self.client_socket, command
            )
        elif (
            self.is_enable_encrypto
            and self.crypto is not None
            and command.lower().split(" ")[0] == "/crypto_ready"
        ):
            with self._crypto_lock:
                self._crypto_server_ready = True
            self._crypto_try_flip()
        elif (
            self.is_enable_encrypto
            and self.crypto is not None
            and command.lower().split(" ")[0] == CRYPTO_FRAMES_START
        ):
            _session, channel = self._crypto_control_state(self.client_socket)
            if channel is not None:
                channel.note_peer_frames_ready()
                self._crypto_publish_ecdh_pub(self.client_socket)
        elif (
            self.is_enable_encrypto
            and self.crypto is not None
            and command.lower().split(" ")[0] == CRYPTO_ECDH_PUB
        ):
            # only ever accepted inside an RSA frame (see _PLAINTEXT_LINE_PREFIXES)
            parts = command.split(" ", 1)
            session = self._crypto_session_ensure()
            if session is not None and len(parts) > 1:
                if not session.set_peer_ecdh_b64(parts[1]):
                    self._log("crypto: rejected the server ECDH key")
        elif shlex.split(command.lower())[0] == "/file":
            self.file_transfer_client_recv_server_start_thread(
                client_id, self.client_socket, command
            )
        elif shlex.split(command.lower())[0] == "/file_folder":
            self.file_folder_transfer_client_recv_server_start_thread(
                command, client_id, self.client_socket
            )
        elif shlex.split(command.lower())[0] == "/forward_upload":
            try:
                parts = shlex.split(command)
                fid = int(parts[1])
                sport = int(parts[2])
            except (IndexError, ValueError):
                return
            stamp = parts[3] if len(parts) > 3 else "plain"  # upload leg's mode+nonce
            self._forward_upload_queue.put((fid, sport, stamp))
        elif shlex.split(command.lower())[0] == "/pause_trans":
            fid = _pause_command_fid(command)
            if fid is not None:
                self.pause_trans(fid)
        elif shlex.split(command.lower())[0] == "/start_trans":
            fid = _pause_command_fid(command)
            if fid is not None:
                self.start_trans(fid)
        elif shlex.split(command.lower())[0] == RESUME_LINE:
            parsed = parse_resume_command(command)
            if parsed is not None:
                threading.Thread(
                    target=self.resume_trans,
                    args=((self.host, self.port), parsed[0], parsed[1]),
                    daemon=True,
                ).start()
        elif shlex.split(command.lower())[0] == FORWARD_RESUME_LINE:
            parsed = parse_forward_resume_command(command)
            if parsed is not None:
                self._resume_forward(parsed[0], parsed[1], parsed[2])
        elif shlex.split(command.lower())[0] == "/forward_error":
            self._forward_error = command
        else:
            cmd_parts = shlex.split(command.strip())
            if not cmd_parts:
                return
            cmd_name = cmd_parts[0].lower()
            if cmd_name in self._custom_handlers[0]:
                handler = self._custom_handlers[0][cmd_name]
                run_in_thread = self._custom_handler_threaded[0].get(cmd_name, False)
                if run_in_thread:
                    self.submit_task(
                        self._execute_custom_handler,
                        handler,
                        command,
                        self.client_socket,
                        client_id,
                    )
                else:
                    self._execute_custom_handler(handler, command, self.client_socket, client_id)
            else:
                self._log(f"Unknown server command: {command}")

    def _console_forward_send_msg(self, command):
        """Client console entry point for ``/forward_send_msg`` (client-only).

        The forwarding command is only meaningful on a client: it parses the
                typed request and asks the server (which runs the same-named relay)
                to push each message to every listed destination.

        Args:
            command (object): Value used by the operation.

        Returns:
            NoneType | object: Result produced by the operation.
        """
        parts = shlex.split(command)
        items, addrs = parse_forward_items_and_addrs(parts[1:])
        if not items or not addrs:
            self._log(
                "forward_send_msg: need at least one message and one destination, "
                'e.g. /forward_send_msg "msg" "(\'127.0.0.1\', 3000)"'
            )
            return None
        return self.forward_messages(items, addrs)

    def forward_messages(self, messages, addrs):
        """Forward plain messages to other connected clients through the server.

        The console command ``/forward_send_msg`` uses this; the client must be
        connected. The server wraps each message in a ``/send_msg_from`` envelope so
        the receiving client can attribute it back to this one.

        Args:
            messages (list[str]): Message texts to forward.
            addrs (list[tuple]): Destination ``(ip, port)`` tuples.

        Returns:
            bool: True when the request was written to the server; False when the
                client is not connected.
        """
        request = "/forward_send_msg " + " ".join(shlex.quote(m) for m in messages)
        request += " " + " ".join(shlex.quote(str(a)) for a in addrs)
        return self.send_message(self.client_socket, request)

    def _execute_custom_handler(self, handler, command, client_socket=None, client_address=None):
        """Handle the operation.

        Args:
            handler (callable): Value used by the operation.
            command (object): Value used by the operation.
            client_socket (socket.socket): Value used by the operation.
            client_address (tuple): Value used by the operation.

        Returns:
            NoneType | object: Result produced by the operation.
        """
        try:
            result = handler(client_socket, client_address, command)
            if result is not None:
                if isinstance(result, str) and not result.endswith("\n"):
                    result += "\n"
                try:
                    self.send_message(client_socket, result)
                except Exception as e:
                    self._log(f"Error sending message: {e}")
                return result
            return None
        except Exception as e:
            error_msg = f"Error in custom command handler: {e}\n"
            self._log_exc()
            try:
                self.send_message(client_socket, error_msg)
            except Exception as e:
                self._log(f"Error sending error message: {e}")
            return error_msg

    def interactive_mode(self):  # Interactive mode
        """Read console lines and act on them until the client stops.

        ``/quit`` closes the connection; ``/send_msg``, ``/file``,
        ``/multiple_file``, ``/file_folder``, ``/multiple_file_folder``,
        ``/forward_file``, ``/forward_folder`` and ``/forward_send_msg`` are handled
        locally; any other name goes to the handlers registered with
        ``where_to_run="client"``, and anything left is sent to the server as it
        stands. Ctrl-C and EOF close the connection.
        """
        client_id = f"{self.client_host}:{self.client_port}"
        try:
            while self.running:
                try:  # get user input
                    message = input()
                    if not self.running:
                        break
                    if message.strip():
                        if message.lower() == "/quit":
                            self.send_message(self.client_socket, "/quit")
                            time.sleep(0.5)
                            break
                        elif shlex.split(message.lower())[0] == "/send_msg":
                            self.send_message_to_server(message)
                        elif shlex.split(message.lower())[0] == "/file":
                            self.file_transfer_client_recv_client_start_thread(message)
                        elif shlex.split(message.lower())[0] == "/multiple_file":
                            self.multiple_file_transfer_client_recv_client_start(message)
                        elif shlex.split(message.lower())[0] == "/file_folder":
                            self.folder_file_transfer_client_recv_client_start(message)
                        elif shlex.split(message.lower())[0] == "/multiple_file_folder":
                            self.multiple_folder_file_transfer_client_recv_client_start(message)
                        elif shlex.split(message.lower())[0] == "/forward_file":
                            self.forward_file_console(message)
                        elif shlex.split(message.lower())[0] == "/forward_folder":
                            self.forward_folder_console(message)
                        elif shlex.split(message.lower())[0] == "/forward_send_msg":
                            # client-only message forwarding: relayed by the server
                            self._console_forward_send_msg(message)
                        else:
                            # the registered name is the whole first token
                            # ("/mycmd"), not the line's first character
                            cmd_parts = shlex.split(message)
                            cmd_name = cmd_parts[0].lower() if cmd_parts else ""
                            if cmd_name in self._custom_handlers[1]:
                                handler = self._custom_handlers[1][cmd_name]
                                run_in_thread = self._custom_handler_threaded[1].get(
                                    cmd_name, False
                                )
                                if run_in_thread:
                                    self.submit_task(
                                        self._execute_custom_handler,
                                        handler,
                                        message,
                                        self.client_socket,
                                        client_id,
                                    )
                                else:
                                    self._execute_custom_handler(
                                        handler, message, self.client_socket, client_id
                                    )
                            else:
                                self.send_message(self.client_socket, message)
                                self._log(f"Unknown server command: {message}")
                except KeyboardInterrupt:
                    self.close()
                    self._log("\nshutting down...")
                    self._log_exc()
                    self.send_message(self.client_socket, "/quit")
                    time.sleep(0.5)
                    break
                except EOFError:
                    self.close()
                    self._log("\nshutting down...")
                    self._log_exc()
                    self.send_message(self.client_socket, "/quit")
                    time.sleep(0.5)
                    break
                except:
                    self._log_exc()
                    pass
        finally:
            self.close()

    def multiple_folder_file_transfer_client_recv_client_start(self, message):
        """Start folder transfers for each path in a client command.

        Args:
            message (object): Value used by the operation.
        """
        transfer_folder_file_list = shlex.split(message)[1:]
        destination_path = None
        if len(transfer_folder_file_list) >= 2 and not os.path.isdir(transfer_folder_file_list[-1]):
            destination_path = transfer_folder_file_list.pop()
        for transfer_folder_file in transfer_folder_file_list:
            command = "/file_folder {}".format(shlex.quote(transfer_folder_file))
            if destination_path:
                command += " {}".format(shlex.quote(destination_path))
            folder_file_transfer_client_recv_client_start_thread = threading.Thread(
                target=self.folder_file_transfer_client_recv_client_start,
                args=(command,),
                daemon=True,
            )
            folder_file_transfer_client_recv_client_start_thread.start()

    def folder_file_transfer_client_recv_client_start(self, message):
        """Send a folder's contents to the connected server.

        Args:
            message (object): Value used by the operation.
        """
        command_part = shlex.split(message)
        folder_path = command_part[1]
        destination_path = command_part[2] if len(command_part) >= 3 else None
        if os.path.isdir(folder_path) == False:
            self._log(f"{folder_path} is not a valid folder path")
            return False
        base_path = os.path.dirname(folder_path)

        def get_relative_path(base_path, abs_path):
            """Handle the operation.

            Args:
                base_path (str): Value used by the operation.
                abs_path (str): Value used by the operation.
            """
            base = os.path.normpath(base_path)
            abs_ = os.path.normpath(abs_path)
            common = os.path.commonpath([base, abs_])
            if common != base:
                raise ValueError(f"'{abs_path}' is not a subpath of '{base_path}'")
            rel = os.path.relpath(abs_, base)
            if rel == ".":
                return ""
            rel = rel.replace(os.sep, "/")
            return "/" + rel

        def send_folder_transfer_command(folder_path, file_name=None, abspath=None):
            """Handle the operation.

            Args:
                folder_path (str): Value used by the operation.
                file_name (str): Value used by the operation.
                abspath (str): Value used by the operation.
            """
            folder_transfer_command_message = "/file_folder {}".format(shlex.quote(folder_path))
            if file_name:
                each_file_transfer_command_message = "/file_folder {} {}".format(
                    shlex.quote(folder_path), shlex.quote(file_name)
                )
                if destination_path:
                    each_file_transfer_command_message += " {}".format(
                        shlex.quote(destination_path)
                    )
                self.file_transfer_client_recv_client_start_thread(
                    each_file_transfer_command_message, abspath
                )
                self._log(f"start to send folder command: {each_file_transfer_command_message}")
            else:
                if destination_path:
                    folder_transfer_command_message += " {}".format(shlex.quote(destination_path))
                self.send_message(self.client_socket, folder_transfer_command_message.strip())
                self._log(f"start to send folder command: {folder_transfer_command_message}")

        def start_file_transfer_with_limit(rel_dir, file, root):
            """Handle the operation.

            Args:
                rel_dir (str): Value used by the operation.
                file (str): Value used by the operation.
                root (object): Value used by the operation.
            """
            cmd = f"/file_folder {shlex.quote(rel_dir)} {shlex.quote(file)}"
            if destination_path:
                cmd += " {}".format(shlex.quote(destination_path))

            def limited_transfer():
                """Handle the operation."""
                self.file_semaphore.acquire()
                try:
                    self.file_transfer_client_recv_client_start(cmd, root)
                finally:
                    self.file_semaphore.release()

            thread = threading.Thread(target=limited_transfer, daemon=True)
            thread.start()
            self._log(f"start to send file: {cmd} (limit {self.max_thread_num})")

        def get_all_files_in_folder():
            """Handle the operation."""
            for root, dirs, files in os.walk(folder_path):
                rel_dir = get_relative_path(base_path, root)
                if root != folder_path:
                    send_folder_transfer_command(rel_dir)
                for file in files:
                    start_file_transfer_with_limit(rel_dir, file, root)
            self._log(f"finished sending all files in folder {folder_path}")

        transfer_path = get_relative_path(base_path, folder_path)
        send_folder_transfer_command(transfer_path)
        get_all_files_in_folder()

    def multiple_file_transfer_client_recv_client_start(self, message):
        """Start file transfers for each path in a client command.

        Args:
            message (object): Value used by the operation.
        """
        file_list = shlex.split(message)[1:]
        destination_path = None
        if len(file_list) >= 2 and not os.path.isfile(file_list[-1]):
            destination_path = file_list.pop()
        for file in file_list:
            each_file_transfer_command_message = "/file {}".format(shlex.quote(file))
            if destination_path:
                each_file_transfer_command_message += " {}".format(shlex.quote(destination_path))

            def limited_transfer(cmd=each_file_transfer_command_message):
                # hold the slot for the whole transfer, not only for the
                # thread start, so max_thread_num really caps concurrency
                self.file_semaphore.acquire()
                try:
                    self.file_transfer_client_recv_client_start(cmd, None)
                finally:
                    self.file_semaphore.release()

            threading.Thread(target=limited_transfer, daemon=True).start()
            self._log(f"start to send file command: {each_file_transfer_command_message}")

    def file_transfer_client_recv_client_start_thread(self, message, file_folder_abspath=None):
        """Start a thread to send a file or folder item to the server.

        Args:
            message (object): Value used by the operation.
            file_folder_abspath (str): Value used by the operation.
        """
        file_transfer_client_recv_client_start_thread = threading.Thread(
            target=self.file_transfer_client_recv_client_start,
            args=(message, file_folder_abspath),
            daemon=True,
        )
        file_transfer_client_recv_client_start_thread.start()

    def file_transfer_client_recv_client_start(self, message, file_folder_abspath):
        """Send a file or folder item to the connected server.

        Args:
            message (object): Value used by the operation.
            file_folder_abspath (str): Value used by the operation.
        """
        client_id = None
        with self.file_client_id_lock:
            client_id = copy.copy(self.file_client_id)
            send_msg = message.strip() + " " + str(self.file_client_id)
            self.file_client_id += 1
        try:
            waiting_time = 0
            if shlex.split(message.lower())[0] == "/file_folder":
                filename = os.path.join(file_folder_abspath, shlex.split(message)[2])
            else:
                filename = shlex.split(message)[1]
            self.send_message(self.client_socket, send_msg)
            file_transfer_client_port = self.palloc()
            file_server_port = None
            stamp = "plain"
            is_find_port = True
            while is_find_port:
                time.sleep(1)
                if len(self.file_server_port_list) > 0:
                    with self.file_transfer_server_port_lock:
                        for port_info in self.file_server_port_list:
                            if port_info[1] == client_id:
                                file_server_port = port_info[0]
                                stamp = port_info[2]  # the listener's mode+nonce
                                self.file_server_port_list.remove(port_info)
                                is_find_port = False
                                break
                    pass
                waiting_time += 1
                if waiting_time >= 20:
                    self._log(
                        "ErrorWhileReceiveFileServerPort: transfer port waiting timeout, file sending failed"
                    )
                    return False
            self.file_transfer_mode(
                filename,
                self.host,
                file_server_port,
                file_transfer_client_port,
                control_socket=self.client_socket,
                crypto=stamp,
                fid=client_id,
                peer=(self.host, self.port),  # the receiver a resume is asked to continue from
            )
            self.pfree(file_transfer_client_port)
        except IndexError:
            self._log_exc()
            self._log("invalid command, please use '/file <filename>'")

    def file_transfer_mode(  # noqa: PLR0911 - peer-close and timeout exits are distinct outcomes
        self,
        filename,
        server_address,
        server_port,
        client_port,
        control_socket=None,
        crypto=None,
        fid=None,
        peer=None,
        resume_offset=0,
    ):
        """Send one file over a transfer sub-connection.

        Args:
            filename (str): Source file path.
            server_address (str): Transfer listener address.
            server_port (int): Transfer listener port.
            client_port (int): Local port to bind for the transfer.
            control_socket (socket.socket | None): Control socket carrying the mode stamp.
            crypto (str | None): ``"plain"`` or encrypted transfer stamp.
            fid (int | None): Transfer ID used for progress and pause tracking.
            peer (tuple | None): Original receiver ``(ip, port)`` for resuming.
            resume_offset (int): Byte offset to resume from; defaults to zero.

        Returns:
            bool: True when the receiver acknowledges the file; otherwise False.
        """
        self._log(f"start to send file: {filename}")
        client_file_socket = None
        reset_time = 0

        def close_socket():
            """Handle the operation."""
            nonlocal file_running
            nonlocal client_file_socket
            file_running = False
            self._crypto_drop_channel(client_file_socket)
            client_file_socket.close()
            with self.file_client_id_lock:
                self.file_client_id -= 1

        while True:
            try:
                client_file_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                client_file_socket.bind((self.client_host, client_port))
                client_file_socket.connect((server_address, server_port))
                break
            except Exception as e:
                self._log(f"file transfer connection error: {e}")
                self._log_exc()
                if reset_time >= 20:
                    close_socket()
                    self._log("unable to connect to file transfer server, file sending failed")
                    return False
                reset_time += 1
                time.sleep(1)
        try:
            if crypto and crypto != "plain":
                self._crypto_attach_transfer_out(client_file_socket, control_socket, crypto)
        except _CryptoUnavailable as exc:
            self._log(f"crypto: file transfer aborted: {exc}")
            close_socket()
            return False
        file_running = True
        self._register_transfer_socket(fid, client_file_socket)
        file_receive_data_from_server = ""

        def receive_file_transfer_messages():
            """Handle the operation."""
            nonlocal file_running
            nonlocal client_file_socket
            nonlocal file_receive_data_from_server
            while file_running:
                try:
                    data = self.receive_message(client_file_socket, self.recv_messages_chunk_len)
                    if not data:
                        self._log("\nbreak the file transfer connection from server")
                        try:
                            self.send_message(client_file_socket, self.error_sign)
                        except Exception:
                            pass  # send_message already logged real errors; a dead peer is expected
                        close_socket()
                        break
                    file_receive_data_from_server = data.decode("utf-8").strip()
                    if file_receive_data_from_server == self.error_sign:
                        self._log(
                            "\nError sign received from server, file transfer may have failed"
                        )
                        close_socket()
                        break
                except Exception as e:
                    self._log(f"\nget file transfer msg error: {e}")
                    if not _is_closed_socket_error(e):
                        self._log_exc()
                    try:
                        self.send_message(client_file_socket, self.error_sign)
                    except Exception:
                        pass  # send_message already logged real errors; a dead peer is expected
                    close_socket()
                    break

        receive_thread = threading.Thread(target=receive_file_transfer_messages, daemon=True)
        receive_thread.start()
        waiting_time = 0
        progress = None  # this leg's file_upload_progress entry, when tracked
        file_sent = False  # True once the receiver acknowledged the whole file
        source_missing = False  # True when the source file no longer exists
        try:
            while True:
                if file_receive_data_from_server == self.server_start_file_transfer_sign:
                    break
                if file_receive_data_from_server == self.error_sign:
                    close_socket()
                    break
                if not file_running:
                    # peer closed the file connection: the receive thread
                    # already cleaned up; nothing more to wait for
                    return False
                time.sleep(1)
                waiting_time += 1
                if waiting_time >= 10:
                    try:
                        self.send_message(client_file_socket, self.error_sign)
                    except Exception:
                        pass  # send_message already logged real errors; a dead peer is expected
                    self._log(
                        f"ErrorWhileSendFile: \
                          Wait file transfer function start sign timeout, \
                          file {filename} sending failed"
                    )
                    close_socket()
                    return False
            waiting_time = 0
            file_size = os.path.getsize(filename)
            file_name_encoded = os.path.basename(filename).encode("utf-8")
            name_len = len(file_name_encoded)
            self.send_message(client_file_socket, name_len.to_bytes(4, "big"))
            self.send_message(client_file_socket, file_name_encoded)
            self.send_message(client_file_socket, file_size.to_bytes(8, "big"))
            if resume_offset < 0 or resume_offset > file_size:
                resume_offset = 0  # not this file's offset any more: send it whole
            progress = self._file_progress_start(
                self.file_upload_progress,
                fid,
                file_size,
                filename,
                peer=peer,
                initial=resume_offset,
            )
            with open(filename, "rb") as f:
                if resume_offset:
                    f.seek(resume_offset)
                while True:
                    self._wait_transfer_paused(fid)
                    file_data = f.read(self.file_transfer_chunk_len)
                    if not file_data:
                        break
                    self.send_message(client_file_socket, file_data)
                    self._file_progress_update(progress, len(file_data), is_upload=True)
            extra_time = (file_size // (100 * 1024 * 1024)) * 10
            timeout = int(30 + extra_time)
            while True:
                if file_receive_data_from_server == self.server_received_file_data_sign:
                    break
                if file_receive_data_from_server == self.error_sign:
                    close_socket()
                    break
                if not file_running:
                    # peer closed the file connection: the receive thread
                    # already cleaned up; nothing more to wait for
                    return False
                time.sleep(1)
                waiting_time += 1
                if waiting_time >= timeout:
                    try:
                        self.send_message(client_file_socket, self.error_sign)
                    except Exception:
                        pass  # send_message already logged real errors; a dead peer is expected
                    close_socket()
                    self._log(
                        f"ErrorWhileSendFileData: \
                          wait file transfer confirmation sign timeout, \
                          file {filename} sending may have failed"
                    )
                    return False
            file_sent = True
            self._log(f"Success: file {filename} sent successfully")
            close_socket()
            return True
        except FileNotFoundError:
            # the source is gone: this transfer cannot be resumed later
            source_missing = True
            self._log_exc()
            try:
                self.send_message(client_file_socket, self.error_sign)
            except Exception:
                pass  # send_message already logged real errors; a dead peer is expected
            close_socket()
            self._log(f"file {filename} not exist")
            return False
        except Exception as e:
            if not _is_closed_socket_error(e):
                self._log_exc()
            try:
                self.send_message(client_file_socket, self.error_sign)
            except Exception:
                pass  # send_message already logged real errors; a dead peer is expected
            close_socket()
            self._log(f"send error: {e}")
            return False
        finally:
            self._file_progress_finish(
                self.file_upload_progress,
                fid,
                progress,
                keep=(
                    not file_sent
                    and not source_missing
                    and progress is not None
                    and progress[4] is not None  # resumable: it has a peer
                ),
            )
            self._release_transfer_socket(fid, client_file_socket)
            with self._transfer_pause_cond:
                self._transfer_pause.pop(fid, None)

    # ---- transfer reconnect (client side) ----------------------------------

    def _request_resumes(self):
        """Continue this client's interrupted downloads after it (re)connected.

        Reads the persisted stores back (`_load_transfer_progress`). A download
        from the server gets its offset set to the bytes already on disk and
        ``/resume_trans <fid> <offset>`` is sent to the server. A download
        forwarded from another client is requested through the server
        (`_request_forward_resume`). Uploads need no action here.

        Returns:
            int: Number of resume requests sent.
        """
        restored = self._load_transfer_progress()
        if restored:
            self._log(f"transfer reconnect: restored {restored} interrupted transfer(s)")
        if not self.running or self.client_socket is None:
            return 0
        server_peer = (self.host, self.port)
        if not self._wait_control_secure(self.client_socket):
            return 0
        started = 0
        for fid, entry in list(self.file_recv_progress.items()):
            if entry[4] is None:
                continue  # without a peer there is nobody to continue with
            with self._forward_resume_lock:
                if fid in self._forward_resume_records:
                    continue  # this client relays that transfer: its target drives it
            with self._transfer_progress_lock:
                if fid in self._active_transfer_fids:
                    continue  # a leg is already running for this fid
            peer_addr = tuple(entry[4])
            if peer_addr == server_peer:
                entry[0] = _resume_offset_for(entry)
                if self.send_message(self.client_socket, f"{RESUME_LINE} {fid} {entry[0]}"):
                    started += 1
            elif self._request_forward_resume(fid, entry, peer_addr):
                started += 1
        return started

    def _request_forward_resume(self, fid, entry, originator):
        """Ask the server to have a forward's originator continue a transfer.

        The local path is registered before the request goes out: the fresh
                relay leg arrives under a new fid but writes the same path, and
                `_take_pending_resume` hands its offset to that leg.

        Args:
            fid (int): Value used by the operation.
            entry (object): Value used by the operation.
            originator (object): Value used by the operation.

        Returns:
            bool: Result produced by the operation.
        """
        offset = _resume_offset_for(entry)
        with self._forward_resume_lock:
            self._resume_pending_paths[entry[3]] = (fid, offset)
        if not self.send_message(self.client_socket, f"{RESUME_LINE} {fid} {offset}"):
            with self._forward_resume_lock:
                self._resume_pending_paths.pop(entry[3], None)
            return False
        self._log(
            f"resume: asked the server to continue transfer {fid} from {originator} "
            f"at byte {offset}"
        )
        return True

    def _take_pending_resume(self, path):
        """Return the offset a pending resume of this path starts at, or None.

        A forward target that lost a transfer asks its originator to re-send
                it; the fresh leg the server then pushes carries a new transfer id but
                lands on the same path, so the state registered by the request is what
                the leg appends from — and the interrupted entry it replaces is
                dropped here.

        Args:
            path (str): Value used by the operation.

        Returns:
            NoneType | object: Result produced by the operation.
        """
        with self._forward_resume_lock:
            pending = self._resume_pending_paths.pop(path, None)
        if pending is None:
            return None
        old_fid, offset = pending
        old_entry = self.file_recv_progress.pop(old_fid, None)
        if old_entry is not None:
            with self._transfer_progress_lock:
                self._active_transfer_fids.discard(old_fid)
        return offset

    def resume_trans(self, peer_addr, fid, offset):
        """Continue one interrupted transfer this instance was sending.

        Runs when a receiver asks to continue a transfer (``/resume_trans``,
        see `_request_resumes` on the receiving side): ``/file <name> <fid>``
        is sent so the receiver opens a listener, and the source file is read
        from the byte offset the receiver already holds. Also the public API
        for resuming one transfer on demand.

        Args:
            peer_addr (tuple | None): Receiver ``(ip, port)`` the request came
                from; None skips the peer check.
            fid (int): Transfer id of the interrupted transfer.
            offset (int): Byte offset the receiver already holds.

        Returns:
            bool: True when the resume leg was started.
        """
        entry = self.file_upload_progress.get(fid)
        if entry is None:
            self._log(f"resume: no outgoing transfer with fid {fid} to continue")
            return False
        if peer_addr is not None and entry[4] is not None and tuple(entry[4]) != tuple(peer_addr):
            self._log(f"resume: transfer {fid} is for {entry[4]}, not {peer_addr}, ignored")
            return False
        if not self.running or self.client_socket is None:
            self._log(f"resume: not connected to the server, transfer {fid} kept")
            return False
        self._forget_transfer_port(fid)  # drop a dead session's stale announcement
        if not self.send_message(
            self.client_socket, f"/file {shlex.quote(os.path.basename(str(entry[3])))} {fid}"
        ):
            self._log(f"resume: cannot send /file for transfer {fid}")
            return False
        port_info = self._wait_transfer_server_port(fid)
        if port_info is None:
            self._log(f"resume: {peer_addr} did not announce a transfer port for {fid}")
            return False
        port, stamp = port_info
        def run_resume_send_leg():
            client_port = self.palloc()
            try:
                self.file_transfer_mode(
                    entry[3],
                    peer_addr[0] if peer_addr else self.host,
                    port,
                    client_port,
                    control_socket=self.client_socket,
                    crypto=stamp,
                    fid=fid,
                    peer=peer_addr,
                    resume_offset=offset,
                )
            finally:
                self.pfree(client_port)

        threading.Thread(target=run_resume_send_leg, daemon=True).start()
        return True


    def _forward_resume_record(self, fid):
        """Return the relay record a forwarded resume for ``fid`` belongs to.

        Args:
            fid (int): Value used by the operation.

        Returns:
            object: Result produced by the operation.
        """
        with self._forward_resume_lock:
            return self._forward_resume_records.get(fid)

    def _resume_forward(self, target, fid, offset):
        """Re-drive a forwarded transfer to one target, from ``offset``.

        Runs when the server relays a target's resume request
                (``/forward_resume``): the recorded relay says what the transfer was,
                so the item is forwarded again — only to the target that asked, and
                only the remainder of the file it already holds partially.

        Args:
            target (tuple): Target client ``(ip, port)`` that lost the file.
            fid (int): The relay's transfer id (this client's own namespace).
            offset (int): Byte offset the target already holds.

        Returns:
        bool: True when the re-forward was started.
        """
        record = self._forward_resume_record(fid)
        with self._forward_resume_lock:
            self._forward_resume_records.pop(fid, None)
        entry = self.file_upload_progress.pop(fid, None)
        if record is None and entry is None:
            self._log(f"resume: no forwarded transfer with fid {fid} to continue")
            return False
        record = record or {}
        targets = [tuple(item) for item in record.get("targets") or []]
        kind = record.get("kind") or "file"
        rel_dir = record.get("rel_dir") or ""
        fname = record.get("fname") or os.path.basename(str((entry or [None, None, None, ""])[3]))
        path = record.get("path") or (entry[3] if entry else None)
        destination = record.get("destination")
        if path is None or not os.path.isfile(path):
            self._log(f"resume: the source of forwarded transfer {fid} is gone, giving up")
            return False
        if targets and tuple(target) not in targets:
            self._log(f"resume: {target} is not a target of forwarded transfer {fid}, ignored")
            return False
        self._log(f"resume: forwarding {path} to {target} again, from byte {offset}")
        threading.Thread(
            target=self._forward_one,
            args=(kind, rel_dir, fname, path, [tuple(target)], destination),
            kwargs={"resume_offset": offset},
            daemon=True,
        ).start()
        return True

    # ---- native in-memory forward (client side) ----------------------------

    def _forward_parse(self, tokens):
        """Handle the operation.

        Args:
            tokens (list | dict): Value used by the operation.

        Returns:
            list | tuple: Result produced by the operation.
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
                if isinstance(addr, tuple) and len(addr) == 2 and isinstance(addr[0], str):
                    addrs.append(addr)
                else:
                    items.append(token)
            else:
                items.append(token)
        return items, addrs

    def _forward_parse_command(self, tokens):
        """Split forward console tokens into (items, addrs, destination_path).

        Files/folders come first and address tuples last; an optional
                destination directory, when present, is the very last argument.

        Args:
            tokens (list | dict): Value used by the operation.

        Returns:
            list | tuple: Result produced by the operation.
        """
        destination_path = None
        if tokens and not (tokens[-1].startswith("(") and tokens[-1].endswith(")")):
            destination_path = tokens[-1]
            tokens = tokens[:-1]
        items, addrs = self._forward_parse(tokens)
        return items, addrs, destination_path

    def forward_file_console(self, message):
        """Parse a client command and forward its files.

        Args:
            message (str): ``/forward_file`` command with file paths, target address
                tuples, and an optional destination directory.
        """
        parts = shlex.split(message)
        items, addrs, destination_path = self._forward_parse_command(parts[1:])
        files = [p for p in items if os.path.isfile(p)]
        for p in items:
            if not os.path.isfile(p):
                self._log(f"forward: {p} is not a valid file, skipped")
        if not files:
            return
        if not addrs:
            self._log("forward: no target clients given")
            return
        threading.Thread(
            target=self._forward_driver,
            args=(files, addrs, False, destination_path),
            daemon=True,
        ).start()

    def forward_folder_console(self, message):
        """Parse a client command and forward its folders.

        Args:
            message (str): ``/forward_folder`` command with folder paths, target address
                tuples, and an optional destination directory.
        """
        parts = shlex.split(message)
        items, addrs, destination_path = self._forward_parse_command(parts[1:])
        folders = [p for p in items if os.path.isdir(p)]
        for p in items:
            if not os.path.isdir(p):
                self._log(f"forward: {p} is not a valid folder, skipped")
        if not folders:
            return
        if not addrs:
            self._log("forward: no target clients given")
            return
        threading.Thread(
            target=self._forward_driver,
            args=(folders, addrs, True, destination_path),
            daemon=True,
        ).start()

    def _forward_driver(self, items, addrs, folder_mode, destination_path=None):
        """Handle the operation.

        Args:
            items (list | dict): Value used by the operation.
            addrs (list | dict): Value used by the operation.
            folder_mode (bool): Value used by the operation.
            destination_path (str): Value used by the operation.
        """
        try:
            if not folder_mode:
                for path in items:
                    self._forward_one(
                        "file", "", os.path.basename(path), path, addrs, destination_path
                    )
            else:
                for folder in items:
                    base_path = os.path.dirname(folder)
                    for root, _dirs, files in os.walk(folder):
                        rel_dir = self._forward_rel_path(base_path, root)
                        for file in files:
                            self._forward_one(
                                "folder",
                                rel_dir,
                                file,
                                os.path.join(root, file),
                                addrs,
                                destination_path,
                            )
        except Exception as e:
            self._log(f"forward error: {e}")
            self._log_exc()

    def _forward_rel_path(self, base_path, abs_path):
        """Handle the operation.

        Args:
            base_path (str): Value used by the operation.
            abs_path (str): Value used by the operation.

        Returns:
            object | str: Result produced by the operation.
        """
        base = os.path.normpath(base_path)
        abs_ = os.path.normpath(abs_path)
        common = os.path.commonpath([base, abs_])
        if common != base:
            return ""
        rel = os.path.relpath(abs_, base)
        if rel == ".":
            return ""
        return "/" + rel.replace(os.sep, "/")

    def _forward_one(
        self, kind, rel_dir, fname, abspath, addrs, destination_path=None, resume_offset=0
    ):
        """Forward one item to every target through the server's relay.

        The relay the server answers with is recorded under its fid so a
                target that lost the transfer can have it re-driven after a reconnect
                (`_resume_forward`); ``resume_offset`` re-sends only the remainder of
                a file the target already holds partially.

        Args:
            kind (object): Value used by the operation.
            rel_dir (str): Value used by the operation.
            fname (object): Value used by the operation.
            abspath (str): Value used by the operation.
            addrs (list | dict): Value used by the operation.
            destination_path (str): Value used by the operation.
            resume_offset (object): Value used by the operation.

        Returns:
            bool | object: Result produced by the operation.
        """
        if not os.path.isfile(abspath):
            self._log(f"forward: {abspath} is not a valid file, skipped")
            return False
        msg = f"/forward_item {kind}"
        if resume_offset:
            # the relay must know how much of the file does not travel again
            msg += f" resume={int(resume_offset)}"
        if kind == "folder":
            msg += f" {shlex.quote(rel_dir)}"
        msg += f" {shlex.quote(fname)} " + " ".join(shlex.quote(str(a)) for a in addrs)
        if destination_path:
            msg += f" {shlex.quote(destination_path)}"
        self._forward_error = None
        self.send_message(self.client_socket, msg)
        upload = None
        deadline = time.time() + 30
        while time.time() < deadline:
            if self._forward_error is not None:
                self._log(f"forward: server error: {self._forward_error}")
                self._forward_error = None
                return False
            try:
                upload = self._forward_upload_queue.get(timeout=0.2)
                break
            except queue.Empty:
                continue
        if upload is None:
            self._log(f"forward: timed out waiting for an upload slot for {fname}")
            return False
        fid, sport, stamp = upload
        targets = [(_peer_from_json(target) or (target[0], int(target[1]))) for target in addrs]
        record = {
            "fid": fid,
            "kind": kind,
            "rel_dir": rel_dir,
            "fname": fname,
            "path": abspath,
            "destination": destination_path,
            "targets": [list(target) for target in targets],
        }
        with self._forward_resume_lock:
            self._forward_resume_records[fid] = record
        client_port = self.palloc()
        try:
            sent = self.file_transfer_mode(
                abspath,
                self.host,
                sport,
                client_port,
                control_socket=self.client_socket,
                crypto=stamp,
                fid=fid,
                peer=targets[0] if targets else None,  # a forwarded leg stores its original target
                resume_offset=resume_offset,
            )
        finally:
            self.pfree(client_port)
            with self._transfer_pause_cond:
                self._transfer_pause.pop(fid, None)
        if sent:
            # the whole item went through: nothing is left to resume for this relay
            with self._forward_resume_lock:
                self._forward_resume_records.pop(fid, None)
        return sent

    def file_folder_transfer_client_recv_server_start_thread(
        self, command, client_id, client_socket
    ):
        """Start a client-side folder receive operation.

        Args:
            command (object): Value used by the operation.
            client_id (object): Value used by the operation.
            client_socket (socket.socket): Value used by the operation.
        """
        command_part = shlex.split(command)
        relative_folder_path = command_part[1]
        # >= 4 tokens means a file transfer (/file_folder <rel> <file> [<dest>] <id>);
        # 2-3 tokens is folder creation (optional destination, no client id)
        file_name = command_part[2] if len(command_part) >= 4 else None
        folder_transfer_client_recv_server_start_thread = threading.Thread(
            target=self.file_transfer_client_recv_server_start,
            args=(client_id, client_socket, command, relative_folder_path, file_name),
            daemon=True,
        )
        folder_transfer_client_recv_server_start_thread.start()

    def file_transfer_client_recv_server_start_thread(self, client_id, client_socket, command):
        """Start a client-side file receive operation.

        Args:
            client_id (object): Value used by the operation.
            client_socket (socket.socket): Value used by the operation.
            command (object): Value used by the operation.
        """
        file_transfer_client_recv_server_start_thread = threading.Thread(
            target=self.file_transfer_client_recv_server_start,
            args=(client_id, client_socket, command),
            daemon=True,
        )
        file_transfer_client_recv_server_start_thread.start()

    def file_transfer_client_recv_server_start(
        self, client_id, client_socket, command, new_save_path=None, file_name=None
    ):
        """Receive a file pushed by the server.

        Args:
            client_id (object): Value used by the operation.
            client_socket (socket.socket): Value used by the operation.
            command (object): Value used by the operation.
            new_save_path (str): Value used by the operation.
            file_name (str): Value used by the operation.
        """
        file_transfer_server_port = self.palloc()
        self.file_transfer_mode_recv(
            self.host,
            file_transfer_server_port,
            client_socket,
            client_id,
            new_save_path,
            file_name,
            command,
            peer=self._command_peer(command),  # the sender a resume is asked to continue from
        )
        self.pfree(file_transfer_server_port)

    def _own_address_text(self):
        """Return this client's own ``"ip:port"`` as the server sees it, when known.

        Returns:
            NoneType | object: Result produced by the operation.
        """
        if self.client_socket is not None:
            try:
                address = self.client_socket.getsockname()
                return f"{address[0]}:{address[1]}"
            except OSError:
                pass  # the socket is gone: fall back to the configured address
        if self.client_port:
            return f"{self.client_host}:{self.client_port}"
        return None

    def _command_peer(self, command):
        """Return the sender a received transfer command belongs to.

        A file pushed by the server itself carries this client's own address,
                which `parse_forward_originator` filters out: the sender is then the
                server. A forwarded item carries the originating client's address —
                the original peer a resume asks to continue the transfer.

        Args:
            command (object): Value used by the operation.

        Returns:
        tuple: ``(ip, port)`` of the transfer's original sender.
        """
        originator = parse_forward_originator(command, own_address=self._own_address_text())
        peer = _peer_from_text(originator) if originator else None
        return peer if peer is not None else (self.host, self.port)

    def file_transfer_mode_recv(
        self,
        server_file_address,
        server_file_port,
        client_socket,
        client_id,
        new_save_path,
        file_name,
        command,
        peer=None,
    ):
        """Receive one file over a transfer sub-connection.

        Args:
            server_file_address (str): Local address for the transfer listener.
            server_file_port (int): Local port for the transfer listener.
            client_socket (socket.socket): Server control socket.
            client_id (str): Sender identifier supplied by the server.
            new_save_path (str | None): Relative directory for folder contents.
            file_name (str | None): Destination filename, when specified.
            command (str): Wire command that initiated the transfer.
            peer (tuple | None): Original sender ``(ip, port)`` for resuming.

        Returns:
            None: after the leg starts or finishes.
        """
        file_running = True
        client_file_socket = None
        server_file_socket = None
        save_path = None

        def close_socket():
            """Handle the operation."""
            nonlocal file_running
            nonlocal client_file_socket
            nonlocal server_file_socket
            file_running = False
            self._crypto_drop_channel(client_file_socket)
            client_file_socket.close()
            server_file_socket.close()

        def setting_file_save_path():
            """Handle the operation."""
            nonlocal save_path
            save_path = self.file_transfer_dir
            if new_save_path or destination_path:
                if destination_path:
                    if os.path.isabs(destination_path):
                        save_path = destination_path
                    else:
                        save_path = os.path.join(save_path, destination_path)
                if new_save_path:
                    path_list = new_save_path.split("/")
                    if path_list and path_list[0] == "":
                        del path_list[0]
                    for node in path_list:
                        save_path = os.path.join(save_path, node)
                os.makedirs(save_path, exist_ok=True)
            if file_name or new_save_path == None:
                return save_path
            close_socket()
            return None

        def file_transfer_client_recv(client_id):
            """Handle the operation.

            Args:
                client_id (object): Value used by the operation.
            """
            nonlocal file_running
            nonlocal client_file_socket
            nonlocal server_file_socket
            filename = None
            full_path = None
            progress = None  # this leg's file_recv_progress entry, when tracked
            finished_ok = False
            try:
                self.send_message(client_file_socket, self.server_start_file_transfer_sign)
            except Exception as e:
                # the instance stopped (or the peer vanished) between the accept
                # and this start sign: the transfer is over. A closing instance
                # raises RuntimeError("connection error") by design — the normal
                # teardown signal — and it must never kill this thread.
                if not _is_closed_socket_error(e):
                    self._log(f"error while starting the file transfer: {e}")
                    self._log_exc()
                close_socket()
                return False
            try:
                name_len_bytes = b""
                while len(name_len_bytes) < 4:
                    chunk = self.receive_message(client_file_socket, 4 - len(name_len_bytes))
                    self._debug(chunk)
                    if not chunk:
                        try:
                            self.send_message(client_file_socket, self.error_sign)
                        except Exception:
                            pass  # send_message already logged real errors; a dead peer is expected
                        close_socket()
                        raise ConnectionError(
                            "ErrorWhileReceivingFileNameLength: client disconnected"
                        )
                    name_len_bytes += chunk
                    if name_len_bytes.strip() == self.error_sign.encode("utf-8"):
                        close_socket()
                        raise ConnectionError(
                            "ErrorSignReceivedWhileReceivingFileNameLength: client reported error and disconnected"
                        )
                name_len = int.from_bytes(name_len_bytes, "big")
                file_name_encoded = b""
                while len(file_name_encoded) < name_len:
                    chunk = self.receive_message(
                        client_file_socket, name_len - len(file_name_encoded)
                    )
                    if not chunk:
                        try:
                            self.send_message(client_file_socket, self.error_sign)
                        except Exception:
                            pass  # send_message already logged real errors; a dead peer is expected
                        close_socket()
                        raise ConnectionError("ErrorWhileReceivingFileName: client disconnected")
                    file_name_encoded += chunk
                    if file_name_encoded.strip() == self.error_sign.encode("utf-8"):
                        close_socket()
                        raise ConnectionError(
                            "ErrorSignReceivedWhileReceivingFileName: client reported error and disconnected"
                        )
                filename = file_name_encoded.decode("utf-8")
                filename = filename.strip()
                filename = os.path.basename(filename)
                size_bytes = b""
                while len(size_bytes) < 8:
                    chunk = self.receive_message(client_file_socket, 8 - len(size_bytes))
                    if not chunk:
                        try:
                            self.send_message(client_file_socket, self.error_sign)
                        except Exception:
                            pass  # send_message already logged real errors; a dead peer is expected
                        close_socket()
                        raise ConnectionError("ErrorWhileReceivingFileSize: client disconnected")
                    size_bytes += chunk
                    if size_bytes.strip() == self.error_sign.encode("utf-8"):
                        close_socket()
                        raise ConnectionError(
                            "ErrorSignReceivedWhileReceivingFileSize: client reported error and disconnected"
                        )
                file_size = int.from_bytes(size_bytes, "big")
                if resume_entry is not None and resume_entry[1] != file_size:
                    raise ConnectionError(
                        f"resume rejected: {filename} is {file_size} bytes, "
                        f"the partial file expects {resume_entry[1]}"
                    )
                self.send_message(client_file_socket, self.server_received_file_header_sign)
                original_filename = filename
                if resume_entry is not None:
                    # continued transfer: the receiver's partial file keeps the
                    # bytes that are already on disk and the sender continues
                    full_path = resume_entry[3]
                    final_filename = os.path.basename(full_path)
                    resume_from = resume_entry[0]  # the offset the sender was asked for
                else:
                    if file_name:
                        final_filename = file_name.strip()
                    elif command_part[0] == "/crypto_pub_key":
                        # concurrent key pushes share received_files/: a fixed name would be
                        # overwritten/moved by another connection before this one consumes it
                        final_filename = "{}.pem".format(uuid.uuid4().hex)
                    else:
                        final_filename = os.path.basename(original_filename)
                    full_path = os.path.join(save_path, final_filename).strip()
                    # a forwarded transfer this client asked to continue arrives
                    # as a fresh leg on the same path: keep the bytes on disk
                    resume_from = self._take_pending_resume(full_path) or 0
                if resume_from <= 0 or resume_from > file_size:
                    resume_from = 0  # not this file's offset any more: start it over
                os.makedirs(os.path.dirname(full_path), exist_ok=True)
                progress = self._file_progress_start(
                    self.file_recv_progress,
                    progress_fid,
                    file_size,
                    full_path,
                    peer=peer,
                    initial=resume_from,
                )
                with _open_transfer_target(full_path, resume_from) as f:
                    remaining = file_size - resume_from
                    while remaining > 0:
                        chunk = self.receive_message(
                            client_file_socket, min(self.file_transfer_chunk_len, remaining)
                        )
                        if not chunk:
                            try:
                                self.send_message(client_file_socket, self.error_sign)
                            except:
                                self._log_exc()
                                pass
                            close_socket()
                            raise ConnectionError(
                                "ErrorWhileReceivingFileData: client disconnected"
                            )
                        f.write(chunk)
                        remaining -= len(chunk)
                        self._file_progress_update(progress, len(chunk), is_upload=False)
                    f.truncate()  # a longer partial file drops its stale tail
                finished_ok = True
                # TOFU first: the ack is only a notification, and a failed
                # ack send must not skip the key registration (readiness
                # would never be announced and the handshake hangs)
                self._notify_file_received(full_path, final_filename, file_size, command)
                self._update_event_timestamp(
                    client_socket,
                    self._splice_event_command(command, fname=final_filename),
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                )
                self._log(f"file {filename} received from {client_id}, size {file_size} bytes")
                if command_part[0] == "/crypto_pub_key":
                    self._crypto_store_received_pub(full_path, "server", (self.host, self.port))
                try:
                    self.send_message(client_file_socket, self.server_received_file_data_sign)
                except Exception:
                    self._log_exc()
                close_socket()
            except Exception as e:
                self._log_exc()
                if full_path is not None and os.path.exists(full_path) and progress is None:
                    try:
                        os.remove(full_path)  # untracked partial transfer: nothing can resume it
                    except OSError:
                        pass
                try:
                    self.send_message(client_file_socket, self.error_sign)
                except Exception:
                    pass  # send_message already logged real errors; a dead peer is expected
                close_socket()
                self._log(f"ErrorWhileReceiveFile: {e}")
                return False
            else:
                close_socket()
                return None
            finally:
                self._file_progress_finish(
                    self.file_recv_progress,
                    progress_fid,
                    progress,
                    keep=(
                        not finished_ok
                        and progress is not None
                        and progress[4] is not None  # resumable: it has a peer
                    ),
                )
                self._release_transfer_socket(progress_fid, client_file_socket)

        server_file_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_file_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_file_socket.bind((server_file_address, server_file_port))
        file_transfer_server_port = server_file_socket.getsockname()[1]
        command_part = shlex.split(command)
        file_client_id = command_part[len(command_part) - 1]
        progress_fid = _transfer_fid(command_part)  # keys this leg in file_recv_progress
        resume_entry = _resumable_entry(self.file_recv_progress, progress_fid, peer, command_part)
        destination_path = _parse_destination_path(command_part)
        stamp = self._crypto_transfer_stamp(
            client_socket, force_plain=command_part[0] == "/crypto_pub_key"
        )
        self._forget_transfer_port(progress_fid)  # an older session's entry is stale
        transfer_server_port_msg = "/server_file_transfer_port {} {} {}\n".format(
            file_transfer_server_port, file_client_id, stamp
        )
        self.send_message(
            client_socket, transfer_server_port_msg, is_send_raw=True
        )  # coordination line: always plaintext (like the handshake)
        server_file_socket.listen(1)
        if resume_entry is not None:
            # the sender answers this resume with /file; its connection is the continued leg
            server_file_socket.settimeout(RESUME_ACCEPT_TIMEOUT)
        try:
            client_file_socket, client_file_address = server_file_socket.accept()
            if resume_entry is not None:
                client_file_socket.settimeout(None)  # the accept timeout is the listener's only
            self._crypto_attach_transfer_in(client_file_socket, client_socket, stamp)
            self._register_transfer_socket(progress_fid, client_file_socket)
            if resume_entry is not None:
                threading.Thread(
                    target=file_transfer_client_recv, args=(client_id,), daemon=True
                ).start()
            else:
                is_open_file_transfer = setting_file_save_path()
                if is_open_file_transfer == None:
                    pass
                else:
                    threading.Thread(
                        target=file_transfer_client_recv, args=(client_id,), daemon=True
                    ).start()
        except socket.timeout:
            self._log(
                f"resume: {client_id} did not continue transfer {progress_fid}, "
                f"it stays in the progress stores for the next reconnect"
            )
        except Exception as e:
            self._log(f"\nget file transfer msg error: {e}")
            self._log_exc()
            close_socket()
        finally:
            server_file_socket.close()

    def close(self):  # close connection
        """Close the connection and release everything the client owns.

        Stops the receive loop, releases the port range, flushes the message and
        event stores, persists the interrupted transfers and closes the socket.
        Safe to call more than once.
        """
        self.running = False
        self._stopped = True  # also cancels a connect() still in flight
        self.free_port()
        self._flush_record_stores()
        # transfers still in flight are kept on disk for the next connect
        self._persist_transfer_progress()
        with self._crypto_lock:
            if self.client_socket is not None:
                self._channels.pop(self.client_socket, None)
                self._encrypted_sockets.discard(self.client_socket)
                self._control_sockets.discard(self.client_socket)
            self._crypto_session = None
        if self.client_socket:
            try:
                # the receive thread may be blocked in recv() on this socket:
                # close() alone sends no FIN and leaves that read waiting for its
                # timeout, so the server would not see the disconnect
                self.client_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass  # peer already gone
            self.client_socket.close()
        self._log("connection closed")

    def _release_client_socket(self):
        """Close the socket a finished session left behind.

        An auto-reconnect binds the configured ``client_port`` again, so the
                dead session's socket must go away completely: it is closed abortively
                (``SO_LINGER`` on with a zero timeout, which the TCP stack turns into a
                RST and which skips ``TIME_WAIT`` entirely). A socket that only gets
                shut down keeps the port, and an ordinary close leaves the local port
                in ``TIME_WAIT`` — Windows then refuses to bind or connect from it
                again (``WSAEADDRINUSE``) for the whole two-minute delay.

                The receive thread of that session has already ended when this runs, so
                nothing needs the shutdown a graceful close would perform.
        """
        sock = self.client_socket
        if sock is None:
            return
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        except OSError:
            pass  # not a real socket any more
        try:
            sock.close()
        except OSError:
            pass
        with self._crypto_lock:
            self._channels.pop(sock, None)
            self._encrypted_sockets.discard(sock)
            self._control_sockets.discard(sock)

    def start_TCP_client(self):  # start client
        """Connect to the server and start the client loop.

        Enters `interactive_mode` when ``is_input_command_in_console`` is True,
        otherwise keeps the process alive while the connection is up. Exits the
        process with status 1 when the connection cannot be established; Ctrl-C and
        the end of the connection both run `close`. A client that was closed while
        still connecting returns quietly, because that stop was asked for.

        With ``is_auto_reconnect`` True a dropped connection does not end the
        client: it reconnects (waiting for the server when ``is_wait_server``
        is set) and every session carries the interrupted transfers of the
        previous one (`_request_resumes`). A close or ``/quit`` still stops
        the client.
        """
        while True:
            if not self.connect():
                if self._stopped:
                    return  # close() won the race with the connection attempt
                if not self.is_auto_reconnect:
                    sys.exit(1)
                # a dead socket still holds the configured client port: the
                # next attempt must be able to bind it again (Windows refuses
                # the second bind otherwise)
                self._release_client_socket()
                self._log("reconnect in 1 second...")
                time.sleep(1)
                continue
            try:
                if self.is_input_command_in_console:
                    self.interactive_mode()
                else:
                    while self.running:
                        time.sleep(1)
            except KeyboardInterrupt:
                self._log("\nclient shutting down...")
                self._log_exc()
            if self._stopped or not self.is_auto_reconnect:
                self.close()
                return
            self._release_client_socket()
            self._log("connection lost, reconnecting in 1 second...")
            time.sleep(1)
