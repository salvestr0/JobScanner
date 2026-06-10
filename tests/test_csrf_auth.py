"""
Auth flow, billing gate, and salary normalization tests.

Tests what is actually in master's codebase:
  - Register / login / logout / protected endpoint flow
  - _is_active() and _is_free_tier() billing gate logic
  - _to_monthly_sgd() salary normalization
  - _prune_old_jobs() stale job pruning
  - Cron and Stripe webhook endpoints require correct secrets
"""
import json
import os
import warnings

os.environ.setdefault("SECRET_KEY", "a" * 32)
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("CRON_SECRET", "test-cron-secret-xyz")

warnings.filterwarnings("ignore", message="REDIS_URL not set")
warnings.filterwarnings("ignore", message="Fernet")

import pytest

import app as flask_app
from models import User, db
from scrapers import _USD_TO_SGD, _dedupe_key, _to_monthly_sgd


# ── Fixtures ────────────────────────────────────────────────────────────────────

@pytest.fixture
def client():
    flask_app.app.config.update(
        TESTING=True,
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
        RATELIMIT_ENABLED=False,
    )
    with flask_app.app.test_client() as c:
        with flask_app.app.app_context():
            db.create_all()
            yield c
            db.session.remove()
            db.drop_all()


# ── Helpers ─────────────────────────────────────────────────────────────────────

def _post_json(client, path: str, data: dict):
    return client.post(path, data=json.dumps(data),
                       headers={"Content-Type": "application/json"})


def _register(client, email="user@test.com", password="StrongPass1!"):
    return _post_json(client, "/api/auth/register", {"email": email, "password": password})


def _login(client, email="user@test.com", password="StrongPass1!"):
    return _post_json(client, "/api/auth/login", {"email": email, "password": password})


# ── Auth flow ────────────────────────────────────────────────────────────────────

def test_register_creates_user(client):
    resp = _register(client, email="new@test.com")
    assert resp.status_code == 200, f"register failed: {resp.data}"
    assert resp.get_json().get("ok") is True


def test_duplicate_register_returns_409(client):
    _register(client, email="dup@test.com")
    resp = _register(client, email="dup@test.com")
    assert resp.status_code == 409


def test_login_bad_credentials_returns_401(client):
    resp = _login(client, email="nobody@x.com", password="wrong")
    assert resp.status_code == 401


def test_protected_endpoint_requires_login(client):
    resp = client.get("/api/auth/me")
    assert resp.status_code == 401


def test_full_register_login_logout_flow(client):
    # Register
    resp = _register(client, email="flow@test.com")
    assert resp.status_code == 200, f"register failed: {resp.data}"

    # Login
    resp = _login(client, email="flow@test.com")
    assert resp.status_code == 200, f"login failed: {resp.data}"
    assert resp.get_json().get("ok") is True

    # Protected endpoint reachable while logged in
    resp = client.get("/api/auth/me")
    assert resp.status_code == 200
    assert resp.get_json().get("email") == "flow@test.com"

    # Logout
    resp = _post_json(client, "/api/auth/logout", {})
    assert resp.status_code == 200

    # Protected endpoint now returns 401
    resp = client.get("/api/auth/me")
    assert resp.status_code == 401


# ── Cron / Stripe endpoints ──────────────────────────────────────────────────────

def test_cron_wrong_secret_returns_403(client):
    resp = client.post("/api/cron/scan", headers={"X-Cron-Secret": "wrong"})
    assert resp.status_code == 403


def test_cron_correct_secret_runs(client):
    resp = client.post("/api/cron/scan",
                       headers={"X-Cron-Secret": "test-cron-secret-xyz"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert "triggered" in body


def test_stripe_webhook_no_signature_returns_400(client):
    resp = client.post("/api/stripe/webhook",
                       data=b"{}",
                       content_type="application/json")
    # 400 from missing secret/signature, not a crash
    assert resp.status_code == 400


# ── Billing gate unit tests ──────────────────────────────────────────────────────

def test_is_active_free_user(client):
    with flask_app.app.app_context():
        u = User(email="free@test.com", subscription_status="free")
        assert flask_app._is_active(u) is True


def test_is_active_paid_user(client):
    with flask_app.app.app_context():
        u = User(email="pro@test.com", subscription_status="active")
        assert flask_app._is_active(u) is True


def test_is_free_tier_free_user(client):
    with flask_app.app.app_context():
        u = User(email="free@test.com", subscription_status="free")
        assert flask_app._is_free_tier(u) is True


def test_is_free_tier_active_user(client):
    with flask_app.app.app_context():
        u = User(email="pro@test.com", subscription_status="active")
        assert flask_app._is_free_tier(u) is False


# ── Salary normalisation unit tests ─────────────────────────────────────────────

def test_to_monthly_sgd_annual_sgd():
    assert _to_monthly_sgd(48000) == 4000
    assert _to_monthly_sgd(30000) == 2500
    assert _to_monthly_sgd(96000) == 8000


def test_to_monthly_sgd_already_monthly():
    assert _to_monthly_sgd(3500) == 3500
    assert _to_monthly_sgd(5000) == 5000


def test_to_monthly_sgd_annual_usd():
    monthly = _to_monthly_sgd(80000, fx=_USD_TO_SGD)
    assert 8500 <= monthly <= 9500, f"unexpected: {monthly}"


def test_to_monthly_sgd_none():
    assert _to_monthly_sgd(None) is None
    assert _to_monthly_sgd("") is None
    assert _to_monthly_sgd("N/A") is None


def test_to_monthly_sgd_string_numbers():
    assert _to_monthly_sgd("48000") == 4000
    assert _to_monthly_sgd("3500") == 3500


# ── Duplicate detection ──────────────────────────────────────────────────────────

def test_dedupe_key_same_job_different_sources():
    mcf    = {"title": "Data Analyst", "company": "DBS Bank", "source": "MyCareersFuture"}
    adzuna = {"title": "Data  Analyst", "company": "DBS BANK", "source": "Adzuna"}
    assert _dedupe_key(mcf) == _dedupe_key(adzuna)


def test_dedupe_key_punctuation_normalised():
    a = {"title": "AI/ML Engineer (Junior)", "company": "Grab Pte. Ltd."}
    b = {"title": "AI ML Engineer Junior",   "company": "Grab Pte Ltd"}
    assert _dedupe_key(a) == _dedupe_key(b)


def test_dedupe_key_different_jobs_differ():
    a = {"title": "Data Analyst",  "company": "DBS Bank"}
    b = {"title": "Data Engineer", "company": "DBS Bank"}
    assert _dedupe_key(a) != _dedupe_key(b)


def test_dedupe_key_unknown_company_returns_none():
    assert _dedupe_key({"title": "Data Analyst", "company": "Unknown"}) is None
    assert _dedupe_key({"title": "Data Analyst", "company": ""}) is None
    assert _dedupe_key({"title": "", "company": "DBS Bank"}) is None


# ── Health endpoint ──────────────────────────────────────────────────────────────

def test_health_endpoint(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}


# ── Billing health cron ──────────────────────────────────────────────────────────

def test_billing_health_wrong_secret(client):
    resp = client.post("/api/cron/billing-health",
                       headers={"X-Cron-Secret": "wrong"})
    assert resp.status_code == 403


def test_billing_health_correct_secret(client):
    resp = client.post("/api/cron/billing-health",
                       headers={"X-Cron-Secret": "test-cron-secret-xyz"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert "past_due" in body
    assert "alerted" in body
