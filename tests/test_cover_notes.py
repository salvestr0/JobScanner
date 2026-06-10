"""
Cover note generation tests for cover_notes.py.

Covers the template fallback (no Gemini key), skill detection from the job
description, work-history sentence normalisation, the AI path with faked
HTTP, the AI-failure fallback to the template, and save_cover_note filename
sanitisation.
"""
import pytest

import cover_notes
from cover_notes import (
    _generate_template_cover_note,
    generate_cover_note,
    save_cover_note,
)

PROFILE = {
    "name": "Jayden Tan",
    "email": "jayden@test.sg",
    "phone": "+65 9123 4567",
    "education": "Diploma in AI & Infocomm",
    "technical_skills": ["SQL", "Excel", "Python"],
    "work_history": [
        {"title": "Data Intern", "company": "Acme", "period": "2025",
         "summary": "I built dashboards for the operations team. Also did ad-hoc reports."},
    ],
    "projects": [
        {"name": "JobScanner", "description": "A job matching tool."},
    ],
    "experience_summary": "Hands-on data work during internship.",
}


def _job(**overrides):
    job = {
        "title": "Junior Data Analyst",
        "company": "DBS Bank",
        "description": "Looking for someone with sql and excel skills.",
        "match_reasons": ["Title match: Data Analyst"],
        "score": 85,
        "url": "https://example.com/job/123",
    }
    job.update(overrides)
    return job


# ── Template path ───────────────────────────────────────────────────────────────

def test_no_api_key_falls_back_to_template(monkeypatch):
    monkeypatch.setattr(cover_notes, "GEMINI_API_KEY", "")
    monkeypatch.setattr(cover_notes.requests, "post",
                        lambda *a, **k: pytest.fail("should not call Gemini"))
    note = generate_cover_note(_job(), api_key=None, profile=PROFILE)
    assert "Junior Data Analyst" in note
    assert "DBS Bank" in note


def test_template_detects_skills_from_description():
    note = _generate_template_cover_note(_job(), PROFILE)
    assert "SQL for database querying" in note
    assert "Microsoft Excel" in note


def test_template_falls_back_to_profile_skills():
    job = _job(description="A role with no recognisable tool keywords.")
    note = _generate_template_cover_note(job, PROFILE)
    assert "SQL, Excel, Python" in note


def test_template_uses_recent_work_history():
    note = _generate_template_cover_note(_job(), PROFILE)
    assert "as Data Intern at Acme" in note
    # "I built..." summary is normalised without doubling the "I"
    assert "I built dashboards for the operations team" in note
    assert "I i built" not in note.lower()


def test_template_without_history_uses_generic_line():
    bare = {**PROFILE, "work_history": [], "experience_summary": "", "projects": []}
    note = _generate_template_cover_note(_job(), bare)
    assert "desire to learn" in note


def test_template_signs_off_with_contact_details():
    note = _generate_template_cover_note(_job(), PROFILE)
    assert "Jayden Tan" in note
    assert "jayden@test.sg" in note
    assert "+65 9123 4567" in note


def test_template_mentions_project():
    note = _generate_template_cover_note(_job(), PROFILE)
    assert "JobScanner" in note


# ── AI path ─────────────────────────────────────────────────────────────────────

class FakeGeminiResponse:
    def __init__(self, text="Dear team, here is my AI note."):
        self._text = text

    def raise_for_status(self):
        pass

    def json(self):
        return {"candidates": [{"content": {"parts": [{"text": self._text}]}}]}


def test_api_key_uses_gemini(monkeypatch):
    monkeypatch.setattr(cover_notes.requests, "post",
                        lambda *a, **k: FakeGeminiResponse("AI-generated note"))
    note = generate_cover_note(_job(), api_key="test-key", profile=PROFILE)
    assert note == "AI-generated note"


def test_gemini_failure_falls_back_to_template(monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("Gemini down")
    monkeypatch.setattr(cover_notes.requests, "post", boom)
    note = generate_cover_note(_job(), api_key="test-key", profile=PROFILE)
    assert "DBS Bank" in note  # template content, not a crash


# ── save_cover_note ─────────────────────────────────────────────────────────────

def test_save_cover_note_writes_file(tmp_path):
    path = save_cover_note(_job(), "note body", output_dir=str(tmp_path))
    content = open(path).read()
    assert "Junior Data Analyst" in content
    assert "note body" in content
    assert "85/100" in content


def test_save_cover_note_sanitises_filename(tmp_path):
    job = _job(company="Acme, Pte. Ltd!", title="Data/Analyst (Junior)")
    path = save_cover_note(job, "note", output_dir=str(tmp_path))
    filename = path.rsplit("/", 1)[-1]
    assert filename.endswith(".txt")
    for ch in ",.!/()":
        assert ch not in filename.removesuffix(".txt")
    assert " " not in filename
