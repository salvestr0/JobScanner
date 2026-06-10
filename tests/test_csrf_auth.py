"""
CSRF protection and authentication flow tests.

Verifies:
  - POST endpoints reject requests without a CSRF token (HTTP 400)
  - POST endpoints accept requests that carry a valid CSRF token
  - @csrf.exempt endpoints are reachable without a token
  - Full register → login → protected-endpoint flow works end-to-end
  - _is_paid() / _is_active() billing gate logic is correct
  - _to_monthly_sgd() salary normalisation is correct
"""
import json
import os
import warnings

# Must be set before importing app — app.py raises RuntimeError otherwise.
os.environ.setdefault("SECRET_KEY", "a" * 32)
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("CRON_SECRET", "test-cron-secret-xyz")

warnings.filterwarnings("ignore", message="REDIS_URL not set")

import pytest

import app as flask_app
from models import User, db
from scrapers import _to_monthly_sgd, _USD_TO_SGD


# ── Fixtures ────────────────────────────────────────────────────────────────────

@pytest.fixture
def client():
    flask_app.app.config.update(
        TESTING=True,
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
        WTF_CSRF_ENABLED=True,
        WTF_CSRF_TIME_LIMIT=None,
    )
    with flask_app.app.test_client() as c:
        with flask_app.app.app_context():
            db.create_all()
            yield c
            db.session.remove()
            db.drop_all()


# ── Helpers ─────────────────────────────────────────────────────────────────────

def _csrf_token(client) -> str:
    """GET /login to seed the session, then return the csrf_token cookie value."""
    client.get("/login")
    cookie = client.get_cookie("csrf_token")
    return cookie.value if cookie else ""


def _post_json(client, path: str, data: dict, *, csrf: str = ""):
    headers = {"Content-Type": "application/json"}
    if csrf:
        headers["X-CSRFToken"] = csrf
    return client.post(path, data=json.dumps(data), headers=headers)


def _register(client, email="user@test.com", password="StrongPass1!"):
    token = _csrf_token(client)
    return _post_json(client, "/api/auth/register",
                      {"email": email, "password": password},
                      csrf=token)


def _login(client, email="user@test.com", password="StrongPass1!"):
    token = _csrf_token(client)
    return _post_json(client, "/api/auth/login",
                      {"email": email, "password": password},
                      csrf=token)


# ── CSRF rejection tests ─────────────────────────────────────────────────────────

def test_login_without_csrf_returns_400(client):
    resp = _post_json(client, "/api/auth/login", {"email": "x@x.com", "password": "x"})
    assert resp.status_code == 400, f"expected CSRF 400, got {resp.status_code}: {resp.data}"


def test_register_without_csrf_returns_400(client):
    resp = _post_json(client, "/api/auth/register", {"email": "x@x.com", "password": "Password1!"})
    assert resp.status_code == 400, f"expected CSRF 400, got {resp.status_code}: {resp.data}"


def test_logout_without_csrf_returns_400(client):
    # Even for logged-in users, CSRF must be present
    _register(client)
    _login(client)
    resp = _post_json(client, "/api/auth/logout", {})
    assert resp.status_code == 400, f"expected CSRF 400, got {resp.status_code}"


# ── CSRF acceptance tests ────────────────────────────────────────────────────────

def test_login_with_csrf_reaches_auth_logic(client):
    """
    With a valid CSRF token the request gets past CSRF validation.
    Bad credentials → 401, proving the endpoint was reached (not rejected at CSRF).
    """
    token = _csrf_token(client)
    assert token, "csrf_token cookie was not set after GET /login"
    resp = _post_json(client, "/api/auth/login",
                      {"email": "nobody@x.com", "password": "wrong"},
                      csrf=token)
    assert resp.status_code == 401, f"expected 401 (bad creds), got {resp.status_code}: {resp.data}"


def test_register_with_csrf_creates_user(client):
    resp = _register(client, email="new@test.com")
    assert resp.status_code == 200, f"register failed: {resp.status_code}: {resp.data}"
    assert resp.get_json().get("ok") is True


def test_duplicate_register_returns_409(client):
    _register(client, email="dup@test.com")
    resp = _register(client, email="dup@test.com")
    # 409 means CSRF was accepted and auth logic ran (not a CSRF rejection)
    assert resp.status_code == 409, f"expected 409 conflict, got {resp.status_code}"


# ── CSRF-exempt endpoints ────────────────────────────────────────────────────────

def test_cron_without_csrf_not_blocked_by_csrf(client):
    """
    /api/cron/scan is @csrf.exempt.
    A request without X-CSRFToken should be stopped by the wrong cron secret (403),
    not by CSRF (400).
    """
    resp = client.post("/api/cron/scan",
                       headers={"X-Cron-Secret": "wrong-secret"})
    assert resp.status_code == 403, f"expected 403 (bad cron secret), got {resp.status_code}"


def test_cron_with_correct_secret_passes_csrf(client):
    """Correct cron secret, no CSRF token — should NOT get 400."""
    resp = client.post("/api/cron/scan",
                       headers={"X-Cron-Secret": "test-cron-secret-xyz"})
    # 200 or 204 — endpoint ran; CSRF didn't block it
    assert resp.status_code != 400, f"cron endpoint blocked by CSRF: {resp.data}"


def test_stripe_webhook_without_csrf_not_blocked(client):
    """
    /api/stripe/webhook is @csrf.exempt.
    Without X-CSRFToken the response must NOT come from CSRF rejection.
    It will 400 because STRIPE_WEBHOOK_SECRET isn't configured in test env,
    but the body should be empty (Stripe's early return), not a CSRF error message.
    """
    resp = client.post("/api/stripe/webhook",
                       data=b"{}",
                       content_type="application/json")
    # CSRF block returns a JSON/text body with "CSRF" in it; Stripe's early
    # return is an empty body.
    if resp.status_code == 400:
        assert b"CSRF" not in resp.data, (
            f"Stripe webhook was blocked by CSRF (not exempt): {resp.data}"
        )


# ── Full auth flow ───────────────────────────────────────────────────────────────

def test_full_register_login_logout_flow(client):
    # Register
    resp = _register(client, email="flow@test.com")
    assert resp.status_code == 200, f"register failed: {resp.data}"

    # Login
    resp = _login(client, email="flow@test.com")
    assert resp.status_code == 200, f"login failed: {resp.data}"
    assert resp.get_json().get("ok") is True

    # Protected endpoint reachable while logged in
    token = _csrf_token(client)
    resp = client.get("/api/auth/me")
    assert resp.status_code == 200, f"/api/auth/me failed: {resp.data}"
    body = resp.get_json()
    assert body.get("email") == "flow@test.com"

    # Logout
    token = _csrf_token(client)
    resp = _post_json(client, "/api/auth/logout", {}, csrf=token)
    assert resp.status_code == 200, f"logout failed: {resp.data}"

    # Protected endpoint now returns 401
    resp = client.get("/api/auth/me")
    assert resp.status_code == 401, f"expected 401 after logout, got {resp.status_code}"


def test_protected_endpoint_requires_login(client):
    """Unauthenticated GET of /api/auth/me returns 401."""
    resp = client.get("/api/auth/me")
    assert resp.status_code == 401


# ── Billing gate unit tests ──────────────────────────────────────────────────────

def test_is_paid_free_user(client):
    with flask_app.app.app_context():
        u = User(email="free@test.com", subscription_status="free")
        assert flask_app._is_paid(u) is False


def test_is_paid_active_user(client):
    with flask_app.app.app_context():
        u = User(email="pro@test.com", subscription_status="active")
        assert flask_app._is_paid(u) is True


def test_is_active_includes_free(client):
    """_is_active() is True for both free and active — used for login guard."""
    with flask_app.app.app_context():
        free = User(email="f@t.com", subscription_status="free")
        paid = User(email="p@t.com", subscription_status="active")
        assert flask_app._is_active(free) is True
        assert flask_app._is_active(paid) is True


# ── Salary normalisation unit tests ─────────────────────────────────────────────

def test_to_monthly_sgd_annual_sgd():
    """Adzuna annual SGD → monthly SGD."""
    assert _to_monthly_sgd(48000) == 4000
    assert _to_monthly_sgd(30000) == 2500
    assert _to_monthly_sgd(96000) == 8000


def test_to_monthly_sgd_already_monthly():
    """Values below 12 000 treated as already monthly."""
    assert _to_monthly_sgd(3500) == 3500
    assert _to_monthly_sgd(5000) == 5000


def test_to_monthly_sgd_annual_usd():
    """RemoteOK annual USD → monthly SGD."""
    monthly = _to_monthly_sgd(80000, fx=_USD_TO_SGD)
    # 80000 / 12 * 1.35 ≈ 9000
    assert 8500 <= monthly <= 9500, f"unexpected: {monthly}"


def test_to_monthly_sgd_none():
    assert _to_monthly_sgd(None) is None
    assert _to_monthly_sgd("") is None
    assert _to_monthly_sgd("N/A") is None


def test_to_monthly_sgd_string_numbers():
    assert _to_monthly_sgd("48000") == 4000
    assert _to_monthly_sgd("3500") == 3500
