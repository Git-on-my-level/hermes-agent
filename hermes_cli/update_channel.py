"""Resolve the git remote/branch ``hermes update`` tracks.

Stock default is ``origin`` / ``main``. Hosts on a maintained fork set
``updates.remote`` / ``updates.branch`` in host-local ``config.yaml``.
CLI ``--remote`` / ``--branch`` override config. Empty/whitespace CLI
values do not override.

This module is the single owner so ``update_cmd.py`` / ``main.py`` do not
grow a second channel state machine.
"""

from __future__ import annotations

from typing import Any


_DEFAULT_REMOTE = "origin"
_DEFAULT_BRANCH = "main"


def load_update_channel_defaults() -> tuple[str, str]:
    """Return ``(remote, branch)`` from config, falling back to origin/main."""
    remote = _DEFAULT_REMOTE
    branch = _DEFAULT_BRANCH
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
        updates = cfg.get("updates") or {}
        if isinstance(updates, dict):
            raw_remote = str(updates.get("remote") or "").strip()
            raw_branch = str(updates.get("branch") or "").strip()
            if raw_remote:
                remote = raw_remote
            if raw_branch:
                branch = raw_branch
    except Exception:
        pass
    return remote, branch


def resolve_update_target(args: Any = None) -> tuple[str, str]:
    """CLI overrides, then config, then origin/main."""
    remote, branch = load_update_channel_defaults()
    cli_remote = str(getattr(args, "remote", None) or "").strip()
    cli_branch = str(getattr(args, "branch", None) or "").strip()
    if cli_remote:
        remote = cli_remote
    if cli_branch:
        branch = cli_branch
    return remote, branch


def resolve_update_branch(args: Any = None) -> str:
    """Branch half of :func:`resolve_update_target` (legacy call sites)."""
    return resolve_update_target(args)[1]


def is_stock_upstream_probe(remote: str, branch: str) -> bool:
    """True when --check may still prefer a fork's ``upstream/main``."""
    return remote == _DEFAULT_REMOTE and branch == _DEFAULT_BRANCH
