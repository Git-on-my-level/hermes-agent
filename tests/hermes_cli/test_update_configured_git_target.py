"""Git target selection through the real config loader and update command."""
import subprocess
from types import SimpleNamespace

import pytest

from hermes_cli import main, update_cmd, update_converge
from hermes_cli.update_git_target import configured_git_target
from tests.hermes_cli.test_update_target_identity import git, update_tree  # noqa: F401


@pytest.mark.parametrize("config,target", [
    ("updates:\n  remote: fork\n  branch: prod\n", ("fork", "prod")),
    ("updates:\n  remote: ''\n  branch: ''\n", None),
    ("updates: {}\n", None),
])
def test_real_config_selects_git_target(tmp_path, monkeypatch, config, target):
    # A -> B -> A proves defaults/config are not cached across homes.
    homes = [tmp_path / "a", tmp_path / "b"]
    for home, body in zip(homes, [config, "updates: {}\n"]):
        home.mkdir()
        (home / "config.yaml").write_text(body)
    for home, expected in [(homes[0], target), (homes[1], None), (homes[0], target)]:
        monkeypatch.setenv("HERMES_HOME", str(home))
        assert configured_git_target() == expected
        assert configured_git_target(SimpleNamespace(remote="origin", branch="main")) == ("origin", "main")
        assert configured_git_target(SimpleNamespace(channel="stable")) is None


def setup_target(t, tmp_path, monkeypatch, configured=True):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    body = "updates:\n  remote: fork\n  branch: prod\n  converge: true\n" if configured else "updates: {}\n"
    (home / "config.yaml").write_text(body)
    git(t.origin, "branch", "prod", t.wanted)
    git(t.clone, "remote", "add", "fork", str(t.origin))
    git(t.clone, "fetch", "fork", "+refs/heads/prod:refs/remotes/fork/prod")
    git(t.clone, "checkout", "-qb", "prod", t.base)
    t.args.channel = None
    return home


@pytest.mark.parametrize("mode", ["check", "apply", "converge", "failed-pin", "absent-pin", "missing-local-prod", "parked", "in-place", "narrow", "stock"])
def test_command_never_detours_to_upstream(update_tree, tmp_path, monkeypatch, mode, capsys):
    t = update_tree
    monkeypatch.setattr("hermes_cli.update_owning_install.retarget_to_owning_install", lambda *_: None)
    setup_target(t, tmp_path, monkeypatch, configured=mode != "stock")
    if mode == "stock":
        monkeypatch.setattr(main, "_sync_with_upstream_if_needed", lambda *_a, **_k: None)
        git(t.clone, "checkout", "-q", "main")
    if mode == "missing-local-prod":
        git(t.clone, "checkout", "-q", "--detach", t.base)
        git(t.clone, "branch", "-D", "prod")
    if mode in {"parked", "in-place"}:
        git(t.clone, "checkout", "-qb", "feature")
        if mode == "in-place":
            git(t.clone, "commit", "--allow-empty", "-qm", "local feature")
            config = tmp_path / "home" / "config.yaml"
            config.write_text(config.read_text() + "  parked_branch_strategy: update_in_place\n")
    if mode == "narrow":
        git(t.clone, "config", "remote.fork.fetch", "+refs/heads/main:refs/remotes/fork/main")
        git(t.clone, "update-ref", "-d", "refs/remotes/fork/prod")
    commands = []
    run = subprocess.run

    def traced(command, *args, **kwargs):
        commands.append(command)
        if mode == "failed-pin" and command[-3:] == ["reset", "--hard", t.wanted]:
            return subprocess.CompletedProcess(command, 1, "", "simulated reset failure")
        return run(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", traced)
    if mode in {"converge", "failed-pin", "absent-pin"}:
        settings = update_converge.ConvergeSettings(
            enabled=True, pin="f" * 40 if mode == "absent-pin" else t.wanted,
            interval=1800, busy_sla=21600, skip_gateway_restart=False,
            remote="fork", branch="prod")
        monkeypatch.setattr(update_converge, "load_converge_settings", lambda: settings)
        monkeypatch.setattr(update_converge, "gateway_is_busy", lambda: False)
        monkeypatch.setattr(update_converge, "live_code_sha", lambda: t.base)
        monkeypatch.setattr(update_converge, "_mark_planned_drain", lambda: None)
        if mode in {"failed-pin", "absent-pin"}:
            with pytest.raises(SystemExit) as exc:
                update_converge.cmd_converge_tick(t.args)
            assert exc.value.code == 1
        else:
            update_converge.cmd_converge_tick(t.args)
    else:
        t.args.check = mode == "check"
        main.cmd_update(t.args)
    expected = t.newer if mode == "stock" else t.wanted
    if mode == "check":
        assert git(t.clone, "rev-parse", "HEAD") == t.base
        assert "fork/prod" in capsys.readouterr().out
    elif mode in {"failed-pin", "absent-pin"}:
        assert git(t.clone, "rev-parse", "HEAD") == t.base
        assert not t.requests
    else:
        if mode == "in-place":
            git(t.clone, "merge-base", "--is-ancestor", expected, "HEAD")
            assert git(t.clone, "branch", "--show-current") == "feature"
            assert t.requests[-1]["expected_sha"] == git(t.clone, "rev-parse", "HEAD")
        else:
            assert git(t.clone, "rev-parse", "HEAD") == expected
            assert t.requests[-1]["expected_sha"] == expected
    if mode != "stock":
        assert not any("origin/main" in str(c) for c in commands)
        assert not any("checkout" in c and "main" in c for c in commands)
        assert not any("fetch" in c and "origin" in c for c in commands)
