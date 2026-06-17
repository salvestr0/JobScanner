"""
Tests for the resume AI endpoints (paid-tier features) and PDF download.

Gemini HTTP is faked by monkeypatching requests.post. The PDF download
test runs the real reportlab generator in resume_builder.py against a
full profile so the layout code is actually exercised.
"""
import json
import re
import struct
from datetime import date
from io import BytesIO
from pathlib import Path

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


def _pack_u16(buf: bytearray, offset: int, value: int) -> None:
    struct.pack_into("<H", buf, offset, value)


def _pack_u32(buf: bytearray, offset: int, value: int) -> None:
    struct.pack_into("<I", buf, offset, value & 0xFFFFFFFF)


def _directory_entry(name: str, entry_type: int, left: int, right: int,
                     child: int, start_sector: int, stream_size: int) -> bytes:
    entry = bytearray(128)
    encoded_name = (name + "\0").encode("utf-16le")
    entry[:len(encoded_name)] = encoded_name
    _pack_u16(entry, 64, len(encoded_name))
    entry[66] = entry_type
    entry[67] = 1
    _pack_u32(entry, 68, left)
    _pack_u32(entry, 72, right)
    _pack_u32(entry, 76, child)
    _pack_u32(entry, 116, start_sector)
    _pack_u32(entry, 120, stream_size)
    return bytes(entry)


@pytest.fixture
def legacy_doc_bytes():
    # Minimal Word 97-2003 OLE file with WordDocument and 0Table streams.
    sector_size = 512
    free_sector = 0xFFFFFFFF
    end_of_chain = 0xFFFFFFFE
    fat_sector = 0xFFFFFFFD

    header = bytearray(sector_size)
    header[:8] = bytes.fromhex("d0cf11e0a1b11ae1")
    _pack_u16(header, 24, 0x003E)
    _pack_u16(header, 26, 0x0003)
    _pack_u16(header, 28, 0xFFFE)
    _pack_u16(header, 30, 9)
    _pack_u16(header, 32, 6)
    _pack_u32(header, 44, 1)
    _pack_u32(header, 48, 1)
    _pack_u32(header, 56, 4096)
    _pack_u32(header, 60, free_sector)
    _pack_u32(header, 68, free_sector)
    for offset in range(76, sector_size, 4):
        _pack_u32(header, offset, free_sector)
    _pack_u32(header, 76, 0)

    fat = bytearray(b"\xff" * sector_size)
    _pack_u32(fat, 0, fat_sector)
    _pack_u32(fat, 4, end_of_chain)
    for sector in range(2, 9):
        _pack_u32(fat, sector * 4, sector + 1)
    _pack_u32(fat, 9 * 4, end_of_chain)
    for sector in range(10, 17):
        _pack_u32(fat, sector * 4, sector + 1)
    _pack_u32(fat, 17 * 4, end_of_chain)

    directory = bytearray(sector_size)
    directory[0:128] = _directory_entry("Root Entry", 5, free_sector, free_sector, 1, end_of_chain, 0)
    directory[128:256] = _directory_entry("WordDocument", 2, 2, free_sector, free_sector, 2, 4096)
    directory[256:384] = _directory_entry("0Table", 2, free_sector, free_sector, free_sector, 10, 4096)

    word_document = bytearray(4096)
    _pack_u16(word_document, 0, 0xA5EC)
    _pack_u16(word_document, 2, 0x00D9)
    text_offset = 0x200
    text = b"Jayden Tan\rData Analyst\rPython SQL Excel"
    word_document[text_offset:text_offset + len(text)] = text

    plcpcd = bytearray(16)
    _pack_u32(plcpcd, 4, len(text))
    _pack_u32(plcpcd, 10, (text_offset * 2) | 0x40000000)

    table_stream = bytearray(4096)
    table_stream[0] = 0x02
    _pack_u32(table_stream, 1, len(plcpcd))
    table_stream[5:5 + len(plcpcd)] = plcpcd
    _pack_u32(word_document, 0x01A2, 0)
    _pack_u32(word_document, 0x01A6, 5 + len(plcpcd))

    return bytes(header + fat + directory + word_document + table_stream)


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


def test_extract_doc_handles_minimal_legacy_doc(legacy_doc_bytes):
    from resume_parser import extract_text

    text = extract_text(legacy_doc_bytes, "resume.doc")

    assert "Jayden Tan" in text
    assert "Data Analyst" in text
    assert "Python SQL Excel" in text


def test_parse_resume_accepts_real_legacy_doc_file(client, gemini_key, monkeypatch):
    _login(client, email="resume-doc@test.com")
    sample_doc = Path("tests/fixtures/sample_resume.doc")
    captured = {}

    def fake_parse_with_gemini(text, api_key):
        captured["text"] = text
        captured["api_key"] = api_key
        return {"name": "Conway Deng", "email": "test@example.com"}

    monkeypatch.setattr("resume_parser.parse_with_gemini", fake_parse_with_gemini)

    resp = client.post(
        "/api/profile/parse-resume",
        data={"resume": (BytesIO(sample_doc.read_bytes()), "sample_resume.doc")},
        content_type="multipart/form-data",
    )

    assert resp.status_code == 200, resp.data
    assert resp.get_json()["name"] == "Conway Deng"
    assert captured["api_key"] == "test-gemini-key"
    assert "Software Engineering Intern" in captured["text"]
    assert "Conway Deng" in captured["text"]
    assert "test@example.com" in captured["text"]
    assert "中文测试" in captured["text"]
    assert "HYPERLINK" not in captured["text"]
    assert not re.search(r"[ \t]{2,}", captured["text"])


def test_parse_resume_rejects_corrupted_legacy_doc_files(client, gemini_key):
    _login(client, email="resume-doc-corrupt@test.com")

    resp = client.post(
        "/api/profile/parse-resume",
        data={"resume": (BytesIO(b"not a valid ole word document"), "resume.doc")},
        content_type="multipart/form-data",
    )

    assert resp.status_code == 400
    assert "Could not parse .doc file" in resp.get_json()["error"]


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
