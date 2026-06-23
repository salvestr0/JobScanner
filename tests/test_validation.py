import json

import pytest

from models import ApplicationStatus, Job, User, db


def _post_json(client, path: str, data: dict):
    return client.post(
        path,
        data=json.dumps(data),
        headers={"Content-Type": "application/json"},
    )


def _register_and_login(client, email="validation@test.com") -> User:
    payload = {"email": email, "password": "StrongPass1!"}
    resp = _post_json(client, "/api/auth/register", payload)
    assert resp.status_code == 200, resp.data
    resp = _post_json(client, "/api/auth/login", payload)
    assert resp.status_code == 200, resp.data
    return User.query.filter_by(email=email).first()


@pytest.mark.parametrize("time_value", ["00:00", "23:59"])
def test_schedule_accepts_valid_24_hour_times(client, time_value):
    _register_and_login(client, email=f"schedule-{time_value.replace(':', '')}@test.com")

    resp = client.post("/api/schedule", json={"enabled": True, "time": time_value})

    assert resp.status_code == 200, resp.data
    assert resp.get_json() == {"ok": True, "enabled": True, "time": time_value}


@pytest.mark.parametrize("payload", [
    {"enabled": True, "time": "24:00"},
    {"enabled": True, "time": "99:99"},
    {"enabled": True, "time": "9:00"},
    {"enabled": True},
    {"enabled": True, "time": 900},
])
def test_schedule_rejects_invalid_times(client, payload):
    _register_and_login(client, email=f"schedule-invalid-{len(str(payload))}@test.com")

    resp = client.post("/api/schedule", json=payload)

    assert resp.status_code == 400
    assert "Invalid time format" in resp.get_json()["error"]


@pytest.mark.parametrize("status", ["applied", "interview", "skip"])
def test_application_accepts_valid_statuses(client, status):
    user = _register_and_login(client, email=f"status-{status}@test.com")
    job_id = f"job-{status}"
    db.session.add(Job(user_id=user.id, source_job_id=job_id, title="Data Analyst", company="Acme"))
    db.session.commit()

    resp = client.post(f"/api/applications/{job_id}", json={"status": status})

    assert resp.status_code == 200, resp.data
    assert resp.get_json() == {"ok": True}
    row = db.session.get(ApplicationStatus, (user.id, job_id))
    assert row is not None
    assert row.status == status


def test_application_accepts_clear_status(client):
    user = _register_and_login(client, email="status-clear@test.com")
    job_id = "job-clear"
    db.session.add(ApplicationStatus(user_id=user.id, job_source_id=job_id, status="applied"))
    db.session.commit()

    resp = client.post(f"/api/applications/{job_id}", json={"status": "clear"})

    assert resp.status_code == 200, resp.data
    assert resp.get_json() == {"ok": True}
    assert db.session.get(ApplicationStatus, (user.id, job_id)) is None


def test_application_rejects_invalid_status(client):
    user = _register_and_login(client, email="status-invalid@test.com")

    resp = client.post("/api/applications/job-invalid", json={"status": "hired"})

    assert resp.status_code == 400
    assert "Unsupported application status" in resp.get_json()["error"]
    assert db.session.get(ApplicationStatus, (user.id, "job-invalid")) is None
