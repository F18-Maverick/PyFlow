========================
Flow Setup Launcher
========================

The ``flow_setup.py`` script is a launcher for the TCP
server/client framework defined in ``connect_tcp.py``.
It spawns one or more server and client instances, either
interactively or from command-line arguments.
Each launched instance runs in its own terminal window
(or a detached background process on headless systems).

Features
========

- **Interactive mode** – step-by-step creation of server
  and client instances, plus a vim-style editor for
  changing or deleting the instances already stored.
- **Command-line mode** – launch one server or one client
  with all parameters in one command.
- **Web-tool mode** – ``--web_server`` / ``--web_client``
  start the browser UI of the ``transfer_web`` web tool.
- **Extension registration** – ``--add`` / ``--delete``
  register or unregister extension protocol files; the
  registered files are loaded on every launch.
- **Persistent configuration** – stores the latest instance
  definitions in ``setup.json`` (same directory as the
  script). Every entry is completed with the full default
  parameter set of its type when it is saved.
- **Multiple instances** – the ``servers`` and ``clients``
  lists may hold any number of entries. Adding an entry
  identical to an existing one of the same type is skipped;
  different entries are appended.
- **Cross-platform** – supports Windows (cmd), Linux
  (gnome-terminal, xterm, x-terminal-emulator, or a
  detached background process), and macOS (Terminal.app).
- **Complete parameter support** – all parameters accepted
  by ``TCP_Server_Base`` and ``TCP_Client_Base`` can be
  stored in ``setup.json`` for fine-tuning.

Usage
=====

Interactive Mode
-----------------

Run the script without any arguments:

.. code-block:: bash

    python -m PyFlow

When ``setup.json`` already exists, the launcher first asks
whether to overwrite it, and the interactive collection
itself asks whether the stored instances should be edited:

1. ``setup.json exists. Overwrite configuration data? (Y/N):``
   – ``N`` launches every stored instance and exits; ``Y``
   (or no existing file) continues to the collection below.
2. ``Delete or change existing instances or configs? (Y/N):``
   – asked only when stored instances exist. ``Y`` opens the
   instance editor (see below); ``N`` keeps them and goes
   straight to the add flow.
3. ``Choose the type (0 for Server, 1 for Client)``.
4. Enter the bind address and port (``host:port``).
5. If Client, also enter the server address and port to
   connect to.
6. Decide whether to add another instance.
   An entry that already exists in the list is not added
   again; a new one is appended, so several instances of the
   same type can be kept side by side.
7. After editing, the launcher asks ``Add new instances? (Y/N)``
   before returning to the add flow.

When the editor is left with changes, ``setup.json`` is
rewritten with every instance, and all stored instances
(reused plus newly added) are launched.

Global commands
---------------

The following words can be typed at **every** prompt,
including field-value prompts:

+----------------------+-------------------------------------------------------+
| Command              | Effect                                                |
+======================+=======================================================+
| ``Help``             | Print the usage text of the current prompt.           |
+----------------------+-------------------------------------------------------+
| ``Fix_Config``       | Jump to the instance editor (or, inside it, save and  |
|                      | return to the list).                                  |
+----------------------+-------------------------------------------------------+
| ``Setup``            | Launch every instance configured in ``setup.json``    |
|                      | and exit the setup program.                           |
+----------------------+-------------------------------------------------------+
| ``Add_Extension``    | Prompt for extension file path(s) and register them.  |
+----------------------+-------------------------------------------------------+
| ``Delete_Extension`` | Prompt for registered extension path(s) to remove.    |
+----------------------+-------------------------------------------------------+
| ``Quit``             | Exit the setup program immediately.                   |
+----------------------+-------------------------------------------------------+

Instance editor
---------------

The editor is reached by typing ``Fix_Config`` (at any prompt)
or by answering ``Y`` to the "Delete or change existing
instances" question. On an interactive terminal it reads real
keys (and enables mouse tracking); on a pipe or in tests it
falls back to line commands. ``j``/``k`` or the arrow
keys move the selection, a mouse click selects the clicked
instance, ``Enter`` or a double click opens that instance's
config editor, and ``Delete``/``Backspace`` or ``dd`` deletes
the selected instance.

Vim-style commands:

- ``:w`` – write ``setup.json`` without leaving the editor.
- ``:wq`` – write ``setup.json`` and leave the editor.
- ``:q`` – leave; refused while there are unsaved changes.
- ``:q!`` – leave and discard all changes.
- ``Quit`` – exit the setup program immediately.

Inside the config editor, ``j``/``k`` move between fields,
``Esc`` or ``back`` returns to the list, and a field index or
``Enter`` edits the selected field. An empty input keeps the
current value; booleans accept ``true``/``false``/``1``/``0``/
``y``/``n``, integers are parsed with ``int()``, and the word
``none`` resets a nullable field (``host``, ``client_port``,
``timeout``, ``is_custom_keys``).

Command‑line Mode
------------------

Use the following options:

+-------------------------------+-----------------------------------------------+
| Option                        | Description                                   |
+===============================+===============================================+
| ``--type {0,1}``              | 0 = Server, 1 = Client. Required for a plain  |
|                               | command-line launch; without it the launcher  |
|                               | runs interactively.                           |
+-------------------------------+-----------------------------------------------+
| ``--setup_addr_port``         | Bind address and port (e.g.                   |
|                               | ``127.0.0.1:8000``). Required with            |
|                               | ``--type``.                                   |
+-------------------------------+-----------------------------------------------+
| ``--connect_addr_port``       | Server address and port to connect to.        |
|                               | Required for Client; rejected in Server mode. |
+-------------------------------+-----------------------------------------------+
| ``--setup_num``               | Number of instances to launch. Only 1 is      |
|                               | allowed; a larger value is accepted but       |
|                               | ignored with a warning.                       |
+-------------------------------+-----------------------------------------------+
| ``--web_server``              | Launch the ``transfer_web`` server tool       |
|                               | (browser UI) instead of a TCP instance.       |
+-------------------------------+-----------------------------------------------+
| ``--web_client``              | Launch the ``transfer_web`` client tool       |
|                               | (browser UI) instead of a TCP instance.       |
+-------------------------------+-----------------------------------------------+
| ``--add PATH [PATH ...]``     | Register extension protocol file(s), then     |
|                               | exit.                                         |
+-------------------------------+-----------------------------------------------+
| ``--delete PATH [PATH ...]``  | Unregister extension protocol file(s), then   |
|                               | exit.                                         |
+-------------------------------+-----------------------------------------------+

A command-line launch replaces ``setup.json`` with the single
launching instance, so any other stored instance is dropped.

Examples
--------

**Launch a single server** on ``127.0.0.1:8000``:

.. code-block:: bash

    python -m PyFlow --type 0 --setup_addr_port 127.0.0.1:8000

**Launch a client** bound to port ``9000``, connecting 
to a server at ``127.0.0.1:8000``:

.. code-block:: bash

    python -m PyFlow --type 1 --setup_addr_port 127.0.0.1:9000 --connect_addr_port 127.0.0.1:8000

**Launch from an existing configuration** 
(if ``setup.json`` is present):

.. code-block:: bash

    python -m PyFlow   # then answer 'N' when asked to overwrite

**Launch the web tool** instead of a TCP instance:

.. code-block:: bash

    python -m PyFlow --web_server
    python -m PyFlow --web_client

**Register an extension protocol file**:

.. code-block:: bash

    python -m PyFlow --add path/to/my_extension.py

Configuration File
==================

The script writes a file named ``setup.json`` in the 
same directory. Its structure is:

.. code-block:: json

    {
      "servers": [
        {
          "host": "127.0.0.1",
          "port": 8000,
          // the remaining server parameters, see below
        }
      ],
      "clients": [
        {
          "client_host": "127.0.0.1",
          "client_port": 9000,
          "host": "127.0.0.1",
          "port": 8000,
          // the remaining client parameters, see below
        }
      ]
    }

**Both lists may contain any number of objects.** Every entry
is completed with the default value of each parameter that
the entry does not set. The stored server entry therefore
carries ``host``, ``port``, ``max_clients``, ``port_add_step``,
``port_range_num``, ``max_file_transfer_thread_num``,
``is_hand_alloc_port``, ``is_input_command_in_console``,
``max_custom_workers``, ``is_extend_command``,
``is_enable_encrypto``, ``is_custom_keys``, ``max_mem_buff``,
``is_asynic_clients_io``, ``is_debug`` and ``is_print_log``;
the client entry carries ``host``, ``client_host``, ``port``,
``client_port``, ``timeout``, ``port_add_step``,
``max_thread_num``, ``is_input_command_in_console``,
``is_wait_server``, ``max_custom_workers``,
``is_extend_command``, ``is_enable_encrypto``,
``is_custom_keys``, ``max_mem_buff``, ``is_debug`` and
``is_print_log``.

Custom Parameters
-----------------

You may edit ``setup.json`` by hand (or change the same fields
in the instance editor) to give any parameter accepted by
``TCP_Server_Base`` or ``TCP_Client_Base`` a non-default value.
Custom values of an existing entry survive later interactive
runs, because the launcher loads the existing entries and
merges the defaults underneath them. A **command-line** launch
does not: it builds the new instance from ``host``/``port``
(and, for a client, the connect address) only and rewrites
``setup.json`` from that, so hand-written extra keys are lost.

Extension Protocols and Startup Mode
-------------------------------------

Two extension protocols ship with the launcher and are
loaded automatically for every instance whose ``setup.json``
entry sets ``is_extend_command=True``:

- ``command_control_extension_tcp.py`` – remote command
  execution with per-client log collection (``/command``,
  with the ``/command_done`` completion report).
- ``forward_extension_tcp.py`` – forwarding messages, files,
  multiple files, folders and multiple folders to any
  number of destination clients (``/file_forward``,
  ``/multiple_file_forward``, ``/folder_forward``,
  ``/multiple_folder_forward`` on the client, and the
  ``/forward_file`` / ``/forward_folder`` relays on the
  server).

Plain-message forwarding is native to the TCP protocol
(no extension needed): the client-only command
``/forward_send_msg`` relays messages to the listed
destination clients through the server.

With ``is_extend_command=False`` (the default) the raw TCP
protocol is started without these two built-in extensions.

Independently of ``is_extend_command``, every launch also
loads the extension protocol files registered in
``PyFlow/added_extensions.json`` (see ``--add`` /
``Add_Extension`` above) via
``add_extension.load_registered_extensions``.

The ``is_input_command_in_console`` flag selects how the
instance is started:

- ``True`` (default) – the instance's own start method
  (``start_TCP_Server()`` / ``start_TCP_client()``) runs and
  keeps its console input loop in the foreground.
- ``False`` – the instance is started in a background thread
  and the launcher keeps the process alive until the
  instance stops (useful for headless deployments).

Both extensions also expose injectable registration
(``setup_server_commands(server)`` /
``setup_client_commands(client)``) and a convenience
``client_setup(instance=None, is_input_command_in_console=True)`` /
``server_setup(instance=None, is_input_command_in_console=True)``
that accepts an existing instance, so several extensions
can be loaded onto the same instance from code.

Internal Operation
==================

- Each instance is launched in a new terminal window
  (or background process).
- The configuration is passed via a temporary JSON
  file to avoid shell escaping issues.
- If an instance fails to start, the error is
  displayed and the window pauses for inspection.

Requirements
============

- Python 3.10+
- The ``PyFlow.network_api.connect_tcp`` module must be
  importable (the script imports ``TCP_Server_Base``
  and ``TCP_Client_Base`` from there).
