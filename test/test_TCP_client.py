import os
import sys

package_dictionary = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if package_dictionary not in os.sys.path:
    sys.path.insert(0, package_dictionary)

from PyFlow.network_api.connect_tcp import TCP_Client_Base


def test_tcp_client_init():
    client = TCP_Client_Base(
        host="127.0.0.1", port=65003, client_host="127.0.0.1", is_extend_command=True
    )
    assert client.host == "127.0.0.1"
    assert client.port == 65003  # noqa: PLR2004
    assert callable(client.connect)
    assert client.is_enable_encrypto is True
    assert client.is_custom_keys is None
