TCP Port Allocation Mechanism
==============================

This document describes the port allocation subsystem
implemented in both the
``TCP_Server_Base`` and ``TCP_Client_Base`` classes. The
port allocation
mechanism provides a unified interface for obtaining
ephemeral ports, supporting
both **automatic** (OS‑assigned) and **manual**
(range‑based) allocation modes.

Port allocation is used primarily for file transfer
secondary connections,
temporary servers, and temporary clients. The design
ensures that ports are
allocated and released without conflicts, even when
multiple server or client
instances run on the same host.

For a high‑level overview of the server and client
classes, please refer to
:doc:`../Network_APIs/TCP_Server_APIs` and :doc:`../Network_APIs/TCP_Client_APIs`.

.. _port-allocation-overview:

Overview
--------

The port allocation API consists of two core methods:

- ``palloc()`` – obtain an available port.
- ``pfree(port)`` – release a previously allocated port.

The behaviour of these methods depends on the value of the
``is_hand_alloc_port`` flag:

- **Automatic mode** (``is_hand_alloc_port`` false, the
  default): ``palloc()``
  returns ``0``. When used in a socket ``bind()`` call,
  the operating system
  automatically assigns a free ephemeral port.
  ``pfree()`` does nothing.
- **Manual mode** (``is_hand_alloc_port`` true): Ports
  are drawn from a
  configurable numeric range. The caller must eventually
  release each allocated
  port with ``pfree()``.

``TCP_Server_Base`` takes the flag as a constructor
argument (``is_hand_alloc_port=False`` by default).
``TCP_Client_Base`` has no such argument: its
``is_hand_alloc_port`` attribute starts as ``None``
(automatic mode) and is set by the server's port-range
announcement, see :ref:`client-range-configuration`.

Automatic mode is strongly recommended for most
applications because it avoids
port conflicts and simplifies code. Manual mode is
provided for environments
where port ranges must be strictly controlled (e.g.,
firewalls, testing, or
multiple processes sharing the same host).

.. _manual-allocation-range:

Manual Allocation Range
-----------------------

When manual mode is enabled, the allocatable port range is
determined by three
parameters set during class initialisation:

- ``port`` – the main server or client port (e.g.,
  65432).
- ``port_add_step`` – the step size for
  incrementing/decrementing ports.
- ``port_range_num`` – the total number of steps to scan
  in each direction.

From these, the minimum and maximum allocatable ports are
calculated as:

- ``min_port = port - port_add_step * port_range_num``
- ``max_port = port + 1 + port_add_step *
  port_range_num``

For example, with ``port=65432``, ``port_add_step=1``,
``port_range_num=100``,
the allocatable ports range from ``65432 - 100 = 65332``
up to
``65432 + 1 + 100 = 65533``.

Two independent allocation “directions” are maintained:

- **Additive allocation** – the cursor starts at ``port+1``
  and is advanced by ``port_add_step`` *before* the port is
  handed out, so the first additive port actually returned
  is ``port + 1 + port_add_step`` (``port+2`` with the
  default step). When the cursor would pass ``max_port``,
  a fallback scan walks ``port+1`` upward by
  ``port_add_step`` (up to but not including ``max_port``)
  and returns the first port that is not in the global
  allocated list.
- **Subtractive allocation** – starts from ``port`` and
  moves downward by
  ``port_add_step`` each time (first returned port:
  ``port - port_add_step``), down to (but not
  including) ``min_port``. Its fallback scan starts at
  ``port`` itself.

The two directions use separate locks and separate current
position pointers,
allowing two concurrent allocations to proceed in opposite
directions without
colliding, thereby reducing contention.

.. _palloc-behaviour:

palloc() Behaviour in Manual Mode
---------------------------------

When ``palloc()`` is called in manual mode, it first tries
additive allocation. If additive allocation returns a
port, that port is
returned immediately. Otherwise, it tries subtractive
allocation. If
subtractive allocation also
fails (meaning no ports are available in either direction),
the loop waits 0.1 seconds for a port to come back and
retries. Only the exhausted round pays that wait; a
successful ``palloc()`` returns straight away.

In automatic mode ``file_palloc()`` returns ``0`` (which
is not ``None``), so ``palloc()`` returns ``0`` on the
first iteration without waiting.

Additive allocation works as follows:

- If the next additive step would exceed the maximum
  allowed value, the
  allocator scans all remaining ports in the additive
  direction (starting from
  ``port+1``, stepping by ``port_add_step``, up to but
  not including
  ``max_port``). The first port that is not already in
  the global allocated list
  is returned. If none is found, additive allocation
  fails.
- Otherwise, the additive pointer is advanced by
  ``port_add_step``, the new port
  is added to the global allocated list, and the port is
  returned.

Subtractive allocation works symmetrically, scanning
downward from ``port`` to
(but not including) ``min_port``.

.. _pfree-behaviour:

pfree() Behaviour in Manual Mode
--------------------------------

When a port is released via ``pfree(port)``:

- The port is removed from the global allocated list (if
  present).
- The additive pointer is **decremented** by
  ``port_add_step``.
- The subtractive pointer is **incremented** by
  ``port_add_step``.

These pointer adjustments occur regardless of which
direction originally
allocated the port. This allows freed ports to be reused
in future allocations.

.. _persistence-and-file-locks:

Persistence and File Locks (Manual Mode Only)
----------------------------------------------

When manual mode is enabled, the server or client must
remember allocated port
ranges across multiple instances to avoid conflicts. This
is achieved through
persistent log files stored in the ``.Flow/temp_info/``
directory.

- **Server**: ``server_port_info.log`` stores a list of
  dictionaries, each
  describing a server instance (server_id, host, port,
  min_port, max_port,
  is_running, pid) plus the ``started_at`` timestamp of
  the entry.
- **Client**: ``clients_port_info.log`` stores similar
  information for client
  instances.

``is_running`` is written once, when the entry is created;
``pid`` is the process that owns the range and is what the
sweep actually checks: the next instance drops every entry
whose process no longer exists (a killed instance never
reaches ``free_port``, so this is what reclaims its range).
A record written before ``pid`` existed falls back to the
old rule (``is_running=False`` is removed). Windows uses
``OpenProcess``/``WaitForSingleObject`` for the check; on
POSIX it is ``os.kill(pid, 0)``. When the state cannot be
determined the entry is kept — a stale range costs a few
ports, dropping a live one causes a conflict.

Before reading or writing these files, a lock file
(``server_port_lock.lock`` or
``client_port_lock.lock``) is used to prevent concurrent
access. The lock is
implemented by simply creating the file; the existence of
the file indicates
that another process is currently modifying the port
information, and the allocator waits for it to disappear.

The file carries no owner and no timestamp: an instance
that is killed while it holds the lock leaves it behind, and
every later manual-mode instance waits for it indefinitely.
The allocator releases the lock in a ``finally`` block, so a
failed allocation does not leak it; only a hard kill between
the lock and the unlock does. Deleting the file (or calling
``server_port_temp_info_file_unlock`` /
``client_port_temp_info_file_unlock``) clears it.

When a new server or client instance is created with
manual allocation enabled,
its range is initialised using the persistent log:

- If the log file does not exist, a new list is created
  with the current
  instance as the first entry.
- If the log file exists, the list of previous instances
  is read. The next
  instance ID is the previous ID + 1.
- If the user‑supplied port falls into a band one full
  range around the last recorded instance — that is,
  between ``last.min_port - port_add_step * port_range_num - 1``
  and ``last.max_port + port_add_step * port_range_num + 1``
  — it is moved to the fixed value
  ``last.max_port + port_add_step * port_range_num + 1`` to
  prevent overlap.
- Entries whose owning process is gone are removed first,
  so a range held by a killed instance is reclaimed here.
  The new instance’s information is then appended to the
  list and the list is written back. Its entry survives
  because its own process is alive, and ``hand_free_port``
  removes it on a clean stop; an entry from a killed
  instance is only kept if its pid was reused by an
  unrelated process before the next allocation.

When the server or client stops, its own entry is removed
from the log file. If
the list becomes empty, the log file is deleted.

.. _client-range-configuration:

Client‑Side Range Configuration
-------------------------------

The client’s manual allocation mode is enabled not by a
constructor argument
directly, but by a **message from the server**. Each
accepted connection is greeted with a command:

``/client_alloc_port_range <each_client_port_range>``

where ``each_client_port_range = port_range_num //
max_clients``, and ``/client_alloc_port_range NO_LIMIT``
is sent instead when the server runs with
``is_hand_alloc_port=False``. The message goes only to the
connection that just joined; the range is fixed for the
server's lifetime.

The client’s ``handle_server_command`` processes this:

- If the value is ``"NO_LIMIT"``, the client sets
  ``is_hand_alloc_port=False``
  (automatic mode).
- Otherwise, it sets ``is_hand_alloc_port=True``, keeps
  the value in ``each_client_port_range`` and
  calls its internal
  initialisation routine with the received range, using
  its own ``port`` (the
  server port it connected to) as the base.

This allows the server to control the port‑allocation
policy for all connected
clients uniformly. Until that message arrives (and whenever
the server runs in automatic mode) the client stays in
automatic mode.

.. _api-definitions:

Public API Definitions
----------------------

### Server‑Side Port Allocation APIs

.. code-block:: python

    def palloc(self)
    def pfree(self, port)

``palloc()`` returns an available port number. In automatic
mode it returns ``0``. In manual mode it returns a concrete
port from the configured range and waits 0.1 seconds per
retry while the range is exhausted.

``pfree(port)`` releases a port previously obtained by
``palloc()``.
In automatic mode it does nothing. In manual mode it
removes the port from the internal allocated list and
adjusts the allocation pointers.

Both are thin wrappers around the direction-specific
helpers, which are public as well:
``file_palloc()`` / ``file_pfree(port)`` for the additive
direction and ``spy_palloc()`` / ``spy_pfree(port)`` for
the subtractive one. ``alloc_port(port_add_step, port_range_num)``
and ``free_port()`` reserve and release the instance's range
in the persistent log (both no-ops in automatic mode), and
``hand_alloc_port`` / ``hand_free_port`` implement the
on-disk bookkeeping.

### Client‑Side Port Allocation APIs

The client provides the same method set
(``palloc``, ``pfree``, ``file_palloc``, ``file_pfree``,
``spy_palloc``, ``spy_pfree``, ``alloc_port``, ``free_port``,
``hand_alloc_port``, ``hand_free_port``).

Additionally, the client receives the range from the
server via the
``/client_alloc_port_range`` command.

.. _port-allocation-api-summary:

Public API Summary
-------------------

All public port‑allocation related APIs are listed below.
For a complete list
of all public APIs, please see the respective server and
client documentation.

### TCP_Server_Base

- ``palloc`` / ``pfree``
- ``file_palloc`` / ``file_pfree``
- ``spy_palloc`` / ``spy_pfree``
- ``alloc_port`` / ``free_port``
- ``hand_alloc_port`` / ``hand_free_port``

### TCP_Client_Base

- ``palloc`` / ``pfree``
- ``file_palloc`` / ``file_pfree``
- ``spy_palloc`` / ``spy_pfree``
- ``alloc_port`` / ``free_port``
- ``hand_alloc_port`` / ``hand_free_port``

Note that the port counters differ between the two classes:
the server seeds ``all_allocated_ports_list`` with its own
port (``[self.port]``), the client starts from an empty list.
The subtractive fallback scan includes the base port itself,
so an exhausted client range can hand out the port the
server never allocates for itself.

See Also
--------

For more information about the TCP server and client base
classes, please refer
to:

- :doc:`../Network_APIs/TCP_Server_APIs`
- :doc:`../Network_APIs/TCP_Client_APIs`

For details on the file transfer mechanism that uses these
port allocation APIs,
see :doc:`../File_Transfer/File_Transfer`.
