

import PyFlow.command_control_extension_tcp as ctl


def test_setup_client_and_server_commands(server_client):
    server, client = server_client

    ctl._setup_command()
    # server should register '/command' to run on clients (index 1) and '/command_done' on server (index 0)
    assert "/command" in server._custom_handlers[1]
    assert "/command_done" in server._custom_handlers[0]

    ctl._setup_client_command()
    # client should register '/command' to run on server (index 0)
    assert "/command" in client._custom_handlers[0]
