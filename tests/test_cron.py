"""
Cron job tests: the ±7-minute scheduling window in /api/cron/scan
(including the midnight wraparound) and the retention rules in
/api/cron/cleanup.

Scan threads are stubbed out so no real scanning or network traffic
happens. Window tests derive schedule_time from the real clock with
margins wide enough that a minute ticking over mid-test can't flip
the result.
"""
from datetime import datetime, timedelta, timezone

import pytest

import app as flask_app
from models import ApplicationStatus, Job, ScanHistory, User, UserSettings, db

CRON_HEADERS = {"X-Cron-Secret": "test-cron-secret-xyz"}


def _hhmm(offset_min: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=offset_min)).strftime("%H:%M")


@pytest.fixture
def stub_scan(monkeypatch):
    """Replace the scan worker so cron tests never run a real scan."""
    monkeypatch.setattr(flask_app, "_run_scan_inprocess",
                        lambda user_id, mode, notify, q, extra_env: None)


def _scheduled_user(time_str, enabled=True, email="cron@test.com") -> User:
    user = User(email=email)
    db.session.add(user)
    db.session.commit()
    db.session.add(UserSettings(
        user_id=user.id, schedule_enabled=enabled, schedule_time=time_str,
    ))
    db.session.commit()
    return user


# ── /api/cron/scan scheduling window ────────────────────────────────────────────

def test_scan_triggers_inside_window(client, stub_scan):
    user = _scheduled_user(_hhmm(6))
    resp = client.post("/api/cron/scan", headers=CRON_HEADERS)
    assert resp.status_code == 200
    assert user.email in resp.get_json()["triggered"]


def test_scan_skips_outside_window(client, stub_scan):
    _scheduled_user(_hhmm(30))
    resp = client.post("/api/cron/scan", headers=CRON_HEADERS)
    assert resp.get_json()["triggered"] == []


def test_scan_window_wraps_around_midnight(client, stub_scan):
    # Schedule 3 minutes "behind" via the day boundary: e.g. now 00:01,
    # schedule 23:58 — raw diff is ~1437 minutes but it's really 3
    user = _scheduled_user(_hhmm(-3 + 1440))
    resp = client.post("/api/cron/scan", headers=CRON_HEADERS)
    assert user.email in resp.get_json()["triggered"]


def test_scan_skips_disabled_schedules(client, stub_scan):
    _scheduled_user(_hhmm(0), enabled=False)
    resp = client.post("/api/cron/scan", headers=CRON_HEADERS)
    assert resp.get_json()["triggered"] == []


def test_scan_survives_malformed_schedule_time(client, stub_scan):
    _scheduled_user("banana")
    resp = client.post("/api/cron/scan", headers=CRON_HEADERS)
    assert resp.status_code == 200
    assert resp.get_json()["triggered"] == []


def test_scan_not_retriggered_while_running(client, stub_scan):
    user = _scheduled_user(_hhmm(0))
    flask_app._get_scan(user.id)["running"] = True
    resp = client.post("/api/cron/scan", headers=CRON_HEADERS)
    assert resp.get_json()["triggered"] == []


# ── /api/cron/cleanup retention rules ───────────────────────────────────────────

def test_cleanup_requires_secret(client):
    resp = client.post("/api/cron/cleanup", headers={"X-Cron-Secret": "wrong"})
    assert resp.status_code == 403


def test_cleanup_retention_rules(client):
    user = User(email="cleanup@test.com")
    db.session.add(user)
    db.session.commit()

    now = datetime.now(timezone.utc)
    db.session.add_all([
        Job(user_id=user.id, source_job_id="stale_untracked", title="Old",
            scan_date=now - timedelta(days=61)),
        Job(user_id=user.id, source_job_id="stale_tracked", title="Old but applied",
            scan_date=now - timedelta(days=61)),
        Job(user_id=user.id, source_job_id="recent", title="New",
            scan_date=now - timedelta(days=5)),
        ApplicationStatus(user_id=user.id, job_source_id="stale_tracked", status="applied"),
        ScanHistory(user_id=user.id, mode="analyst", status="done",
                    started_at=now - timedelta(days=91)),
        ScanHistory(user_id=user.id, mode="analyst", status="done",
                    started_at=now - timedelta(days=10)),
    ])
    db.session.commit()

    resp = client.post("/api/cron/cleanup", headers=CRON_HEADERS)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["deleted_jobs"] == 1
    assert body["deleted_scan_history"] == 1

    remaining = {j.source_job_id for j in Job.query.filter_by(user_id=user.id)}
    assert remaining == {"stale_tracked", "recent"}
    assert ScanHistory.query.filter_by(user_id=user.id).count() == 1
