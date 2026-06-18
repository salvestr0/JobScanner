"""
Email digest tests for notifier.py.

All HTTP is faked by monkeypatching requests.post — no real Resend calls.
Covers config guards, subject lines, HTML escaping, unsafe-URL handling,
the 20-job cap, match-reason pills (both list and "|"-joined string input —
the latter is what Job.to_dict() produces for the weekly digest), and
Resend error handling.
"""
import pytest

import notifier
from notifier import _reason_list, send_email_digest, send_weekly_digest


class FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


@pytest.fixture
def resend(monkeypatch):
    """Set Resend env vars and capture outgoing email payloads."""
    sent = []

    def fake_post(url, headers=None, json=None, timeout=None):
        sent.append(json)
        return FakeResponse(200)

    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    monkeypatch.setenv("RESEND_FROM", "Test <noreply@test.dev>")
    monkeypatch.setattr(notifier.requests, "post", fake_post)
    return sent


def _job(**overrides):
    job = {
        "title": "Data Analyst",
        "company": "Acme Pte Ltd",
        "location": "Sengkang",
        "source": "MyCareersFuture",
        "url": "https://example.com/job/1",
        "score": 85,
        "salary_min": 3000,
        "salary_max": 4000,
        "match_reasons": ["Title match: Data Analyst", "Skills match: sql"],
    }
    job.update(overrides)
    return job


SETTINGS = {"email_to": "user@test.com"}


# ── _reason_list ────────────────────────────────────────────────────────────────

def test_reason_list_passes_lists_through():
    assert _reason_list(["a", "b"]) == ["a", "b"]


def test_reason_list_splits_db_string():
    # Job.to_dict() returns the "|"-joined DB string; before this helper the
    # weekly digest sliced it as a string and rendered one pill per character
    assert _reason_list("Title match: X | Skills match: y") == \
        ["Title match: X", "Skills match: y"]


def test_reason_list_empty_inputs():
    assert _reason_list(None) == []
    assert _reason_list("") == []
    assert _reason_list([]) == []


# ── send_email_digest guards ────────────────────────────────────────────────────

def test_digest_without_api_key_returns_false(monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.setattr(notifier.requests, "post",
                        lambda *a, **k: pytest.fail("should not call Resend"))
    assert send_email_digest([_job()], SETTINGS) is False


def test_digest_without_recipient_returns_false(resend):
    assert send_email_digest([_job()], {"email_to": ""}) is False
    assert not resend


# ── send_email_digest rendering ─────────────────────────────────────────────────

def test_digest_empty_jobs_sends_no_matches_email(resend):
    assert send_email_digest([], SETTINGS) is True
    payload = resend[0]
    assert payload["subject"] == "CareerScan: No new matches today"
    assert "No new matching jobs" in payload["html"]
    assert payload["to"] == ["user@test.com"]


def test_digest_subject_counts_matches(resend):
    send_email_digest([_job()], SETTINGS)
    send_email_digest([_job(), _job()], SETTINGS)
    assert resend[0]["subject"] == "CareerScan: 1 new match today"
    assert resend[1]["subject"] == "CareerScan: 2 new matches today"


def test_digest_renders_job_row(resend):
    send_email_digest([_job()], SETTINGS)
    html = resend[0]["html"]
    assert "Data Analyst" in html
    assert "Acme Pte Ltd" in html
    assert "$3,000–$4,000/mo" in html
    assert 'href="https://example.com/job/1"' in html
    assert "Title match: Data Analyst" in html  # reason pill


def test_digest_escapes_html_in_job_fields(resend):
    send_email_digest([_job(title="<script>alert(1)</script>")], SETTINGS)
    html = resend[0]["html"]
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_digest_non_http_url_becomes_hash(resend):
    send_email_digest([_job(url="javascript:alert(1)")], SETTINGS)
    html = resend[0]["html"]
    assert "javascript:alert(1)" not in html
    assert 'href="#"' in html


def test_digest_caps_at_20_jobs(resend):
    jobs = [_job(title=f"UniqueRole{i}") for i in range(25)]
    send_email_digest(jobs, SETTINGS)
    html = resend[0]["html"]
    assert "UniqueRole19" in html
    assert "UniqueRole20" not in html


def test_digest_accepts_string_match_reasons(resend):
    send_email_digest([_job(match_reasons="Title match: X | Skills match: y")], SETTINGS)
    html = resend[0]["html"]
    assert "Title match: X" in html
    assert "Skills match: y" in html


# ── send_email_digest failure handling ──────────────────────────────────────────

def test_digest_resend_rejection_returns_false(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    monkeypatch.setattr(notifier.requests, "post",
                        lambda *a, **k: FakeResponse(422, "bad from address"))
    assert send_email_digest([_job()], SETTINGS) is False


def test_digest_network_error_returns_false(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")

    def boom(*a, **k):
        raise ConnectionError("network down")
    monkeypatch.setattr(notifier.requests, "post", boom)
    assert send_email_digest([_job()], SETTINGS) is False


# ── send_weekly_digest ──────────────────────────────────────────────────────────

def test_weekly_digest_without_api_key_returns_false(monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    assert send_weekly_digest("user@test.com", [_job()]) is False


def test_weekly_digest_without_recipient_returns_false(resend):
    assert send_weekly_digest("", [_job()]) is False
    assert not resend


def test_weekly_digest_renders_stats_and_dashboard_link(resend):
    ok = send_weekly_digest("user@test.com", [_job()],
                            base_url="https://app.test", week_total=12)
    assert ok is True
    payload = resend[0]
    html = payload["html"]
    assert payload["subject"] == "Your top job matches this week · CareerScan"
    assert "12 jobs scanned" in html
    assert "1 top match" in html
    assert "https://app.test/app" in html


def test_weekly_digest_renders_pills_from_db_string(resend):
    # The weekly cron passes Job.to_dict(), where match_reasons is a string
    job = _job(match_reasons="Title match: Data Analyst | ✅ Accepts diploma holders")
    send_weekly_digest("user@test.com", [job])
    html = resend[0]["html"]
    assert "Title match: Data Analyst" in html
    assert "✅ Accepts diploma holders" in html
