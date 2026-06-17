import json

from sqlalchemy.exc import IntegrityError

import app as flask_app
import config
from models import User, UserSettings, db


def _make_user(email="settings@test.com") -> User:
    user = User(email=email, subscription_status="free")
    user.set_password("StrongPass1!")
    db.session.add(user)
    db.session.commit()
    return user


def _post_json(client, path: str, data: dict):
    return client.post(
        path,
        data=json.dumps(data),
        headers={"Content-Type": "application/json"},
    )


def test_get_or_create_settings_creates_defaults_for_new_user(client):
    user = _make_user("settings-create@test.com")

    settings = flask_app._get_or_create_settings(user.id)

    assert settings.user_id == user.id
    assert settings.min_salary == config.SEARCH_CONFIG["min_salary"]
    assert settings.max_salary == config.SEARCH_CONFIG["max_salary"]
    assert settings.min_score_threshold == config.SEARCH_CONFIG["min_score_threshold"]
    assert settings.max_jobs_per_notification == config.SEARCH_CONFIG["max_jobs_per_notification"]
    assert settings.target_titles == config.SEARCH_CONFIG["target_titles"]
    assert db.session.get(UserSettings, user.id) is not None


def test_get_or_create_settings_returns_existing_row(client):
    user = _make_user("settings-existing@test.com")
    existing = UserSettings(
        user_id=user.id,
        min_salary=3300,
        max_salary=5200,
        target_titles=["QA Analyst"],
    )
    db.session.add(existing)
    db.session.commit()

    settings = flask_app._get_or_create_settings(user.id)

    assert settings.user_id == user.id
    assert settings.min_salary == 3300
    assert settings.max_salary == 5200
    assert settings.target_titles == ["QA Analyst"]
    assert UserSettings.query.filter_by(user_id=user.id).count() == 1


def test_get_or_create_settings_rolls_back_and_requeries_on_integrity_error(client, monkeypatch):
    user_id = "race-user-id"
    existing = UserSettings(user_id=user_id, min_salary=3100)
    calls = {"get": 0, "add": 0, "rollback": 0}

    def fake_get(model, key):
        assert model is UserSettings
        assert key == user_id
        calls["get"] += 1
        return None if calls["get"] == 1 else existing

    def fake_add(row):
        assert isinstance(row, UserSettings)
        assert row.user_id == user_id
        calls["add"] += 1

    def fake_commit():
        raise IntegrityError("insert user settings", {}, Exception("duplicate user_id"))

    def fake_rollback():
        calls["rollback"] += 1

    monkeypatch.setattr(db.session, "get", fake_get)
    monkeypatch.setattr(db.session, "add", fake_add)
    monkeypatch.setattr(db.session, "commit", fake_commit)
    monkeypatch.setattr(db.session, "rollback", fake_rollback)

    settings = flask_app._get_or_create_settings(user_id)

    assert settings is existing
    assert calls == {"get": 2, "add": 1, "rollback": 1}


def test_api_config_response_defaults_unchanged_for_new_user(client):
    payload = {"email": "settings-api@test.com", "password": "StrongPass1!"}
    resp = _post_json(client, "/api/auth/register", payload)
    assert resp.status_code == 200, resp.data

    resp = client.get("/api/config")
    assert resp.status_code == 200, resp.data
    data = resp.get_json()

    assert data["min_salary"] == config.SEARCH_CONFIG["min_salary"]
    assert data["max_salary"] == config.SEARCH_CONFIG["max_salary"]
    assert data["min_score_threshold"] == config.SEARCH_CONFIG["min_score_threshold"]
    assert data["max_jobs_per_notification"] == config.SEARCH_CONFIG["max_jobs_per_notification"]
    assert data["target_titles"] == config.SEARCH_CONFIG["target_titles"]
    assert data["preferred_keywords"] == config.SEARCH_CONFIG["preferred_keywords"]
    assert data["negative_keywords"] == config.SEARCH_CONFIG["negative_keywords"]
    assert data["location_keywords"] == config.SEARCH_CONFIG["location_keywords"]
    assert data["preferred_location"] == config.SEARCH_CONFIG["preferred_location"]
