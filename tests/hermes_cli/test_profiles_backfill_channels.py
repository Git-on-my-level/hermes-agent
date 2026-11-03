"""``hermes update``'s .env backfill copies the default's credentials into a profile that predates
per-profile ``.env`` seeding.

Those profiles ran on the root install's environment, so the copy preserves the credentials they
were effectively using; the user diverges per profile from there. An install with no default
``.env`` gets the placeholder header instead.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli.profiles import backfill_profile_envs, create_profile


@pytest.fixture()
def profile_env(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    default_home = tmp_path / ".hermes"
    default_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    return tmp_path


def test_backfill_copies_the_default_credentials(profile_env):
    (profile_env / ".hermes" / ".env").write_text(
        "OPENROUTER_API_KEY=root-key\nTELEGRAM_BOT_TOKEN=123:abc\nDISCORD_BOT_TOKEN=d1\n", encoding="utf-8"
    )
    p = create_profile("legacy", no_alias=True, no_skills=True)
    (p / ".env").unlink()

    assert backfill_profile_envs(quiet=True) == ["legacy"]
    assert ((p / ".env").read_text(encoding="utf-8-sig")
            == "OPENROUTER_API_KEY=root-key\nTELEGRAM_BOT_TOKEN=123:abc\nDISCORD_BOT_TOKEN=d1\n")


def test_backfill_seeds_the_placeholder_when_the_default_has_no_env(profile_env):
    p = create_profile("fresh", no_alias=True, no_skills=True)
    (p / ".env").unlink()

    assert backfill_profile_envs(quiet=True) == ["fresh"]
    assert all(line.startswith("#") or not line.strip()
               for line in (p / ".env").read_text(encoding="utf-8-sig").splitlines())
