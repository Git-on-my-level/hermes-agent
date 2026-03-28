"""Update target selection policy for Hermes repositories."""

from __future__ import annotations

from typing import Iterable, Tuple


DEFAULT_UPDATE_REMOTE = "origin"
DEFAULT_UPDATE_BRANCH = "main"
PROD_UPDATE_REMOTE = "fork"
PROD_UPDATE_BRANCH = "prod"


def resolve_update_target(current_branch: str, remote_names: Iterable[str]) -> Tuple[str, str]:
    """Resolve which git remote/branch Hermes should update from.

    Policy:
    - prod branch with a fork remote -> update from fork/prod
    - everything else -> update from origin/main
    """
    remotes = set(remote_names)
    if current_branch == PROD_UPDATE_BRANCH and PROD_UPDATE_REMOTE in remotes:
        return PROD_UPDATE_REMOTE, PROD_UPDATE_BRANCH
    return DEFAULT_UPDATE_REMOTE, DEFAULT_UPDATE_BRANCH


def format_update_target(remote: str, branch: str) -> str:
    return f"{remote}/{branch}"
