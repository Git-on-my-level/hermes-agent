#!/usr/bin/env python3
"""Fork branch retention: decide which remote branches survive, delete the rest.

Implements the retention policy in FORK.md "Branch retention" (#36):

    keep   1. the channel branches (``main``, ``prod``) and the remote HEAD,
           2. the head branch of every open PR that lives in this fork
              (fork-internal and fork -> upstream),
           3. every branch whose tip is newer than --max-age-days (90),
           4. the newest --archive-keep (2) ``archive/*-pre-sync-<date>``
              snapshot dates.
    delete the rest.

Two passes, on purpose:

    1. plan (default) — evaluates the rules and writes the delete list
       (name + tip SHA + tip date). The list is meant to be committed: it is
       the recovery record for everything pass 2 removes.
    2. apply — deletes exactly the refs a committed list names. Every delete
       is guarded by ``--force-with-lease=<ref>:<sha>``, so a branch that
       moved since the list was published is rejected, not deleted; channel
       branches and open PR heads are re-checked and skipped.

Stdlib only. Plan mode is read-only apart from the list file it writes.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_REMOTE = "fork"
DEFAULT_REPO = "Git-on-my-level/hermes-agent"
DEFAULT_UPSTREAM_REPO = "NousResearch/hermes-agent"
CHANNEL_BRANCHES = ("main", "prod")
LIST_DIR = REPO_ROOT / "scripts" / "prune-lists"
PUSH_CHUNK = 100
PR_LIMIT = 500

# archive/<channel>-pre-sync-<date> — the snapshot pair every sync leaves.
PRE_SYNC_RE = re.compile(
    r"^archive/(?P<channel>.+)-pre-sync-(?P<date>\d{4}-\d{2}-\d{2})$"
)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class Branch:
    name: str
    sha: str
    tip: datetime  # committer date of the tip commit


@dataclass(frozen=True)
class Decision:
    branch: Branch
    keep: str | None  # reason the branch is kept; None = delete


def valid_refname(name: str) -> bool:
    """Reject anything we would not hand to `git push` as a refspec."""
    if not name or name != name.strip():
        return False
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._+/~-]*$", name):
        return False
    return not (
        ".." in name
        or "@{" in name
        or name.endswith("/")
        or name.endswith(".lock")
        or any(part.startswith(".") for part in name.split("/"))
    )


# ---------------------------------------------------------------- git / gh IO


def _run(argv: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(argv, capture_output=True, text=True)
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise SystemExit(
            f"command failed ({proc.returncode}): {' '.join(argv)}\n{detail}"
        )
    return proc


def fetch_remote(run, remote: str) -> None:
    # Targeted refspec fetch, never `git fetch --all --tags` (FORK.md).
    run(["git", "fetch", remote, f"+refs/heads/*:refs/remotes/{remote}/*", "--prune"])


def collect_branches(run, remote: str) -> list[Branch]:
    out = run([
        "git",
        "for-each-ref",
        f"refs/remotes/{remote}",
        "--format=%(refname:strip=3) %(objectname) %(committerdate:iso-strict)",
    ]).stdout
    branches = []
    for line in out.splitlines():
        name, sha, date = line.split(" ")
        if name == "HEAD":  # the symref for-each-ref also lists
            continue
        try:
            tip = datetime.fromisoformat(date)
        except ValueError as exc:
            raise SystemExit(f"unparsable tip date for {name}: {date!r}") from exc
        branches.append(Branch(name=name, sha=sha, tip=tip))
    return branches


def default_branch(run, remote: str) -> str | None:
    proc = run(["git", "symbolic-ref", f"refs/remotes/{remote}/HEAD"], check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip().removeprefix(f"refs/remotes/{remote}/")


def open_pr_heads(run, repo: str, upstream_repo: str) -> set[str]:
    """Head branches of open PRs in the fork, plus fork -> upstream PRs."""
    heads: set[str] = set()
    for row in json.loads(
        run([
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--json",
            "headRefName",
            "--limit",
            str(PR_LIMIT),
        ]).stdout
    ):
        heads.add(row["headRefName"])
    owner = repo.split("/")[0]
    for row in json.loads(
        run([
            "gh",
            "pr",
            "list",
            "--repo",
            upstream_repo,
            "--state",
            "open",
            "--json",
            "headRepositoryOwner,headRefName",
            "--limit",
            str(PR_LIMIT),
        ]).stdout
    ):
        # Only heads that live in this fork name a branch we could delete.
        if (row.get("headRepositoryOwner") or {}).get("login") == owner:
            heads.add(row["headRefName"])
    return heads


def live_heads(run, remote: str, names: list[str]) -> set[str]:
    """Which of `names` still exist on the remote (authoritative post-state)."""
    out = run(["git", "ls-remote", remote, *[f"refs/heads/{n}" for n in names]]).stdout
    prefix = "refs/heads/"
    return {line.split("\t", 1)[1][len(prefix) :] for line in out.splitlines() if line}


# ----------------------------------------------------------------- the rules


def newest_pre_sync_dates(branches: list[Branch], keep: int) -> set[str]:
    dates = {m.group("date") for b in branches if (m := PRE_SYNC_RE.match(b.name))}
    return set(sorted(dates, reverse=True)[: max(keep, 0)])


def plan(
    branches: list[Branch],
    *,
    now: datetime,
    open_pr_heads: set[str] = frozenset(),
    protected: set[str] = frozenset(CHANNEL_BRANCHES),
    max_age_days: int = 90,
    archive_keep: int = 2,
) -> list[Decision]:
    cutoff = now - timedelta(days=max_age_days)
    kept_dates = newest_pre_sync_dates(branches, archive_keep)
    decisions = []
    for branch in branches:
        if branch.name in protected:
            decisions.append(Decision(branch, "channel/default branch"))
        elif branch.name in open_pr_heads:
            decisions.append(Decision(branch, "open PR head"))
        elif m := PRE_SYNC_RE.match(branch.name):
            # Snapshot retention is date-based, period: a pre-sync archive is
            # a frozen position of a channel branch, so its tip is always as
            # fresh as the sync that made it — the age rule would keep every
            # pair for 90 days and the archive set would grow forever.
            if m.group("date") in kept_dates:
                decisions.append(
                    Decision(branch, f"pre-sync snapshot (newest {archive_keep} dates)")
                )
            else:
                decisions.append(Decision(branch, None))
        elif branch.tip >= cutoff:
            decisions.append(Decision(branch, f"tip newer than {max_age_days} days"))
        else:
            decisions.append(Decision(branch, None))
    return decisions


# ------------------------------------------------------------- the list file


def render_list(
    decisions: list[Decision],
    *,
    remote: str,
    now: datetime,
    max_age_days: int,
    archive_keep: int,
) -> str:
    deleting = sorted(
        (d for d in decisions if d.keep is None), key=lambda d: d.branch.name
    )
    lines = [
        f"# fork branch prune list: remote={remote} generated={now.isoformat(timespec='seconds')}",
        f"# rule: keep channel/default branches and open PR heads, tips newer than"
        f" {max_age_days} days, newest {archive_keep} pre-sync dates",
        "# recover a ref: git push <remote> <sha>:refs/heads/<name>",
    ]
    lines += [
        f"{d.branch.sha} {d.branch.tip.isoformat(timespec='seconds')} {d.branch.name}"
        for d in deleting
    ]
    return "\n".join(lines) + "\n"


def parse_list(text: str) -> list[tuple[str, str]]:
    """(name, sha) pairs from a committed list; malformed input is fatal."""
    entries: list[tuple[str, str]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        if len(parts) != 3:
            raise ValueError(
                f"line {lineno}: expected '<sha> <date> <name>', got {stripped!r}"
            )
        sha, date, name = parts
        if not SHA_RE.match(sha):
            raise ValueError(f"line {lineno}: not a 40-hex sha: {sha!r}")
        if not valid_refname(name):
            raise ValueError(f"line {lineno}: not a branch name: {name!r}")
        try:
            datetime.fromisoformat(date)
        except ValueError as exc:
            raise ValueError(f"line {lineno}: unparsable date {date!r}") from exc
        entries.append((name, sha))
    if not entries:
        raise ValueError("list has no entries")
    return entries


# ---------------------------------------------------------------- apply pass


def push_argv(remote: str, chunk: list[tuple[str, str]]) -> list[str]:
    argv = ["git", "push", remote]
    argv += [f"--force-with-lease=refs/heads/{name}:{sha}" for name, sha in chunk]
    argv += ["--delete", *[f"refs/heads/{name}" for name, _ in chunk]]
    return argv


def apply_deletions(
    run, remote: str, entries: list[tuple[str, str]], *, skip: dict[str, str]
) -> list[tuple[str, str, str]]:
    """Delete `entries` (skipping `skip`); returns (name, status, detail)."""
    pending = [e for e in entries if e[0] not in skip]
    for start in range(0, len(pending), PUSH_CHUNK):
        chunk = pending[start : start + PUSH_CHUNK]
        if run(push_argv(remote, chunk), check=False).returncode != 0:
            # Isolate the offender(s): one rejected ref fails the whole batch.
            for entry in chunk:
                run(push_argv(remote, [entry]), check=False)
    still_live = live_heads(run, remote, [name for name, _ in pending])
    results = [(name, "skipped", reason) for name, reason in sorted(skip.items())]
    results += [
        (name, "deleted" if name not in still_live else "FAILED", "still on remote")
        for name, _ in sorted(pending)
    ]
    return sorted(results)


# --------------------------------------------------------------------- main


def plan_main(args, run) -> int:
    if not args.no_fetch:
        fetch_remote(run, args.remote)
    branches = collect_branches(run, args.remote)
    head = default_branch(run, args.remote)
    protected = set(CHANNEL_BRANCHES)
    if head:
        protected.add(head)
    try:
        open_heads = open_pr_heads(run, args.repo, args.upstream_repo)
    except SystemExit:
        print(
            "warning: could not read open PRs (gh unavailable?) — the list is "
            "advisory until --apply re-checks; re-run with gh on PATH",
            file=sys.stderr,
        )
        open_heads = set()
    now = datetime.now(timezone.utc)
    decisions = plan(
        branches,
        now=now,
        open_pr_heads=open_heads,
        protected=protected,
        max_age_days=args.max_age_days,
        archive_keep=args.archive_keep,
    )

    keep_reasons: dict[str, int] = {}
    prefixes: dict[str, int] = {}
    for decision in decisions:
        if decision.keep:
            keep_reasons[decision.keep] = keep_reasons.get(decision.keep, 0) + 1
        else:
            prefix = decision.branch.name.split("/", 1)[0]
            prefixes[prefix] = prefixes.get(prefix, 0) + 1
    deletes = sum(prefixes.values())
    print(
        f"{args.remote}: {len(branches)} branches, "
        f"{len(branches) - deletes} keep, {deletes} delete"
    )
    for reason, count in sorted(keep_reasons.items(), key=lambda kv: -kv[1]):
        print(f"  keep    {count:4d}  {reason}")
    for prefix, count in sorted(prefixes.items(), key=lambda kv: -kv[1]):
        print(f"  delete  {count:4d}  {prefix}/…")

    list_file = args.list_file or LIST_DIR / f"{now:%Y-%m-%d}.txt"
    list_file.parent.mkdir(parents=True, exist_ok=True)
    list_file.write_text(
        render_list(
            decisions,
            remote=args.remote,
            now=now,
            max_age_days=args.max_age_days,
            archive_keep=args.archive_keep,
        ),
        encoding="utf-8",
    )
    try:  # repo-relative for the copy-paste hint; absolute if written elsewhere
        shown = list_file.resolve().relative_to(REPO_ROOT)
    except ValueError:
        shown = list_file.resolve()
    print(f"\nwrote {shown}")
    print("commit it, then delete exactly what it names:")
    print(f"  python3 scripts/prune_fork_branches.py --apply --list-file {shown}")
    return 0


def apply_main(args, run) -> int:
    if not args.list_file:
        print(
            "--apply requires --list-file pointing at the committed list",
            file=sys.stderr,
        )
        return 1
    entries = parse_list(args.list_file.read_text(encoding="utf-8"))
    # Re-derive the guards now, not at plan time: a PR may have been opened,
    # a branch moved, since the list was published.
    open_heads = open_pr_heads(run, args.repo, args.upstream_repo)
    head = default_branch(run, args.remote) or ""
    protected = set(CHANNEL_BRANCHES) | {head}
    skip = {name: "channel/default branch" for name, _ in entries if name in protected}
    skip.update({name: "open PR head" for name, _ in entries if name in open_heads})

    deleting = len(entries) - len(skip)
    print(
        f"{args.list_file.name}: {len(entries)} entries, deleting {deleting}, skipping {len(skip)}"
    )
    if not args.yes:
        try:
            answer = input(f"delete {deleting} refs from {args.remote}? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("aborted")
            return 1

    results = apply_deletions(run, args.remote, entries, skip=skip)
    failed = 0
    for name, status, detail in results:
        print(f"  {status:<8} {name}  {detail if status != 'deleted' else ''}".rstrip())
        failed += status == "FAILED"
    print(f"\ndone: {deleting - failed} deleted, {len(skip)} skipped, {failed} failed")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--remote",
        default=DEFAULT_REMOTE,
        help="fork remote name (default: %(default)s)",
    )
    parser.add_argument(
        "--repo", default=DEFAULT_REPO, help="fork slug for open-PR lookup"
    )
    parser.add_argument(
        "--upstream-repo",
        default=DEFAULT_UPSTREAM_REPO,
        help="upstream slug for fork->upstream PR heads",
    )
    parser.add_argument(
        "--max-age-days",
        type=int,
        default=90,
        help="keep branches with a tip newer than this (default: %(default)s)",
    )
    parser.add_argument(
        "--archive-keep",
        type=int,
        default=2,
        help="newest pre-sync snapshot dates to keep (default: %(default)s)",
    )
    parser.add_argument(
        "--no-fetch", action="store_true", help="use current remote-tracking refs as-is"
    )
    parser.add_argument(
        "--list-file", type=Path, help="delete list to write (plan) or apply (apply)"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="delete exactly the refs named in --list-file",
    )
    parser.add_argument(
        "--yes", action="store_true", help="skip the confirmation prompt"
    )
    args = parser.parse_args(argv)
    try:
        return apply_main(args, _run) if args.apply else plan_main(args, _run)
    except SystemExit as exc:
        if exc.code == 0:
            return 0
        print(exc.code if isinstance(exc.code, str) else "failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
