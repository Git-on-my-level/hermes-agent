"""End-to-end contracts for the personal-fork POSIX installer.

The installer is run as a real shell process against local bare repositories.
A tiny git shim substitutes only the two public GitHub URLs, so the test
exercises clone, remote reconciliation, branch tracking, rerun updates, and
commit pinning without network access.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"


def _git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


def _commit(repo: Path, content: str) -> str:
    (repo / "marker.txt").write_text(content, encoding="utf-8")
    _git("add", "marker.txt", cwd=repo)
    _git("commit", "-m", content, cwd=repo)
    return _git("rev-parse", "HEAD", cwd=repo)


def _make_bare_repo(tmp_path: Path, name: str) -> tuple[Path, Path, str]:
    worktree = tmp_path / f"{name}-worktree"
    _git("init", "--initial-branch=prod", str(worktree))
    _git("config", "user.email", "installer-test@example.invalid", cwd=worktree)
    _git("config", "user.name", "Installer Test", cwd=worktree)
    first_commit = _commit(worktree, "first")
    _commit(worktree, "second")
    bare = tmp_path / f"{name}.git"
    _git("clone", "--bare", str(worktree), str(bare))
    return worktree, bare, first_commit


def _write_git_shim(tmp_path: Path, *, fork_url: str, upstream_url: str) -> Path:
    real_git = shutil.which("git")
    assert real_git is not None
    shim = tmp_path / "git"
    shim.write_text(
        f"""#!/bin/sh
set -eu
real_git={real_git!r}
fork_url={fork_url!r}
upstream_url={upstream_url!r}
fork_https='https://github.com/Git-on-my-level/hermes-agent.git'
fork_ssh='git@github.com:Git-on-my-level/hermes-agent.git'
upstream_https='https://github.com/NousResearch/hermes-agent.git'

map_url() {{
    case \"$1\" in
        \"$fork_https\"|\"$fork_ssh\") printf '%s\\n' \"$fork_url\" ;;
        \"$upstream_https\") printf '%s\\n' \"$upstream_url\" ;;
        *) printf '%s\\n' \"$1\" ;;
    esac
}}

if [ \"$1\" = clone ]; then
    # The installer supplies: clone --depth 1 --branch <branch> <url> <dir>.
    exec \"$real_git\" \"$1\" \"$2\" \"$3\" \"$4\" \"$5\" \"$(map_url \"$6\")\" \"$7\"
fi
if [ \"$1\" = remote ] && [ \"$2\" = add ]; then
    exec \"$real_git\" remote add \"$3\" \"$(map_url \"$4\")\"
fi
if [ \"$1\" = remote ] && [ \"$2\" = set-url ]; then
    exec \"$real_git\" remote set-url \"$3\" \"$(map_url \"$4\")\"
fi
exec \"$real_git\" \"$@\"
""",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return shim


def _run_repository_stage(
    tmp_path: Path,
    *,
    install_dir: Path,
    fork_url: str,
    upstream_url: str,
    commit: str | None = None,
) -> None:
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir(exist_ok=True)
    _write_git_shim(shim_dir, fork_url=fork_url, upstream_url=upstream_url)
    env = os.environ | {
        "PATH": f"{shim_dir}{os.pathsep}{os.environ['PATH']}",
        "HERMES_HOME": str(tmp_path / "hermes-home"),
    }
    command = [
        "bash",
        str(INSTALL_SH),
        "--stage",
        "repository",
        "--non-interactive",
        "--dir",
        str(install_dir),
    ]
    if commit:
        command.extend(["--commit", commit])
    subprocess.run(command, env=env, check=True, text=True, capture_output=True)


def test_fork_installer_tracks_fork_prod_and_repairs_it_on_rerun(tmp_path: Path) -> None:
    fork_worktree, fork_bare, _ = _make_bare_repo(tmp_path, "fork")
    _, upstream_bare, _ = _make_bare_repo(tmp_path, "upstream")
    install_dir = tmp_path / "installed-hermes"
    fork_url = fork_bare.as_uri()
    upstream_url = upstream_bare.as_uri()

    _run_repository_stage(
        tmp_path,
        install_dir=install_dir,
        fork_url=fork_url,
        upstream_url=upstream_url,
    )

    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=install_dir) == "prod"
    assert _git("remote", "get-url", "fork", cwd=install_dir) == fork_url
    assert _git("remote", "get-url", "origin", cwd=install_dir) == upstream_url
    assert _git("config", "branch.prod.remote", cwd=install_dir) == "fork"
    assert _git("config", "branch.prod.merge", cwd=install_dir) == "refs/heads/prod"

    deployed_commit = _commit(fork_worktree, "third")
    _git("push", str(fork_bare), "prod", cwd=fork_worktree)
    _run_repository_stage(
        tmp_path,
        install_dir=install_dir,
        fork_url=fork_url,
        upstream_url=upstream_url,
    )
    assert _git("rev-parse", "HEAD", cwd=install_dir) == deployed_commit

    _git("config", "user.email", "installer-test@example.invalid", cwd=install_dir)
    _git("config", "user.name", "Installer Test", cwd=install_dir)
    _commit(install_dir, "local-only")
    _run_repository_stage(
        tmp_path,
        install_dir=install_dir,
        fork_url=fork_url,
        upstream_url=upstream_url,
    )
    assert _git("rev-parse", "HEAD", cwd=install_dir) == deployed_commit


def test_fork_installer_honors_a_commit_pin(tmp_path: Path) -> None:
    _, fork_bare, first_commit = _make_bare_repo(tmp_path, "fork")
    _, upstream_bare, _ = _make_bare_repo(tmp_path, "upstream")
    install_dir = tmp_path / "pinned-hermes"

    _run_repository_stage(
        tmp_path,
        install_dir=install_dir,
        fork_url=fork_bare.as_uri(),
        upstream_url=upstream_bare.as_uri(),
        commit=first_commit,
    )

    assert _git("rev-parse", "HEAD", cwd=install_dir) == first_commit
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=install_dir) == "HEAD"
    assert _git("remote", "get-url", "fork", cwd=install_dir) == fork_bare.as_uri()
    assert _git("remote", "get-url", "origin", cwd=install_dir) == upstream_bare.as_uri()


def test_fork_installer_configures_the_supported_update_channel_without_touching_env(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    # The installer normally creates venv/ before its config stage. Build the
    # smallest real runtime layout here, using the wrapper's interpreter and
    # source modules rather than faking the config writer.
    (runtime / "hermes_cli").symlink_to(REPO_ROOT / "hermes_cli")
    (runtime / "utils.py").symlink_to(REPO_ROOT / "utils.py")
    (runtime / "hermes_constants.py").symlink_to(REPO_ROOT / "hermes_constants.py")
    (runtime / ".env.example").symlink_to(REPO_ROOT / ".env.example")
    (runtime / "cli-config.yaml.example").symlink_to(
        REPO_ROOT / "cli-config.yaml.example"
    )
    venv_bin = runtime / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    runtime_python = venv_bin / "python"
    runtime_python.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8"
    )
    runtime_python.chmod(0o755)

    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    env_file = hermes_home / ".env"
    env_file.write_text("EXISTING_API_KEY=keep-me\n", encoding="utf-8")
    env = os.environ | {"HERMES_HOME": str(hermes_home)}

    result = subprocess.run(
        [
            "bash",
            str(INSTALL_SH),
            "--stage",
            "config",
            "--non-interactive",
            "--dir",
            str(runtime),
        ],
        cwd=runtime,
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )

    assert "Configured Hermes updates: fork/prod" in result.stdout, result.stderr
    config = yaml.safe_load((hermes_home / "config.yaml").read_text(encoding="utf-8"))
    assert config["updates"]["remote"] == "fork"
    assert config["updates"]["branch"] == "prod"
    assert env_file.read_text(encoding="utf-8") == "EXISTING_API_KEY=keep-me\n"
