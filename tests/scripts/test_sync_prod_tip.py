"""Sync rechecks absorb only a single, clean late prod commit."""
import importlib.util
from pathlib import Path
import subprocess

import pytest

from tests.hermes_cli.test_update_target_identity import git, update_tree  # noqa: F401

spec = importlib.util.spec_from_file_location(
    "sync_prod_tip", Path(__file__).resolve().parents[2] / "scripts/check_sync_prod_tip.py")
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


@pytest.mark.parametrize("case", ["unchanged", "clean", "conflict", "multiple", "protected", "dirty"])
def test_recheck_prod(update_tree, monkeypatch, case):
    t = update_tree
    git(t.origin, "branch", "prod", t.base if case == "unchanged" else t.newer if case == "multiple" else t.wanted)
    git(t.clone, "remote", "add", "fork", str(t.origin))
    git(t.clone, "checkout", "-qb", "sync/test")
    if case == "conflict":
        (t.clone / "content.txt").write_text("incompatible sync\n")
        git(t.clone, "commit", "-qam", "conflicting sync")
    if case == "protected":
        git(t.clone, "checkout", "-q", "main")
    if case == "dirty":
        (t.clone / "content.txt").write_text("uncommitted\n")
    before = git(t.clone, "rev-parse", "HEAD")
    monkeypatch.chdir(t.clone)
    if case in {"unchanged", "clean"}:
        tip = sync.check_prod_tip(t.base)
        assert tip == (t.base if case == "unchanged" else t.wanted)
        assert git(t.clone, "show", "fork/prod:content.txt") == (t.clone / "content.txt").read_text().strip()
        after = git(t.clone, "rev-parse", "HEAD")
        sync.check_prod_tip(t.base)
        assert git(t.clone, "rev-parse", "HEAD") == after
    else:
        with pytest.raises(ValueError):
            sync.check_prod_tip(t.base)
        assert git(t.clone, "rev-parse", "HEAD") == before
        assert subprocess.run(["git", "rev-parse", "--verify", "CHERRY_PICK_HEAD"], capture_output=True).returncode != 0
