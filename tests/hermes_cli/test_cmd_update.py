"""Tests for cmd_update update-target resolution and branch switching."""

import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import hermes_cli.main as hermes_main
from hermes_cli.main import cmd_update


def _make_run_side_effect(branch="main", commit_count="0"):
    """Build a side_effect function for subprocess.run that simulates git commands."""

    def side_effect(cmd, **kwargs):
        joined = " ".join(str(c) for c in cmd)

        # git rev-parse --abbrev-ref HEAD  (get current branch)
        if "rev-parse" in joined and "--abbrev-ref" in joined:
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{branch}\n", stderr="")

        # git rev-list HEAD..FETCH_HEAD --count
        if "rev-list" in joined:
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{commit_count}\n", stderr="")

        # Fallback: return a successful CompletedProcess with empty stdout
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    return side_effect


@pytest.fixture
def mock_args():
    return SimpleNamespace()


class TestCmdUpdateBranchFallback:
    """cmd_update updates against the configured target branch."""

    @patch("shutil.which", return_value=None)
    @patch("subprocess.run")
    def test_update_uses_fetch_head_when_switching_from_feature_branch(
        self, mock_run, _mock_which, mock_args, capsys
    ):
        mock_run.side_effect = _make_run_side_effect(branch="fix/stoicneko", commit_count="3")

        with patch.object(hermes_main, "detect_update_target", return_value=("main", "origin", "main")), \
             patch.object(hermes_main, "write_update_channel"):
            cmd_update(mock_args)

        commands = [" ".join(str(a) for a in c.args[0]) for c in mock_run.call_args_list]

        # rev-list should compare against the fetched update target, not the current feature branch
        rev_list_cmds = [c for c in commands if "rev-list" in c]
        assert len(rev_list_cmds) == 1
        assert "HEAD..FETCH_HEAD" in rev_list_cmds[0]

        # pull should use main, not fix/stoicneko
        pull_cmds = [c for c in commands if "pull" in c]
        assert len(pull_cmds) == 1
        assert "main" in pull_cmds[0]

    @patch("shutil.which", return_value=None)
    @patch("subprocess.run")
    def test_update_uses_current_branch_when_on_remote(
        self, mock_run, _mock_which, mock_args, capsys
    ):
        mock_run.side_effect = _make_run_side_effect(branch="main", commit_count="2")

        with patch.object(hermes_main, "detect_update_target", return_value=("main", "origin", "main")), \
             patch.object(hermes_main, "write_update_channel"):
            cmd_update(mock_args)

        commands = [" ".join(str(a) for a in c.args[0]) for c in mock_run.call_args_list]

        rev_list_cmds = [c for c in commands if "rev-list" in c]
        assert len(rev_list_cmds) == 1
        assert "HEAD..FETCH_HEAD" in rev_list_cmds[0]

        pull_cmds = [c for c in commands if "pull" in c]
        assert len(pull_cmds) == 1
        assert "main" in pull_cmds[0]

    @patch("shutil.which", return_value=None)
    @patch("subprocess.run")
    def test_update_already_up_to_date(
        self, mock_run, _mock_which, mock_args, capsys
    ):
        mock_run.side_effect = _make_run_side_effect(branch="main", commit_count="0")

        with patch.object(hermes_main, "detect_update_target", return_value=("main", "origin", "main")), \
             patch.object(hermes_main, "write_update_channel"):
            cmd_update(mock_args)

        captured = capsys.readouterr()
        assert "Already up to date!" in captured.out

        # Should NOT have called pull
        commands = [" ".join(str(a) for a in c.args[0]) for c in mock_run.call_args_list]
        pull_cmds = [c for c in commands if "pull" in c]
        assert len(pull_cmds) == 0

    def test_update_non_interactive_skips_migration_prompt(self, mock_args, capsys):
        """When stdin/stdout aren't TTYs, config migration prompt is skipped."""
        with patch("shutil.which", return_value=None), patch(
            "subprocess.run"
        ) as mock_run, patch("builtins.input") as mock_input, patch(
            "hermes_cli.config.get_missing_env_vars", return_value=["MISSING_KEY"]
        ), patch("hermes_cli.config.get_missing_config_fields", return_value=[]), patch(
            "hermes_cli.config.check_config_version", return_value=(1, 2)
        ), patch("hermes_cli.main.sys") as mock_sys, patch.object(
            hermes_main, "detect_update_target", return_value=("main", "origin", "main")
        ), patch.object(
            hermes_main, "write_update_channel"
        ):
            mock_sys.stdin.isatty.return_value = False
            mock_sys.stdout.isatty.return_value = False
            mock_run.side_effect = _make_run_side_effect(branch="main", commit_count="1")

            cmd_update(mock_args)

            mock_input.assert_not_called()
            captured = capsys.readouterr()
            assert "Non-interactive session" in captured.out
