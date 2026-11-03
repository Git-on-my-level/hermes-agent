"""Disk admission for source updates, sharing launch completion's retry budget."""
from pathlib import Path


def require_source_write_space(root: Path) -> None:
    from hermes_cli.venv_sync import _record_completion_attempt, completion_pending_path
    from pm.disk_space import DiskFloorError, require_generation_space
    from pm.environments import install_state_dir
    from pm.paths import writable_store_root

    try:
        require_generation_space(root, install_state_dir(root), writable_store_root())
    except DiskFloorError:
        # Bookkeeping stays writable: refusing a generation must consume a retry.
        completion_pending_path(root).parent.mkdir(parents=True, exist_ok=True)
        _record_completion_attempt(root, failed=True)
        raise
