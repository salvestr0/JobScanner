"""
Shared fixtures for the test suite.

Env vars must be set before `app` is imported, and pytest imports conftest
before any test module, so this is the safe place to do it.
"""
import os
import warnings

os.environ.setdefault("SECRET_KEY", "a" * 32)
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
# Forced (not setdefault): the cron/billing tests assert against this exact
# value, so a stray CRON_SECRET from the CI env must not override it.
os.environ["CRON_SECRET"] = "test-cron-secret-xyz"

warnings.filterwarnings("ignore", message="REDIS_URL not set")
warnings.filterwarnings("ignore", message="Fernet")

import pytest

import app as flask_app
from models import db


@pytest.fixture
def client():
    flask_app.app.config.update(
        TESTING=True,
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
    )
    # RATELIMIT_ENABLED in config is read at init_app time (import), so it
    # can't disable the limiter here — the in-memory counters would otherwise
    # accumulate across tests and 429 strict limits like delete-account's 3/hour.
    flask_app.limiter.enabled = False
    with flask_app.app.test_client() as c:
        with flask_app.app.app_context():
            db.create_all()
            yield c
            db.session.remove()
            db.drop_all()
