"""A serving snapshot uses its mutable checkout's updater, with the existing IPC."""
import json
import os
import shlex
import sys
from unittest.mock import Mock

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource
from pm.environments import install_key


def _snapshot(tmp_path, checkout):
    state = tmp_path / "data" / "installs" / install_key(checkout)
    source = state / "serving-source" / ("a" * 40)
    for directory in ("hermes_cli", "skills", "locales", "scripts"):
        (source / directory).mkdir(parents=True)
    (source / "hermes_cli" / "main.py").touch()
    (source / "install-stamp.json").write_text(json.dumps({"commit": source.name, "dirty": False}))
    (checkout / ".git").mkdir(parents=True)
    (checkout / "hermes_cli").mkdir()
    (checkout / "hermes_cli" / "main.py").touch()
    launcher = checkout / ".hermes" / "bin" / ("hermes.exe" if os.name == "nt" else "hermes")
    launcher.parent.mkdir(parents=True)
    launcher.touch()
    return state, source, launcher


@pytest.mark.asyncio
@pytest.mark.parametrize("proof", ["pointer", "legacy", "legacy_launcher", "wrong_key", "no_git", "no_launcher", "not_snapshot", "stock"])
async def test_update_uses_only_proven_checkout(tmp_path, monkeypatch, proof):
    import gateway.run as run
    import gateway.slash_commands as slash
    from gateway.slash_commands import t

    checkout = tmp_path / "custom checkout"
    if proof == "legacy":
        checkout = tmp_path / "data" / "arbitrary-name"
    state, source, launcher = _snapshot(tmp_path, checkout)
    if proof not in {"legacy", "legacy_launcher"}:
        pointer = state / "inputs" / ".project-root"
        pointer.parent.mkdir()
        pointer.write_text(str(checkout if proof != "wrong_key" else tmp_path))
    if proof == "no_git":
        (checkout / ".git").rmdir()
    if proof == "no_launcher":
        launcher.unlink()
    if proof == "not_snapshot":
        (source / "install-stamp.json").unlink()
    if proof == "stock":
        source = checkout
    if proof == "legacy_launcher":
        wrapper = tmp_path / "hermes"
        if os.name == "nt":
            # Windows conveniences are links to the install-local command.
            wrapper.symlink_to(launcher)
        else:
            wrapper.write_text(f'#!/bin/sh\nexec {shlex.quote(str(launcher))} "$@"\n')
        path_command = str(wrapper)
    else:
        path_command = None
    monkeypatch.setattr("shutil.which", lambda name: path_command if name == "hermes" else None)
    monkeypatch.setattr(slash, "__file__", str(source / "gateway" / "slash_commands.py"))
    monkeypatch.setattr(run, "_hermes_home", tmp_path)
    monkeypatch.setenv("HERMES_INSTALL_ROOT", str(source))
    monkeypatch.setattr("hermes_cli.config.is_managed", lambda: False)
    monkeypatch.setattr("hermes_cli.config.detect_install_method", lambda root: "git")
    stock_command = [sys.executable, "-m", "hermes_cli.main"]
    resolve = Mock(return_value=stock_command)
    monkeypatch.setattr(run, "_resolve_hermes_bin", resolve)
    popen = Mock()
    monkeypatch.setattr("subprocess.Popen", popen)
    runner = object.__new__(run.GatewayRunner)
    runner._session_key_for_source = lambda src: "test-session"
    runner._schedule_update_notification_watch = Mock()
    event = MessageEvent(text="/update", source=SessionSource(
        platform=Platform.TELEGRAM, chat_id="123", user_id="456"))

    result = await runner._handle_update_command(event)

    if proof in {"wrong_key", "no_git", "no_launcher", "not_snapshot"}:
        assert result == t("gateway.update.not_git_repo")
        popen.assert_not_called()
        runner._schedule_update_notification_watch.assert_not_called()
    else:
        assert result == t("gateway.update.starting")
        if proof == "stock":
            assert "env" not in popen.call_args.kwargs
        else:
            assert "HERMES_INSTALL_ROOT" not in popen.call_args.kwargs["env"]
        argv = popen.call_args.args[0]
        expected = stock_command if proof == "stock" else [str(launcher)]
        if os.name == "nt":
            assert argv[-len(expected)-2:] == [*expected, "update", "--gateway"]
        else:
            words = shlex.split(argv[-1])
            assert words[1:len(expected)+3] == [*expected, "update", "--gateway"]
        assert json.loads((tmp_path / ".update_pending.json").read_text())["chat_id"] == "123"
        runner._schedule_update_notification_watch.assert_called_once()
    if proof == "stock":
        resolve.assert_called_once_with()
    else:
        resolve.assert_not_called()
