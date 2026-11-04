"""Locate the mutable updater behind a completed serving snapshot."""
from __future__ import annotations

import os
from pathlib import Path
import shlex
import shutil


def snapshot_update_command(source: Path) -> list[str] | None:
    """Prove snapshot ownership before selecting a checkout-published launcher.

    Older installs have no inputs/.project-root. Their checkout may live beside
    installs/ or be named by the published PATH convenience wrapper. Candidate
    discovery is only a hint: the install key must independently match its path.
    Never execute a PATH command to discover the owner.
    """
    from hermes_cli.serving_generation import _complete_source
    from pm.environments import install_key

    try:
        source = source.resolve()
        state = source.parent.parent
        if (source.parent.name != "serving-source" or state.parent.name != "installs"
                or not _complete_source(source, source.name)):
            return None
        candidates = []
        pointer = state / "inputs" / ".project-root"
        if pointer.is_file():
            recorded = Path(pointer.read_text(encoding="utf-8-sig").strip())
            if recorded.is_absolute():
                candidates.append(recorded)
        # Compatibility with installs published before the checkout pointer.
        candidates.extend(state.parent.parent.iterdir())
        try:
            command = shutil.which("hermes")
            if command:
                launcher = Path(command).resolve()
                if launcher.suffix.lower() not in {".exe", ".cmd"}:
                    # POSIX convenience wrappers are: #!/bin/sh; exec <launcher> "$@".
                    words = shlex.split(launcher.read_text(encoding="utf-8-sig"), comments=True)
                    if len(words) == 3 and words[0] == "exec" and words[2] == "$@":
                        launcher = Path(words[1]).resolve()
                if launcher.parent.name == "bin" and launcher.parent.parent.name == ".hermes":
                    candidates.append(launcher.parents[2])
        except (OSError, ValueError, RuntimeError):
            pass  # An unrelated PATH launcher cannot invalidate recorded ownership.
        for candidate in candidates:
            checkout = candidate.resolve()
            if install_key(checkout) != state.name or not (checkout / ".git").exists():
                continue
            names = ("hermes.exe", "hermes.cmd") if os.name == "nt" else ("hermes",)
            for name in names:
                launcher = checkout / ".hermes" / "bin" / name
                if launcher.is_file():
                    return [str(launcher)]
    except (OSError, ValueError, RuntimeError):
        pass
    return None
