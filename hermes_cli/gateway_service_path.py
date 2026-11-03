"""Gateway service PATH assembly (launchd plist + systemd unit).

Extracted from ``hermes_cli/gateway.py``. Bodies read facade helpers through ``_gw()``
(late binding on ``hermes_cli.gateway``) so the seams tests and callers patch on the
facade keep intercepting the moved code, per AGENTS.md (new behaviour goes in a topical
sibling, never appended to a facade).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _gw():
    from hermes_cli import gateway  # late: the facade imports this module
    return gateway


def _expand_tilde(entry: str, home: Path) -> str:
    """Expand a leading ``~`` against *home* explicitly (os.path.expanduser reads $HOME, which
    under sudo is the INSTALLER's home — the target user's must be passed in)."""
    if entry == "~":
        return str(home)
    if entry.startswith("~/"):
        return str(home / entry[2:])
    return entry


def _resolve_service_path_entry(entry: str, home: Path | None = None) -> str:
    """Resolve one service PATH entry to an absolute path WITHOUT changing its filesystem
    meaning: relative entries anchor to *home* (else the service working dir), ``~`` expands
    against *home*. ``..`` components are kept verbatim: PATH lookup resolves them through
    the kernel (following symlinks), so liveness checks and carried-forward regeneration must
    see the same spelling — a lexical or realpath rewrite would change what the entry means
    (or resurrect a dir the entry no longer reaches when a symlink target was removed)."""
    target_home = Path(home) if home is not None else Path.home()
    expanded = _gw()._expand_tilde(entry, target_home)
    if not os.path.isabs(expanded):
        expanded = str(Path(_gw()._stable_service_working_dir()) / expanded)
    return expanded


def _service_path_extra_dirs(home: Path | None = None) -> list[str]:
    """``gateway.service_path_extra`` — host-configured extra service PATH dirs, so hosts don't
    hand-edit plists. Entries are kept verbatim (tilde expanded against *home*, relative
    anchored to the service working dir, ``..`` filesystem-resolved) even when they don't
    exist yet: the operator knows the host. Malformed values fail open as no extra dirs.
    Spellings are preserved so regeneration reads back and deduplicates the same string."""
    try:
        from hermes_cli.config import load_config_readonly
        gateway_cfg = (load_config_readonly() or {}).get("gateway", {}) or {}
        raw = gateway_cfg.get("service_path_extra", [])
    except Exception:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    out = []
    for entry in raw:
        entry = str(entry).strip()
        if not entry:
            continue
        out.append(_resolve_service_path_entry(entry, home))
    return out


def _service_tool_dirs(home: Path | None = None) -> list[str]:
    """Package-manager and user bin dirs a service PATH must start with.

    Deterministic: each dir is included whenever it exists on the host, never inherited from
    the invoking shell. Which process regenerates a service definition (an interactive shell,
    ``hermes update``, or the ai.hermes.converge launchd job under launchd's minimal PATH)
    must not change what the service can exec: a converge-driven regeneration once dropped
    /opt/homebrew/bin from the plist and blinded the prod monitors for a week. System dirs
    are appended separately, last (see _service_system_dirs). *home* is the account whose
    ~/.local/bin is probed — system units pass the TARGET user's home, never the installer's."""
    base_home = Path(home) if home is not None else Path.home()
    candidates = [str(base_home / ".local" / "bin")]
    # Known-dir tables live in hermes_platform (AGENTS.md): one spelling per host dir,
    # shared with runtime resolution instead of a private copy here.
    from hermes_platform.resolver.known_dirs import package_manager_dirs
    candidates += package_manager_dirs()
    out: list[str] = []
    for candidate in candidates:
        try:
            if Path(candidate).is_dir():
                out.append(candidate)
        except OSError:
            continue
    return out


def _service_system_dirs() -> list[str]:
    """Trailing system dirs for a service PATH (always last, after tool/carry-forward dirs)."""
    if sys.platform == "darwin":
        return ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    return ["/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin"]


def _normalize_service_path_entry(entry: str) -> str:
    """Comparison spelling for one service PATH entry: collapse duplicate slashes, ``.``
    components and trailing slashes — none of these change which directory a path names —
    but keep ``..`` components verbatim: ``..`` traverses through symlinks at lookup time,
    so a lexical collapse (normpath) would change what the entry means. Purely lexical
    (no filesystem access, no symlink resolution)."""
    if not entry:
        return entry
    normalized = entry
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    while "/./" in normalized:
        normalized = normalized.replace("/./", "/")
    if normalized.endswith("/.") and normalized != "/.":
        normalized = normalized[:-2]
    while normalized.endswith("/") and normalized != "/":
        normalized = normalized[:-1]
    return normalized


def _strip_canonical_system_dirs(entries: list[str], system_dirs: list[str]) -> list[str]:
    """Drop canonical system dirs from *entries* (normalized comparison) — the caller appends
    the canonical tail once, last, so a system dir can never shadow custom tools from a
    mid-PATH position."""
    canonical = {_normalize_service_path_entry(d) for d in system_dirs}
    out = []
    for entry in entries:
        norm = _normalize_service_path_entry(entry)
        if norm in canonical:
            continue
        out.append(entry)
    return out


def _build_service_path_dirs(project_root: Path | None = None, home: Path | None = None) -> list[str]:
    """Deterministic head of the service PATH: host tool dirs, config extras, then project and
    hermes-home extras — excluding non-existent dirs.

    The invoking shell's PATH must never leak in by accident: whichever process regenerates a
    service definition (an interactive shell, ``hermes update``, the converge launchd job)
    must produce the same PATH, so the caller's environment is deliberately not consulted
    (carry-forward/system segments are handled by the backend generators on top of this list).
    *home* scopes user-relative discovery (``~/.local/bin`` probe, config-extra expansion):
    system units pass the TARGET user's home; everyone else means the invoking account."""
    if project_root is None:
        project_root = _gw().PROJECT_ROOT

    def _is_dir(path: Path) -> bool:
        try:
            return path.is_dir()
        except OSError:
            return False

    candidates = _gw()._service_tool_dirs(home)
    candidates += _gw()._service_path_extra_dirs(home)
    # Python and dependency executable paths are selected at boot, not persisted.

    hermes_home = _gw().get_hermes_home()
    extras = (project_root / "node_modules" / ".bin", hermes_home / "node" / "bin", hermes_home / "node_modules" / ".bin")
    for extra in extras:
        if _is_dir(extra):
            candidates.append(str(extra))
    return candidates


