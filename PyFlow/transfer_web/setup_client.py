#!/usr/bin/env python3
"""PyFlow TCP client web launcher.

Starts a lightweight Flask backend on 127.0.0.1 and opens the connect
UI in the browser.  The user enters the server address (an http/https
domain or a bare IP); the backend asks the server's web backend for the
TCP server address/port, starts the TCP client, and keeps the backend
running to relay the user's frontend actions.  The account of the server
logs in from the same backend: saved credentials in
``.Flow_Web/client_login.json`` are replayed on every start, and the
login window appears whenever no session could be restored.
"""

import os
import sys

WEB_ROOT = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(WEB_ROOT)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from transfer_web.web_front.client_backend import ClientWebApp  # noqa: E402


def main():
    """Start the client web backend and serve its UI."""
    app = ClientWebApp()
    app.start_from_config()
    app.run()


if __name__ == "__main__":
    main()
