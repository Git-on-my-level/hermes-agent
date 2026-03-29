from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional, Tuple

from hermes_cli.config import get_hermes_home

CHANNEL_FILE_NAME = "install-channel"
DEFAULT_TARGET: Tuple[str, str, str] = ("main", "origin", "main")
PROD_TARGET: Tuple[str, str, str] = ("prod", "fork", "prod")


def channel_file_path(hermes_home: Optional[Path] = None) -> Path:
    home = hermes_home or get_hermes_home()
    return home / CHANNEL_FILE_NAME


def read_update_channel(hermes_home: Optional[Path] = None) -> Optional[str]:
    path = channel_file_path(hermes_home)
    try:
        value = path.read_text().strip().lower()
    except Exception:
        return None
    return value if value in {"main", "prod"} else None


def write_update_channel(channel: str, hermes_home: Optional[Path] = None) -> None:
    normalized = channel.strip().lower()
    if normalized not in {"main", "prod"}:
        return
    path = channel_file_path(hermes_home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(normalized + "\n")
    except Exception:
        pass


def _git_output(repo_dir: Path, args: list[str], *, check: bool = False) -> Optional[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        if check:
            raise subprocess.CalledProcessError(result.returncode, ["git", *args], output=result.stdout, stderr=result.stderr)
        return None
    return result.stdout.strip()


def detect_update_target(repo_dir: Path, hermes_home: Optional[Path] = None) -> Tuple[str, str, str]:
    explicit = read_update_channel(hermes_home)
    if explicit == "prod":
        return PROD_TARGET
    if explicit == "main":
        return DEFAULT_TARGET

    upstream = _git_output(repo_dir, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "prod@{upstream}"])
    if upstream == "fork/prod":
        return PROD_TARGET

    current_branch = _git_output(repo_dir, ["rev-parse", "--abbrev-ref", "HEAD"])
    if current_branch == "prod":
        has_fork_prod = _git_output(repo_dir, ["rev-parse", "--verify", "refs/remotes/fork/prod"])
        if has_fork_prod is not None:
            return PROD_TARGET

    return DEFAULT_TARGET
