"""Select a git deploy target before release-channel resolution or mutation."""
from __future__ import annotations

from types import SimpleNamespace

from hermes_cli.update_channel import git_update_target


def configured_git_target(args=None, *, branch=None, branch_explicit=False, remote=None, channel=None):
    """CLI git selectors win, then updates.remote/branch; empty keys keep release feeds.

    An explicit --channel overrides host git defaults. A pin always uses the git
    target, including origin/main on stock installs. Never swallow config errors
    and silently redirect a configured install to upstream.
    """
    from hermes_cli.config import get_config_path, require_readable_config_before_write
    from hermes_cli.config_effective import load_user_config_effective

    args = args or SimpleNamespace()
    cli_remote = str(remote or getattr(args, "remote", None) or "").strip()
    cli_branch = str((branch if branch_explicit else getattr(args, "branch", None)) or "").strip()
    transient = channel or getattr(args, "channel", None)
    if transient and not cli_remote and not cli_branch and not getattr(args, "sha", None):
        return None
    require_readable_config_before_write(get_config_path())
    config = load_user_config_effective(fail_closed=True)
    updates = config.get("updates") or {}
    if not isinstance(updates, dict):
        raise ValueError("config key updates must be a mapping")
    configured = isinstance(updates, dict) and any(
        str(updates.get(key) or "").strip() for key in ("remote", "branch"))
    if not (configured or cli_remote or cli_branch or getattr(args, "sha", None)):
        return None
    default_remote, default_branch = git_update_target(config)
    target = cli_remote or default_remote, cli_branch or default_branch
    # Reject option-like values and revision expressions before passing them to git.
    for value in target:
        if value.startswith("-") or any(c.isspace() for c in value) or any(
            c in value for c in "~^:?*[\\"):
            raise ValueError(f"Invalid git update target: {value!r}")
    return target
