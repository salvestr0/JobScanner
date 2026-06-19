"""
Resume parser — extracts text from PDF/DOCX/TXT and uses Gemini to
return a structured profile dict matching data/profile.json schema.
"""
import io
import json
import re
import struct

import requests

GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
DOC_PARSE_ERROR = "Could not parse .doc file"

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
    elif ext == "docx":
        return _extract_docx(file_bytes)
    elif ext == "doc":
        return _extract_doc(file_bytes)
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


def _extract_doc(data: bytes) -> str:
    try:
        import olefile
    except ImportError:
        raise ImportError("olefile not installed - run: pip install olefile")

    ole = None
    try:
        ole = olefile.OleFileIO(io.BytesIO(data))
        if not ole.exists("WordDocument"):
            raise ValueError(DOC_PARSE_ERROR)

        word_document = ole.openstream("WordDocument").read()
        if len(word_document) < 0x01AA:
            raise ValueError(DOC_PARSE_ERROR)

        flags = _read_u16(word_document, 0x0A)
        table_name = "1Table" if flags & 0x0200 else "0Table"
        if not ole.exists(table_name):
            raise ValueError(DOC_PARSE_ERROR)

        table_stream = ole.openstream(table_name).read()
        return _extract_doc_text_from_piece_table(word_document, table_stream)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(DOC_PARSE_ERROR) from exc
    finally:
        if ole is not None:
            ole.close()


def _extract_doc_text_from_piece_table(word_document: bytes, table_stream: bytes) -> str:
    fc_clx = _read_u32(word_document, 0x01A2)
    lcb_clx = _read_u32(word_document, 0x01A6)
    if lcb_clx <= 0 or fc_clx + lcb_clx > len(table_stream):
        raise ValueError(DOC_PARSE_ERROR)

    clx = table_stream[fc_clx:fc_clx + lcb_clx]
    pos = 0
    while pos < len(clx):
        marker = clx[pos]
        pos += 1

        if marker == 0x01:
            grpprl_size = _read_u16(clx, pos)
            pos += 2 + grpprl_size
        elif marker == 0x02:
            plcpcd_size = _read_u32(clx, pos)
            pos += 4
            if plcpcd_size <= 4 or pos + plcpcd_size > len(clx):
                raise ValueError(DOC_PARSE_ERROR)
            return _extract_doc_text_from_plcpcd(clx[pos:pos + plcpcd_size], word_document)
        else:
            raise ValueError(DOC_PARSE_ERROR)

    raise ValueError(DOC_PARSE_ERROR)


def _extract_doc_text_from_plcpcd(plcpcd: bytes, word_document: bytes) -> str:
    if len(plcpcd) < 16 or (len(plcpcd) - 4) % 12 != 0:
        raise ValueError(DOC_PARSE_ERROR)

    piece_count = (len(plcpcd) - 4) // 12
    pcd_start = 4 * (piece_count + 1)
    if pcd_start + piece_count * 8 != len(plcpcd):
        raise ValueError(DOC_PARSE_ERROR)

    parts = []
    for i in range(piece_count):
        cp_start = _read_u32(plcpcd, i * 4)
        cp_end = _read_u32(plcpcd, (i + 1) * 4)
        if cp_end <= cp_start:
            continue

        pcd_offset = pcd_start + i * 8
        fc_compressed = _read_u32(plcpcd, pcd_offset + 2)
        is_compressed = bool(fc_compressed & 0x40000000)
        char_count = cp_end - cp_start

        if is_compressed:
            byte_offset = (fc_compressed & 0x3FFFFFFF) // 2
            byte_count = char_count
            encoding = "cp1252"
        else:
            byte_offset = fc_compressed & 0x3FFFFFFF
            byte_count = char_count * 2
            encoding = "utf-16le"

        if byte_offset + byte_count > len(word_document):
            raise ValueError(DOC_PARSE_ERROR)

        chunk = word_document[byte_offset:byte_offset + byte_count]
        parts.append(chunk.decode(encoding, errors="ignore"))

    text = _clean_doc_text("".join(parts))
    if not text:
        raise ValueError(DOC_PARSE_ERROR)
    return text


def _read_u16(data: bytes, offset: int) -> int:
    if offset + 2 > len(data):
        raise ValueError(DOC_PARSE_ERROR)
    return struct.unpack_from("<H", data, offset)[0]


def _read_u32(data: bytes, offset: int) -> int:
    if offset + 4 > len(data):
        raise ValueError(DOC_PARSE_ERROR)
    return struct.unpack_from("<I", data, offset)[0]


def _clean_doc_text(text: str) -> str:
    text = re.sub(
        r'\x13\s*HYPERLINK\s+"[^"]*"[^\x14]*\x14([^\x15]*)\x15',
        r"\1",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r'\bHYPERLINK\s+"[^"]*"\s*([^\n]*)',
        r"\1",
        text,
        flags=re.IGNORECASE,
    )
    text = text.replace("\r", "\n").replace("\x07", "\n").replace("\x0b", "\n").replace("\x0c", "\n")
    text = re.sub(r"[\x00-\x08\x0e-\x1f]", "", text)
    text = "\n".join(re.sub(r"[ \t]{2,}", " ", line).strip() for line in text.splitlines())
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


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
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                # gemini-2.5-flash thinks by default; for structured extraction
                # that only burns the output budget and can leave the response
                # with finishReason=MAX_TOKENS and no text. Disable it.
                "thinkingConfig": {"thinkingBudget": 0},
                "responseMimeType": "application/json",
                "maxOutputTokens": 4096,
            },
        },
        timeout=30,
    )
    if resp.status_code == 400:
        raise ValueError("Invalid or rejected Gemini API key")
    if resp.status_code in (401, 403):
        raise ValueError("Gemini API key is unauthorized or expired")
    if resp.status_code == 429:
        raise ValueError("Gemini rate limit or quota exceeded — try again later")
    resp.raise_for_status()

    raw = _extract_text_from_response(resp.json())
    match = re.search(r"\{[\s\S]*\}", raw)
    if not match:
        raise ValueError("Gemini returned no JSON object")

    return json.loads(match.group())


def _extract_text_from_response(body: dict) -> str:
    """Pull the model text out of a generateContent response, with clear
    errors for the empty/blocked cases instead of a bare KeyError."""
    prompt_feedback = body.get("promptFeedback") or {}
    if prompt_feedback.get("blockReason"):
        raise ValueError(f"Resume blocked by Gemini ({prompt_feedback['blockReason']})")

    candidates = body.get("candidates") or []
    if not candidates:
        raise ValueError("Gemini returned no candidates")

    candidate = candidates[0]
    parts = (candidate.get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts).strip()
    if not text:
        reason = candidate.get("finishReason", "unknown")
        raise ValueError(f"Gemini returned empty response (finishReason={reason})")
    return text
