"""
Resume parser — extracts text from PDF/DOCX/TXT and uses Gemini to
return a structured profile dict matching data/profile.json schema.
"""
import io
import json
import re

import requests

GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"

PROFILE_SCHEMA = """
{
  "name": "string",
  "email": "string",
  "phone": "string",
  "education": "string (highest qualification, school, year)",
  "certifications": ["list of certification strings"],
  "technical_skills": ["list of technical skill strings"],
  "soft_skills": ["list of soft skill strings"],
  "experience_summary": "2-3 sentence professional summary",
  "work_history": [
    {"title": "string", "company": "string", "period": "string", "summary": "string"}
  ],
  "projects": [
    {"name": "string", "description": "string"}
  ]
}
"""


def extract_text(file_bytes: bytes, filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower()

    if ext == "pdf":
        return _extract_pdf(file_bytes)
    elif ext in ("docx", "doc"):
        return _extract_docx(file_bytes)
    elif ext == "txt":
        return file_bytes.decode("utf-8", errors="replace")
    else:
        raise ValueError(f"Unsupported file type: .{ext}")


def _extract_pdf(data: bytes) -> str:
    try:
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(data))
        pages = [p.extract_text() or "" for p in reader.pages]
        return "\n".join(pages)
    except ImportError:
        raise ImportError("pypdf not installed — run: pip install pypdf")


def _extract_docx(data: bytes) -> str:
    try:
        import docx
        doc = docx.Document(io.BytesIO(data))
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    except ImportError:
        raise ImportError("python-docx not installed — run: pip install python-docx")


def parse_with_gemini(text: str, api_key: str) -> dict:
    prompt = f"""You are a resume parser. Extract structured information from the resume text below and return ONLY a valid JSON object — no markdown fences, no explanation.

Use this exact schema (omit keys where information is not present, use empty string or empty list):
{PROFILE_SCHEMA}

Rules:
- technical_skills: programming languages, tools, software, platforms (e.g. Python, SQL, Excel, AWS)
- soft_skills: interpersonal/professional traits (e.g. Communication, Leadership)
- certifications: formal certificates and courses only (not degrees)
- experience_summary: write a concise 2-3 sentence summary in first person based on the resume
- work_history: most recent first; summary should be 1-2 sentences of key achievements
- projects: only include if explicitly mentioned as projects

RESUME TEXT:
{text[:6000]}"""

    resp = requests.post(
        GEMINI_API_URL,
        params={"key": api_key},
        headers={"Content-Type": "application/json"},
        json={"contents": [{"parts": [{"text": prompt}]}]},
        timeout=30,
    )
    resp.raise_for_status()

    raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    match = re.search(r"\{[\s\S]*\}", raw)
    if not match:
        raise ValueError("Gemini returned no JSON object")

    return json.loads(match.group())
