# Test layout

Tests are split first by the real dependencies they touch, then mirrored
against the package layout one level deep: `PyFlow/<pkg>/` maps to
`test/<tier>/<pkg>/`, and root-level modules map to the tier root. A new
subdirectory appears only when the source gains the matching package.

```
test/
├── unit/           no network I/O, no subprocesses
│   ├── test_flow_setup.py   launcher logic with mocked Popen (PyFlow/flow_setup.py)
│   └── network_api/         object construction, pure logic, real crypto without sockets
├── integration/    real sockets, but server + client live in the test process
│   ├── conftest.py          shared server/client/udp fixtures
│   ├── helpers.py           wait_until / server_ready polling helpers
│   ├── test_forward_extension.py, test_command_handlers.py, ...
│   │                        extension modules at the PyFlow package root
│   └── network_api/         encrypted channel, file transfer, udp, event store, ...
└── crypto_api/     C library tests (CMake/ctest, not pytest), mirroring PyFlow/crypto_api
```

The tree is illustrative, not an inventory — it shows the pattern, not
every file.

- **unit** may load the real crypto library through ctypes; that is not
  network I/O even though it pulls in a shared object.