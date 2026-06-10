"""
Shared fixtures for the test suite.

Env vars must be set before `app` is imported, and pytest imports conftest
before any test module, so this is the safe place to do it.
"""
import os
import warnings

os.environ.setdefault("SECRET_KEY", "a" * 32)
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("CRON_SECRET", "test-cron-secret-xyz")

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
        RATELIMIT_ENABLED=False,
    )
    with flask_app.app.test_client() as c:
        with flask_app.app.app_context():
            db.create_all()
            yield c
            db.session.remove()
            db.drop_all()
