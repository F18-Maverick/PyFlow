import os
import sys

package_dictionary = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if package_dictionary not in os.sys.path:
    sys.path.insert(0, package_dictionary)

from PyFlow.network_api.connect_tcp import TCP_Server_Base


def test_tcp_server_init():
    server = TCP_Server_Base(host="127.0.0.1", port=65002, is_extend_command=True)
    assert server.host == "127.0.0.1"
    assert server.port == 65002  # noqa: PLR2004
    assert callable(server.start_TCP_Server)
    assert server.is_enable_encrypto is True
    assert server.is_custom_keys is None
