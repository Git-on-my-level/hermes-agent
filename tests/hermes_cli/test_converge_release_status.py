"""Release assertions must not equate a scheduler skip with runtime health."""
import json
from types import SimpleNamespace

import pytest

from hermes_cli import main, update_converge as converge
from tests.hermes_cli.test_update_target_identity import git, update_tree


@pytest.mark.parametrize("runtime,skip_restart,passed", [
    ("current", False, True), ("stale", False, False),
    ("stale", True, False), ("unknown", False, False),
])
def test_status_reports_checkout_and_running_code(update_tree, tmp_path, monkeypatch, capsys,
                                                 runtime, skip_restart, passed):
    from gateway import status
    t = update_tree
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / "config.yaml").write_text(
        f"updates:\n  converge: true\n  pin: {t.base}\n  skip_gateway_restart: {str(skip_restart).lower()}\n")
    live = t.base if runtime == "current" else t.wanted if runtime == "stale" else ""
    state = home / "gateway_state.json"
    state.write_text(json.dumps({"pid": 12345, "code_sha": live, "gateway_state": "running", "active_agents": 1}))
    monkeypatch.setattr(status, "_get_runtime_status_path", lambda: state)
    monkeypatch.setattr(status, "runtime_status_pid_is_live", lambda rec: True)
    monkeypatch.setattr(status, "get_running_pid", lambda: 12345)
    monkeypatch.setattr(main, "PROJECT_ROOT", t.clone)
    args = SimpleNamespace(converge_action="status", assert_current=True)
    if passed:
        converge.cmd_converge(args)
    else:
        with pytest.raises(SystemExit) as exc:
            converge.cmd_converge(args)
        assert exc.value.code == 1
    out = capsys.readouterr().out
    assert f"checkout_sha={t.base}" in out
    assert f"live_sha={live or 'unknown'}" in out
    assert f"target_sha={t.base}" in out
    assert f"match={'YES' if passed else 'NO'}" in out
    # Busy or restart opt-out still cannot pass the independent release assertion.
    args.assert_current = False
    converge.cmd_converge(args)


def test_channel_status_fetches_and_failed_fetch_is_not_current(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    settings = converge.ConvergeSettings(
        enabled=True, pin="", interval=1800, busy_sla=21600,
        skip_gateway_restart=False, remote="fork", branch="prod",
    )
    monkeypatch.setattr(converge, "load_converge_settings", lambda: settings)
    monkeypatch.setattr(converge, "checkout_sha", lambda root: "a" * 40)
    monkeypatch.setattr(converge, "live_code_sha", lambda: "a" * 40)
    monkeypatch.setattr(converge, "channel_tip_sha", lambda *args: "a" * 40)
    monkeypatch.setattr(converge, "checkout_is_dirty", lambda root: False)
    monkeypatch.setattr(converge, "gateway_is_busy", lambda: False)
    monkeypatch.setattr(converge, "pin_age_seconds", lambda *args: 0.0)
    fetches = []

    def failed_fetch(root, remote, branch):
        fetches.append((root, remote, branch))
        return False

    monkeypatch.setattr(converge, "fetch_channel_tip", failed_fetch)
    monkeypatch.setattr(main, "PROJECT_ROOT", tmp_path)
    args = SimpleNamespace(converge_action="status", assert_current=True)

    with pytest.raises(SystemExit) as exc:
        converge.cmd_converge(args)

    assert exc.value.code == 1
    assert fetches == [(tmp_path, "fork", "prod")]
    out = capsys.readouterr().out
    assert "match=NO" in out
    assert "reason=channel_fetch_failed" in out
    assert "already_current" not in out
