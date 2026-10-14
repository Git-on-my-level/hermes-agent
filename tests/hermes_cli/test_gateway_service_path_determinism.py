"""Deterministic service PATH for the launchd plist (and the systemd unit).

Regression for the fleet "cron PATH drift" class: the plist PATH used to be
``node dirs + os.environ["PATH"] of whatever process regenerated it``, so a
converge-driven regeneration under launchd's minimal PATH silently dropped
/opt/homebrew/bin and blinded the prod monitors for a week. The service PATH
must be a pure function of the host — never of the caller's environment.
"""
import logging
import plistlib
import sys
from pathlib import Path

import pytest


def _make_dir(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


@pytest.fixture
def gateway_cli():
    import hermes_cli.gateway as gateway_cli

    return gateway_cli


@pytest.fixture
def launchd(gateway_cli):
    import hermes_cli.gateway_launchd as launchd

    return launchd


@pytest.fixture(autouse=True)
def _isolated_host(monkeypatch, tmp_path):
    """Isolate every test from the real host: home resolves under tmp_path, hermes home points
    at tmp_path, and the caller PATH stays minimal (/usr/bin:/bin) — the converge-style env."""
    import hermes_cli.gateway as gateway_cli

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    home = tmp_path / ".hermes"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(gateway_cli, "get_hermes_home", lambda: home)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    yield


def _fake_these_dirs_exist(monkeypatch, paths):
    """Make Path.is_dir() report True for exactly *paths* (plus really-existing dirs)."""
    real_is_dir = Path.is_dir
    faked = {str(p) for p in paths}

    def fake_is_dir(self):
        if str(self) in faked:
            return True
        return real_is_dir(self)

    monkeypatch.setattr(Path, "is_dir", fake_is_dir)


class TestServiceToolDirs:
    def test_minimal_caller_path_still_yields_host_tool_dirs(self, gateway_cli, tmp_path, monkeypatch):
        """Core regression: building the service dirs with PATH=/usr/bin:/bin (launchd minimal
        env) must include Homebrew and ~/.local/bin whenever they exist on the host."""
        from hermes_cli.gateway import _service_tool_dirs

        _fake_these_dirs_exist(
            monkeypatch,
            [tmp_path / ".local" / "bin", Path("/opt/homebrew/bin"),
             Path("/opt/homebrew/sbin"), Path("/usr/local/bin")],
        )
        with monkeypatch.context() as m:
            m.setattr(sys, "platform", "darwin")
            dirs = _service_tool_dirs()
        assert dirs == [
            str(tmp_path / ".local" / "bin"),
            "/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin",
        ]

    def test_nonexistent_dirs_excluded(self, gateway_cli, monkeypatch):
        from hermes_cli.gateway import _service_tool_dirs

        with monkeypatch.context() as m:
            m.setattr(sys, "platform", "darwin")
            dirs = _service_tool_dirs()
        # Real host, real check: this tmp home has no ~/.local/bin and LinuxCI /opt paths are
        # only included when they exist.
        assert str(Path.home() / ".local" / "bin") not in dirs

    def test_linux_candidates(self, gateway_cli, monkeypatch):
        from hermes_cli.gateway import _service_tool_dirs

        _fake_these_dirs_exist(monkeypatch, [Path("/usr/local/bin")])
        with monkeypatch.context() as m:
            m.setattr(sys, "platform", "linux")
            dirs = _service_tool_dirs()
        assert dirs == ["/usr/local/bin"]

    def test_system_dirs_trail(self, gateway_cli, monkeypatch):
        from hermes_cli.gateway import _service_system_dirs

        with monkeypatch.context() as m:
            m.setattr(sys, "platform", "darwin")
            assert _service_system_dirs() == ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
        with monkeypatch.context() as m:
            m.setattr(sys, "platform", "linux")
            assert _service_system_dirs() == [
                "/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin",
            ]


class TestBuildServicePathDirs:
    def test_includes_node_bins_and_ignores_caller_path(self, gateway_cli, tmp_path, monkeypatch):
        nm_bin = _make_dir(tmp_path / "node_modules" / ".bin")
        home = Path.home() / ".hermes"
        node_bin = _make_dir(home / "node" / "bin")

        dirs = gateway_cli._build_service_path_dirs(project_root=tmp_path)
        assert nm_bin in dirs and node_bin in dirs

        # The caller's PATH must not leak in.
        monkeypatch.setenv("PATH", f"/usr/bin:/bin:{nm_bin}:/some/caller/dir")
        assert gateway_cli._build_service_path_dirs(project_root=tmp_path) == dirs

    def test_deterministic_regardless_of_caller_path(self, gateway_cli, tmp_path, monkeypatch):
        dirs_min = gateway_cli._build_service_path_dirs(project_root=tmp_path)
        monkeypatch.setenv("PATH", "/usr/bin:/bin:/opt/homebrew/bin:/some/other/dir")
        assert gateway_cli._build_service_path_dirs(project_root=tmp_path) == dirs_min

    def test_config_extras_appended_after_tool_dirs(self, gateway_cli, tmp_path, monkeypatch):
        extra = _make_dir(tmp_path / "tools" / "bin")
        monkeypatch.setattr(gateway_cli, "_service_path_extra_dirs", lambda: [extra])
        dirs = gateway_cli._build_service_path_dirs(project_root=tmp_path)
        assert dirs.index(extra) >= 0
        if len(dirs) > 1:
            assert dirs[-1] == extra or dirs[-1] in (str(tmp_path / "node_modules" / ".bin"),)


class TestServicePathExtraConfig:
    def test_reads_gateway_service_path_extra(self, gateway_cli, tmp_path, monkeypatch):
        import hermes_cli.config as config_mod

        monkeypatch.setattr(
            config_mod, "load_config_readonly",
            lambda: {"gateway": {"service_path_extra": ["/opt/tools/bin", str(tmp_path / "bin")]}},
        )
        dirs = gateway_cli._service_path_extra_dirs()
        assert dirs == ["/opt/tools/bin", str(tmp_path / "bin")]

    def test_tilde_expansion(self, gateway_cli, tmp_path, monkeypatch):
        import hermes_cli.config as config_mod

        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(
            config_mod, "load_config_readonly",
            lambda: {"gateway": {"service_path_extra": ["~/bin"]}},
        )
        assert gateway_cli._service_path_extra_dirs() == [str(tmp_path / "bin")]

    def test_string_value_treated_as_single_entry(self, gateway_cli, monkeypatch):
        import hermes_cli.config as config_mod

        monkeypatch.setattr(
            config_mod, "load_config_readonly",
            lambda: {"gateway": {"service_path_extra": "/opt/tools/bin"}},
        )
        assert gateway_cli._service_path_extra_dirs() == ["/opt/tools/bin"]

    def test_malformed_values_fail_open(self, gateway_cli, monkeypatch):
        import hermes_cli.config as config_mod

        monkeypatch.setattr(
            config_mod, "load_config_readonly", lambda: {"gateway": {"service_path_extra": 42}},
        )
        assert gateway_cli._service_path_extra_dirs() == []
        monkeypatch.setattr(config_mod, "load_config_readonly", lambda: None)
        assert gateway_cli._service_path_extra_dirs() == []


class TestLaunchdServicePath:
    def test_minimal_env_plist_keeps_homebrew_and_local_bin(
        self, gateway_cli, launchd, tmp_path, monkeypatch,
    ):
        """Generate the plist with PATH=/usr/bin:/bin: Homebrew and ~/.local/bin must be
        present when they exist on the host (faked dirs), and no caller-only entry may leak."""
        brew_bin = _make_dir(tmp_path / "hb" / "bin")
        local_bin = _make_dir(tmp_path / ".local" / "bin")
        monkeypatch.setattr(
            gateway_cli, "_build_service_path_dirs", lambda project_root=None: [brew_bin, local_bin],
        )
        monkeypatch.setattr(gateway_cli, "get_launchd_plist_path", lambda: tmp_path / "absent.plist")

        plist = gateway_cli.generate_launchd_plist()
        parsed = plistlib.loads(plist.encode("utf-8"))
        service_path = parsed["EnvironmentVariables"]["PATH"].split(":")
        assert brew_bin in service_path
        assert local_bin in service_path
        # System dirs trail exactly once; caller entries arrive only through that segment.
        assert service_path[-4:] == ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
        assert service_path.count("/usr/bin") == 1
        # caller-only dirs (existing or not) are never baked in.
        monkeypatch.setenv("PATH", f"/usr/bin:/bin:{_make_dir(tmp_path / 'caller-only')}")
        plist2 = gateway_cli.generate_launchd_plist()
        assert plist2 == plist

    def test_regeneration_from_minimal_env_does_not_drop_installed_entries(
        self, gateway_cli, launchd, tmp_path, monkeypatch,
    ):
        """A minimal-env regeneration must not shrink the installed service PATH: entries of
        the installed plist that still exist are carried forward."""
        installed = tmp_path / "ai.hermes.gateway.plist"
        brew_bin = _make_dir(tmp_path / "hb" / "bin")
        custom = _make_dir(tmp_path / "custom-tool-dir")
        installed.write_bytes(plistlib.dumps({
            "Label": "ai.hermes.gateway",
            "EnvironmentVariables": {"PATH": f"{brew_bin}:{custom}:/usr/bin:/bin"},
        }))

        # Converge-style caller: minimal PATH, tool-dir builder knows only brew.
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        sane = launchd._launchd_service_path([brew_bin], installed).split(":")
        assert custom in sane
        assert sane.index(brew_bin) < sane.index(custom) < sane.index("/usr/bin")

    def test_reset_service_path_drops_carry_forward(
        self, gateway_cli, launchd, tmp_path, monkeypatch,
    ):
        installed = tmp_path / "ai.hermes.gateway.plist"
        brew_bin = _make_dir(tmp_path / "hb" / "bin")
        custom = _make_dir(tmp_path / "custom-tool-dir")
        installed.write_bytes(plistlib.dumps({
            "EnvironmentVariables": {"PATH": f"{brew_bin}:{custom}:/usr/bin:/bin"},
        }))
        monkeypatch.setattr(gateway_cli, "_reset_service_path", True, raising=False)

        sane = launchd._launchd_service_path([brew_bin], installed).split(":")
        assert custom not in sane
        assert sane.index(brew_bin) < sane.index("/usr/bin")

    def test_carry_forward_skips_dead_dirs(self, gateway_cli, launchd, tmp_path, monkeypatch, caplog):
        installed = tmp_path / "ai.hermes.gateway.plist"
        dead = str(tmp_path / "gone-dir")
        installed.write_bytes(plistlib.dumps({
            "EnvironmentVariables": {"PATH": f"{dead}:/usr/bin:/bin"},
        }))
        with caplog.at_level(logging.WARNING, logger="hermes_cli.gateway_launchd"):
            sane = launchd._launchd_service_path([], installed).split(":")
        assert dead not in sane
        assert any("no longer present on disk" in r.getMessage() for r in caplog.records)

    def test_carry_forward_warns_when_caller_lacks_entries(
        self, gateway_cli, launchd, tmp_path, monkeypatch, caplog,
    ):
        installed = tmp_path / "ai.hermes.gateway.plist"
        custom = _make_dir(tmp_path / "custom-tool-dir")
        installed.write_bytes(plistlib.dumps({
            "EnvironmentVariables": {"PATH": f"{custom}:/usr/bin:/bin"},
        }))
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        with caplog.at_level(logging.WARNING, logger="hermes_cli.gateway_launchd"):
            sane = launchd._launchd_service_path([], installed).split(":")
        assert custom in sane
        assert any("carried forward" in r.getMessage() for r in caplog.records)

    def test_no_installed_plist_still_deterministic(self, gateway_cli, launchd, tmp_path, monkeypatch):
        absent = tmp_path / "absent.plist"
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        a = launchd._launchd_service_path([], absent)
        monkeypatch.setenv("PATH", "/usr/bin:/bin:/opt/homebrew/bin")
        b = launchd._launchd_service_path([], absent)
        assert a == b


class TestLaunchdPlistIsCurrent:
    def test_same_answer_under_minimal_and_full_caller_env(
        self, gateway_cli, launchd, tmp_path, monkeypatch,
    ):
        """launchd_plist_is_current() must answer identically whether the caller is an SSH
        shell with Homebrew on PATH or the converge job with launchd's minimal PATH, and a
        minimal-env regeneration must be byte-identical to the installed plist."""
        plist_path = tmp_path / "ai.hermes.gateway.plist"
        monkeypatch.setattr(gateway_cli, "get_launchd_plist_path", lambda: plist_path)

        monkeypatch.setenv("PATH", "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin")
        installed_text = gateway_cli.generate_launchd_plist()
        plist_path.write_text(installed_text, encoding="utf-8")

        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        assert gateway_cli.launchd_plist_is_current() is True
        assert gateway_cli.generate_launchd_plist() == installed_text

        monkeypatch.setenv("PATH", "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin")
        assert gateway_cli.launchd_plist_is_current() is True

    def test_detects_drifted_installed_path(self, gateway_cli, launchd, tmp_path, monkeypatch):
        """An installed plist whose PATH lost Homebrew (pre-fix install) is detected as stale
        so the refresh path repairs it — the old PATH-blind comparison hid exactly this."""
        plist_path = tmp_path / "ai.hermes.gateway.plist"
        brew_bin = _make_dir(tmp_path / "hb" / "bin")
        monkeypatch.setattr(gateway_cli, "get_launchd_plist_path", lambda: plist_path)
        monkeypatch.setattr(gateway_cli, "_build_service_path_dirs", lambda project_root=None: [brew_bin])

        good = plistlib.loads(gateway_cli.generate_launchd_plist().encode("utf-8"))
        drifted = dict(good)
        drifted["EnvironmentVariables"] = dict(good["EnvironmentVariables"])
        drifted["EnvironmentVariables"]["PATH"] = "/usr/bin:/bin"
        plist_path.write_bytes(plistlib.dumps(drifted))

        assert gateway_cli.launchd_plist_is_current() is False


class TestSystemdUnitPath:
    def test_unit_path_starts_with_tool_dirs_and_ignores_caller_path(
        self, gateway_cli, tmp_path, monkeypatch,
    ):
        """The systemd unit never carried the caller PATH but also missed Homebrew; the
        deterministic head must now include it (faked) without letting the caller PATH in."""
        brew_bin = "/opt/homebrew/bin"
        _fake_these_dirs_exist(
            monkeypatch,
            [tmp_path / ".local" / "bin", Path(brew_bin),
             Path("/opt/homebrew/sbin"), Path("/usr/local/bin")],
        )
        caller_only = _make_dir(tmp_path / "caller-only")
        monkeypatch.setenv("PATH", f"/usr/bin:/bin:{caller_only}")

        unit = gateway_cli.generate_systemd_unit(system=False)
        env_line = next(l for l in unit.splitlines() if l.startswith('Environment="PATH='))
        raw = env_line[len('Environment="PATH='):].rstrip('"')
        entries = raw.split(":")
        assert entries[0] == str(tmp_path / ".local" / "bin")  # user bin first, then package managers
        assert brew_bin in entries
        assert caller_only not in entries
        # macOS system tail; /usr/local/* now live in the deterministic head (above).
        assert entries[-4:] == ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]

    def test_unit_is_current_under_minimal_and_full_env(self, gateway_cli, tmp_path, monkeypatch):
        unit_path = tmp_path / "hermes-gateway.service"
        monkeypatch.setattr(gateway_cli, "get_systemd_unit_path", lambda system=False: unit_path)

        monkeypatch.setenv("PATH", "/usr/bin:/bin:/opt/homebrew/bin")
        unit_path.write_text(gateway_cli.generate_systemd_unit(system=False), encoding="utf-8")

        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        assert gateway_cli.systemd_unit_is_current(system=False) is True
        monkeypatch.setenv("PATH", "/usr/bin:/bin:/opt/homebrew/bin")
        assert gateway_cli.systemd_unit_is_current(system=False) is True


class TestNormalizeLaunchdPlist:
    def test_normalize_keeps_path_payload(self, gateway_cli):
        text = "<key>PATH</key>\n    <string>/usr/bin:/bin</string>"
        norm = gateway_cli._normalize_launchd_plist_for_comparison(text)
        assert "/usr/bin:/bin" in norm
