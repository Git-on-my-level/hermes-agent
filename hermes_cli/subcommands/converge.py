"""``hermes converge`` — install/run the idle pin-converge agent."""
from __future__ import annotations

from typing import Callable


def build_converge_parser(subparsers, *, cmd_converge: Callable) -> None:
    p = subparsers.add_parser(
        "converge",
        help="Converge this install onto updates.pin when idle",
        description=(
            "When idle (or after the current turn past converge_busy_sla), apply "
            "updates.pin if set, else the configured update channel tip "
            "(updates.remote/updates.branch). No fleetctl dependency."
        ),
    )
    sub = p.add_subparsers(dest="converge_action")
    sub.add_parser("run", help="One tick (same as hermes update --converge -y)")
    ins = sub.add_parser("install", help="Install the macOS LaunchAgent timer")
    ins.add_argument("--force", action="store_true", help="Write the plist even if updates.converge is false")
    sub.add_parser("uninstall", help="Remove the converge LaunchAgent")
    sub.add_parser("status", help="Show pin, skip reason, and agent path")
    p.set_defaults(func=cmd_converge)
