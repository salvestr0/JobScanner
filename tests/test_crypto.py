"""
Tests for the Gemini API key encryption helpers in app.py.

Covers the Fernet round trip, the plaintext passthrough when ENCRYPTION_KEY
is not configured, and the InvalidToken fallback that returns the raw value
instead of crashing when the key doesn't match.
"""
import pytest
from cryptography.fernet import Fernet

import app as flask_app


@pytest.fixture
def fernet(monkeypatch):
    f = Fernet(Fernet.generate_key())
    monkeypatch.setattr(flask_app, "_fernet", f)
    return f


def test_encrypt_decrypt_round_trip(fernet):
    token = flask_app._encrypt_api_key("my-gemini-key")
    assert token != "my-gemini-key"  # actually encrypted, not stored as-is
    assert flask_app._decrypt_api_key(token) == "my-gemini-key"


def test_empty_value_passes_through(fernet):
    assert flask_app._encrypt_api_key("") == ""
    assert flask_app._decrypt_api_key("") == ""


def test_no_encryption_key_stores_plaintext(monkeypatch):
    monkeypatch.setattr(flask_app, "_fernet", None)
    assert flask_app._encrypt_api_key("my-gemini-key") == "my-gemini-key"
    assert flask_app._decrypt_api_key("my-gemini-key") == "my-gemini-key"


@pytest.mark.filterwarnings("ignore:Fernet decryption failed")
def test_wrong_key_returns_raw_value_instead_of_crashing(fernet):
    other = Fernet(Fernet.generate_key())
    token = other.encrypt(b"secret").decode()
    assert flask_app._decrypt_api_key(token) == token


@pytest.mark.filterwarnings("ignore:Fernet decryption failed")
def test_garbage_ciphertext_returns_raw_value(fernet):
    assert flask_app._decrypt_api_key("not-a-fernet-token") == "not-a-fernet-token"
