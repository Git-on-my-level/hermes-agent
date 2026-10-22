"""Deterministic service PATH for the launchd plist (and the systemd unit).

Regression for the fleet "cron PATH drift" class: the plist PATH used to be
``node dirs + os.environ["PATH"] of whatever process regenerated it``, so a
converge-driven regeneration under launchd's minimal PATH silently dropped
/opt/homebrew/bin and blinded the prod monitors for a week. The service PATH
must be a pure function of the host — never of the caller's environment.
"""
import logging
import os
import plistlib
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


def _fake_only_these_dirs_exist(monkeypatch, paths):
    """Make Path.is_dir() report True for EXACTLY *paths* — the host's real dirs are invisible,
    so exact candidate lists are host-independent."""
    faked = {str(p) for p in paths}

    def fake_is_dir(self):
        return str(self) in faked

    monkeypatch.setattr(Path, "is_dir", fake_is_dir)


class TestServiceToolDirs:
    @pytest.mark.platforms("macos")
    def test_minimal_caller_path_still_yields_host_tool_dirs(self, gateway_cli, tmp_path, monkeypatch):
        """Core regression: building the service dirs with PATH=/usr/bin:/bin (launchd minimal
        env) must include Homebrew and ~/.local/bin whenever they exist on the host. Directory
        existence is fully faked so the exact list is host-independent."""
        from hermes_cli.gateway import _service_tool_dirs

        _fake_only_these_dirs_exist(
            monkeypatch,
            [tmp_path / ".local" / "bin", Path("/opt/homebrew/bin"),
             Path("/opt/homebrew/sbin"), Path("/usr/local/bin")],
        )
        dirs = _service_tool_dirs()
        assert dirs == [
            str(tmp_path / ".local" / "bin"),
            "/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin",
        ]

    @pytest.mark.platforms("macos")
    def test_nonexistent_dirs_excluded(self, gateway_cli, monkeypatch):
        from hermes_cli.gateway import _service_tool_dirs

        # Nothing fake-exists: every candidate dir is excluded.
        _fake_only_these_dirs_exist(monkeypatch, [])
        assert _service_tool_dirs() == []

    @pytest.mark.platforms("linux")
    def test_linux_candidates(self, gateway_cli, monkeypatch):
        from hermes_cli.gateway import _service_tool_dirs

        _fake_only_these_dirs_exist(monkeypatch, [Path("/usr/local/bin")])
        assert _service_tool_dirs() == ["/usr/local/bin"]

    @pytest.mark.platforms("macos")
    def test_system_dirs_trail_macos(self, gateway_cli):
        from hermes_cli.gateway import _service_system_dirs

        assert _service_system_dirs() == ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]

    @pytest.mark.platforms("linux")
    def test_system_dirs_trail_linux(self, gateway_cli):
        from hermes_cli.gateway import _service_system_dirs

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
        monkeypatch.setattr(gateway_cli, "_service_path_extra_dirs", lambda home=None: [extra])
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

    def test_key_registered_in_config_defaults(self, gateway_cli, tmp_path, monkeypatch):
        """gateway.service_path_extra must be a REGISTERED key: the real config loader
        (DEFAULT_CONFIG defaults) recognizes it, so hermes config tooling does not warn
        'unrecognized config key' for a supported setting."""
        from hermes_cli.config import load_config_readonly
        from hermes_cli.config_defaults import DEFAULT_CONFIG

        assert "service_path_extra" in DEFAULT_CONFIG["gateway"]
        (tmp_path / ".hermes" / "config.yaml").write_text(
            "gateway:\n  service_path_extra:\n    - /opt/from-config/bin\n",
            encoding="utf-8",
        )
        config = load_config_readonly()
        assert config["gateway"]["service_path_extra"] == ["/opt/from-config/bin"]


@pytest.mark.platforms("macos")
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

    def test_installed_system_dirs_move_to_canonical_tail(
        self, gateway_cli, launchd, tmp_path, monkeypatch,
    ):
        """A pre-fix installed PATH like /usr/bin:/bin:<custom> must regenerate with <custom>
        BEFORE the canonical system tail — system dirs are never carried into mid-PATH
        positions, and the result is stable under a second regeneration."""
        installed = tmp_path / "ai.hermes.gateway.plist"
        custom = _make_dir(tmp_path / "custom-tool-dir")
        installed.write_bytes(plistlib.dumps({
            "EnvironmentVariables": {"PATH": f"/usr/bin:/bin:{custom}"},
        }))
        monkeypatch.setenv("PATH", "/usr/bin:/bin")

        sane = launchd._launchd_service_path([], installed).split(":")
        assert sane.index(custom) < sane.index("/usr/bin")
        assert sane[-4:] == ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
        assert sane.count("/usr/bin") == 1

    def test_config_extra_system_dir_bubbles_to_tail(
        self, gateway_cli, launchd, tmp_path, monkeypatch,
    ):
        """An operator listing a canonical system dir in service_path_extra must not pin it
        into a mid-PATH position: the tail stays canonical and last."""
        custom = _make_dir(tmp_path / "custom-tool-dir")
        sane = launchd._launchd_service_path(["/usr/bin", custom], tmp_path / "absent.plist").split(":")
        assert sane.index(custom) < sane.index("/usr/bin")
        assert sane[-4:] == ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
        assert sane.count("/usr/bin") == 1

    def test_equivalent_system_dir_spellings_bubble_to_tail(
        self, gateway_cli, launchd, tmp_path, monkeypatch,
    ):
        """/usr/bin/ (trailing slash) is the same dir as /usr/bin: it must be stripped from
        the head and the canonical spelling appended in the tail exactly once."""
        custom = _make_dir(tmp_path / "custom-tool-dir")
        sane = launchd._launchd_service_path(
            ["/usr/bin/", str(custom)], tmp_path / "absent.plist",
        ).split(":")
        assert "/usr/bin/" not in sane
        assert sane.index(custom) < sane.index("/usr/bin")
        assert sane.count("/usr/bin") == 1
        assert sane[-4:] == ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]

    def test_double_leading_slash_spellings_bubble_to_tail(
        self, gateway_cli, launchd, tmp_path, monkeypatch,
    ):
        """POSIX treats a double leading slash as implementation-defined but path lookup
        resolves it to the ordinary root-relative dir: '//usr/bin/' must deduplicate against
        the canonical '/usr/bin' — in the head AND when carried forward from an install."""
        custom = _make_dir(tmp_path / "custom-tool-dir")
        sane = launchd._launchd_service_path(
            ["//usr/bin/", str(custom)], tmp_path / "absent.plist",
        ).split(":")
        assert not any(e.startswith("//") for e in sane)
        assert sane.index(custom) < sane.index("/usr/bin")
        assert sane.count("/usr/bin") == 1

        installed = tmp_path / "ai.hermes.gateway.plist"
        installed.write_bytes(plistlib.dumps({
            "EnvironmentVariables": {"PATH": f"//usr/bin/:{custom}"},
        }))
        sane2 = launchd._launchd_service_path([], installed).split(":")
        assert not any(e.startswith("//") for e in sane2)
        assert sane2.index(str(custom)) < sane2.index("/usr/bin")
        assert sane2.count("/usr/bin") == 1

    def test_relative_config_extra_anchors_and_stabilizes(
        self, gateway_cli, launchd, tmp_path, monkeypatch,
    ):
        """A relative service_path_extra entry is anchored to the service working dir at
        generation time, so the installed absolute spelling round-trips WITHOUT duplicating
        (regeneration from the installed PATH yields the identical PATH)."""
        import plistlib as _pl
        import hermes_cli.config as config_mod

        workdir = tmp_path / "service-home"
        workdir.mkdir()
        (workdir / "bin").mkdir()
        monkeypatch.setattr(gateway_cli, "_stable_service_working_dir", lambda: str(workdir))
        monkeypatch.setattr(
            config_mod, "load_config_readonly",
            lambda: {"gateway": {"service_path_extra": ["bin"]}},
        )

        monkeypatch.setattr(gateway_cli, "get_launchd_plist_path", lambda: tmp_path / "absent.plist")
        first = gateway_cli.generate_launchd_plist()
        first_path = _pl.loads(first.encode("utf-8"))["EnvironmentVariables"]["PATH"]
        assert str(workdir / "bin") in first_path.split(":")
        assert "bin" != first_path.split(":")[0]

        # Install it, then regenerate THROUGH the real carry-forward path: the getter must
        # point at the installed plist so its PATH is read back and carried forward.
        installed = tmp_path / "ai.hermes.gateway.plist"
        installed.write_text(first, encoding="utf-8")
        monkeypatch.setattr(gateway_cli, "get_launchd_plist_path", lambda: installed)
        assert gateway_cli.launchd_plist_is_current() is True
        second = gateway_cli.generate_launchd_plist()
        assert second == first

    def test_relative_carry_forward_anchors_to_service_workdir(
        self, gateway_cli, launchd, tmp_path, monkeypatch,
    ):
        """A relative installed entry must resolve against the service's stable working
        directory, never against the regenerating process's cwd (caller-dependence)."""
        import os as _os

        installed = tmp_path / "ai.hermes.gateway.plist"
        installed.write_bytes(plistlib.dumps({
            "EnvironmentVariables": {"PATH": "bin:/usr/bin:/bin"},
        }))
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.setattr(gateway_cli, "_stable_service_working_dir", lambda: str(tmp_path))

        cwd_a = tmp_path / "caller-a"
        cwd_a.mkdir()

        for make_workdir_bin in (True, False):
            workdir_bin = tmp_path / "bin"
            if make_workdir_bin:
                workdir_bin.mkdir()
            try:
                sane = launchd._launchd_service_path([], installed).split(":")
            finally:
                if make_workdir_bin:
                    workdir_bin.rmdir()
            expected = str(tmp_path / "bin")
            if make_workdir_bin:
                assert sane.index(expected) < sane.index("/usr/bin")
            else:
                assert expected not in sane
        assert cwd_a.exists()  # cwd of the "caller" is irrelevant to the outcome

    def test_healthy_system_tail_not_reported_dead(
        self, gateway_cli, launchd, tmp_path, monkeypatch, caplog,
    ):
        """Regenerating a canonical installed PATH must not warn that /usr/bin etc. are dead —
        they land in the tail; warnings fire only for entries truly absent from the result."""
        installed = tmp_path / "ai.hermes.gateway.plist"
        installed.write_bytes(plistlib.dumps({
            "EnvironmentVariables": {"PATH": "/usr/bin:/bin"},
        }))
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        with caplog.at_level(logging.WARNING, logger="hermes_cli.gateway_launchd"):
            sane = launchd._launchd_service_path([], installed)
        assert sane.endswith("/usr/bin:/bin:/usr/sbin:/sbin")
        assert not [r for r in caplog.records if "no longer present on disk" in r.getMessage()]

    def test_dotdot_carry_forward_keeps_symlink_semantics(
        self, gateway_cli, launchd, tmp_path, monkeypatch,
    ):
        """An installed entry '<link>/../bin' with link -> actual/vX.Y keeps its verbatim
        spelling: PATH lookup resolves '..' through the kernel (following the symlink), so
        the entry is live exactly while <link>/../bin exists. The carried entry must NOT be
        rewritten (a lexical collapse to tmp/bin, or a realpath rewrite, would change what
        the entry means or resurrect a dir the entry no longer reaches)."""
        installed = tmp_path / "ai.hermes.gateway.plist"
        version_dir = _make_dir(tmp_path / "actual" / "vX.Y")
        actual_bin = _make_dir(tmp_path / "actual" / "bin")
        link = tmp_path / "link"
        link.symlink_to(Path(version_dir))
        raw_entry = f"{link}/../bin"
        installed.write_bytes(plistlib.dumps({
            "EnvironmentVariables": {"PATH": f"{raw_entry}:/usr/bin:/bin"},
        }))
        monkeypatch.setenv("PATH", "/usr/bin:/bin")

        sane = launchd._launchd_service_path([], installed).split(":")
        assert raw_entry in sane  # verbatim, liveness checked through the kernel
        # And the entry really resolves to actual/bin via PATH semantics:
        assert os.path.isdir(raw_entry) and os.path.realpath(raw_entry) == os.path.realpath(actual_bin)

    def test_dotdot_carry_forward_dropped_when_symlink_target_removed(
        self, gateway_cli, launchd, tmp_path, monkeypatch, caplog,
    ):
        """When the symlink target is removed, '<link>/../bin' is no longer resolvable:
        regeneration must DROP it (the kernel cannot resolve it either) — a non-strict
        realpath rewrite would resurrect the old target's bin and change lookup meaning."""
        installed = tmp_path / "ai.hermes.gateway.plist"
        version_dir = tmp_path / "actual" / "vX.Y"
        version_dir.mkdir(parents=True)
        _make_dir(tmp_path / "actual" / "bin")
        link = tmp_path / "link"
        link.symlink_to(Path(version_dir))
        raw_entry = f"{link}/../bin"
        installed.write_bytes(plistlib.dumps({
            "EnvironmentVariables": {"PATH": f"{raw_entry}:/usr/bin:/bin"},
        }))
        monkeypatch.setenv("PATH", "/usr/bin:/bin")

        # The version dir disappears; link dangles, so link/../bin is unresolvable.
        version_dir.rmdir()
        assert not os.path.isdir(raw_entry)

        with caplog.at_level(logging.WARNING, logger="hermes_cli.gateway_launchd"):
            sane = launchd._launchd_service_path([], installed).split(":")
        assert raw_entry not in sane
        assert any("no longer present on disk" in r.getMessage() for r in caplog.records)

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


class TestLaunchdCarryForwardWarning:
    def test_carry_forward_does_not_warn_for_host_derived_dirs(
        self, gateway_cli, launchd, tmp_path, monkeypatch, caplog,
    ):
        """A healthy converge (minimal PATH, same host-derived dirs as the installed
        plist) must not log carry-forward for Homebrew/~/.local/bin: those were
        re-derived, not rescued. A custom installed-only dir still warns."""
        installed = tmp_path / "ai.hermes.gateway.plist"
        brew_bin = _make_dir(tmp_path / "hb" / "bin")
        local_bin = _make_dir(tmp_path / ".local" / "bin")
        custom = _make_dir(tmp_path / "custom-tool-dir")
        installed.write_bytes(plistlib.dumps({
            "EnvironmentVariables": {
                "PATH": f"{local_bin}:{brew_bin}:{custom}:/usr/bin:/bin",
            },
        }))
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        with caplog.at_level(logging.WARNING, logger="hermes_cli.gateway_launchd"):
            sane = launchd._launchd_service_path([local_bin, brew_bin], installed).split(":")
        assert sane.index(local_bin) < sane.index(brew_bin) < sane.index(custom)
        carry_msgs = [r.getMessage() for r in caplog.records if "carried forward" in r.getMessage()]
        assert len(carry_msgs) == 1
        assert custom in carry_msgs[0]
        assert brew_bin not in carry_msgs[0]
        assert local_bin not in carry_msgs[0]


@pytest.mark.platforms("macos")
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
        """An installed plist whose PATH payload was swapped for a Homebrew-less one (pre-fix
        install, converge-regenerated) is detected as stale so the refresh path repairs it —
        the old PATH-blind comparison answered "current" for exactly this plist. The plist text
        is the generator's own output with ONLY the PATH value rewritten, so any inequality
        comes from the PATH alone; with the old PATH-blanking normalizer restored this test
        fails (it would return True)."""
        plist_path = tmp_path / "ai.hermes.gateway.plist"
        brew_bin = _make_dir(tmp_path / "hb" / "bin")
        monkeypatch.setattr(gateway_cli, "get_launchd_plist_path", lambda: plist_path)
        monkeypatch.setattr(gateway_cli, "_build_service_path_dirs", lambda project_root=None: [brew_bin])

        generated = gateway_cli.generate_launchd_plist()
        import re as _re

        old_path_value = _re.search(
            r"<key>PATH</key>\s*<string>(.*?)</string>", generated, _re.DOTALL,
        ).group(1)
        drifted_text = generated.replace(old_path_value, "/usr/bin:/bin")
        assert drifted_text != generated
        plist_path.write_text(drifted_text, encoding="utf-8")

        assert gateway_cli.launchd_plist_is_current() is False

    def test_unchanged_plist_with_xml_hostile_dir_round_trips(
        self, gateway_cli, launchd, tmp_path, monkeypatch,
    ):
        """A carried-forward dir name containing '&' must survive a generation round-trip:
        the payload is XML-escaped once, and the escaped text stays byte-stable (no
        double-escaping on the next regeneration from the installed plist)."""
        import plistlib as _pl

        plist_path = tmp_path / "ai.hermes.gateway.plist"
        custom = _make_dir(tmp_path / "tools&helpers")
        monkeypatch.setattr(gateway_cli, "get_launchd_plist_path", lambda: plist_path)
        monkeypatch.setattr(gateway_cli, "_build_service_path_dirs", lambda project_root=None: [custom])

        first = gateway_cli.generate_launchd_plist()
        assert "tools&amp;helpers" in first
        parsed = _pl.loads(first.encode("utf-8"))
        assert parsed["EnvironmentVariables"]["PATH"].split(":")[0] == custom

        # Install it, regenerate from the installed plist: the escaped entry is carried
        # forward decoded ('tools&helpers'), re-escaped once — byte-identical output.
        plist_path.write_text(first, encoding="utf-8")
        second = gateway_cli.generate_launchd_plist()
        assert second == first

    def test_malformed_installed_plist_fails_open(self, gateway_cli, launchd, tmp_path, monkeypatch):
        """A syntactically valid plist with a non-string PATH must not break generation:
        carry-forward fails open and the PATH is derived from the host alone."""
        plist_path = tmp_path / "ai.hermes.gateway.plist"
        brew_bin = _make_dir(tmp_path / "hb" / "bin")
        monkeypatch.setattr(gateway_cli, "get_launchd_plist_path", lambda: plist_path)
        plist_path.write_bytes(plistlib.dumps({
            "EnvironmentVariables": {"PATH": 42},
        }))
        sane = launchd._launchd_service_path([brew_bin], plist_path).split(":")
        assert brew_bin in sane
        assert sane[-4:] == ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]


class TestSystemdUnitPath:
    @pytest.mark.platforms("macos")
    def test_unit_path_starts_with_tool_dirs_and_ignores_caller_path(
        self, gateway_cli, tmp_path, monkeypatch,
    ):
        """The systemd unit never carried the caller PATH but also missed Homebrew; the
        deterministic head must now include it (faked) without letting the caller PATH in."""
        brew_bin = "/opt/homebrew/bin"
        _fake_only_these_dirs_exist(
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

    def test_unit_path_escapes_hostile_dir_names(self, gateway_cli, tmp_path, monkeypatch):
        """A service_path_extra dir with quotes/backslashes/% must survive systemd's
        Environment= quoting and % specifier expansion (config-load regression)."""
        hostile = _make_dir(tmp_path / 'tools"help\\er%s')
        monkeypatch.setattr(
            gateway_cli, "_service_path_extra_dirs", lambda home=None: [hostile],
        )

        unit = gateway_cli.generate_systemd_unit(system=False)
        import re as _re
        m = _re.search(r'Environment="PATH=(.*?)"\n', unit)
        assert m, "unit has no PATH Environment line"
        escaped = m.group(1)
        assert escaped.count('%"') == 0  # no raw quote leaked from the dir name
        assert "\\%" in escaped or "%%" in escaped.replace("\\%", "")  # % specifier doubled
        # Round-trips through the same unquoting systemd's parsers use (see _unit_environment_value).
        unescaped = escaped.replace('\\"', '"').replace("\\\\", "\\").replace("%%", "%")
        assert unescaped.split(":")[0] == hostile or hostile in unescaped.split(":")

    def test_unit_config_system_dir_bubbles_to_tail(self, gateway_cli, tmp_path, monkeypatch):
        """The systemd unit honors the same tail contract as the plist: a canonical system dir
        in service_path_extra (any spelling) must not precede custom dirs."""
        custom = _make_dir(tmp_path / "custom-tool-dir")
        monkeypatch.setattr(
            gateway_cli, "_service_path_extra_dirs", lambda home=None: ["/usr/bin/", str(custom)],
        )

        unit = gateway_cli.generate_systemd_unit(system=False)
        env_line = next(l for l in unit.splitlines() if l.startswith('Environment="PATH='))
        raw = env_line[len('Environment="PATH='):].rstrip('"')
        entries = raw.split(":")
        system_dirs = gateway_cli._service_system_dirs()
        assert "/usr/bin/" not in entries
        # custom precedes every canonical system dir (they appear only in the tail).
        assert entries.index(str(custom)) < entries.index(system_dirs[-1]) or str(custom) not in entries
        for system_dir in system_dirs:
            assert entries.count(system_dir) == 1
        assert entries[-len(system_dirs):] == system_dirs

    def test_system_unit_reads_target_users_config_extras(self, gateway_cli, tmp_path, monkeypatch):
        """A system unit's service_path_extra must come from the TARGET user's hermes-home
        config, not the invoking (sudo) caller's: with distinct configs, the installer-only
        dir is absent and the target-only dir present."""
        import hermes_cli.config as config_mod

        root_home = tmp_path / "root-home"
        target_home = tmp_path / "target-home"
        installer_extra = _make_dir(root_home / "installer-only" / "bin")
        target_extra = _make_dir(target_home / "target-only" / "bin")

        configs = {
            root_home / ".hermes": {"gateway": {"service_path_extra": [installer_extra]}},
            target_home / ".hermes": {"gateway": {"service_path_extra": [target_extra]}},
        }

        def fake_load_config_readonly():
            from hermes_constants import get_hermes_home

            return configs.get(Path(get_hermes_home()), {})

        monkeypatch.setattr(config_mod, "load_config_readonly", fake_load_config_readonly)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: root_home))
        monkeypatch.setenv("HERMES_HOME", str(root_home / ".hermes"))
        monkeypatch.setattr(
            gateway_cli, "_system_service_identity",
            lambda run_as_user=None: ("alice", "alice", str(target_home), 1001),
        )
        monkeypatch.setattr(gateway_cli, "get_hermes_home", lambda: root_home / ".hermes")

        unit = gateway_cli.generate_systemd_unit(system=True, run_as_user="alice")

        assert installer_extra not in unit
        assert target_extra in unit

    def test_system_unit_probes_target_users_local_bin(self, gateway_cli, tmp_path, monkeypatch):
        """The ~/.local/bin probe for a system unit must run against the TARGET user's OS
        home — Path.home()/$HOME stay the installer's under sudo. With the target's
        .local/bin present and the installer's absent, the target's dir wins its slot."""
        root_home = tmp_path / "root-home"
        target_home = tmp_path / "target-home"
        root_hermes = root_home / ".hermes"
        root_hermes.mkdir(parents=True)

        real_is_dir = Path.is_dir
        faked = {target_home / ".local" / "bin"}

        def fake_is_dir(self):
            if self in faked:
                return True
            return real_is_dir(self)

        monkeypatch.setattr(Path, "is_dir", fake_is_dir)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: root_home))
        monkeypatch.setenv("HERMES_HOME", str(root_hermes))
        monkeypatch.setattr(
            gateway_cli, "_system_service_identity",
            lambda run_as_user=None: ("alice", "alice", str(target_home), 1001),
        )
        monkeypatch.setattr(gateway_cli, "get_hermes_home", lambda: root_hermes)

        unit = gateway_cli.generate_systemd_unit(system=True, run_as_user="alice")

        assert str(target_home / ".local" / "bin") in unit
        assert str(root_home / ".local" / "bin") not in unit

    def test_system_unit_ranks_managed_node_after_tool_dirs_and_extras(
        self, gateway_cli, tmp_path, monkeypatch,
    ):
        """User units append managed Node; system units must not prepend it ahead of
        ~/.local/bin or service_path_extra (the leftover prepend was for remapped
        caller-PATH entries that no longer exist)."""
        local_bin = _make_dir(tmp_path / ".local" / "bin")
        extra = _make_dir(tmp_path / "opt" / "tools" / "bin")
        node_bin = "/managed/node/bin"

        monkeypatch.setattr(
            gateway_cli, "_build_service_path_dirs",
            lambda project_root=None, home=None: [local_bin, extra],
        )

        def _append_node(entries, hermes_root=None):
            if node_bin not in entries:
                entries.append(node_bin)

        monkeypatch.setattr(gateway_cli, "_append_node_dir_for_service", _append_node)
        monkeypatch.setattr(
            gateway_cli, "_system_service_identity",
            lambda run_as_user=None: ("alice", "alice", str(tmp_path), 1001),
        )

        def _path_entries(unit: str) -> list[str]:
            env_line = next(l for l in unit.splitlines() if l.startswith('Environment="PATH='))
            return env_line[len('Environment="PATH='):].rstrip('"').split(":")

        user_entries = _path_entries(gateway_cli.generate_systemd_unit(system=False))
        system_entries = _path_entries(
            gateway_cli.generate_systemd_unit(system=True, run_as_user="alice"),
        )
        for entries in (user_entries, system_entries):
            assert entries.index(local_bin) < entries.index(extra) < entries.index(node_bin)


@pytest.mark.platforms("macos")
class TestNormalizeLaunchdPlist:
    def test_normalize_keeps_path_payload(self, gateway_cli):
        text = "<key>PATH</key>\n    <string>/usr/bin:/bin</string>"
        norm = gateway_cli._normalize_launchd_plist_for_comparison(text)
        assert "/usr/bin:/bin" in norm
