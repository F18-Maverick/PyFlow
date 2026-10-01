"""Build the crypto_api C library into the installed package.

``PyFlow.network_api.rsa_crypto`` loads the library with ``ctypes`` at run
time, so it is declared as a setuptools *extension* only to make the build
backend compile it and place it next to the Python modules: the produced file
has no ``PyInit_*`` and is never imported. Using the extension mechanism gives
it the platform tag (``PyFlow._crypto_api`` -> ``PyFlow/_crypto_api.cpython-3XX-
<platform>.so``) and turns the wheel from ``py3-none-any`` into a
platform-specific one built against the toolchain present on the machine.

Installing the source distribution (or the repository) therefore produces the
library automatically; building the repository with CMake is only needed for
the C test suite and for C consumers (see ``CMakeLists.txt``).

Requirements: a C compiler plus OpenSSL 1.1.1 development headers. On Windows
OpenSSL has no default location, so ``OPENSSL_ROOT_DIR`` is honoured there, as
in the CMake build, with the usual installer directories as fallbacks.
"""

import os
import sys
from pathlib import Path

from setuptools import Extension, setup

# setuptools rejects absolute paths in setup() arguments: everything below is
# relative to this file's directory.
CRYPTO_API_DIR = "PyFlow/crypto_api"
CRYPTO_API_INCLUDE = CRYPTO_API_DIR + "/include"
SOURCES = sorted(str(path) for path in Path(CRYPTO_API_DIR).glob("*.c"))

# Installer default locations of the OpenSSL development files on Windows.
_WINDOWS_OPENSSL_ROOTS = (
    r"C:\Program Files\OpenSSL-Win64",
    r"C:\Program Files\OpenSSL",
    r"C:\Program Files (x86)\OpenSSL-Win32",
)

OPENSSL_MISSING = (
    "OpenSSL development files were not found; PyFlow compiles its crypto library "
    "against them. Install OpenSSL 1.1.1 or newer, or point OPENSSL_ROOT_DIR at a "
    "directory containing include/openssl/ and lib/libcrypto.lib."
)


def _windows_openssl_root():
    """Return the first Windows OpenSSL root that has headers and an import library."""
    env_root = os.environ.get("OPENSSL_ROOT_DIR")
    roots = [env_root, *_WINDOWS_OPENSSL_ROOTS] if env_root else list(_WINDOWS_OPENSSL_ROOTS)
    for root in roots:
        path = Path(root)
        if (path / "include" / "openssl" / "opensslv.h").is_file() and (
            path / "lib" / "libcrypto.lib"
        ).is_file():
            return path
    return None


include_dirs = [CRYPTO_API_INCLUDE]
library_dirs = []
define_macros = []
libraries = ["crypto"]

if sys.platform == "win32":
    define_macros.append(("PF_CRYPTO_SHARED", "1"))  # __declspec(dllexport)
    openssl_root = _windows_openssl_root()
    if openssl_root is None:
        raise SystemExit(OPENSSL_MISSING)
    include_dirs.append((openssl_root / "include").as_posix())
    library_dirs.append((openssl_root / "lib").as_posix())
    libraries = ["libcrypto"]

crypto_api = Extension(
    "PyFlow._crypto_api",
    sources=SOURCES,
    include_dirs=include_dirs,
    library_dirs=library_dirs,
    libraries=libraries,
    define_macros=define_macros,
)

setup(ext_modules=[crypto_api])
