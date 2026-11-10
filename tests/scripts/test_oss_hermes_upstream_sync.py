"""Behavior gates for the automated upstream sync and its installed timer."""

import json
from pathlib import Path
from typing import Callable

import pytest

from scripts.ops import oss_hermes_upstream_sync as sync_module
from scripts.prune_fork_branches import PRE_SYNC_RE

PROD_BASELINE = "1" * 40
MAIN_BASELINE = "2" * 40
NOUS_SHA = "3" * 40
CANDIDATE = "4" * 40
OTHER_SHA = "5" * 40


class FakeRunner:
    def __init__(self, handler: Callable | None = None):
        self.handler = handler or (lambda args, cwd: sync_module.Result(0))
        self.calls: list[list[str]] = []

    def __call__(self, args, cwd=None, check=True):
        args = list(args)
        self.calls.append(args)
        result = self.handler(args, cwd)
        if check and result.returncode:
            raise sync_module.SyncError(f"fake command failed: {args[0]}")
        return result


def make_sync(tmp_path: Path, runner: FakeRunner):
    receipt = sync_module.Receipt("20261110-020000", "sync/upstream-20261110-020000")
    config = sync_module.Config(
        source=tmp_path / "live", state_root=tmp_path / "state",
        ci_timeout_seconds=0, ci_poll_seconds=0,
    )
    engine = sync_module.UpstreamSync(config, receipt, runner=runner, monotonic=lambda: 0)
    receipt.source_sha = NOUS_SHA
    receipt.target_prod_sha = PROD_BASELINE
    receipt.target_main_sha = MAIN_BASELINE
    return engine


def ci_run(sha=CANDIDATE, status="completed", conclusion="success", branch=None):
    return {
        "databaseId": 100, "url": f"https://example.test/runs/{sha[:8]}",
        "headBranch": branch or "sync/upstream-20261110-020000", "headSha": sha,
        "status": status, "conclusion": conclusion,
    }


def green_handler(runner: FakeRunner, changed_paths="", *, same_tree=False):
    def handle(args, cwd):
        if len(args) >= 3 and args[1] == "rev-parse" and str(args[2]).endswith("^{tree}"):
            spec = args[2].removesuffix("^{tree}")
            baseline = spec in {PROD_BASELINE, MAIN_BASELINE}
            tree = "b" * 40 if same_tree or baseline else "a" * 40
            return sync_module.Result(0, tree + "\n")
        if args[:3] == ["git", "rev-parse", "HEAD"]:
            return sync_module.Result(0, CANDIDATE + "\n")
        if args[:3] == ["git", "status", "--porcelain"]:
            return sync_module.Result(0)
        if args[:4] == ["git", "rev-parse", "--verify", "CHERRY_PICK_HEAD"]:
            return sync_module.Result(1)
        if args[:3] == ["git", "rev-parse", "refs/remotes/fork/prod"]:
            landed = any(call[:3] == ["git", "push", "--atomic"] for call in runner.calls)
            return sync_module.Result(0, (CANDIDATE if landed else PROD_BASELINE) + "\n")
        if args[:3] == ["git", "rev-parse", "refs/remotes/fork/main"]:
            landed = any(call[:3] == ["git", "push", "--atomic"] for call in runner.calls)
            return sync_module.Result(0, (CANDIDATE if landed else MAIN_BASELINE) + "\n")
        if args[:3] == ["git", "rev-parse", "refs/remotes/fork/sync/upstream-20261110-020000"]:
            return sync_module.Result(0, CANDIDATE + "\n")
        if args[:3] == ["git", "diff", "--name-only"]:
            return sync_module.Result(0, changed_paths)
        if args[:2] == ["gh", "run"]:
            return sync_module.Result(0, json.dumps([ci_run()]))
        return sync_module.Result(0)
    return handle


@pytest.mark.parametrize("condition,expected", [
    ("missing", "is missing"),
    ("disabled", "is not enabled"),
    ("inactive", "is not active"),
])
def test_check_install_refuses_missing_disabled_or_inactive_timer(condition, expected):
    def handle(args, cwd):
        if args[:2] == ["systemctl", "cat"]:
            failed = condition == "missing" and args[2] == sync_module.UNIT_NAME
            return sync_module.Result(1 if failed else 0, "" if failed else "[Unit]\n")
        if args[:2] == ["systemctl", "is-enabled"]:
            return sync_module.Result(1 if condition == "disabled" else 0,
                                      "disabled\n" if condition == "disabled" else "enabled\n")
        if args[:2] == ["systemctl", "is-active"]:
            return sync_module.Result(1 if condition == "inactive" else 0,
                                      "inactive\n" if condition == "inactive" else "active\n")
        raise AssertionError(args)

    failures = sync_module.check_install(FakeRunner(handle))
    assert any(expected in failure for failure in failures)


def test_check_install_accepts_only_an_enabled_active_timer():
    runner = FakeRunner(lambda args, cwd: sync_module.Result(
        0, "active\n" if args[1] == "is-active" else "enabled\n" if args[1] == "is-enabled" else "[Unit]\n"))
    assert sync_module.check_install(runner) == []
    assert all(call[1] not in {"enable", "start"} for call in runner.calls)


@pytest.mark.parametrize("failed_gate", ["features", "tests"])
def test_failed_local_gate_cannot_reach_candidate_push(tmp_path, failed_gate):
    def handle(args, cwd):
        if args[:3] == ["git", "rev-parse", "HEAD"]:
            return sync_module.Result(0, CANDIDATE + "\n")
        if args[:4] == ["git", "rev-parse", "--verify", "CHERRY_PICK_HEAD"]:
            return sync_module.Result(1)
        if "scripts/check_fork_features.py" in args and failed_gate == "features":
            return sync_module.Result(1)
        if "scripts/run_tests.sh" in args and failed_gate == "tests":
            return sync_module.Result(1)
        return sync_module.Result(0)

    runner = FakeRunner(handle)
    engine = make_sync(tmp_path, runner)
    with pytest.raises(sync_module.SyncError):
        engine.push_wait_and_stabilize()
    assert not any(call[:2] == ["git", "push"] for call in runner.calls)


def test_clean_green_unrelated_candidate_cannot_reach_any_push(tmp_path):
    def handle(args, cwd):
        if args[:3] == ["git", "rev-parse", "HEAD"]:
            return sync_module.Result(0, CANDIDATE + "\n")
        if args[:4] == ["git", "rev-parse", "--verify", "CHERRY_PICK_HEAD"]:
            return sync_module.Result(1)
        if args[:3] == ["git", "merge-base", "--is-ancestor"]:
            return sync_module.Result(1)
        return sync_module.Result(0)

    runner = FakeRunner(handle)
    engine = make_sync(tmp_path, runner)
    with pytest.raises(sync_module.SyncError):
        engine.push_wait_and_stabilize()
    assert not any(call[:2] == ["git", "push"] for call in runner.calls)


def test_delegated_reporter_inherits_no_git_write_credentials(tmp_path):
    engine = make_sync(tmp_path, FakeRunner())
    command = engine.delegation_command("port one keep", yolo=True)
    assert command[:11] == [
        "env", "-u", "SSH_AUTH_SOCK", "-u", "GIT_ASKPASS", "-u", "SSH_ASKPASS",
        "-u", "GH_TOKEN", "-u", "GITHUB_TOKEN",
    ]
    assert "GIT_CONFIG_NOSYSTEM=1" in command
    assert any(value.startswith("GIT_CONFIG_GLOBAL=") for value in command)
    assert any(value.startswith("GH_CONFIG_DIR=") for value in command)
    assert any(value.startswith("GIT_SSH_COMMAND=") and "-F /dev/null" in value for value in command)


@pytest.mark.parametrize("status", ["failed", "running"])
def test_health_check_refuses_failed_or_stale_last_receipt(tmp_path, status):
    report = tmp_path / "reports" / "latest.json"
    report.parent.mkdir()
    report.write_text(json.dumps({"status": status}), encoding="utf-8")
    runner = FakeRunner(lambda args, cwd: sync_module.Result(
        0, "active\n" if args[1] == "is-active" else "enabled\n" if args[1] == "is-enabled" else "[Unit]\n"))

    failures = sync_module.check_install(runner, state_root=tmp_path, now=report.stat().st_mtime + 13 * 60 * 60)
    assert failures


@pytest.mark.parametrize("rows", [
    [],
    [ci_run(sha=OTHER_SHA)],
    [ci_run(branch="sync/other-branch")],
    [ci_run(status="completed", conclusion="failure")],
    [ci_run(status="completed", conclusion="cancelled")],
])
def test_missing_stale_mismatched_or_failed_ci_cannot_land(tmp_path, rows):
    runner = FakeRunner()
    engine = make_sync(tmp_path, runner)

    def handle(args, cwd):
        if args[:3] == ["git", "rev-parse", "HEAD"]:
            return sync_module.Result(0, CANDIDATE + "\n")
        if args[:4] == ["git", "rev-parse", "--verify", "CHERRY_PICK_HEAD"]:
            return sync_module.Result(1)
        if args[:2] == ["gh", "run"]:
            return sync_module.Result(0, json.dumps(rows))
        return sync_module.Result(0)

    runner.handler = handle
    with pytest.raises(sync_module.SyncError):
        engine.qualify_and_land()
    assert not any(call[:3] == ["git", "push", "--atomic"] for call in runner.calls)
    assert not engine.receipt.archive_refs


def test_happy_path_lands_automatically_without_human_approval(tmp_path):
    runner = FakeRunner()
    runner.handler = green_handler(runner)
    engine = make_sync(tmp_path, runner)

    engine.qualify_and_land()

    assert engine.receipt.candidate_sha == CANDIDATE
    assert engine.receipt.ci_run_url == ci_run()["url"]
    assert engine.receipt.tip_verification == {"prod": CANDIDATE, "main": CANDIDATE}
    assert not engine.receipt.manual_flip_required
    assert not any(call[:2] == ["gh", "pr"] for call in runner.calls)
    assert not any(call[0] == "input" for call in runner.calls)


def test_both_archives_precede_atomic_exact_sha_dual_lease_landing(tmp_path):
    runner = FakeRunner()
    runner.handler = green_handler(runner)
    engine = make_sync(tmp_path, runner)

    engine.qualify_and_land()

    archive_index = next(index for index, call in enumerate(runner.calls)
                         if call[:2] == ["git", "push"] and "archive" in " ".join(call))
    land_index = next(index for index, call in enumerate(runner.calls)
                      if call[:3] == ["git", "push", "--atomic"])
    assert archive_index < land_index
    archive = runner.calls[archive_index]
    prod_archive = "archive/prod-pre-sync-2026-11-10"
    main_archive = "archive/main-pre-sync-2026-11-10"
    assert f"{PROD_BASELINE}:refs/heads/{prod_archive}" in archive
    assert f"{MAIN_BASELINE}:refs/heads/{main_archive}" in archive
    assert PRE_SYNC_RE.match(prod_archive)
    assert PRE_SYNC_RE.match(main_archive)
    assert PRE_SYNC_RE.match("archive/prod-pre-sync-20261110-020000") is None
    assert runner.calls[land_index] == [
        "git", "push", "--atomic",
        f"--force-with-lease=refs/heads/prod:{PROD_BASELINE}",
        f"--force-with-lease=refs/heads/main:{MAIN_BASELINE}",
        "fork", f"{CANDIDATE}:refs/heads/prod", f"{CANDIDATE}:refs/heads/main",
    ]


def test_same_day_archive_is_replaced_under_lease(tmp_path):
    previous = "6" * 40
    archive_pushes = {"n": 0}

    def handle(args, cwd):
        if args[:2] == ["git", "push"] and any("pre-sync" in arg for arg in args):
            archive_pushes["n"] += 1
            if archive_pushes["n"] == 1:
                return sync_module.Result(1, "", "non-fast-forward")
            assert f"--force-with-lease=refs/heads/archive/prod-pre-sync-2026-11-10:{previous}" in args
            assert f"--force-with-lease=refs/heads/archive/main-pre-sync-2026-11-10:{previous}" in args
            return sync_module.Result(0)
        if args[:3] == ["git", "rev-parse", "--verify"] and any("pre-sync" in arg for arg in args):
            return sync_module.Result(0, previous + "\n")
        return base(args, cwd)

    runner = FakeRunner()
    base = green_handler(runner)
    runner.handler = handle
    engine = make_sync(tmp_path, runner)

    engine.qualify_and_land()

    assert archive_pushes["n"] == 2
    assert engine.receipt.tip_verification == {"prod": CANDIDATE, "main": CANDIDATE}


def test_actuator_delta_emits_prominent_manual_flip_receipt(tmp_path):
    runner = FakeRunner()
    runner.handler = green_handler(runner, "hermes_cli/update_converge.py\nhermes_cli/update_cmd.py\n")
    engine = make_sync(tmp_path, runner)

    engine.qualify_and_land()
    engine.receipt.status = "landed"
    sync_module.write_report(engine.config.state_root, engine.receipt)

    reports = engine.config.state_root / "reports"
    data = json.loads((reports / "latest.json").read_text(encoding="utf-8"))
    markdown = (reports / "latest.md").read_text(encoding="utf-8")
    assert data["manual_flip_required"] is True
    assert data["actuator_paths"] == ["hermes_cli/update_cmd.py", "hermes_cli/update_converge.py"]
    assert sync_module.MANUAL_FLIP_NOTICE in data["manual_flip_notice"]
    assert markdown.splitlines()[4] == f"**{sync_module.MANUAL_FLIP_NOTICE}**"


def test_candidate_changed_by_ci_reruns_local_and_exact_sha_ci_gates(tmp_path):
    current = {"sha": CANDIDATE}
    fetches = {"count": 0}
    gate_shas = []

    def handle(args, cwd):
        if args[:3] == ["git", "rev-parse", "HEAD"]:
            return sync_module.Result(0, current["sha"] + "\n")
        if args[:4] == ["git", "rev-parse", "--verify", "CHERRY_PICK_HEAD"]:
            return sync_module.Result(1)
        if "scripts/run_tests.sh" in args:
            gate_shas.append(current["sha"])
        if args[:2] == ["gh", "run"]:
            return sync_module.Result(0, json.dumps([ci_run(sha=current["sha"])]))
        if args[:2] == ["git", "fetch"] and "sync/upstream" in " ".join(args):
            fetches["count"] += 1
        if args[:3] == ["git", "rev-parse", "refs/remotes/fork/sync/upstream-20261110-020000"]:
            remote_sha = OTHER_SHA if fetches["count"] == 1 else current["sha"]
            return sync_module.Result(0, remote_sha + "\n")
        if args[:2] == ["git", "merge"]:
            current["sha"] = OTHER_SHA
        return sync_module.Result(0)

    runner = FakeRunner(handle)
    engine = make_sync(tmp_path, runner)
    engine.push_wait_and_stabilize()

    assert gate_shas == [CANDIDATE, OTHER_SHA]
    assert engine.receipt.candidate_sha == OTHER_SHA
    assert len([call for call in runner.calls if call[:2] == ["gh", "run"]]) == 2


def _prepare_result(args, *, ancestor):
    """SHA and ancestry answers for ``UpstreamSync.prepare`` plus the no-op check."""
    if args[:3] == ["git", "rev-parse", "refs/remotes/nous/main"]:
        return sync_module.Result(0, NOUS_SHA + "\n")
    if args[:3] == ["git", "rev-parse", "refs/remotes/fork/prod"]:
        return sync_module.Result(0, PROD_BASELINE + "\n")
    if args[:3] == ["git", "rev-parse", "refs/remotes/fork/main"]:
        return sync_module.Result(0, MAIN_BASELINE + "\n")
    if args[:3] == ["git", "rev-parse", "HEAD"]:
        return sync_module.Result(0, NOUS_SHA + "\n")
    if args[:4] == ["git", "rev-parse", "--verify", "CHERRY_PICK_HEAD"]:
        return sync_module.Result(1)
    if (len(args) >= 5 and args[:3] == ["git", "merge-base", "--is-ancestor"]
            and args[4] == PROD_BASELINE):
        return sync_module.Result(0 if ancestor else 1)
    if len(args) >= 3 and args[1] == "rev-parse" and str(args[2]).endswith("^{tree}"):
        return sync_module.Result(0, "c" * 40 + "\n")
    return None


def test_unchanged_upstream_does_not_rewrite_tips(tmp_path):
    def handle(args, cwd):
        prepared = _prepare_result(args, ancestor=True)
        if prepared is not None:
            return prepared
        if args[:3] == ["git", "rev-list", "--reverse"]:
            raise AssertionError("re-port started for an unchanged upstream")
        return sync_module.Result(0)

    runner = FakeRunner(handle)
    engine = make_sync(tmp_path, runner)
    engine.run()

    assert engine.receipt.status == "unchanged"
    assert engine.receipt.tip_verification == {"prod": PROD_BASELINE, "main": MAIN_BASELINE}
    assert not any(call[:2] == ["git", "push"] for call in runner.calls)
    assert not any(call[:2] == ["git", "cherry-pick"] for call in runner.calls)
    sync_module.write_report(engine.config.state_root, engine.receipt)
    report = engine.config.state_root / "reports" / "latest.md"
    text = report.read_text(encoding="utf-8")
    assert "tips were not rewritten" in text
    assert "Landing is not verified" not in text


def test_moved_upstream_still_re_ports(tmp_path):
    def handle(args, cwd):
        prepared = _prepare_result(args, ancestor=False)
        if prepared is not None:
            return prepared
        if args[:3] == ["git", "rev-list", "--reverse"]:
            return sync_module.Result(0, f"{'e' * 40}\n")
        if args[:3] == ["git", "cherry", "HEAD"]:
            return sync_module.Result(0, f"+ {args[3]}\n")
        if args[:2] == ["git", "cherry-pick"]:
            return sync_module.Result(1, "", "conflict")
        if args[0] == "env":
            return sync_module.Result(1, "", "delegate stopped")
        return sync_module.Result(0)

    runner = FakeRunner(handle)
    engine = make_sync(tmp_path, runner)
    with pytest.raises(sync_module.SyncError, match="delegation failed"):
        engine.run()
    assert any(call[:2] == ["git", "cherry-pick"] for call in runner.calls)


def test_identical_candidate_tree_does_not_force_lease(tmp_path):
    runner = FakeRunner()
    runner.handler = green_handler(runner, same_tree=True)
    engine = make_sync(tmp_path, runner)

    engine.qualify_and_land()

    assert engine.receipt.status == "unchanged"
    assert engine.receipt.tip_verification == {"prod": PROD_BASELINE, "main": MAIN_BASELINE}
    assert engine.receipt.archive_refs == {}
    assert not any(call[:3] == ["git", "push", "--atomic"] for call in runner.calls)
    assert not any("pre-sync" in arg for call in runner.calls for arg in call)


def test_health_check_accepts_an_unchanged_receipt(tmp_path):
    report = tmp_path / "reports" / "latest.json"
    report.parent.mkdir()
    report.write_text(json.dumps({"status": "unchanged"}), encoding="utf-8")
    runner = FakeRunner(lambda args, cwd: sync_module.Result(
        0, "active\n" if args[1] == "is-active" else "enabled\n" if args[1] == "is-enabled" else "[Unit]\n"))

    failures = sync_module.check_install(
        runner, state_root=tmp_path, now=report.stat().st_mtime + 13 * 60 * 60)
    assert failures == []


def _budget_engine(tmp_path, runner, clock):
    engine = make_sync(tmp_path, runner)
    engine.monotonic = lambda: clock["now"]
    clock["deadline"] = engine.deadline
    return engine


def test_re_port_stops_before_the_next_keep_once_the_budget_is_spent(tmp_path):
    clock = {"now": 0.0, "deadline": 0.0}
    seen = []

    def handle(args, cwd):
        if args[:3] == ["git", "rev-list", "--reverse"]:
            return sync_module.Result(0, f"{'a' * 40}\n{'b' * 40}\n")
        if args[:3] == ["git", "cherry", "HEAD"]:
            return sync_module.Result(0, f"+ {args[3]}\n")
        if args[:2] == ["git", "cherry-pick"]:
            seen.append(args[2])
            clock["now"] = clock["deadline"]
            return sync_module.Result(0)
        if args[:3] == ["git", "status", "--porcelain"]:
            return sync_module.Result(0)
        if args[:4] == ["git", "rev-parse", "--verify", "CHERRY_PICK_HEAD"]:
            return sync_module.Result(1)
        raise AssertionError(args)

    runner = FakeRunner(handle)
    engine = _budget_engine(tmp_path, runner, clock)
    with pytest.raises(sync_module.SyncError, match="10-hour budget"):
        engine.port_kept_commits()
    assert seen == ["a" * 40]


def test_re_port_does_not_delegate_after_the_budget_is_spent(tmp_path):
    clock = {"now": 0.0, "deadline": 0.0}

    def handle(args, cwd):
        if args[:3] == ["git", "rev-list", "--reverse"]:
            return sync_module.Result(0, f"{'c' * 40}\n")
        if args[:3] == ["git", "cherry", "HEAD"]:
            return sync_module.Result(0, f"+ {args[3]}\n")
        if args[:2] == ["git", "cherry-pick"]:
            clock["now"] = clock["deadline"]
            return sync_module.Result(1, "", "conflict")
        raise AssertionError(args)

    runner = FakeRunner(handle)
    engine = _budget_engine(tmp_path, runner, clock)
    with pytest.raises(sync_module.SyncError, match="10-hour budget"):
        engine.port_kept_commits()
    assert not any(call[0] == "env" for call in runner.calls)


def test_yolo_retry_stops_at_the_run_budget(tmp_path):
    clock = {"now": 0.0, "deadline": 0.0}
    hermes_calls = {"n": 0}

    def handle(args, cwd):
        if args[:3] == ["git", "rev-list", "--reverse"]:
            return sync_module.Result(0, f"{'d' * 40}\n")
        if args[:3] == ["git", "cherry", "HEAD"]:
            return sync_module.Result(0, "- equivalent\n")
        if args[0] == "env":
            hermes_calls["n"] += 1
            clock["now"] = clock["deadline"]
            return sync_module.Result(1, "", "unrecognized argument: --yolo")
        raise AssertionError(args)

    runner = FakeRunner(handle)
    engine = _budget_engine(tmp_path, runner, clock)
    with pytest.raises(sync_module.SyncError, match="10-hour budget"):
        engine.port_kept_commits()
    assert hermes_calls["n"] == 1


def test_command_runner_inherits_and_enforces_the_sync_budget(tmp_path, monkeypatch):
    runner = sync_module.CommandRunner()
    sync_module.UpstreamSync(
        sync_module.Config(source=tmp_path / "live", state_root=tmp_path / "state"),
        sync_module.Receipt("20261110-020000", "sync/upstream-20261110-020000"),
        runner=runner,
        monotonic=lambda: 5.0,
    )
    assert runner.deadline == 5.0 + sync_module.Config().max_run_seconds
    assert runner.monotonic() == 5.0

    captured = {}

    def fake_run(argv, **kwargs):
        captured["timeout"] = kwargs["timeout"]
        return sync_module.subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(sync_module.subprocess, "run", fake_run)
    runner.deadline = 1_000.0
    runner.monotonic = lambda: 700.0
    runner([sync_module.HERMES, "chat", "-q", "port"], None, True)
    assert captured["timeout"] == 300.0

    def refuse(*args, **kwargs):
        raise AssertionError("started after the budget")

    monkeypatch.setattr(sync_module.subprocess, "run", refuse)
    runner.monotonic = lambda: 1_000.0
    with pytest.raises(sync_module.SyncError, match="10-hour budget"):
        runner(["git", "status"], None, True)
