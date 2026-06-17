"""
Tests for the resume AI endpoints (paid-tier features) and PDF download.

Gemini HTTP is faked by monkeypatching requests.post. The PDF download
test runs the real reportlab generator in resume_builder.py against a
full profile so the layout code is actually exercised.
"""
import json
from datetime import date
from io import BytesIO

import pytest

import config
from models import Job, User, db


def _gemini_response(payload: dict):
    """Build a Gemini-shaped HTTP response whose text is a JSON object."""
    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "candidates": [{"content": {"parts": [{"text": json.dumps(payload)}]}}],
                "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 20,
                                  "totalTokenCount": 30},
            }
    return FakeResponse()


def _login(client, email="resume@test.com", password="StrongPass1!"):
    payload = json.dumps({"email": email, "password": password})
    headers = {"Content-Type": "application/json"}
    assert client.post("/api/auth/register", data=payload, headers=headers).status_code == 200
    assert client.post("/api/auth/login", data=payload, headers=headers).status_code == 200
    return User.query.filter_by(email=email).first()


@pytest.fixture
def gemini_key(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-gemini-key")


PROFILE = {
    "name": "Jayden Tan",
    "experience_summary": "did some data stuff",
    "technical_skills": ["sql", "excel"],
    "soft_skills": ["teamwork"],
    "work_history": [
        {"title": "Intern", "company": "Acme", "period": "2025",
         "summary": "made reports"},
    ],
}


def test_parse_resume_rejects_legacy_doc_files(client, gemini_key):
    _login(client)
    resp = client.post(
        "/api/profile/parse-resume",
        data={"resume": (BytesIO(b"legacy doc bytes"), "resume.doc")},
        content_type="multipart/form-data",
    )

    assert resp.status_code == 400
    assert "Unsupported file type: .doc" in resp.get_json()["error"]


# ── /api/resume/polish ──────────────────────────────────────────────────────────

def test_polish_requires_login(client):
    assert client.post("/api/resume/polish", json={}).status_code == 401


def test_polish_without_any_gemini_key_returns_400(client, monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    _login(client)
    resp = client.post("/api/resume/polish", json={"profile": PROFILE})
    assert resp.status_code == 400
    assert "not configured" in resp.get_json()["error"]


def test_polish_merges_ai_fields_onto_profile(client, gemini_key, monkeypatch):
    _login(client)
    polished = {
        "experience_summary": "Results-driven analyst.",
        "work_history": [{"title": "Intern", "company": "Acme", "period": "2025",
                          "summary": "• Automated reports"}],
        "technical_skills": ["SQL", "Excel"],
        "soft_skills": ["Collaboration"],
    }
    monkeypatch.setattr("requests.post", lambda *a, **k: _gemini_response(polished))

    resp = client.post("/api/resume/polish", json={"profile": PROFILE})
    assert resp.status_code == 200
    merged = resp.get_json()["profile"]
    assert merged["experience_summary"] == "Results-driven analyst."
    assert merged["technical_skills"] == ["SQL", "Excel"]
    assert merged["name"] == "Jayden Tan"  # untouched fields survive the merge


def test_polish_ai_failure_returns_502(client, gemini_key, monkeypatch):
    _login(client)

    def boom(*a, **k):
        raise ConnectionError("gemini down")
    monkeypatch.setattr("requests.post", boom)

    resp = client.post("/api/resume/polish", json={"profile": PROFILE})
    assert resp.status_code == 502


def test_polish_unparseable_ai_output_returns_502(client, gemini_key, monkeypatch):
    _login(client)

    class ProseResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"candidates": [{"content": {"parts": [{"text": "Sure! Here are some tips..."}]}}]}

    monkeypatch.setattr("requests.post", lambda *a, **k: ProseResponse())
    resp = client.post("/api/resume/polish", json={"profile": PROFILE})
    assert resp.status_code == 502


def test_polish_blocked_when_quota_exhausted(client, gemini_key):
    user = _login(client)
    user.ai_calls_today = 5  # free-tier daily limit
    user.ai_calls_reset_date = date.today()
    db.session.commit()

    resp = client.post("/api/resume/polish", json={"profile": PROFILE})
    assert resp.status_code == 429
    assert resp.get_json()["quota_exceeded"] is True


# ── /api/resume/tailor ──────────────────────────────────────────────────────────

def test_tailor_requires_job_id(client, gemini_key):
    _login(client)
    resp = client.post("/api/resume/tailor", json={})
    assert resp.status_code == 400


def test_tailor_unknown_job_returns_404(client, gemini_key):
    _login(client)
    resp = client.post("/api/resume/tailor", json={"job_id": "nope"})
    assert resp.status_code == 404


def test_tailor_cannot_use_another_users_job(client, gemini_key):
    other = User(email="other@test.com")
    db.session.add(other)
    db.session.commit()
    db.session.add(Job(user_id=other.id, source_job_id="mcf_42", title="DA"))
    db.session.commit()

    _login(client)
    resp = client.post("/api/resume/tailor", json={"job_id": "mcf_42"})
    assert resp.status_code == 404


def test_tailor_returns_suggestions_for_own_job(client, gemini_key, monkeypatch):
    user = _login(client)
    db.session.add(Job(user_id=user.id, source_job_id="mcf_1",
                       title="Data Analyst", company="Acme"))
    db.session.commit()

    tailored = {
        "tailored_summary": "Analyst with Acme-relevant skills.",
        "work_history": [],
        "skills_to_highlight": ["sql"],
        "tips": ["Mention dashboards"],
    }
    monkeypatch.setattr("requests.post", lambda *a, **k: _gemini_response(tailored))

    resp = client.post("/api/resume/tailor", json={"job_id": "mcf_1"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["job"] == {"title": "Data Analyst", "company": "Acme"}
    assert body["tailored"]["skills_to_highlight"] == ["sql"]


# ── /api/resume/download ────────────────────────────────────────────────────────

FULL_PROFILE = {
    "name": "Jayden Tan",
    "email": "jayden@test.sg",
    "phone": "+65 9123 4567",
    "education": "Diploma in AI & Infocomm, Temasek Poly",
    "experience_summary": "Aspiring data analyst with hands-on internship experience.",
    "technical_skills": ["SQL", "Excel", "Python"],
    "soft_skills": ["Teamwork", "Communication"],
    "work_history": [
        {"title": "Data Intern", "company": "Acme", "period": "2025",
         "summary": "• Built dashboards\n• Automated weekly reports"},
    ],
    "projects": [{"name": "JobScanner", "description": "A job matching SaaS."}],
    "certifications": ["Google Data Analytics"],
}


def test_download_generates_real_pdf(client):
    _login(client)
    resp = client.post("/api/resume/download", json=FULL_PROFILE)
    assert resp.status_code == 200
    assert resp.headers["Content-Type"] == "application/pdf"
    assert 'filename="jayden_tan_resume.pdf"' in resp.headers["Content-Disposition"]
    assert resp.data.startswith(b"%PDF")


def test_download_backfills_email_from_account(client, monkeypatch):
    user = _login(client)
    captured = {}

    def fake_pdf(profile):
        captured.update(profile)
        return b"%PDF-fake"

    import resume_builder
    monkeypatch.setattr(resume_builder, "generate_pdf", fake_pdf)

    resp = client.post("/api/resume/download", json={"name": "No Email"})
    assert resp.status_code == 200
    assert captured["email"] == user.email


def test_download_pdf_failure_returns_500(client, monkeypatch):
    _login(client)
    import resume_builder

    def boom(profile):
        raise RuntimeError("layout exploded")
    monkeypatch.setattr(resume_builder, "generate_pdf", boom)

    resp = client.post("/api/resume/download", json={"name": "X"})
    assert resp.status_code == 500
