"""
Tests for the MyCareersFuture fetcher and parsing helpers in scrapers.py.

All HTTP is faked by monkeypatching requests.get — no real MCF traffic.
time.sleep is patched out so the suite stays fast.

Covers:
  - Field parsing of a full MCF job (annual→monthly salary, HTML stripping,
    relative URL expansion, company/location extraction)
  - Alternate response shapes ("results" vs "jobs" key)
  - Deduplication by job id and the md5 fallback when uuid is missing
  - Fallback endpoints on 429, total failure, and timeouts
  - max_results early stop and the no-titles guard
  - scrape_all_sources dedup
  - _parse_salary / _clean_html edge cases
"""
import pytest
import requests

import scrapers
from scrapers import _clean_html, _parse_salary, fetch_mcf


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"HTTP {self.status_code}")


def _mcf_job(uuid="abc123", **overrides):
    job = {
        "uuid": uuid,
        "title": "Data Analyst",
        "description": "<p>Analyse <b>data</b> daily</p>",
        "postedCompany": {"name": "Acme Pte Ltd"},
        "salary": {"minimum": 48000, "maximum": 60000, "type": {"salaryType": "Annual"}},
        "address": {"streetAddress": "1 Raffles Place"},
        "metadata": {
            "newPostingDate": "2026-06-01",
            "closingDate": "2026-07-01",
            "jobDetailsUrl": "/job/abc123",
        },
    }
    job.update(overrides)
    return job


@pytest.fixture
def mcf_env(monkeypatch):
    monkeypatch.setitem(scrapers.SEARCH_CONFIG, "target_titles", ["data analyst"])
    monkeypatch.setattr(scrapers.time, "sleep", lambda s: None)


# ── fetch_mcf parsing ───────────────────────────────────────────────────────────

def test_fetch_mcf_parses_job_fields(mcf_env, monkeypatch):
    monkeypatch.setattr(scrapers.requests, "get",
                        lambda *a, **k: FakeResponse(200, {"results": [_mcf_job()]}))
    jobs = fetch_mcf(max_pages=1)
    assert len(jobs) == 1
    j = jobs[0]
    assert j["id"] == "mcf_abc123"
    assert j["title"] == "Data Analyst"
    assert j["company"] == "Acme Pte Ltd"
    assert "<" not in j["description"] and "Analyse" in j["description"]
    assert j["salary_min"] == 4000   # 48000/year → monthly
    assert j["salary_max"] == 5000
    assert j["location"] == "1 Raffles Place"
    assert j["url"] == "https://www.mycareersfuture.gov.sg/job/abc123"
    assert j["posted_date"] == "2026-06-01"
    assert j["closing_date"] == "2026-07-01"
    assert j["source"] == "MyCareersFuture"


def test_fetch_mcf_monthly_salary_not_divided(mcf_env, monkeypatch):
    job = _mcf_job(salary={"minimum": 3000, "maximum": 4000,
                           "type": {"salaryType": "Monthly"}})
    monkeypatch.setattr(scrapers.requests, "get",
                        lambda *a, **k: FakeResponse(200, {"results": [job]}))
    j = fetch_mcf(max_pages=1)[0]
    assert j["salary_min"] == 3000
    assert j["salary_max"] == 4000


def test_fetch_mcf_accepts_jobs_response_key(mcf_env, monkeypatch):
    monkeypatch.setattr(scrapers.requests, "get",
                        lambda *a, **k: FakeResponse(200, {"jobs": [_mcf_job()]}))
    assert len(fetch_mcf(max_pages=1)) == 1


def test_fetch_mcf_dedupes_same_uuid(mcf_env, monkeypatch):
    monkeypatch.setattr(scrapers.requests, "get",
                        lambda *a, **k: FakeResponse(200, {"results": [_mcf_job(), _mcf_job()]}))
    assert len(fetch_mcf(max_pages=1)) == 1


def test_fetch_mcf_missing_uuid_gets_md5_fallback_id(mcf_env, monkeypatch):
    monkeypatch.setattr(scrapers.requests, "get",
                        lambda *a, **k: FakeResponse(200, {"results": [_mcf_job(uuid=None)]}))
    j = fetch_mcf(max_pages=1)[0]
    assert j["id"].startswith("mcf_")
    assert len(j["id"]) == len("mcf_") + 12


def test_fetch_mcf_skips_non_dict_results(mcf_env, monkeypatch):
    monkeypatch.setattr(scrapers.requests, "get",
                        lambda *a, **k: FakeResponse(200, {"results": ["junk", _mcf_job()]}))
    assert len(fetch_mcf(max_pages=1)) == 1


# ── fetch_mcf failure handling ──────────────────────────────────────────────────

def test_fetch_mcf_falls_back_to_secondary_endpoint_on_429(mcf_env, monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        if url.endswith("/v2/jobs"):
            return FakeResponse(429)
        return FakeResponse(200, {"results": [_mcf_job()]})

    monkeypatch.setattr(scrapers.requests, "get", fake_get)
    jobs = fetch_mcf(max_pages=1)
    assert len(jobs) == 1
    assert any("/v2/search" in u for u in calls)


def test_fetch_mcf_returns_empty_when_all_endpoints_fail(mcf_env, monkeypatch):
    monkeypatch.setattr(scrapers.requests, "get", lambda *a, **k: FakeResponse(429))
    assert fetch_mcf(max_pages=1) == []


def test_fetch_mcf_survives_timeout(mcf_env, monkeypatch):
    def boom(*a, **k):
        raise requests.exceptions.Timeout()
    monkeypatch.setattr(scrapers.requests, "get", boom)
    assert fetch_mcf(max_pages=1) == []


# ── fetch_mcf limits ────────────────────────────────────────────────────────────

def test_fetch_mcf_no_titles_makes_no_requests(monkeypatch):
    monkeypatch.setitem(scrapers.SEARCH_CONFIG, "target_titles", [])
    called = []
    monkeypatch.setattr(scrapers.requests, "get",
                        lambda *a, **k: called.append(1) or FakeResponse(200))
    assert fetch_mcf() == []
    assert not called


def test_fetch_mcf_max_results_stops_before_second_title(monkeypatch):
    monkeypatch.setitem(scrapers.SEARCH_CONFIG, "target_titles", ["first title", "second title"])
    monkeypatch.setattr(scrapers.time, "sleep", lambda s: None)
    searched = []

    def fake_get(url, params=None, **kwargs):
        searched.append(params["search"])
        return FakeResponse(200, {"results": [_mcf_job(uuid=params["search"])]})

    monkeypatch.setattr(scrapers.requests, "get", fake_get)
    jobs = fetch_mcf(max_pages=1, max_results=1)
    assert len(jobs) == 1
    assert searched == ["first title"]


def test_scrape_all_sources_dedupes_across_results(monkeypatch):
    monkeypatch.setattr(scrapers, "fetch_mcf",
                        lambda **k: [{"id": "x"}, {"id": "x"}, {"id": "y"}])
    jobs = scrapers.scrape_all_sources()
    assert [j["id"] for j in jobs] == ["x", "y"]


# ── Parsing helpers ─────────────────────────────────────────────────────────────

def test_parse_salary_strips_currency_formatting():
    assert _parse_salary("$3,500") == 3500
    assert _parse_salary("4000") == 4000
    assert _parse_salary(4000) == 4000


def test_parse_salary_invalid_returns_none():
    assert _parse_salary(None) is None
    assert _parse_salary("negotiable") is None


def test_clean_html_strips_tags():
    assert _clean_html("<p>Hello <b>world</b></p>").split() == ["Hello", "world"]


def test_clean_html_passthrough_and_empty():
    assert _clean_html("plain text") == "plain text"
    assert _clean_html("") == ""
