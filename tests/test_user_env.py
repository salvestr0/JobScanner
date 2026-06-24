"""
Tests for _build_user_env — the per-user config handed to scan runs.

Covers the free-tier 10-result cap (and the admin/active exemptions),
Gemini key decryption into the scan config, profile email backfill, and
settings propagation into search_config.
"""
import json

import app as flask_app
from models import User, UserProfile, UserSettings, db


def _make_user(status="free", admin=False, n=1, **extra) -> User:
    user = User(email=f"env{n}@test.com", subscription_status=status,
                is_admin=admin, **extra)
    db.session.add(user)
    db.session.commit()
    return user


def _user_cfg(env: dict) -> dict:
    return json.loads(env["JOBSCANNER_USER_CONFIG"])


def test_free_tier_gets_10_job_cap(client):
    env = flask_app._build_user_env(_make_user(status="free"))
    assert env["JOBSCANNER_MAX_JOBS"] == "10"


def test_active_subscriber_is_uncapped(client):
    env = flask_app._build_user_env(_make_user(status="active"))
    assert "JOBSCANNER_MAX_JOBS" not in env


def test_admin_is_uncapped_even_on_free_plan(client):
    env = flask_app._build_user_env(_make_user(status="free", admin=True))
    assert "JOBSCANNER_MAX_JOBS" not in env


def test_data_dir_is_per_user(client):
    user = _make_user()
    env = flask_app._build_user_env(user)
    assert env["JOBSCANNER_DATA_DIR"] == f"data/users/{user.id}"


def test_gemini_key_is_decrypted_into_config(client, monkeypatch):
    from cryptography.fernet import Fernet
    f = Fernet(Fernet.generate_key())
    monkeypatch.setattr(flask_app, "_fernet", f)
    user = _make_user(gemini_api_key=f.encrypt(b"user-key").decode())
    cfg = _user_cfg(flask_app._build_user_env(user))
    assert cfg["gemini_api_key"] == "user-key"


def test_profile_email_backfilled_from_account(client):
    user = _make_user()
    db.session.add(UserProfile(user_id=user.id, name="Jayden", email=""))
    db.session.commit()
    cfg = _user_cfg(flask_app._build_user_env(user))
    assert cfg["profile"]["email"] == user.email


def test_settings_propagate_to_search_config(client):
    user = _make_user()
    db.session.add(UserSettings(
        user_id=user.id,
        min_salary=2500,
        max_salary=4500,
        job_region="my",
        target_titles=["data analyst"],
        negative_keywords=["mlm"],
        email_to="digest@test.com",
    ))
    db.session.commit()
    sc = _user_cfg(flask_app._build_user_env(user))["search_config"]
    assert sc["min_salary"] == 2500
    assert sc["max_salary"] == 4500
    assert sc["job_region"] == "my"
    assert sc["target_titles"] == ["data analyst"]
    assert sc["negative_keywords"] == ["mlm"]
    assert sc["email_to"] == "digest@test.com"


def test_user_without_settings_or_profile_still_builds_env(client):
    cfg = _user_cfg(flask_app._build_user_env(_make_user()))
    assert "search_config" not in cfg
    assert "profile" not in cfg
