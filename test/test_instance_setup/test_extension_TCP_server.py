import os
import sys

package_dictionary = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if package_dictionary not in os.sys.path:
    sys.path.insert(0, package_dictionary)
from PyFlow.network_api.connect_tcp import TCP_Server_Base
from PyFlow.command_control_extension_tcp import server_setup

server_setup()
