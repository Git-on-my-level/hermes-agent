"""A serving snapshot uses its mutable checkout's updater, with the existing IPC."""
import json
import os
import shlex
import subprocess
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
    from gateway import run
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
    monkeypatch.setattr("shutil.which", lambda name, path=None: path_command if name == "hermes" else None)
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


# ---------------------------------------------------------------------------
# The Windows helper's real launch behavior.
#
# The parametrized test above mocks ``subprocess.Popen``, so it cannot see that
# CreateProcessW refuses to start a batch file (WinError 193). These tests run
# ``_WINDOWS_UPDATE_HELPER`` for real in a child interpreter and assert what it
# actually launched and what it wrote to the IPC files.
# ---------------------------------------------------------------------------


def _run_update_helper(tmp_path, command, *, comspec=None):
    """Run the real helper and return ``(stdout_text, exit_code_text)``."""
    from gateway.slash_commands import _WINDOWS_UPDATE_HELPER

    output = tmp_path / "update-output.txt"
    exit_code = tmp_path / "update-exit-code"
    env = dict(os.environ)
    if comspec is not None:
        env["COMSPEC"] = str(comspec)
    subprocess.run(
        [sys.executable, "-c", _WINDOWS_UPDATE_HELPER,
         str(output), str(exit_code), *command],
        env=env, check=True, timeout=120)
    return (output.read_text(encoding="utf-8-sig"),
            exit_code.read_text(encoding="utf-8-sig"))


def _batch_launcher(tmp_path, *, exit_code=0):
    """A ``hermes.cmd`` launcher plus the COMSPEC that can start it.

    On Windows both are real. Elsewhere the batch file is a plain shell script
    with no shebang and no exec bit — exactly as unstartable directly as a
    ``.cmd`` is to CreateProcessW — and COMSPEC is a shim that drops the ``/c``
    switch and interprets it, so the helper's routing decision is exercised.
    """
    launcher = tmp_path / "hermes.cmd"
    if os.name == "nt":
        launcher.write_text(
            f"@echo off\r\necho launched %*\r\nexit /b {exit_code}\r\n", encoding="utf-8")
        return launcher, None
    launcher.write_text(f'echo launched "$@"\nexit {exit_code}\n', encoding="utf-8")
    launcher.chmod(0o644)
    comspec = tmp_path / "cmd.exe"
    comspec.write_text('#!/bin/sh\nshift\nexec /bin/sh "$@"\n', encoding="utf-8")
    comspec.chmod(0o755)
    return launcher, comspec


@pytest.mark.live_system_guard_bypass  # the batch file is this test's own throwaway child
def test_windows_helper_launches_cmd_launcher_through_comspec(tmp_path):
    """A ``.cmd``-only install must still run ``update --gateway``.

    ``_launchers.py`` can retire ``hermes.exe`` and leave ``hermes.cmd``; the
    helper's bare ``Popen`` then failed with WinError 193 before the updater ran.
    """
    launcher, comspec = _batch_launcher(tmp_path)
    out, code = _run_update_helper(
        tmp_path, [str(launcher), "update", "--gateway"], comspec=comspec)
    assert "launched update --gateway" in out
    assert code == "0"


@pytest.mark.live_system_guard_bypass  # the batch file is this test's own throwaway child
def test_windows_helper_reports_batch_exit_code(tmp_path):
    """The gateway watcher polls the exit-code file, so a batch failure must land there."""
    launcher, comspec = _batch_launcher(tmp_path, exit_code=3)
    _, code = _run_update_helper(
        tmp_path, [str(launcher), "update", "--gateway"], comspec=comspec)
    assert code == "3"


def test_windows_helper_launches_executable_directly(tmp_path):
    """A non-batch command keeps the direct spawn; COMSPEC must not be consulted."""
    comspec = tmp_path / "cmd.exe"
    comspec.write_text("#!/bin/sh\nexit 97\n", encoding="utf-8")
    comspec.chmod(0o755)
    out, code = _run_update_helper(
        tmp_path, [sys.executable, "-c", "print('direct')"], comspec=comspec)
    assert "direct" in out
    assert code == "0"


@pytest.mark.platforms("windows")
def test_snapshot_update_prefers_cmd_launcher_when_exe_retired(tmp_path):
    """Windows publication can leave only ``hermes.cmd``; the resolver must return it."""
    from gateway.update_source import snapshot_update_command

    checkout = tmp_path / "checkout"
    state, source, _ = _snapshot(tmp_path, checkout)
    (checkout / ".hermes" / "bin" / "hermes.exe").unlink()
    cmd_launcher = checkout / ".hermes" / "bin" / "hermes.cmd"
    cmd_launcher.write_text("@echo off\r\n", encoding="utf-8")
    pointer = state / "inputs" / ".project-root"
    pointer.parent.mkdir()
    pointer.write_text(str(checkout), encoding="utf-8")

    assert snapshot_update_command(source) == [str(cmd_launcher)]
