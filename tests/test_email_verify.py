"""
Email verification flow tests.

The original flow stored a single sha256 token hash that was overwritten on
every resend — so clicking any link except the very latest one failed, and
it failed silently (redirect with no message). These tests pin the new
behaviour: signed stateless tokens where every emailed link stays valid
until the user verifies, legacy hash links still honoured, and explicit
verified=1 / verify_error=1 redirect params.
"""
import hashlib
import json
import re

import pytest

import app as flask_app
from models import User, db


@pytest.fixture
def outbox(monkeypatch):
    """Capture emails instead of sending them; returns list of (to, subject, html)."""
    sent = []
    monkeypatch.setattr(flask_app, "_send_email",
                        lambda to, subject, html: sent.append((to, subject, html)) or True)
    return sent


def _register(client, email="verify@test.com"):
    resp = client.post("/api/auth/register",
                       data=json.dumps({"email": email, "password": "StrongPass1!"}),
                       headers={"Content-Type": "application/json"})
    assert resp.status_code == 200
    return User.query.filter_by(email=email).first()


def _token_from(html: str) -> str:
    return re.search(r'/verify-email\?token=([^"]+)', html).group(1)


# ── Happy path ──────────────────────────────────────────────────────────────────

def test_registration_link_verifies_user(client, outbox):
    user = _register(client)
    assert user.email_verified is False
    assert len(outbox) == 1

    resp = client.get(f"/verify-email?token={_token_from(outbox[0][2])}")
    assert resp.status_code == 302
    assert "verified=1" in resp.headers["Location"]
    assert db.session.get(User, user.id).email_verified is True


def test_verify_while_logged_out_redirects_to_login(client, outbox):
    _register(client)
    token = _token_from(outbox[0][2])
    client.post("/api/auth/logout")

    resp = client.get(f"/verify-email?token={token}")
    assert resp.headers["Location"].startswith("/login")
    assert "verified=1" in resp.headers["Location"]


def test_older_link_still_works_after_resends(client, outbox):
    # Regression: resending used to overwrite the stored token hash, which
    # silently invalidated every previously sent link
    user = _register(client)
    client.post("/api/auth/resend-verification")
    client.post("/api/auth/resend-verification")
    assert len(outbox) == 3

    first_link_token = _token_from(outbox[0][2])
    resp = client.get(f"/verify-email?token={first_link_token}")
    assert "verified=1" in resp.headers["Location"]
    assert db.session.get(User, user.id).email_verified is True


def test_clicking_link_twice_is_idempotent_success(client, outbox):
    user = _register(client)
    token = _token_from(outbox[0][2])
    client.get(f"/verify-email?token={token}")
    resp = client.get(f"/verify-email?token={token}")
    assert "verified=1" in resp.headers["Location"]
    assert db.session.get(User, user.id).email_verified is True


def test_legacy_hash_link_still_verifies(client):
    user = User(email="legacy@test.com",
                email_verify_token=hashlib.sha256(b"old-style-token").hexdigest())
    db.session.add(user)
    db.session.commit()

    resp = client.get("/verify-email?token=old-style-token")
    assert "verified=1" in resp.headers["Location"]
    assert db.session.get(User, user.id).email_verified is True


# ── Failure feedback ────────────────────────────────────────────────────────────

def test_bad_token_redirects_with_error_flag(client, outbox):
    user = _register(client)
    resp = client.get("/verify-email?token=garbage")
    assert "verify_error=1" in resp.headers["Location"]
    assert db.session.get(User, user.id).email_verified is False


def test_missing_token_redirects_with_error_flag(client):
    resp = client.get("/verify-email")
    assert "verify_error=1" in resp.headers["Location"]


def test_expired_token_is_rejected():
    token = flask_app._make_verify_token("some-user-id")
    assert flask_app._load_verify_token(token) == "some-user-id"
    assert flask_app._load_verify_token(token, max_age=-1) is None


# ── Resend endpoint ─────────────────────────────────────────────────────────────

def test_resend_requires_login(client):
    assert client.post("/api/auth/resend-verification").status_code == 401


def test_resend_skips_already_verified_users(client, outbox):
    _register(client)
    user = User.query.filter_by(email="verify@test.com").first()
    user.email_verified = True
    db.session.commit()
    outbox.clear()

    resp = client.post("/api/auth/resend-verification")
    assert resp.status_code == 200
    assert outbox == []


def test_resend_reports_send_failure(client, monkeypatch):
    monkeypatch.setattr(flask_app, "_send_email", lambda *a: True)
    _register(client)
    monkeypatch.setattr(flask_app, "_send_email", lambda *a: False)

    resp = client.post("/api/auth/resend-verification")
    assert resp.status_code == 502
    assert resp.get_json()["ok"] is False
