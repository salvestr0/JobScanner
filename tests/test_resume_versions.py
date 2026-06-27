"""
Tests for the resume-builder version store (now Postgres-backed, previously
ephemeral disk JSON). Covers save -> list -> download -> account-deletion purge,
plus cross-user isolation on download.
"""
import json

from models import ResumeVersion, User, db


def _register_login(client, email="versions@test.com", password="StrongPass1!"):
    payload = json.dumps({"email": email, "password": password})
    headers = {"Content-Type": "application/json"}
    assert client.post("/api/auth/register", data=payload, headers=headers).status_code == 200
    assert client.post("/api/auth/login", data=payload, headers=headers).status_code == 200
    return User.query.filter_by(email=email).first()


def _save(client, name="My CV", **extra):
    body = {"name": name, "profile": {"experience_summary": "Did things.", "email": "x@y.z"}}
    body.update(extra)
    return client.post("/api/resume/versions", data=json.dumps(body),
                       headers={"Content-Type": "application/json"})


def test_save_persists_to_db_and_lists(client):
    user = _register_login(client)

    r = _save(client, name="Data Analyst CV", source="tailored", job_title="Data Analyst")
    assert r.status_code == 200
    vid = r.get_json()["version"]["id"]

    # Persisted to the DB (not the filesystem)
    assert ResumeVersion.query.filter_by(user_id=user.id).count() == 1

    listing = client.get("/api/resume/versions").get_json()
    assert len(listing) == 1
    assert listing[0]["id"] == vid
    assert listing[0]["name"] == "Data Analyst CV"
    assert listing[0]["job_title"] == "Data Analyst"
    assert listing[0]["created_at"]  # formatted SGT string present


def test_save_requires_profile_dict(client):
    _register_login(client)
    r = client.post("/api/resume/versions", data=json.dumps({"profile": "nope"}),
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 400


def test_download_renders_pdf(client):
    _register_login(client)
    vid = _save(client).get_json()["version"]["id"]

    r = client.get(f"/api/resume/versions/{vid}/download")
    assert r.status_code == 200
    assert r.headers["Content-Type"] == "application/pdf"
    assert r.data[:4] == b"%PDF"


def test_download_unknown_id_404(client):
    _register_login(client)
    assert client.get("/api/resume/versions/does-not-exist/download").status_code == 404


def test_versions_are_user_scoped(client):
    """A user must not be able to download another user's version by id."""
    _register_login(client, email="owner@test.com")
    vid = _save(client, name="Owner CV").get_json()["version"]["id"]
    client.get("/api/auth/logout")

    _register_login(client, email="attacker@test.com")
    # Not in attacker's listing
    assert client.get("/api/resume/versions").get_json() == []
    # And not downloadable by id
    assert client.get(f"/api/resume/versions/{vid}/download").status_code == 404


def test_account_deletion_purges_versions(client):
    user = _register_login(client, email="deleteme@test.com")
    _save(client)
    assert ResumeVersion.query.count() == 1

    r = client.post("/api/auth/delete-account",
                    data=json.dumps({"password": "StrongPass1!"}),
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 200
    assert ResumeVersion.query.count() == 0  # cascade purged it
