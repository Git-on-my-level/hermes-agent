"""One argv walk for profile selection, shared by the CLI and read-only health.

The live parser derives which top-level flags take a value. Callers that must not
build that parser pass the canonical fallback snapshot from ``hermes_cli._parser``.
The walk — ``--``, ``mcp add --args``, and values owned by other flags — lives here
so those callers cannot grow a second flag list.
"""

from __future__ import annotations

import os
from typing import NamedTuple

from hermes_constants import PROFILE_ID_RE


class ProfileFlag(NamedTuple):
    """A ``-p``/``--profile`` seen before ``--`` or an ``mcp add --args`` passthrough."""

    name: str | None
    consume: int
    index: int | None
    rejected: str | None
    saw_subcommand: bool
    option_looking: bool


def inside_mcp_add_args(argv: list[str], index: int) -> bool:
    """True once argv reaches ``hermes mcp add ... --args <command argv>``."""
    try:
        mcp_index = argv.index("mcp", 0, index)
        argv.index("add", mcp_index + 1, index)
    except ValueError:
        return False
    return True


def _takes_value(
    argv: list[str],
    index: int,
    value_flags: frozenset[str],
    optional_value_flags: frozenset[str],
) -> bool:
    token = argv[index]
    if "=" in token or index + 1 >= len(argv):
        return False
    if token in value_flags:
        return True
    return token in optional_value_flags and not argv[index + 1].startswith("-")


def scan_profile_flag(
    argv: list[str],
    value_flags: frozenset[str],
    optional_value_flags: frozenset[str],
) -> ProfileFlag:
    """Find ``-p``/``--profile`` before ``--`` and before ``mcp add --args``.

    A value owned by another top-level flag is skipped, so ``--model -p`` does not
    select a profile named ``-p``. ``--profile=NAME`` is returned casefolded without
    a grammar check; the caller validates it. An invalid ``-p NAME`` stops the scan
    and is reported as ``rejected`` rather than as a selected profile.
    """
    index = 0
    saw_subcommand = False
    while index < len(argv):
        token = argv[index]
        if token == "--" or (token == "--args" and inside_mcp_add_args(argv, index)):
            break
        if token in {"--profile", "-p"} and index + 1 < len(argv):
            raw = argv[index + 1]
            value = raw.strip().casefold()
            if PROFILE_ID_RE.fullmatch(value):
                return ProfileFlag(value, 2, index, None, saw_subcommand, False)
            return ProfileFlag(
                None, 0, None, raw, saw_subcommand, raw.startswith("-") or ":" in raw,
            )
        if token.startswith("--profile="):
            return ProfileFlag(
                token.split("=", 1)[1].strip().casefold(), 1, index, None, saw_subcommand, False,
            )
        takes_value = _takes_value(argv, index, value_flags, optional_value_flags)
        if not takes_value and not token.startswith("-"):
            saw_subcommand = True
        index += 2 if takes_value else 1
    return ProfileFlag(None, 0, None, None, saw_subcommand, False)


def command_positionals(argv: list[str], value_flags: frozenset[str]) -> list[str]:
    """Subcommand and its arguments, excluding top-level flags and their values.

    ``value_flags`` already includes optional-value flags the caller wants consumed.
    ``--`` starts the positional region. This is the same walk as
    ``hermes_cli._parser.command_argv``.
    """
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            return argv[index + 1:]
        if not token.startswith("-"):
            return argv[index:]
        index += 2 if "=" not in token and token in value_flags and index + 1 < len(argv) else 1
    return []


def under_gateway_supervisor(argv: list[str]) -> bool:
    """A supervisor-launched gateway child must not follow the sticky profile.

    Matches ``hermes_cli.main._apply_profile_override``: a bare supervised gateway
    keeps the root home it was launched with.
    """
    if os.environ.get("HERMES_SUPERVISED_CHILD") or os.environ.get("HERMES_S6_SUPERVISED_CHILD"):
        return True
    is_gateway_cmd = next((arg for arg in argv if not arg.startswith("-")), None) == "gateway"
    if is_gateway_cmd and os.environ.get("INVOCATION_ID"):
        return True
    return os.environ.get(
        "HERMES_GATEWAY_EXTERNAL_SUPERVISOR", ""
    ).strip().lower() in {"1", "true", "yes", "on"}


def s6_supervised_gateway_run(argv: list[str]) -> bool:
    """A bare ``gateway run`` inside the s6 image does not follow the sticky profile."""
    words = [arg for arg in argv if not arg.startswith("-")]
    if words[:2] != ["gateway", "run"] or "--no-supervise" in argv:
        return False
    if os.environ.get("HERMES_GATEWAY_NO_SUPERVISE", "").lower() in ("1", "true", "yes"):
        return False
    from hermes_cli.service_manager import _s6_running

    return _s6_running()


def sticky_profile_applies(argv: list[str]) -> bool:
    """True when a root home may be redirected by ``active_profile``.

    Explicit ``-p``/``--profile`` is decided by the caller. This is the other half
    of ``_apply_profile_override``: supervisor children, Desktop's SSH backend, and
    the s6 image's ``gateway run`` keep the home they were started with.
    """
    from hermes_cli._startup_fast import is_desktop_ssh_backend_argv

    return not (
        under_gateway_supervisor(argv)
        or is_desktop_ssh_backend_argv(argv)
        or s6_supervised_gateway_run(argv)
    )
