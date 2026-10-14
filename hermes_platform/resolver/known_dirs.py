"""Directories where tools install outside PATH, grouped by the ecosystem that owns them.

Every table in Hermes lives here. A directory literal outside this module fails the ratchet in
`tests/test_managed_runtime_resolution.py`. Each table is empty on an OS where the ecosystem
does not install there, so callers compose tables without OS branches.
"""

from __future__ import annotations

import sys

_POSIX = sys.platform != "win32"
_WIN = sys.platform == "win32"


def homebrew_dirs() -> tuple[str, ...]:
    return ("/opt/homebrew/bin", "/usr/local/bin") if sys.platform == "darwin" else ()


def package_manager_dirs() -> tuple[str, ...]:
    """Package-manager bin/sbin dirs a SERVICE PATH starts with (the caller liveness-checks
    and drops absent ones). macOS composes from ``homebrew_dirs()`` so Homebrew entries keep
    a single spelling here, each prefix contributing its ``bin`` then its ``sbin``; the
    ``sbin`` companions are service-PATH specific (launchd/systemd daemons may need admin
    tools, interactive resolution does not). Empty on Windows: services there are built from
    absolute paths, not a PATH payload."""
    if sys.platform == "darwin":
        return tuple(
            path
            for bin_dir in homebrew_dirs()
            for path in (bin_dir, bin_dir[:-3] + "sbin")
        )
    if _POSIX:
        return ("/usr/local/sbin", "/usr/local/bin")
    return ()


def user_local_bin() -> tuple[str, ...]:
    return ("~/.local/bin",) if _POSIX else ("%USERPROFILE%/.local/bin",)


def rust_tool_dirs() -> tuple[str, ...]:
    return ("~/.cargo/bin",) if _POSIX else ("%USERPROFILE%/.cargo/bin",)


def node_tool_dirs() -> tuple[str, ...]:
    return ("~/.npm-global/bin", "~/.bun/bin", "~/.volta/bin") if _POSIX else ("%APPDATA%/npm", "%USERPROFILE%/.bun/bin", "%LOCALAPPDATA%/Volta/bin")


def hermes_vendored_dirs() -> tuple[str, ...]:
    return ("~/.hermes/bin",) if _POSIX else ("%USERPROFILE%/.hermes/bin",)


def windows_user_program_dirs() -> tuple[str, ...]:
    if not _WIN:
        return ()
    return (
        "%LOCALAPPDATA%/Programs",
        "%USERPROFILE%/scoop/shims",
        "%LOCALAPPDATA%/Microsoft/WinGet/Links",
    )
