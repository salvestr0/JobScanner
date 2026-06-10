"""
Stripe webhook and AI quota tests.

Webhook tests use real Stripe-signed payloads (t=...,v1=hmac-sha256) so the
actual signature verification path in stripe.Webhook.construct_event runs —
no mocking of the Stripe library:
  - Missing configured secret / bad signature / stale timestamp → 400
  - subscription created/updated/deleted and invoice.payment_failed map to
    the right subscription_status
  - Unknown customer, missing customer field, unhandled event types → 200
    without side effects

Quota tests cover _check_ai_quota: per-plan daily limits, the midnight
rollover reset, admin exemption, and the default limit for unknown statuses.
"""
import hashlib
import hmac
import json
import time
from datetime import date, timedelta

import app as flask_app
from models import User, db

WEBHOOK_SECRET = "whsec_test_secret"


# ── Helpers ─────────────────────────────────────────────────────────────────────

def _signed_headers(payload: bytes, secret: str = WEBHOOK_SECRET, ts: int = None) -> dict:
    """Build a valid Stripe-Signature header for the payload."""
    ts = ts or int(time.time())
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return {"Stripe-Signature": f"t={ts},v1={mac}", "Content-Type": "application/json"}


def _event_payload(event_type: str, obj: dict) -> bytes:
    return json.dumps({
        "id": "evt_test_1",
        "object": "event",
        "api_version": "2024-06-20",
        "type": event_type,
        "data": {"object": obj},
    }).encode()


def _create_user(status="free", customer_id="cus_test_1", **extra) -> str:
    user = User(
        email=f"{customer_id}@test.com",
        stripe_customer_id=customer_id,
        subscription_status=status,
        **extra,
    )
    db.session.add(user)
    db.session.commit()
    return user.id


def _post_webhook(client, payload: bytes, headers: dict):
    return client.post("/api/stripe/webhook", data=payload, headers=headers)


def _status_of(user_id: str) -> str:
    return db.session.get(User, user_id).subscription_status


# ── Webhook security ────────────────────────────────────────────────────────────

def test_webhook_without_configured_secret_returns_400(client, monkeypatch):
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    resp = client.post("/api/stripe/webhook", data=b"{}")
    assert resp.status_code == 400


def test_webhook_bad_signature_returns_400(client, monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", WEBHOOK_SECRET)
    uid = _create_user(status="free")
    payload = _event_payload("customer.subscription.updated",
                             {"customer": "cus_test_1", "status": "active"})
    resp = _post_webhook(client, payload,
                         {"Stripe-Signature": "t=1,v1=deadbeef",
                          "Content-Type": "application/json"})
    assert resp.status_code == 400
    assert _status_of(uid) == "free"


def test_webhook_stale_timestamp_returns_400(client, monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", WEBHOOK_SECRET)
    uid = _create_user(status="free")
    payload = _event_payload("customer.subscription.updated",
                             {"customer": "cus_test_1", "status": "active"})
    stale = int(time.time()) - 3600  # outside Stripe's 300s tolerance
    resp = _post_webhook(client, payload, _signed_headers(payload, ts=stale))
    assert resp.status_code == 400
    assert _status_of(uid) == "free"


# ── Webhook status transitions ──────────────────────────────────────────────────

def test_subscription_updated_activates_user(client, monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", WEBHOOK_SECRET)
    uid = _create_user(status="free")
    payload = _event_payload("customer.subscription.updated",
                             {"customer": "cus_test_1", "status": "active"})
    resp = _post_webhook(client, payload, _signed_headers(payload))
    assert resp.status_code == 200
    assert _status_of(uid) == "active"


def test_subscription_created_activates_user(client, monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", WEBHOOK_SECRET)
    uid = _create_user(status="free")
    payload = _event_payload("customer.subscription.created",
                             {"customer": "cus_test_1", "status": "active"})
    resp = _post_webhook(client, payload, _signed_headers(payload))
    assert resp.status_code == 200
    assert _status_of(uid) == "active"


def test_subscription_updated_propagates_non_active_status(client, monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", WEBHOOK_SECRET)
    uid = _create_user(status="active")
    payload = _event_payload("customer.subscription.updated",
                             {"customer": "cus_test_1", "status": "past_due"})
    resp = _post_webhook(client, payload, _signed_headers(payload))
    assert resp.status_code == 200
    assert _status_of(uid) == "past_due"


def test_subscription_deleted_cancels_user(client, monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", WEBHOOK_SECRET)
    uid = _create_user(status="active")
    payload = _event_payload("customer.subscription.deleted",
                             {"customer": "cus_test_1", "status": "canceled"})
    resp = _post_webhook(client, payload, _signed_headers(payload))
    assert resp.status_code == 200
    assert _status_of(uid) == "cancelled"


def test_payment_failed_marks_past_due(client, monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", WEBHOOK_SECRET)
    uid = _create_user(status="active")
    payload = _event_payload("invoice.payment_failed", {"customer": "cus_test_1"})
    resp = _post_webhook(client, payload, _signed_headers(payload))
    assert resp.status_code == 200
    assert _status_of(uid) == "past_due"


# ── Webhook no-op paths ─────────────────────────────────────────────────────────

def test_unknown_customer_returns_200(client, monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", WEBHOOK_SECRET)
    payload = _event_payload("customer.subscription.updated",
                             {"customer": "cus_nobody", "status": "active"})
    resp = _post_webhook(client, payload, _signed_headers(payload))
    assert resp.status_code == 200


def test_event_without_customer_returns_200(client, monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", WEBHOOK_SECRET)
    payload = _event_payload("invoice.payment_failed", {"id": "in_123"})
    resp = _post_webhook(client, payload, _signed_headers(payload))
    assert resp.status_code == 200


def test_unhandled_event_type_leaves_status_unchanged(client, monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", WEBHOOK_SECRET)
    uid = _create_user(status="free")
    payload = _event_payload("checkout.session.completed", {"customer": "cus_test_1"})
    resp = _post_webhook(client, payload, _signed_headers(payload))
    assert resp.status_code == 200
    assert _status_of(uid) == "free"


# ── AI quota (_check_ai_quota) ──────────────────────────────────────────────────

def _quota_user(status="free", admin=False, calls=0, reset=None, n=1) -> User:
    user = User(
        email=f"quota{n}@test.com",
        subscription_status=status,
        is_admin=admin,
        ai_calls_today=calls,
        ai_calls_reset_date=reset if reset is not None else date.today(),
    )
    db.session.add(user)
    db.session.commit()
    return user


def test_quota_free_user_allowed_and_incremented(client):
    user = _quota_user(status="free", calls=0)
    allowed, err = flask_app._check_ai_quota(user)
    assert allowed is True and err is None
    assert user.ai_calls_today == 1


def test_quota_free_user_blocked_at_limit(client):
    user = _quota_user(status="free", calls=5)
    allowed, err = flask_app._check_ai_quota(user)
    assert allowed is False
    assert "limit" in err.lower()
    assert user.ai_calls_today == 5  # not incremented past the cap


def test_quota_active_user_has_higher_limit(client):
    user = _quota_user(status="active", calls=5)
    allowed, _ = flask_app._check_ai_quota(user)
    assert allowed is True


def test_quota_active_user_blocked_at_30(client):
    user = _quota_user(status="active", calls=30)
    allowed, _ = flask_app._check_ai_quota(user)
    assert allowed is False


def test_quota_cancelled_user_has_no_ai_access(client):
    user = _quota_user(status="cancelled", calls=0)
    allowed, err = flask_app._check_ai_quota(user)
    assert allowed is False
    assert "unavailable" in err.lower()


def test_quota_admin_is_unlimited(client):
    user = _quota_user(status="free", admin=True, calls=999)
    allowed, err = flask_app._check_ai_quota(user)
    assert allowed is True and err is None


def test_quota_counter_resets_on_new_day(client):
    yesterday = date.today() - timedelta(days=1)
    user = _quota_user(status="free", calls=5, reset=yesterday)
    allowed, _ = flask_app._check_ai_quota(user)
    assert allowed is True
    assert user.ai_calls_today == 1
    assert user.ai_calls_reset_date == date.today()


def test_quota_unknown_status_defaults_to_free_limit(client):
    user = _quota_user(status="mystery_plan", calls=5)
    allowed, _ = flask_app._check_ai_quota(user)
    assert allowed is False
