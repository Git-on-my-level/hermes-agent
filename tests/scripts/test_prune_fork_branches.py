"""Fork branch retention: the keep rules and the guarded apply pass (#36)."""

import hashlib
import importlib.util
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "prune_fork_branches.py"


def _load():
    spec = importlib.util.spec_from_file_location("prune_fork_branches", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    # dataclass processing under `from __future__ import annotations`
    # resolves the class module through sys.modules — register before exec.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load()

NOW = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=200)
FRESH = NOW - timedelta(days=5)


def branch(name, tip=FRESH, sha=None):
    sha = sha or hashlib.sha1(name.encode()).hexdigest()
    return mod.Branch(name=name, sha=sha, tip=tip)


def reasons(decisions):
    return {d.branch.name: d.keep for d in decisions}


def _ok(argv, stdout=""):
    return subprocess.CompletedProcess(argv, 0, stdout, "")


def test_channels_open_pr_heads_and_fresh_tips_survive_the_age_cutoff():
    decisions = mod.plan(
        [
            branch("main", tip=OLD),
            branch("prod", tip=OLD),
            branch("fix/fresh"),
            branch("fix/in-review", tip=OLD),  # open PR whose tip is 200 days old
            branch("fix/stale", tip=OLD),
            branch("terminal", tip=OLD),
        ],
        now=NOW,
        open_pr_heads={"fix/in-review"},
        protected={"main", "prod", "prod-default"},
    )
    kept = reasons(decisions)
    assert kept["main"] == "channel/default branch"
    assert kept["prod"] == "channel/default branch"
    assert kept["fix/in-review"] == "open PR head"
    assert kept["fix/fresh"] == "tip newer than 90 days"
    assert kept["fix/stale"] is None
    assert kept["terminal"] is None


def test_pre_sync_snapshots_are_retained_by_date_alone():
    dates = ["2026-05-01", "2026-06-01", "2026-08-01", "2026-09-01"]
    branches = [
        branch(f"archive/{ch}-pre-sync-{d}", tip=OLD)
        for d in dates
        for ch in ("main", "prod")
    ]
    # A fresh tip must NOT rescue an old-date pair: snapshots are frozen
    # positions of the channel branches, so their tips are as fresh as the
    # sync that made them — the age rule would keep every pair for 90 days
    # and the archive set would grow forever (#36).
    branches.append(branch("archive/main-pre-sync-2026-07-01", tip=FRESH))
    branches.append(branch("archive/prod-pre-sync-2026-07-01", tip=FRESH))
    # A manual snapshot is not a sync pair: it gets no date treatment at all,
    # only the ordinary age rule.
    branches.append(branch("archive/main-pr50-fold-2026-05-02", tip=OLD))
    branches.append(branch("archive/main-pr50-fold-2026-09-20", tip=FRESH))
    kept = reasons(mod.plan(branches, now=NOW, protected={"main", "prod"}))

    for date in dates[-2:]:  # newest 2 dates kept, despite 200-day-old tips
        for channel in ("main", "prod"):
            assert kept[f"archive/{channel}-pre-sync-{date}"].startswith(
                "pre-sync snapshot"
            )
    for date in dates[:-2]:  # older dates go, even with a fresh tip
        for channel in ("main", "prod"):
            assert kept[f"archive/{channel}-pre-sync-{date}"] is None
    assert kept["archive/main-pre-sync-2026-07-01"] is None
    assert kept["archive/prod-pre-sync-2026-07-01"] is None
    assert kept["archive/main-pr50-fold-2026-05-02"] is None
    assert kept["archive/main-pr50-fold-2026-09-20"] == "tip newer than 90 days"


def test_delete_list_round_trips_exactly_the_deletions():
    decisions = mod.plan(
        [branch("main"), branch("fix/stale", tip=OLD), branch("fix/fresh")],
        now=NOW,
        protected={"main"},
    )
    text = mod.render_list(
        decisions, remote="fork", now=NOW, max_age_days=90, archive_keep=2
    )
    assert mod.parse_list(text) == [("fix/stale", branch("fix/stale", tip=OLD).sha)]

    with pytest.raises(ValueError):  # not a 40-hex sha
        mod.parse_list("z" * 40 + " 2026-09-26T00:00:00+00:00 fix/x\n")
    with pytest.raises(ValueError):  # refspec-unsafe name
        mod.parse_list("0" * 40 + " 2026-09-26T00:00:00+00:00 bad..name\n")
    with pytest.raises(ValueError):  # empty list deletes nothing; refuse
        mod.parse_list("# only a comment\n")


def test_collect_branches_parses_iso_strict_and_ignores_the_head_symref():
    out = (
        "HEAD 6268af3d741357fccd8283dd321428f071ea574a 2026-09-25T23:13:35-04:00\n"
        "prod 6268af3d741357fccd8283dd321428f071ea574a 2026-09-25T23:13:35-04:00\n"
        "archive/prod-pre-sync-2026-09-18 ff5cb49cb5 2026-09-18T06:33:41-07:00\n"
    )

    def run(argv, *, check=True):
        return _ok(argv, out)

    branches = mod.collect_branches(run, "fork")
    assert [b.name for b in branches] == [
        "prod",
        "archive/prod-pre-sync-2026-09-18",
    ]
    assert branches[0].tip.utcoffset() == timedelta(hours=-4)
    assert branches[0].sha == "6268af3d741357fccd8283dd321428f071ea574a"


def test_apply_pushes_only_unprotected_refs_each_guarded_by_its_listed_sha():
    calls = []

    def run(argv, *, check=True):
        calls.append(argv)
        return _ok(argv)

    stale = branch("fix/stale")
    results = mod.apply_deletions(
        run,
        "fork",
        [("fix/stale", stale.sha), ("main", "m" * 40), ("fix/pr", "p" * 40)],
        skip={"main": "channel/default branch", "fix/pr": "open PR head"},
    )

    pushes = [c for c in calls if c[:2] == ["git", "push"]]
    assert len(pushes) == 1
    assert f"--force-with-lease=refs/heads/fix/stale:{stale.sha}" in pushes[0]
    assert "refs/heads/main" not in pushes[0]
    assert "refs/heads/fix/pr" not in pushes[0]
    assert {name: status for name, status, _ in results} == {
        "fix/stale": "deleted",
        "main": "skipped",
        "fix/pr": "skipped",
    }


def test_apply_reports_a_ref_that_survived_its_delete():
    stale = branch("fix/stale")

    def run(argv, *, check=True):
        if argv[:2] == ["git", "ls-remote"]:
            return _ok(argv, f"{stale.sha}\trefs/heads/fix/stale\n")
        return _ok(argv)

    results = mod.apply_deletions(run, "fork", [("fix/stale", stale.sha)], skip={})
    assert results == [("fix/stale", "FAILED", "still on remote")]


def test_a_rejected_batch_falls_back_to_per_ref_pushes():
    calls = []

    def run(argv, *, check=True):
        calls.append(argv)
        if argv[:2] == ["git", "push"]:
            batch = argv[argv.index("--delete") + 1 :]
            rejected = len(batch) > 1  # batches are rejected, singles succeed
            return subprocess.CompletedProcess(argv, int(rejected), "", "stale info")
        return _ok(argv)

    first, second = branch("fix/a"), branch("fix/b")
    mod.apply_deletions(
        run, "fork", [("fix/a", first.sha), ("fix/b", second.sha)], skip={}
    )

    singles = [
        c
        for c in calls
        if c[:2] == ["git", "push"] and len(c[c.index("--delete") + 1 :]) == 1
    ]
    assert len(singles) == 2
