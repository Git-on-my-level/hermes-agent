#!/usr/bin/env python3
"""Re-read fork/prod before landing a sync; absorb one clean late commit locally.

Never pushes. Conflicting cherry-picks are aborted; larger/rebased deltas stop
for an explicit keep-list review rather than replaying the whole fork.
"""
import argparse
import subprocess
import sys


def git(*args):
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


def check_prod_tip(baseline):
    if git("status", "--porcelain"):
        raise ValueError("Use a clean sync worktree")
    branch = git("branch", "--show-current")
    if not branch or branch in {"main", "prod"}:
        raise ValueError("Use a named sync branch, never main/prod or detached HEAD")
    # Resolving ^{commit} also rejects an absent/invalid baseline before fetching.
    baseline = git("rev-parse", "--verify", f"{baseline}^{{commit}}")
    git("fetch", "fork", "+refs/heads/prod:refs/remotes/fork/prod")
    tip = git("rev-parse", "fork/prod")
    if baseline == tip:
        print(f"fork/prod unchanged: {tip}")
        return tip
    parents = git("rev-list", "--parents", "-n", "1", tip).split()
    if parents != [tip, baseline]:
        raise ValueError(f"fork/prod moved beyond one linear commit ({baseline} -> {tip}); review the delta")
    # A repeat invocation after successful absorption is harmless, including a re-ported commit.
    if not any(line.startswith("+") for line in git("cherry", "HEAD", tip, baseline).splitlines()):
        print(f"Late prod commit already represented: {tip}")
        return tip
    try:
        git("cherry-pick", tip)
    except subprocess.CalledProcessError as exc:
        git("cherry-pick", "--abort")
        raise ValueError(f"Late prod commit {tip} conflicts; cherry-pick aborted, review/re-port this delta") from exc
    print(f"Absorbed late prod commit {tip}; re-run focused tests and check_fork_features.py")
    return tip


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, help="Exact fork/prod SHA recorded at sync start")
    args = parser.parse_args()
    try:
        check_prod_tip(args.baseline)
    except (ValueError, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
