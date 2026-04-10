"""Audit and harden installed macOS Hermes gateway launchd plists.

Usage:
    python -m hermes_cli.gateway_plist_audit --dry-run
    python -m hermes_cli.gateway_plist_audit --apply
"""

from __future__ import annotations

import argparse
import os
import plistlib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


DEFAULT_THROTTLE_INTERVAL = 10
DEFAULT_PATH_FALLBACK = "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


@dataclass
class AuditResult:
    path: Path
    found: bool
    changed: bool
    applied: bool
    notes: list[str]
    errors: list[str]


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _split_path(path_value: str | None) -> list[str]:
    if not path_value:
        return []
    return [entry for entry in path_value.split(os.pathsep) if entry]


def _dedupe(entries: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for entry in entries:
        if entry in seen:
            continue
        seen.add(entry)
        ordered.append(entry)
    return ordered


def detect_venv_bin(repo_root: Path | None = None) -> Path:
    repo_root = repo_root or _project_root()
    repo_venv = repo_root / "venv" / "bin"
    repo_dot_venv = repo_root / ".venv" / "bin"
    for candidate in (repo_venv, repo_dot_venv):
        if candidate.exists():
            return candidate

    current_prefix = os.environ.get("VIRTUAL_ENV", "").strip()
    if current_prefix:
        current_prefix_path = Path(current_prefix).expanduser()
        try:
            current_prefix_path.relative_to(repo_root)
            return current_prefix_path / "bin"
        except ValueError:
            pass
    if sys.prefix != sys.base_prefix:
        prefix_path = Path(sys.prefix).expanduser()
        try:
            prefix_path.relative_to(repo_root)
            return prefix_path / "bin"
        except ValueError:
            pass
    return repo_venv


def required_path_entries(home_dir: Path | None = None, repo_root: Path | None = None) -> list[str]:
    home_dir = (home_dir or Path.home()).expanduser()
    return [
        str(detect_venv_bin(repo_root=repo_root)),
        str(home_dir / ".local" / "bin"),
    ]


def build_hardened_path(
    existing_path: str | None,
    *,
    home_dir: Path | None = None,
    repo_root: Path | None = None,
    env_path: str | None = None,
) -> tuple[str, list[str]]:
    required = required_path_entries(home_dir=home_dir, repo_root=repo_root)
    existing_entries = _split_path(existing_path)
    baseline_entries = existing_entries or _split_path(env_path or os.environ.get("PATH")) or _split_path(DEFAULT_PATH_FALLBACK)
    hardened_entries = _dedupe(required + baseline_entries)
    added = [entry for entry in required if entry not in existing_entries]
    return os.pathsep.join(hardened_entries), added


def find_gateway_plists(launch_agents_dir: Path | None = None) -> list[Path]:
    launch_agents_dir = (launch_agents_dir or (Path.home() / "Library" / "LaunchAgents")).expanduser()
    return sorted(launch_agents_dir.glob("ai.hermes.gateway*.plist"))


def audit_plist(
    plist_path: Path,
    *,
    apply_changes: bool,
    home_dir: Path | None = None,
    repo_root: Path | None = None,
    env_path: str | None = None,
) -> AuditResult:
    result = AuditResult(
        path=plist_path,
        found=plist_path.exists(),
        changed=False,
        applied=False,
        notes=[],
        errors=[],
    )
    if not plist_path.exists():
        result.errors.append("file does not exist")
        return result

    try:
        original_bytes = plist_path.read_bytes()
        plist_data = plistlib.loads(original_bytes)
    except Exception as exc:  # pragma: no cover - defensive read error path
        result.errors.append(f"failed to read plist: {exc}")
        return result

    original_keepalive = plist_data.get("KeepAlive")
    if original_keepalive is True:
        result.notes.append("KeepAlive already unconditional true")
    else:
        plist_data["KeepAlive"] = True
        result.changed = True
        result.notes.append(f"KeepAlive: {original_keepalive!r} -> True")

    original_throttle = plist_data.get("ThrottleInterval")
    if original_throttle == DEFAULT_THROTTLE_INTERVAL:
        result.notes.append(f"ThrottleInterval already {DEFAULT_THROTTLE_INTERVAL}")
    else:
        plist_data["ThrottleInterval"] = DEFAULT_THROTTLE_INTERVAL
        result.changed = True
        result.notes.append(f"ThrottleInterval: {original_throttle!r} -> {DEFAULT_THROTTLE_INTERVAL}")

    env_vars = plist_data.get("EnvironmentVariables")
    if isinstance(env_vars, dict):
        env_dict = dict(env_vars)
    else:
        env_dict = {}
        result.changed = True
        result.notes.append("EnvironmentVariables: created missing dict")

    hardened_path, added_entries = build_hardened_path(
        env_dict.get("PATH"),
        home_dir=home_dir,
        repo_root=repo_root,
        env_path=env_path,
    )
    original_path = env_dict.get("PATH")
    if original_path == hardened_path:
        result.notes.append("PATH already includes required entries")
    else:
        env_dict["PATH"] = hardened_path
        plist_data["EnvironmentVariables"] = env_dict
        result.changed = True
        if added_entries:
            result.notes.append(f"PATH: added {', '.join(added_entries)}")
        else:
            result.notes.append("PATH: normalized while preserving existing entries")

    if apply_changes and result.changed:
        try:
            plist_path.write_bytes(plistlib.dumps(plist_data, fmt=plistlib.FMT_XML, sort_keys=False))
            result.applied = True
        except Exception as exc:  # pragma: no cover - defensive write error path
            result.errors.append(f"failed to write plist: {exc}")
    return result


def _print_result(result: AuditResult, *, dry_run: bool) -> None:
    status = "would update" if dry_run and result.changed else "updated" if result.applied else "ok" if not result.changed else "pending"
    print(f"{result.path} [{status}]")
    for note in result.notes:
        print(f"  - {note}")
    for error in result.errors:
        print(f"  - ERROR: {error}")


def run_audit(*, apply_changes: bool) -> int:
    repo_root = _project_root()
    launch_agents_dir = Path.home() / "Library" / "LaunchAgents"
    plist_paths = find_gateway_plists(launch_agents_dir=launch_agents_dir)
    dry_run = not apply_changes

    mode = "apply" if apply_changes else "dry-run"
    print(f"Gateway plist audit ({mode})")
    print(f"Scanning: {launch_agents_dir}")
    print(f"Required PATH entries: {', '.join(required_path_entries(repo_root=repo_root))}")

    if not plist_paths:
        print("No ai.hermes.gateway*.plist files found.")
        return 0

    print(f"Found {len(plist_paths)} plist file(s).")
    changed_count = 0
    error_count = 0

    for plist_path in plist_paths:
        result = audit_plist(
            plist_path,
            apply_changes=apply_changes,
            repo_root=repo_root,
        )
        if result.changed:
            changed_count += 1
        if result.errors:
            error_count += len(result.errors)
        _print_result(result, dry_run=dry_run)

    action = "would change" if dry_run else "changed"
    print(f"Summary: scanned={len(plist_paths)} {action}={changed_count} errors={error_count}")
    return 1 if error_count else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit and harden Hermes gateway launchd plists.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Report required changes without modifying plist files.")
    mode.add_argument("--apply", action="store_true", help="Write plist hardening changes in place.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    apply_changes = bool(args.apply)
    return run_audit(apply_changes=apply_changes)


if __name__ == "__main__":
    raise SystemExit(main())
