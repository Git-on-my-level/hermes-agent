"""Config-driven update channel (updates.remote / updates.branch)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hermes_cli.main import (
    _load_update_channel_defaults,
    _resolve_update_branch,
    _resolve_update_target,
    cmd_update,
)


class TestResolveUpdateTarget:
    def test_defaults_are_origin_main(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.main._load_update_channel_defaults",
            lambda: ("origin", "main"),
        )
        assert _resolve_update_target(SimpleNamespace(remote=None, branch=None)) == (
            "origin",
            "main",
        )
        assert _resolve_update_branch(SimpleNamespace(remote=None, branch=None)) == "main"

    def test_config_defaults_apply_when_cli_omitted(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.main._load_update_channel_defaults",
            lambda: ("fork", "prod"),
        )
        assert _resolve_update_target(SimpleNamespace(remote=None, branch=None)) == (
            "fork",
            "prod",
        )

    def test_cli_overrides_config(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.main._load_update_channel_defaults",
            lambda: ("fork", "prod"),
        )
        args = SimpleNamespace(remote="origin", branch="main")
        assert _resolve_update_target(args) == ("origin", "main")

    def test_empty_cli_values_do_not_override(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.main._load_update_channel_defaults",
            lambda: ("fork", "prod"),
        )
        args = SimpleNamespace(remote="  ", branch="")
        assert _resolve_update_target(args) == ("fork", "prod")

    def test_load_defaults_from_config_dict(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        cfg = {"updates": {"remote": "fork", "branch": "prod"}}
        with patch("hermes_cli.config.load_config", return_value=cfg):
            # Import path used inside helper
            with patch("hermes_cli.main.load_config", create=True):
                pass
        # Call real helper with patched load_config via hermes_cli.config
        with patch("hermes_cli.config.load_config", return_value=cfg):
            assert _load_update_channel_defaults() == ("fork", "prod")


class TestCmdUpdateCheckUsesConfiguredRemote:
    def _side_effect(self, *, remote: str, branch: str, commit_count: str = "3"):
        def side_effect(cmd, **kwargs):
            joined = " ".join(str(c) for c in cmd)
            if "rev-parse" in joined and "--is-shallow-repository" in joined:
                return __import__("subprocess").CompletedProcess(cmd, 0, stdout="false\n", stderr="")
            if "fetch" in joined:
                return __import__("subprocess").CompletedProcess(cmd, 0, stdout="", stderr="")
            if "rev-parse" in joined and "--verify" in joined:
                return __import__("subprocess").CompletedProcess(cmd, 0, stdout="", stderr="")
            if "rev-list" in joined:
                return __import__("subprocess").CompletedProcess(
                    cmd, 0, stdout=f"{commit_count}\n", stderr=""
                )
            return __import__("subprocess").CompletedProcess(cmd, 0, stdout="", stderr="")

        return side_effect

    @patch("hermes_cli.config.detect_install_method", return_value="git")
    @patch("subprocess.run")
    def test_check_fetches_fork_prod_from_config(self, mock_run, _method, monkeypatch, capsys):
        monkeypatch.setattr(
            "hermes_cli.main._load_update_channel_defaults",
            lambda: ("fork", "prod"),
        )
        mock_run.side_effect = self._side_effect(remote="fork", branch="prod")
        args = SimpleNamespace(check=True, branch=None, remote=None)
        cmd_update(args)
        commands = [" ".join(str(a) for a in c.args[0]) for c in mock_run.call_args_list]
        assert any("fetch" in c and "fork" in c and "prod" in c for c in commands), commands
        assert not any("fetch" in c and "upstream" in c for c in commands), commands
        rev = [c for c in commands if "rev-list" in c]
        assert any("fork/prod" in c for c in rev), rev
        out = capsys.readouterr().out
        assert "fork/prod" in out

    @patch("hermes_cli.config.detect_install_method", return_value="git")
    @patch("subprocess.run")
    def test_check_default_main_still_prefers_upstream(self, mock_run, _method, monkeypatch, capsys):
        monkeypatch.setattr(
            "hermes_cli.main._load_update_channel_defaults",
            lambda: ("origin", "main"),
        )

        import subprocess

        def side_effect(cmd, **kwargs):
            joined = " ".join(str(c) for c in cmd)
            if "rev-parse" in joined and "--is-shallow-repository" in joined:
                return subprocess.CompletedProcess(cmd, 0, stdout="false\n", stderr="")
            if "fetch" in joined and "upstream" in joined:
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            if "fetch" in joined and "origin" in joined:
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            if "rev-parse" in joined and "--verify" in joined:
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            if "rev-list" in joined:
                return subprocess.CompletedProcess(cmd, 0, stdout="0\n", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        mock_run.side_effect = side_effect
        args = SimpleNamespace(check=True, branch=None, remote=None)
        cmd_update(args)
        commands = [" ".join(str(a) for a in c.args[0]) for c in mock_run.call_args_list]
        assert any("fetch" in c and "upstream" in c for c in commands), commands
        rev = [c for c in commands if "rev-list" in c]
        assert any("upstream/main" in c for c in rev), rev
