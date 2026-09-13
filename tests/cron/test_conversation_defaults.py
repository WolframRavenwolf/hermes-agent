"""Creation defaults preserve explicit attachment and destination choices."""

import pytest


@pytest.mark.parametrize("requested,expected", [(None, True), (False, False), (True, True)])
def test_new_job_snapshots_enabled_mirror_default(tmp_path, monkeypatch, requested, expected):
    import cron.jobs as jobs
    from cron.scheduler import _target_mirror_eligible
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(jobs, "CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr(jobs, "JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"cron": {"mirror_delivery": True}})
    job = jobs.create_job(prompt="Send report", schedule="every 1h",
                          deliver="mattermost:destination", attach_to_session=requested)
    assert jobs.get_job(job["id"]).get("attach_to_session") is expected
    assert _target_mirror_eligible(job, {"platform": "mattermost", "chat_id": "destination", "_resolved_from": "explicit"},
                                   global_mirror=True, origin_match=False) is expected


def test_disabled_default_keeps_existing_behavior(tmp_path, monkeypatch):
    import cron.jobs as jobs
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(jobs, "CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr(jobs, "JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"cron": {"mirror_delivery": False}})
    job = jobs.create_job(prompt="Send report", schedule="every 1h", deliver="origin")
    assert "attach_to_session" not in job
