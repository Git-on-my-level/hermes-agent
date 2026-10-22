"""Config-driven update channel (updates.remote / updates.branch)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from hermes_cli.update_channel import (
    is_stock_upstream_probe,
    load_update_channel_defaults,
    resolve_update_branch,
    resolve_update_target,
)


class TestResolveUpdateTarget:
    def test_defaults_are_origin_main(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.update_channel.load_update_channel_defaults",
            lambda: ("origin", "main"),
        )
        assert resolve_update_target(SimpleNamespace(remote=None, branch=None)) == (
            "origin",
            "main",
        )
        assert resolve_update_branch(SimpleNamespace(remote=None, branch=None)) == "main"

    def test_config_defaults_apply_when_cli_omitted(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.update_channel.load_update_channel_defaults",
            lambda: ("fork", "prod"),
        )
        assert resolve_update_target(SimpleNamespace(remote=None, branch=None)) == (
            "fork",
            "prod",
        )

    def test_cli_overrides_config(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.update_channel.load_update_channel_defaults",
            lambda: ("fork", "prod"),
        )
        args = SimpleNamespace(remote="origin", branch="main")
        assert resolve_update_target(args) == ("origin", "main")

    def test_empty_cli_values_do_not_override(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.update_channel.load_update_channel_defaults",
            lambda: ("fork", "prod"),
        )
        args = SimpleNamespace(remote="  ", branch="")
        assert resolve_update_target(args) == ("fork", "prod")

    def test_load_defaults_from_config_dict(self, tmp_path):
        cfg = {"updates": {"remote": "fork", "branch": "prod"}}
        with patch("hermes_cli.config.load_config", return_value=cfg):
            assert load_update_channel_defaults() == ("fork", "prod")

    def test_stock_probe_only_for_origin_main(self):
        assert is_stock_upstream_probe("origin", "main")
        assert not is_stock_upstream_probe("fork", "prod")
        assert not is_stock_upstream_probe("origin", "prod")
