"""
Tests for config.py: per-user config loading (load_user_config) and
search-mode switching (set_mode). Gemini generation and the modes cache
are faked; module globals are snapshotted and restored around every test
so mutations can't leak into other test files.
"""
import json

import pytest

import config


@pytest.fixture(autouse=True)
def restore_config():
    profile = dict(config.PROFILE)
    search = dict(config.SEARCH_CONFIG)
    key = config.GEMINI_API_KEY
    yield
    config.PROFILE.clear()
    config.PROFILE.update(profile)
    config.SEARCH_CONFIG.clear()
    config.SEARCH_CONFIG.update(search)
    config.GEMINI_API_KEY = key


# ── load_user_config ────────────────────────────────────────────────────────────

def test_invalid_json_is_ignored():
    before = dict(config.SEARCH_CONFIG)
    config.load_user_config("{not valid json")
    assert config.SEARCH_CONFIG == before


def test_overrides_all_sections():
    config.load_user_config(json.dumps({
        "gemini_api_key": "user-key",
        "profile": {"name": "Jayden"},
        "search_config": {"min_salary": 2500},
    }))
    assert config.GEMINI_API_KEY == "user-key"
    assert config.PROFILE["name"] == "Jayden"
    assert config.SEARCH_CONFIG["min_salary"] == 2500


def test_partial_payload_leaves_other_sections_alone():
    before_profile = dict(config.PROFILE)
    config.load_user_config(json.dumps({"search_config": {"max_salary": 5000}}))
    assert config.SEARCH_CONFIG["max_salary"] == 5000
    assert config.PROFILE == before_profile


# ── set_mode ────────────────────────────────────────────────────────────────────

def test_analyst_mode_is_a_noop():
    before = dict(config.SEARCH_CONFIG)
    config.set_mode("analyst")
    assert config.SEARCH_CONFIG == before


def test_cached_mode_replaces_search_config(monkeypatch):
    cached = {"target_titles": ["staff nurse"], "preferred_keywords": ["icu"]}
    monkeypatch.setattr(config, "_load_modes_cache", lambda: {"healthcare": cached})
    config.set_mode("healthcare")
    assert config.SEARCH_CONFIG["target_titles"] == ["staff nurse"]
    assert "min_salary" not in config.SEARCH_CONFIG  # clear() then update()


def test_generation_failure_keeps_existing_config(monkeypatch):
    monkeypatch.setattr(config, "_load_modes_cache", lambda: {})
    monkeypatch.setattr(config, "_generate_mode_via_gemini", lambda mode: None)
    before = dict(config.SEARCH_CONFIG)
    config.set_mode("astronaut")
    assert config.SEARCH_CONFIG == before


def test_refresh_regenerates_and_saves_cache(monkeypatch):
    generated = {"target_titles": ["vet tech"], "preferred_keywords": ["animal care"]}
    saved = []
    monkeypatch.setattr(config, "_load_modes_cache",
                        lambda: {"vet": {"target_titles": ["stale cached titles"]}})
    monkeypatch.setattr(config, "_save_modes_cache", lambda cache: saved.append(cache))
    monkeypatch.setattr(config, "_generate_mode_via_gemini", lambda mode: generated)

    config.set_mode("vet", refresh=True)

    assert config.SEARCH_CONFIG["target_titles"] == ["vet tech"]
    assert saved and saved[0]["vet"] == generated
