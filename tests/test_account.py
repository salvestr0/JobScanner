"""
Account deletion tests (PDPA compliance).

Verifies the confirmation gates (password for email users, the word DELETE
for Google-only users) and — the part that matters legally — that deleting
an account actually removes every row of the user's data, not just the
users record.
"""
import json

from models import ApplicationStatus, Job, ScanHistory, SeenJob, User, db


def _register_and_login(client, email="del@test.com", password="StrongPass1!"):
    payload = json.dumps({"email": email, "password": password})
    headers = {"Content-Type": "application/json"}
    resp = client.post("/api/auth/register", data=payload, headers=headers)
    assert resp.status_code == 200, f"register failed: {resp.data}"
    resp = client.post("/api/auth/login", data=payload, headers=headers)
    assert resp.status_code == 200, f"login failed: {resp.data}"
    return User.query.filter_by(email=email).first().id


def _seed_user_data(user_id: str):
    db.session.add_all([
        Job(user_id=user_id, source_job_id="mcf_1", title="Data Analyst"),
        SeenJob(user_id=user_id, job_source_id="mcf_1"),
        ApplicationStatus(user_id=user_id, job_source_id="mcf_1", status="applied"),
        ScanHistory(user_id=user_id, mode="default", status="done"),
    ])
    db.session.commit()


def test_delete_account_rejects_wrong_password(client):
    uid = _register_and_login(client)
    resp = client.post("/api/auth/delete-account", json={"password": "wrong"})
    assert resp.status_code == 403
    assert db.session.get(User, uid) is not None


def test_delete_account_removes_all_user_data(client):
    uid = _register_and_login(client)
    _seed_user_data(uid)

    resp = client.post("/api/auth/delete-account", json={"password": "StrongPass1!"})
    assert resp.status_code == 200
    assert resp.get_json().get("ok") is True

    assert db.session.get(User, uid) is None
    for model in (Job, SeenJob, ApplicationStatus, ScanHistory):
        assert model.query.filter_by(user_id=uid).count() == 0, \
            f"{model.__name__} rows survived account deletion"

    # Session is invalidated too
    resp = client.get("/api/auth/me")
    assert resp.status_code == 401


def test_google_only_user_confirms_with_delete_word(client):
    user = User(email="google-only@test.com", google_id="g-123")
    db.session.add(user)
    db.session.commit()
    with client.session_transaction() as sess:
        sess["_user_id"] = user.id
        sess["_fresh"] = True

    resp = client.post("/api/auth/delete-account", json={"confirm": "nope"})
    assert resp.status_code == 403
    assert db.session.get(User, user.id) is not None

    resp = client.post("/api/auth/delete-account", json={"confirm": "DELETE"})
    assert resp.status_code == 200
    assert db.session.get(User, user.id) is None
