The UDP API
===========

UDP setup API
-------------

The UDP Setup API is used to create a UDP endpoint. The UDP
endpoint in the protocol is usually used for service discovery,
broadcast, and simple request-response communication.

.. code-block:: python

    class UDP:
        def __init__(self, host="0.0.0.0", port=0):
            ...

The UDP endpoint is defined in the ``UDP`` class. The socket is
created and bound in the constructor. Both ``SO_REUSEADDR`` and
``SO_BROADCAST`` are set automatically.

Parameters of the ``__init__`` method are as follows:

- ``host``: The interface to bind to. Default is ``"0.0.0.0"``
  (all interfaces).
- ``port``: The port to bind to. Default is ``0``, which lets
  the OS choose an available port dynamically.

Every parameter has a default value:

- ``host``: Default is ``"0.0.0.0"``
- ``port``: Default is ``0``

*Note: The public API of the UDP endpoint is based on IPv4 form.*

The module also exposes ``MAX_DATAGRAM`` (65535), the receive
buffer size used by ``listen``.

UDP endpoint API
----------------

The public methods available on a ``UDP`` instance are:

.. code-block:: python

    def send(self, data, addr):
        ...
    def broadcast(self, data, port):
        ...
    def listen(self, handler):
        ...
    def close(self):
        ...

``send``
    Wraps ``socket.sendto(data, addr)``. The ``addr`` parameter
    should be a ``(host, port)`` tuple. Raises ``OSError`` if
    the socket is closed or the address is invalid. Note that
    a successful return does not confirm delivery.

``broadcast``
    Sends a datagram to the limited broadcast address
    ``255.255.255.255`` on the given port. This is implemented
    by calling ``send`` with the address ``("255.255.255.255", port)``.

``listen``
    Starts a daemon thread that receives incoming datagrams.
    The handler is a callable ``handler(data, addr)`` that
    is invoked for each datagram received. Datagrams are read
    with a buffer of ``MAX_DATAGRAM`` (65535) bytes and a
    0.1-second socket timeout, so an idle endpoint wakes up
    ten times a second instead of blocking indefinitely.
    Handler exceptions
    are logged via the ``logging`` module rather than propagated,
    so a faulty handler does not terminate the receive loop.
    This method is idempotent: calling it while the receive
    thread is already running does nothing.

``close``
    Signals the receive loop to exit and closes the underlying
    socket. The ``_closed`` flag is set to ``True`` and the
    socket's ``close()`` method is called; the receive loop
    notices the closed socket on its next timeout tick (or
    when ``recvfrom`` raises ``OSError``) and returns. This
    method is idempotent:
    calling it multiple times does not raise an error.

*Note: When ``listen`` is called after ``close``, a*
*``RuntimeError`` *with the message "endpoint is closed" is*
*raised, because the underlying socket is no longer available.*

.. code-block:: python

    @property
    def port(self):
        ...
    @property
    def local_addr(self):
        ...

``port``
    Returns the port number the socket is bound to. Useful when
    ``port=0`` was passed to the constructor and the OS assigned
    a dynamic port.

``local_addr``
    Returns the full ``(host, port)`` tuple that the socket is
    bound to.

The ``UDP`` class also supports the context manager protocol,
so it can be used in a ``with`` statement:

.. code-block:: python

    with UDP("127.0.0.1", 65000) as u:
        ...

*Note: The context manager calls ``close`` automatically when*
*the ``with`` block exits.*

Key differences from TCP
------------------------

- **No file transfer**. UDP is unreliable; use TCP classes for file transfers.
- **No connection tracking**. Each datagram carries its own source address.
- **No port allocation system**. UDP does not need ephemeral ports for
  data connections.
- **Broadcast**. UDP's standout feature — one send reaches all listeners
  on the subnet.

See Also
--------

- :doc:`TCP_Server_APIs` — TCP server.
- :doc:`TCP_Client_APIs` — TCP client.
