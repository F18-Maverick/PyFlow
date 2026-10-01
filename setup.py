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

Requirements: a C compiler plus OpenSSL 1.1.1 development headers. MSVC has no
default location for OpenSSL, so ``build_ext`` locates it on Windows (see
``_find_windows_openssl``) - only when compiling, never while metadata or
requirement lists are generated.
"""

import os
import sys
from pathlib import Path

from setuptools import Extension, setup
from setuptools.command.build_ext import build_ext

# setuptools rejects absolute paths in setup() arguments: everything below is
# relative to this file's directory.
CRYPTO_API_DIR = "PyFlow/crypto_api"
CRYPTO_API_INCLUDE = CRYPTO_API_DIR + "/include"
SOURCES = sorted(str(path) for path in Path(CRYPTO_API_DIR).glob("*.c"))

# Windows: where the OpenSSL installers put their development files. The
# entries with ``lib/VC`` are the layout of Shining Light's (slproweb) full
# installer, which GitHub's windows runners install into Program Files;
# the bare ``lib``/``lib64`` entries cover vcpkg and source builds.
OPENSSL_ROOTS = (
    r"C:\Program Files\OpenSSL",
    r"C:\Program Files\OpenSSL-Win64",
    r"C:\Program Files (x86)\OpenSSL-Win32",
    r"C:\OpenSSL-Win64",
    r"C:\OpenSSL",
)
OPENSSL_LIB_SUBDIRS = (
    ("lib", "VC", "x64", "MD"),  # /MD matches the CPython runtime
    ("lib", "VC", "x64", "MT"),
    ("lib", "VC", "x64"),
    ("lib", "VC"),
    ("lib", "x64"),
    ("lib64",),
    ("lib",),
)

OPENSSL_MISSING = (
    "OpenSSL development files were not found; PyFlow compiles its crypto library "
    "against them. Install OpenSSL 1.1.1 or newer with the Win64 installer from "
    "https://slproweb.com/products/Win32OpenSSL.html - the default installer "
    'includes the development files, the "Light" one does not - or point '
    "OPENSSL_ROOT_DIR at a directory containing include/openssl/opensslv.h and "
    "libcrypto.lib. Searched: {}"
)


def _openssl_roots():
    """Return the Windows OpenSSL roots to search, ``OPENSSL_ROOT_DIR`` first."""
    env_root = os.environ.get("OPENSSL_ROOT_DIR")
    roots = [env_root] if env_root else []
    roots.extend(root for root in OPENSSL_ROOTS if root != env_root)
    return roots


def _find_windows_openssl(roots=None) -> tuple[Path, Path] | None:
    """Return ``(include_dir, library_dir)`` of a usable OpenSSL, or ``None``.

    Args:
        roots (Iterable | None): Candidate installation roots; the environment
            and the built-in list are used when omitted.

    Returns:
        tuple[Path, Path] | None: Include directory and the directory holding
        ``libcrypto.lib``, or ``None`` when no root is usable.
    """
    for root in _openssl_roots() if roots is None else roots:
        path = Path(root)
        include_dir = path / "include"
        if not (include_dir / "openssl" / "opensslv.h").is_file():
            continue
        for parts in OPENSSL_LIB_SUBDIRS:
            library_dir = path.joinpath(*parts)
            if (library_dir / "libcrypto.lib").is_file():
                return include_dir, library_dir
        # Unknown layout: the import library is small and the trees are shallow.
        for found in sorted(path.rglob("libcrypto.lib")):
            return include_dir, found.parent
    return None


class OpenSslBuildExt(build_ext):
    """Compile the crypto library, resolving OpenSSL for MSVC first."""

    def build_extension(self, extension: Extension) -> None:
        """Select the C11 dialect and, on Windows, locate OpenSSL, then build.

        distutils passes no C-standard flag and MSVC's default mode predates
        C11, which the sources use (``_Static_assert``); the CMake build sets
        the same standard through ``C_STANDARD 11``.

        Args:
            extension (Extension): The extension to compile.

        Raises:
            SystemExit: If Windows OpenSSL development files are not found.
        """
        extension.extra_compile_args.append(
            "/std:c11" if self.compiler.compiler_type == "msvc" else "-std=c11"
        )
        if sys.platform == "win32":
            extension.define_macros.append(("PF_CRYPTO_SHARED", "1"))  # dllexport
            if self.compiler.compiler_type == "msvc":
                # MinGW-style toolchains find their own OpenSSL through their
                # own search paths; the layouts below are MSVC import libraries.
                found = _find_windows_openssl()
                if found is None:
                    raise SystemExit(OPENSSL_MISSING.format(", ".join(_openssl_roots())))
                include_dir, library_dir = found
                extension.include_dirs.append(str(include_dir))
                extension.library_dirs.append(str(library_dir))
                extension.libraries = ["libcrypto"]
        super().build_extension(extension)

    def get_export_symbols(self, extension: Extension) -> list[str]:
        """Export no ``PyInit_*`` symbol when MSVC links the library.

        MSVC links every extension with ``/EXPORT:PyInit_<name>``, but the
        crypto library is loaded through ``ctypes`` and never defines that
        symbol, so the link fails with LNK2001. The ``pf_*`` entry points it
        does need to export are marked by ``PF_CRYPTO_SHARED`` instead.

        Args:
            extension (Extension): The extension being linked.

        Returns:
            list[str]: The symbols MSVC is told to export (none for MSVC).
        """
        symbols = super().get_export_symbols(extension)
        compiler = getattr(self, "compiler", None)
        if compiler is not None and compiler.compiler_type == "msvc":
            return []
        return symbols


crypto_api = Extension(
    "PyFlow._crypto_api",
    sources=SOURCES,
    include_dirs=[CRYPTO_API_INCLUDE],
    libraries=["crypto"],
)

setup(ext_modules=[crypto_api], cmdclass={"build_ext": OpenSslBuildExt})
