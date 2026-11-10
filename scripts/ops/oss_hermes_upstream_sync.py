#!/usr/bin/env python3
"""Install or run the weekly, receipt-driven NousResearch-to-fork sync."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any, Callable, Iterable, Sequence

UNIT_NAME = "oss-hermes-upstream-sync.timer"
SERVICE_NAME = "oss-hermes-upstream-sync.service"
SCRIPT_NAME = "oss_hermes_upstream_sync.py"
DEFAULT_STATE_ROOT = Path("/var/lib/oss-hermes-upstream-sync")
DEFAULT_SOURCE = Path("/home/ubuntu/.hermes/hermes-agent")
DEFAULT_UNIT_DIR = Path("/etc/systemd/system")
DEFAULT_LIB_DIR = Path("/usr/local/lib/oss-hermes-upstream-sync")
NOUS_URL = "https://github.com/NousResearch/hermes-agent.git"
FORK_URL = "https://github.com/Git-on-my-level/hermes-agent.git"
FORK_REPO = "Git-on-my-level/hermes-agent"
HERMES = "/home/ubuntu/.local/bin/hermes"
MANUAL_FLIP_NOTICE = (
    "MANUAL_FLIP_REQUIRED: old fleet ticks cannot be trusted to apply that actuator and "
    "the one-time manual flip path is required."
)
FOCUSED_TESTS = (
    "tests/hermes_cli/test_fork_autoupdate_invariants.py",
    "tests/hermes_cli/test_update_converge.py",
)


class SyncError(RuntimeError):
    """A gate failed; the run must not claim a successful landing."""


@dataclass
class Result:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class CommandRunner:
    """Captured subprocess execution without ever invoking a shell."""

    def __init__(self) -> None:
        self.deadline: float | None = None
        self.monotonic: Callable[[], float] = time.monotonic

    def __call__(self, args: Sequence[str], cwd: Path | None = None, check: bool = True) -> Result:
        long_operation = HERMES in args or "scripts/run_tests.sh" in args
        cap = (2 if long_operation else 0.5) * 60 * 60
        timeout, budget_bound = self._timeout(cap)
        try:
            completed = subprocess.run(
                list(args), cwd=cwd, check=False, capture_output=True,
                timeout=timeout,
                text=True, encoding="utf-8", errors="replace",
            )
        except subprocess.TimeoutExpired as exc:
            if budget_bound:
                raise SyncError("Upstream sync exceeded its overall 10-hour budget") from exc
            raise
        result = Result(completed.returncode, completed.stdout, completed.stderr)
        if check and result.returncode:
            raise SyncError(f"{args[0]} command exited {result.returncode}")
        return result

    def _timeout(self, cap: float) -> tuple[float, bool]:
        """Keep one command inside the run budget.

        Hermes re-ports are allowed two hours each. Without this cap a call
        started just before the deadline still runs until systemd kills the
        unit, which skips the failed receipt.
        """
        if self.deadline is None:
            return cap, False
        remaining = self.deadline - self.monotonic()
        if remaining <= 0:
            raise SyncError("Upstream sync exceeded its overall 10-hour budget")
        if remaining < cap:
            return remaining, True
        return cap, False


Runner = Callable[[Sequence[str], Path | None, bool], Result]


@dataclass(frozen=True)
class Config:
    source: Path = DEFAULT_SOURCE
    state_root: Path = DEFAULT_STATE_ROOT
    nous_url: str = NOUS_URL
    fork_url: str = FORK_URL
    fork_repo: str = FORK_REPO
    ci_timeout_seconds: int = 4 * 60 * 60
    ci_poll_seconds: int = 60
    max_ci_rounds: int = 2
    max_run_seconds: int = 10 * 60 * 60


@dataclass
class Receipt:
    run_id: str
    candidate_branch: str
    status: str = "running"
    source_sha: str | None = None
    target_prod_sha: str | None = None
    target_main_sha: str | None = None
    candidate_sha: str | None = None
    ci_run_url: str | None = None
    archive_refs: dict[str, str] = field(default_factory=dict)
    tip_verification: dict[str, str] = field(default_factory=dict)
    manual_flip_required: bool = False
    manual_flip_notice: str | None = None
    actuator_paths: list[str] = field(default_factory=list)
    error: str | None = None


def write_report(state_root: Path, receipt: Receipt) -> None:
    """Write the same sanitized receipt as JSON and concise Markdown."""
    reports = state_root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    for name, content in ((f"{receipt.run_id}.json", _json_report(receipt)),
                          (f"{receipt.run_id}.md", _markdown_report(receipt)),
                          ("latest.json", _json_report(receipt)),
                          ("latest.md", _markdown_report(receipt))):
        destination = reports / name
        staging = reports / f".{name}.new"
        staging.write_text(content, encoding="utf-8")
        os.replace(staging, destination)


def _json_report(receipt: Receipt) -> str:
    return json.dumps(asdict(receipt), indent=2, sort_keys=True) + "\n"


def _markdown_report(receipt: Receipt) -> str:
    lines = [f"# Upstream sync {receipt.run_id}", "", f"**Status: {receipt.status}**", ""]
    if receipt.manual_flip_required:
        lines.extend([f"**{MANUAL_FLIP_NOTICE}**", ""])
    if receipt.status not in {"landed", "unchanged"}:
        lines.extend(["**Landing is not verified; do not treat this receipt as a successful sync.**", ""])
    if receipt.status == "unchanged":
        lines.extend([
            "Fork prod and main already carry this tree; tips were not rewritten.",
            "",
        ])
    if receipt.error:
        lines.extend([f"Failure: {receipt.error}", ""])
    lines.extend([
        f"- NousResearch main: `{receipt.source_sha or 'unresolved'}`",
        f"- Fork prod baseline: `{receipt.target_prod_sha or 'unresolved'}`",
        f"- Fork main baseline: `{receipt.target_main_sha or 'unresolved'}`",
        f"- Candidate: `{receipt.candidate_sha or 'unresolved'}`",
        f"- Candidate branch: `{receipt.candidate_branch}`",
        f"- Fork CI: {receipt.ci_run_url or 'not verified'}",
        f"- Manual flip required: `{str(receipt.manual_flip_required).lower()}`",
    ])
    if receipt.actuator_paths:
        lines.append("- Actuator paths: " + ", ".join(f"`{path}`" for path in receipt.actuator_paths))
    for channel, ref in sorted(receipt.archive_refs.items()):
        lines.append(f"- Archived {channel}: `{ref}`")
    for channel, sha in sorted(receipt.tip_verification.items()):
        lines.append(f"- Verified {channel} tip: `{sha}`")
    return "\n".join(lines) + "\n"


def install_files(unit_dir: Path = DEFAULT_UNIT_DIR, lib_dir: Path = DEFAULT_LIB_DIR) -> None:
    """Install immutable inputs only; enabling and starting are explicit operator actions."""
    source_dir = Path(__file__).resolve().parent
    unit_dir.mkdir(parents=True, exist_ok=True)
    lib_dir.mkdir(parents=True, exist_ok=True)
    _copy_if_changed(source_dir / UNIT_NAME, unit_dir / UNIT_NAME, 0o644)
    _copy_if_changed(source_dir / SERVICE_NAME, unit_dir / SERVICE_NAME, 0o644)
    _copy_if_changed(source_dir / SCRIPT_NAME, lib_dir / SCRIPT_NAME, 0o755)
    print("Installed units and orchestrator; the timer remains disabled and inactive.")
    print("To enable it: sudo systemctl daemon-reload && "
          f"sudo systemctl enable --now {UNIT_NAME}")


def _copy_if_changed(source: Path, destination: Path, mode: int) -> None:
    if destination.exists() and destination.read_bytes() == source.read_bytes():
        destination.chmod(mode)
        return
    staging = destination.with_name(f".{destination.name}.new")
    shutil.copyfile(source, staging)
    staging.chmod(mode)
    os.replace(staging, destination)


def check_install(
        runner: Runner, state_root: Path = DEFAULT_STATE_ROOT,
        now: float | None = None) -> list[str]:
    """Require durable timer presence and a last receipt that is not failed or stale."""
    failures: list[str] = []
    for unit in (SERVICE_NAME, UNIT_NAME):
        result = runner(["systemctl", "cat", unit], None, False)
        if result.returncode or not result.stdout.strip():
            failures.append(f"{unit} is missing")
    enabled = runner(["systemctl", "is-enabled", UNIT_NAME], None, False)
    if enabled.returncode or enabled.stdout.strip() != "enabled":
        failures.append(f"{UNIT_NAME} is not enabled")
    active = runner(["systemctl", "is-active", UNIT_NAME], None, False)
    if active.returncode or active.stdout.strip() != "active":
        failures.append(f"{UNIT_NAME} is not active")
    latest = state_root / "reports" / "latest.json"
    if latest.is_file():
        try:
            receipt = json.loads(latest.read_text(encoding="utf-8-sig"))
            status = receipt.get("status")
            if status == "failed":
                failures.append("last sync failed; see the latest report")
            elif status == "running" and (time.time() if now is None else now) - latest.stat().st_mtime > 12 * 60 * 60:
                failures.append("last sync receipt is stale")
            elif status not in {"landed", "running", "unchanged"}:
                failures.append("last sync receipt has an invalid status")
        except (OSError, ValueError, TypeError):
            failures.append("last sync receipt is unreadable")
    return failures


def touches_actuator(paths: Iterable[str]) -> list[str]:
    """Return updater actuators changed by the complete delivered delta."""
    return sorted({path for path in paths if path == "hermes_cli/update_converge.py"
                   or path == "hermes_cli/update_git_target.py"
                   or path.startswith("hermes_cli/update_cmd")})


def pre_sync_archive_ref(channel: str, run_id: str) -> str:
    """Return ``archive/<channel>-pre-sync-YYYY-MM-DD``.

    Snapshot retention keeps the newest two of those dates. A timestamp
    suffix does not match that pattern and ages out as an ordinary branch.
    """
    day = datetime.strptime(run_id[:8], "%Y%m%d").date().isoformat()
    return f"archive/{channel}-pre-sync-{day}"


def wait_for_ci(
        runner: Runner, repository: str, branch: str, candidate_sha: str,
        timeout_seconds: int, poll_seconds: int,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Wait for the newest Fork CI push run matching both branch and exact SHA."""
    deadline = monotonic() + timeout_seconds
    while True:
        result = runner([
            "gh", "run", "list", "--repo", repository, "--workflow", "fork-ci.yml",
            "--event", "push", "--branch", branch, "--limit", "100", "--json",
            "databaseId,url,headBranch,headSha,status,conclusion",
        ], None, True)
        try:
            rows = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise SyncError("Fork CI returned an unreadable run list") from exc
        matching = [row for row in rows if row.get("headBranch") == branch
                    and row.get("headSha") == candidate_sha]
        matching.sort(key=lambda row: int(row.get("databaseId", 0)), reverse=True)
        if matching:
            newest = matching[0]
            if newest.get("status") == "completed":
                if newest.get("conclusion") == "success":
                    return newest
                raise SyncError(f"Fork CI failed for exact candidate {candidate_sha}")
        if monotonic() >= deadline:
            raise SyncError(f"No successful Fork CI run matched {branch} at {candidate_sha}")
        sleeper(min(poll_seconds, max(0, deadline - monotonic())))


class UpstreamSync:
    def __init__(
            self, config: Config, receipt: Receipt, runner: Runner | None = None,
            sleeper: Callable[[float], None] = time.sleep,
            monotonic: Callable[[], float] = time.monotonic):
        self.config = config
        self.receipt = receipt
        self.runner = runner or CommandRunner()
        self.sleeper = sleeper
        self.monotonic = monotonic
        self.control = config.state_root / "control"
        self.candidate = config.state_root / "worktrees" / receipt.run_id
        self.deadline = monotonic() + config.max_run_seconds
        if isinstance(self.runner, CommandRunner):
            self.runner.deadline = self.deadline
            self.runner.monotonic = monotonic

    def git(self, *args: str, cwd: Path | None = None, check: bool = True) -> Result:
        return self.runner(["git", *args], cwd=cwd or self.control, check=check)

    def git_text(self, *args: str, cwd: Path | None = None) -> str:
        return self.git(*args, cwd=cwd).stdout.strip()

    def run(self) -> None:
        self.prepare()
        if self._tips_already_match_source():
            self._leave_tips_unchanged()
            return
        self.port_kept_commits()
        self.qualify_and_land()
        if self.receipt.status == "running":
            self.receipt.status = "landed"

    def prepare(self) -> None:
        source = self.config.source.resolve()
        state_root = self.config.state_root.resolve()
        if source == state_root or source in state_root.parents:
            raise SyncError("The state and scratch worktrees must be outside the live source checkout")
        self.config.state_root.mkdir(parents=True, exist_ok=True)
        self.control.mkdir(parents=True, exist_ok=True)
        if not (self.control / ".git").exists():
            self.git("init", "--quiet", ".")
        self._configure_remotes()
        self.git("config", "user.name", "OSS Hermes Upstream Sync")
        self.git("config", "user.email", "oss-hermes-upstream-sync@localhost")
        self.git("fetch", "--no-tags", "nous", "+refs/heads/main:refs/remotes/nous/main")
        self.git(
            "fetch", "--no-tags", "fork",
            "+refs/heads/prod:refs/remotes/fork/prod",
            "+refs/heads/main:refs/remotes/fork/main",
        )
        self.receipt.source_sha = self.git_text("rev-parse", "refs/remotes/nous/main")
        self.receipt.target_prod_sha = self.git_text("rev-parse", "refs/remotes/fork/prod")
        self.receipt.target_main_sha = self.git_text("rev-parse", "refs/remotes/fork/main")
        self.candidate.parent.mkdir(parents=True, exist_ok=True)
        self.git("worktree", "add", "--detach", str(self.candidate), "refs/remotes/nous/main")
        self.assert_candidate(self.receipt.source_sha)

    def _configure_remotes(self) -> None:
        existing = set(self.git_text("remote").splitlines())
        for name, url in (("nous", self.config.nous_url), ("fork", self.config.fork_url)):
            if name in existing:
                self.git("remote", "set-url", name, url)
            else:
                self.git("remote", "add", name, url)

    def port_kept_commits(self) -> None:
        assert self.receipt.source_sha and self.receipt.target_prod_sha
        commits = self.git_text(
            "rev-list", "--reverse", f"{self.receipt.source_sha}..{self.receipt.target_prod_sha}",
            cwd=self.candidate,
        ).splitlines()
        for commit in commits:
            # A conflicted keep can run Hermes for two hours. Stop before the
            # next one so the 10-hour budget is recorded before systemd's
            # 12-hour TimeoutStartSec leaves a stale running receipt.
            self.remaining_seconds()
            equivalent = self.git_text("cherry", "HEAD", commit, f"{commit}^", cwd=self.candidate)
            if equivalent.startswith("-"):
                self.delegate(commit, "The kept delta is patch-equivalent to an upstream change; decide retirement.")
                continue
            picked = self.git("cherry-pick", commit, cwd=self.candidate, check=False)
            if picked.returncode:
                self.delegate(commit, "The kept delta cherry-pick stopped on a conflict or an empty change.")
            self.assert_clean_candidate()

    def delegate(self, commit: str, reason: str) -> None:
        self.remaining_seconds()
        prompt = f"""{reason}
Kept fork commit: {commit}.
Work only in this candidate worktree: {self.candidate}. Do not modify any other checkout or worktree.
Do not perform gateway lifecycle operations: no gateway install, start, stop, restart, converge, or deployment.
Do not push to prod or main, do not approve or merge anything, and do not mutate any remote refs.
Preserve existing tests and scripts/check_fork_features.py coverage. Re-port the kept delta unless it can be retired.
Retire a kept delta only when its existing contract test passes without the hunk; run that test before retiring it.
Resolve the current cherry-pick if one is active, run the relevant tests, and finish with a clean committed candidate.
A retirement must leave no active cherry-pick or sequencer state. Do not prepare a partial result and call it success."""
        isolated = self.delegation_command(prompt, yolo=True)
        result = self.runner(isolated, self.candidate, False)
        if result.returncode and _unsupported_yolo(result):
            self.remaining_seconds()
            isolated = self.delegation_command(prompt, yolo=False)
            result = self.runner(isolated, self.candidate, False)
        if result.returncode:
            raise SyncError(f"Hermes delegation failed while re-porting {commit}")
        self.assert_clean_candidate()

    def delegation_command(self, prompt: str, *, yolo: bool) -> list[str]:
        """Model auth is inherited; GitHub write credentials are not."""
        agent_state = self.config.state_root / "agent-state"
        agent_state.mkdir(parents=True, exist_ok=True)
        gitconfig = agent_state / "gitconfig"
        gitconfig.touch(exist_ok=True)
        gh_config = agent_state / "gh"
        gh_config.mkdir(exist_ok=True)
        argv = [HERMES]
        if yolo:
            argv.append("--yolo")
        argv.extend(["chat", "-q", prompt])
        return [
            "env", "-u", "SSH_AUTH_SOCK", "-u", "GIT_ASKPASS", "-u", "SSH_ASKPASS",
            "-u", "GH_TOKEN", "-u", "GITHUB_TOKEN", "GIT_CONFIG_NOSYSTEM=1",
            f"GIT_CONFIG_GLOBAL={gitconfig}", f"GH_CONFIG_DIR={gh_config}",
            "GIT_SSH_COMMAND=/usr/bin/ssh -F /dev/null -o BatchMode=yes -o IdentitiesOnly=yes",
            *argv,
        ]

    def assert_clean_candidate(self) -> None:
        if self.git_text("status", "--porcelain", cwd=self.candidate):
            raise SyncError("Candidate is not clean after cherry-pick or delegation")
        cherry_pick = self.git("rev-parse", "--verify", "CHERRY_PICK_HEAD", cwd=self.candidate, check=False)
        if cherry_pick.returncode == 0:
            raise SyncError("Candidate still has an active cherry-pick")

    def assert_candidate(self, expected_sha: str) -> None:
        actual = self.git_text("rev-parse", "HEAD", cwd=self.candidate)
        if actual != expected_sha:
            raise SyncError(f"Candidate SHA changed unexpectedly: expected {expected_sha}, found {actual}")
        self.assert_clean_candidate()
        assert self.receipt.source_sha
        self.git(
            "merge-base", "--is-ancestor", self.receipt.source_sha, expected_sha,
            cwd=self.candidate,
        )

    def _tips_already_match_source(self) -> bool:
        """True when replaying would only mint new SHAs for the current trees.

        Cherry-pick keeps the author and stamps a new committer time, so a
        week whose upstream is already in prod republishes both channels and
        every exact-SHA converge tick drains the fleet for an empty tree.
        """
        source = self.receipt.source_sha
        prod = self.receipt.target_prod_sha
        main = self.receipt.target_main_sha
        assert source and prod and main
        contained = self.git("merge-base", "--is-ancestor", source, prod, check=False)
        if contained.returncode:
            return False
        return self._tree_oid(prod) == self._tree_oid(main)

    def _candidate_matches_published_trees(
            self, candidate_sha: str, prod: str, main: str) -> bool:
        candidate_tree = self._tree_oid(candidate_sha, cwd=self.candidate)
        return candidate_tree == self._tree_oid(prod) == self._tree_oid(main)

    def _tree_oid(self, rev: str, cwd: Path | None = None) -> str:
        return self.git_text("rev-parse", f"{rev}^{{tree}}", cwd=cwd)

    def _leave_tips_unchanged(self) -> None:
        prod = self.receipt.target_prod_sha
        main = self.receipt.target_main_sha
        assert prod and main
        self.receipt.status = "unchanged"
        self.receipt.tip_verification = {"prod": prod, "main": main}

    def remaining_seconds(self) -> int:
        remaining = self.deadline - self.monotonic()
        if remaining <= 0:
            raise SyncError("Upstream sync exceeded its overall 10-hour budget")
        return int(remaining)

    def run_local_gates(self, candidate_sha: str) -> None:
        self.remaining_seconds()
        self.assert_candidate(candidate_sha)
        self.runner([sys.executable, "scripts/check_fork_features.py"], self.candidate, True)
        self.runner(["bash", "scripts/run_tests.sh", *FOCUSED_TESTS], self.candidate, True)
        self.assert_candidate(candidate_sha)

    def push_wait_and_stabilize(self) -> None:
        for _ in range(self.config.max_ci_rounds):
            candidate_sha = self.git_text("rev-parse", "HEAD", cwd=self.candidate)
            self.receipt.candidate_sha = candidate_sha
            self.run_local_gates(candidate_sha)
            self.git("push", "fork", f"HEAD:refs/heads/{self.receipt.candidate_branch}", cwd=self.candidate)
            run = wait_for_ci(
                self.runner, self.config.fork_repo, self.receipt.candidate_branch, candidate_sha,
                min(self.config.ci_timeout_seconds, self.remaining_seconds()), self.config.ci_poll_seconds,
                sleeper=self.sleeper, monotonic=self.monotonic,
            )
            self.receipt.ci_run_url = run.get("url")
            remote_ref = f"refs/remotes/fork/{self.receipt.candidate_branch}"
            self.git(
                "fetch", "--no-tags", "fork",
                f"+refs/heads/{self.receipt.candidate_branch}:{remote_ref}", cwd=self.candidate,
            )
            remote_sha = self.git_text("rev-parse", remote_ref, cwd=self.candidate)
            if remote_sha == candidate_sha:
                return
            self.git("merge-base", "--is-ancestor", candidate_sha, remote_sha, cwd=self.candidate)
            self.git("merge", "--ff-only", remote_sha, cwd=self.candidate)
        raise SyncError("Fork CI kept changing the candidate; local and CI gates could not stabilize")

    def qualify_and_land(self) -> None:
        self.push_wait_and_stabilize()
        assert self.receipt.target_prod_sha
        candidate_sha = self.receipt.candidate_sha
        assert candidate_sha
        paths = self.git_text(
            "diff", "--name-only", f"{self.receipt.target_prod_sha}..{candidate_sha}", cwd=self.candidate,
        ).splitlines()
        self.receipt.actuator_paths = touches_actuator(paths)
        self.receipt.manual_flip_required = bool(self.receipt.actuator_paths)
        if self.receipt.manual_flip_required:
            self.receipt.manual_flip_notice = MANUAL_FLIP_NOTICE
        self.archive_and_land()

    def archive_and_land(self) -> None:
        """Revalidate baselines, preserve both tips, then atomically dual-land one SHA."""
        prod_baseline = self.receipt.target_prod_sha
        main_baseline = self.receipt.target_main_sha
        candidate_sha = self.receipt.candidate_sha
        assert prod_baseline and main_baseline and candidate_sha
        self.assert_candidate(candidate_sha)
        self._fetch_fork_channels()
        if self.git_text("rev-parse", "refs/remotes/fork/prod") != prod_baseline:
            raise SyncError("fork/prod moved after Fork CI; refusing to archive or land")
        if self.git_text("rev-parse", "refs/remotes/fork/main") != main_baseline:
            raise SyncError("fork/main moved after Fork CI; refusing to archive or land")
        if self._candidate_matches_published_trees(
                candidate_sha, prod_baseline, main_baseline):
            self._leave_tips_unchanged()
            return
        archives = {
            "prod": pre_sync_archive_ref("prod", self.receipt.run_id),
            "main": pre_sync_archive_ref("main", self.receipt.run_id),
        }
        baselines = {"prod": prod_baseline, "main": main_baseline}
        self._push_pre_sync_archives(archives, baselines)
        self.receipt.archive_refs = archives
        self.git(
            "push", "--atomic",
            f"--force-with-lease=refs/heads/prod:{prod_baseline}",
            f"--force-with-lease=refs/heads/main:{main_baseline}",
            "fork", f"{candidate_sha}:refs/heads/prod", f"{candidate_sha}:refs/heads/main",
            cwd=self.candidate,
        )
        self._fetch_fork_channels()
        prod_tip = self.git_text("rev-parse", "refs/remotes/fork/prod")
        main_tip = self.git_text("rev-parse", "refs/remotes/fork/main")
        if prod_tip != candidate_sha or main_tip != candidate_sha:
            raise SyncError("Remote tip verification failed after the atomic landing")
        self.receipt.tip_verification = {"prod": prod_tip, "main": main_tip}

    def _push_pre_sync_archives(self, archives: dict[str, str], baselines: dict[str, str]) -> None:
        """Publish the date-only snapshot pair, moving that date if it exists.

        A second run on the same day reuses the names prune keeps. The update
        is leased to the SHA just fetched so a ref that moved is left alone.
        """
        specs = [f"{baselines[channel]}:refs/heads/{ref}" for channel, ref in archives.items()]
        pushed = self.git("push", "fork", *specs, cwd=self.candidate, check=False)
        if not pushed.returncode:
            return
        self.git(
            "fetch", "--no-tags", "fork",
            *(f"+refs/heads/{ref}:refs/remotes/fork/{ref}" for ref in archives.values()),
            cwd=self.candidate,
        )
        argv = ["push"]
        updates: list[str] = []
        for channel, ref in archives.items():
            baseline = baselines[channel]
            remote = self.git(
                "rev-parse", "--verify", f"refs/remotes/fork/{ref}",
                cwd=self.candidate, check=False,
            )
            current = remote.stdout.strip()
            if remote.returncode == 0 and current == baseline:
                continue
            if remote.returncode == 0 and len(current) == 40:
                argv.append(f"--force-with-lease=refs/heads/{ref}:{current}")
            updates.append(f"{baseline}:refs/heads/{ref}")
        if updates:
            self.git(*argv, "fork", *updates, cwd=self.candidate)

    def _fetch_fork_channels(self) -> None:
        self.git(
            "fetch", "--no-tags", "fork",
            "+refs/heads/prod:refs/remotes/fork/prod",
            "+refs/heads/main:refs/remotes/fork/main",
        )


def _unsupported_yolo(result: Result) -> bool:
    output = f"{result.stdout}\n{result.stderr}".lower()
    return "--yolo" in output and any(marker in output for marker in (
        "unrecognized argument", "unknown option", "unknown flag", "no such option", "unexpected argument",
    ))


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Run gates and land only a verified candidate")
    run.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    run.add_argument("--state-root", type=Path, default=DEFAULT_STATE_ROOT)
    run.add_argument("--nous-url", default=NOUS_URL)
    run.add_argument("--fork-url", default=FORK_URL)
    run.add_argument("--fork-repo", default=FORK_REPO)
    run.add_argument("--ci-timeout-seconds", type=int, default=4 * 60 * 60)
    run.add_argument("--ci-poll-seconds", type=int, default=60)
    install = commands.add_parser("install", help="Copy units and script without enabling anything")
    install.add_argument("--unit-dir", type=Path, default=DEFAULT_UNIT_DIR)
    install.add_argument("--lib-dir", type=Path, default=DEFAULT_LIB_DIR)
    commands.add_parser("check-install", help="Fail unless the installed timer is enabled and active")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "install":
        install_files(args.unit_dir, args.lib_dir)
        return 0
    runner = CommandRunner()
    if args.command == "check-install":
        failures = check_install(runner)
        for failure in failures:
            print(failure, file=sys.stderr)
        return 1 if failures else 0
    config = Config(
        source=args.source, state_root=args.state_root, nous_url=args.nous_url,
        fork_url=args.fork_url, fork_repo=args.fork_repo,
        ci_timeout_seconds=args.ci_timeout_seconds, ci_poll_seconds=args.ci_poll_seconds,
    )
    now = datetime.now(UTC)
    run_id = now.strftime("%Y%m%d-%H%M%S")
    receipt = Receipt(run_id=run_id, candidate_branch=f"sync/upstream-{run_id}")
    sync = UpstreamSync(config, receipt, runner=runner)
    try:
        write_report(config.state_root, receipt)
        sync.run()
    except Exception as exc:  # health: allow BLE001 -- every failure must leave a durable run receipt
        receipt.status = "failed"
        receipt.error = str(exc)
        write_report(config.state_root, receipt)
        print(f"Upstream sync failed: {exc}", file=sys.stderr)
        return 1
    write_report(config.state_root, receipt)
    if receipt.status == "unchanged":
        print("Fork prod and main already carry this tree; tips were not rewritten")
        return 0
    print(f"Landed and verified {receipt.candidate_sha} on fork prod and main")
    if receipt.manual_flip_required:
        print(MANUAL_FLIP_NOTICE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
