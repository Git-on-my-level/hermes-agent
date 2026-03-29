from pathlib import Path
from types import SimpleNamespace

from hermes_cli import update_channel


def test_detect_update_target_prefers_explicit_prod_channel(tmp_path):
    repo_dir = tmp_path / "hermes-agent"
    repo_dir.mkdir()
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    update_channel.write_update_channel("prod", hermes_home)

    target = update_channel.detect_update_target(repo_dir, hermes_home)

    assert target == ("prod", "fork", "prod")


def test_detect_update_target_infers_prod_from_tracking_branch(monkeypatch, tmp_path):
    repo_dir = tmp_path / "hermes-agent"
    repo_dir.mkdir()

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd == ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "prod@{upstream}"]:
            return SimpleNamespace(returncode=0, stdout="fork/prod\n", stderr="")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(update_channel.subprocess, "run", fake_run)

    target = update_channel.detect_update_target(repo_dir, tmp_path / ".hermes")

    assert target == ("prod", "fork", "prod")
    assert calls == [["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "prod@{upstream}"]]


def test_detect_update_target_defaults_to_main_when_prod_not_configured(monkeypatch, tmp_path):
    repo_dir = tmp_path / "hermes-agent"
    repo_dir.mkdir()

    def fake_run(cmd, **kwargs):
        if cmd == ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "prod@{upstream}"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="fatal\n")
        if cmd == ["git", "rev-parse", "--abbrev-ref", "HEAD"]:
            return SimpleNamespace(returncode=0, stdout="feature/demo\n", stderr="")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(update_channel.subprocess, "run", fake_run)

    target = update_channel.detect_update_target(repo_dir, tmp_path / ".hermes")

    assert target == ("main", "origin", "main")


def test_write_update_channel_ignores_invalid_values(tmp_path):
    hermes_home = tmp_path / ".hermes"

    update_channel.write_update_channel("weird", hermes_home)

    assert not (hermes_home / "install-channel").exists()
