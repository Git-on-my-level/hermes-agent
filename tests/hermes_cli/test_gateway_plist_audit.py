import plistlib

from hermes_cli import gateway_plist_audit


def test_audit_plist_updates_hardening_fields(tmp_path):
    home_dir = tmp_path / "home"
    launch_agents_dir = home_dir / "Library" / "LaunchAgents"
    launch_agents_dir.mkdir(parents=True)
    repo_root = tmp_path / "repo"
    (repo_root / "venv" / "bin").mkdir(parents=True)
    plist_path = launch_agents_dir / "ai.hermes.gateway-test.plist"

    original = {
        "Label": "ai.hermes.gateway-test",
        "KeepAlive": {"SuccessfulExit": False},
        "EnvironmentVariables": {"PATH": "/usr/bin:/bin"},
    }
    plist_path.write_bytes(plistlib.dumps(original, fmt=plistlib.FMT_XML, sort_keys=False))

    dry_run_result = gateway_plist_audit.audit_plist(
        plist_path,
        apply_changes=False,
        home_dir=home_dir,
        repo_root=repo_root,
        env_path="/opt/homebrew/bin:/usr/bin:/bin",
    )
    assert dry_run_result.changed is True
    assert dry_run_result.applied is False
    unchanged = plistlib.loads(plist_path.read_bytes())
    assert unchanged["KeepAlive"] == {"SuccessfulExit": False}
    assert "ThrottleInterval" not in unchanged

    apply_result = gateway_plist_audit.audit_plist(
        plist_path,
        apply_changes=True,
        home_dir=home_dir,
        repo_root=repo_root,
        env_path="/opt/homebrew/bin:/usr/bin:/bin",
    )
    assert apply_result.changed is True
    assert apply_result.applied is True

    updated = plistlib.loads(plist_path.read_bytes())
    assert updated["KeepAlive"] is True
    assert updated["ThrottleInterval"] == gateway_plist_audit.DEFAULT_THROTTLE_INTERVAL
    path_entries = updated["EnvironmentVariables"]["PATH"].split(":")
    assert path_entries[0] == str(repo_root / "venv" / "bin")
    assert path_entries[1] == str(home_dir / ".local" / "bin")
    assert "/usr/bin" in path_entries
    assert "/bin" in path_entries


def test_audit_plist_reports_no_change_when_already_hardened(tmp_path):
    home_dir = tmp_path / "home"
    launch_agents_dir = home_dir / "Library" / "LaunchAgents"
    launch_agents_dir.mkdir(parents=True)
    repo_root = tmp_path / "repo"
    (repo_root / "venv" / "bin").mkdir(parents=True)
    plist_path = launch_agents_dir / "ai.hermes.gateway.plist"
    hardened_path = ":".join(
        [
            str(repo_root / "venv" / "bin"),
            str(home_dir / ".local" / "bin"),
            "/usr/bin",
            "/bin",
        ]
    )

    plist_path.write_bytes(
        plistlib.dumps(
            {
                "Label": "ai.hermes.gateway",
                "KeepAlive": True,
                "ThrottleInterval": gateway_plist_audit.DEFAULT_THROTTLE_INTERVAL,
                "EnvironmentVariables": {"PATH": hardened_path},
            },
            fmt=plistlib.FMT_XML,
            sort_keys=False,
        )
    )

    result = gateway_plist_audit.audit_plist(
        plist_path,
        apply_changes=False,
        home_dir=home_dir,
        repo_root=repo_root,
    )

    assert result.changed is False
    assert result.applied is False
    reloaded = plistlib.loads(plist_path.read_bytes())
    assert reloaded["KeepAlive"] is True
    assert reloaded["ThrottleInterval"] == gateway_plist_audit.DEFAULT_THROTTLE_INTERVAL


def test_find_gateway_plists_only_returns_matching_files(tmp_path):
    launch_agents_dir = tmp_path / "LaunchAgents"
    launch_agents_dir.mkdir()
    expected = [
        launch_agents_dir / "ai.hermes.gateway.plist",
        launch_agents_dir / "ai.hermes.gateway-work.plist",
    ]
    for path in expected:
        path.write_text("", encoding="utf-8")
    (launch_agents_dir / "com.example.other.plist").write_text("", encoding="utf-8")

    assert gateway_plist_audit.find_gateway_plists(launch_agents_dir) == expected
