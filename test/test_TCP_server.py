

import errno

from PyFlow.network_api.connect_tcp import TCP_Server_Base, _is_closed_socket_error


def test_is_closed_socket_error_covers_teardown_signals():
    """Peer-drop and shutdown errors are teardown signals, not faults:
    they must not produce tracebacks in the receive/send loops."""
    assert _is_closed_socket_error(ConnectionResetError(errno.ECONNRESET, "reset"))
    assert _is_closed_socket_error(BrokenPipeError(errno.EPIPE, "pipe"))
    assert _is_closed_socket_error(OSError(errno.EBADF, "bad fd"))
    assert _is_closed_socket_error(OSError(errno.ENOTSOCK, "not a socket"))
    assert _is_closed_socket_error(OSError(10038, "winsock"))  # WSAENOTSOCK
    assert _is_closed_socket_error(RuntimeError("connection error"))
    assert not _is_closed_socket_error(OSError(errno.ETIMEDOUT, "timeout"))  # real fault
    assert not _is_closed_socket_error(ValueError("nope"))


def test_tcp_server_init():
    server = TCP_Server_Base(host="127.0.0.1", port=65002, is_extend_command=True)
    assert server.host == "127.0.0.1"
    assert server.port == 65002  # noqa: PLR2004
    assert callable(server.start_TCP_Server)
    assert server.is_enable_encrypto is True
    assert server.is_custom_keys is None
