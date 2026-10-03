"""cron.manage optional ``profile`` param — per-profile store scoping.

Mirrors ``skills.manage`` / ``mcp.catalog``: when a ``profile`` is passed the
handler resolves ``get_profile_dir(profile)`` and wraps the action dispatch in
``set_hermes_home_override`` / ``reset_hermes_home_override``. Because
``cronjob()`` -> ``list_jobs()`` keys off ``get_hermes_home()``, the list action
must then read THAT profile's ``cron/jobs.json``, not the launch profile's.
"""

import json

from tui_gateway import server


def _isolate_cron_store(monkeypatch, tmp_path):
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")


def test_cron_manage_profile_reads_that_profiles_store(tmp_path, monkeypatch):
    # A temp profile home with one job in its cron store.
    profile_home = tmp_path / "profiles" / "botA"
    cron_dir = profile_home / "cron"
    cron_dir.mkdir(parents=True)
    (cron_dir / "jobs.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "id": "job-botA",
                        "name": "botA-only-job",
                        "prompt": "scoped hello",
                        "enabled": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    # Route the profile name the handler resolves to our temp home.
    import hermes_cli.profiles as profiles

    monkeypatch.setattr(profiles, "get_profile_dir", lambda name: profile_home)

    resp = server.handle_request(
        {
            "id": "1",
            "method": "cron.manage",
            "params": {"action": "list", "profile": "botA"},
        }
    )

    assert "result" in resp, resp
    assert resp["result"]["scoped"] == "botA"
    names = [j.get("name") for j in resp["result"]["jobs"]]
    assert "botA-only-job" in names

    # The override must not leak: an unscoped call after this one resolves the
    # launch profile again, which does not contain botA's job.
    from hermes_constants import get_hermes_home_override

    assert get_hermes_home_override() is None


def test_cron_manage_unknown_profile_errors(tmp_path, monkeypatch):
    import hermes_cli.profiles as profiles

    missing = tmp_path / "profiles" / "ghost"
    monkeypatch.setattr(profiles, "get_profile_dir", lambda name: missing)

    resp = server.handle_request(
        {
            "id": "2",
            "method": "cron.manage",
            "params": {"action": "list", "profile": "ghost"},
        }
    )

    assert "error" in resp, resp
    assert resp["error"]["code"] == 4064


def test_cron_manage_pause_without_reason_is_an_rpc_error(tmp_path, monkeypatch):
    _isolate_cron_store(monkeypatch, tmp_path)
    from cron.jobs import create_job, get_job

    job = create_job(prompt="stay up", schedule="every 1h", name="watch")
    resp = server.handle_request(
        {
            "id": "pause-blank",
            "method": "cron.manage",
            "params": {"action": "pause", "name": job["id"]},
        }
    )
    assert "error" in resp, resp
    assert "reason" in resp["error"]["message"].lower()
    assert "result" not in resp
    assert get_job(job["id"])["state"] != "paused"


def test_cron_manage_pause_with_reason_succeeds(tmp_path, monkeypatch):
    _isolate_cron_store(monkeypatch, tmp_path)
    from cron.jobs import create_job, get_job

    job = create_job(prompt="hold", schedule="every 1h", name="hold")
    resp = server.handle_request(
        {
            "id": "pause-reason",
            "method": "cron.manage",
            "params": {
                "action": "pause",
                "name": job["id"],
                "reason": "provider migration",
                "review_after": "2099-01-01",
            },
        }
    )
    assert "result" in resp, resp
    assert resp["result"]["success"] is True
    stored = get_job(job["id"])
    assert stored["state"] == "paused"
    assert stored["paused_reason"] == "provider migration"
    assert stored["paused_review_after"] == "2099-01-01"
