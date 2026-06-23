"""
Generate a professional PDF resume from a user profile dict.
Uses reportlab Platypus for clean, ATS-friendly output.
"""
import io
from xml.sax.saxutils import escape
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib.colors import HexColor
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, HRFlowable, KeepTogether,
)

_INDIGO  = HexColor("#4F46E5")
_DARK    = HexColor("#1E293B")
_MID     = HexColor("#475569")
_LIGHT   = HexColor("#94A3B8")
_RULE    = HexColor("#E2E8F0")


def _styles():
    return {
        "name": ParagraphStyle(
            "name", fontName="Helvetica-Bold", fontSize=22,
            textColor=_DARK, leading=26, spaceAfter=2,
        ),
        "contact": ParagraphStyle(
            "contact", fontName="Helvetica", fontSize=9,
            textColor=_MID, leading=14,
        ),
        "section": ParagraphStyle(
            "section", fontName="Helvetica-Bold", fontSize=9,
            textColor=_INDIGO, leading=12, spaceBefore=14, spaceAfter=4,
            textTransform="uppercase", letterSpacing=1,
        ),
        "body": ParagraphStyle(
            "body", fontName="Helvetica", fontSize=9.5,
            textColor=_DARK, leading=14, spaceAfter=3,
        ),
        "body_light": ParagraphStyle(
            "body_light", fontName="Helvetica", fontSize=9,
            textColor=_MID, leading=13, spaceAfter=2,
        ),
        "job_title": ParagraphStyle(
            "job_title", fontName="Helvetica-Bold", fontSize=10,
            textColor=_DARK, leading=14, spaceAfter=1,
        ),
        "job_meta": ParagraphStyle(
            "job_meta", fontName="Helvetica-Oblique", fontSize=9,
            textColor=_MID, leading=12, spaceAfter=3,
        ),
        "bullet": ParagraphStyle(
            "bullet", fontName="Helvetica", fontSize=9.5,
            textColor=_DARK, leading=14, leftIndent=12,
            bulletIndent=4, spaceAfter=2,
        ),
    }


def _rule():
    return HRFlowable(width="100%", thickness=0.5, color=_RULE, spaceAfter=6)


def _section(title, s):
    return [Paragraph(title, s["section"]), _rule()]


def generate_pdf(profile: dict) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=1.8 * cm,
        rightMargin=1.8 * cm,
        topMargin=1.6 * cm,
        bottomMargin=1.6 * cm,
    )

    s = _styles()
    story = []

    # ── Header ────────────────────────────────────────────────────────────────
    name    = (profile.get("name") or "").strip() or "Your Name"
    email   = (profile.get("email") or "").strip()
    phone   = (profile.get("phone") or "").strip()
    contact_parts = [p for p in [email, phone, "Singapore"] if p]

    story.append(Paragraph(escape(name), s["name"]))
    story.append(Paragraph(escape(" · ".join(contact_parts)), s["contact"]))
    story.append(Spacer(1, 10))

    # ── Summary ───────────────────────────────────────────────────────────────
    summary = (profile.get("experience_summary") or "").strip()
    if summary:
        story += _section("Professional Summary", s)
        story.append(Paragraph(escape(summary), s["body"]))

    # ── Skills ────────────────────────────────────────────────────────────────
    tech   = profile.get("technical_skills") or []
    soft   = profile.get("soft_skills") or []
    if tech or soft:
        story += _section("Skills", s)
        if tech:
            skills_str = escape(", ".join(tech) if isinstance(tech, list) else str(tech))
            story.append(Paragraph(f"<b>Technical:</b>  {skills_str}", s["body"]))
        if soft:
            soft_str = escape(", ".join(soft) if isinstance(soft, list) else str(soft))
            story.append(Paragraph(f"<b>Interpersonal:</b>  {soft_str}", s["body"]))

    # ── Work Experience ────────────────────────────────────────────────────────
    work = profile.get("work_history") or []
    if work:
        story += _section("Work Experience", s)
        for job in work:
            if not isinstance(job, dict):
                continue
            title   = (job.get("title") or "").strip()
            company = (job.get("company") or "").strip()
            period  = (job.get("period") or "").strip()
            summary = (job.get("summary") or "").strip()

            header_left  = f"<b>{escape(title)}</b>" + (f"  ·  {escape(company)}" if company else "")
            block = [Paragraph(header_left, s["job_title"])]
            if period:
                block.append(Paragraph(escape(period), s["job_meta"]))
            if summary:
                for line in summary.split("\n"):
                    line = line.strip().lstrip("•-").strip()
                    if line:
                        block.append(Paragraph(f"• {escape(line)}", s["bullet"]))
            block.append(Spacer(1, 4))
            story.append(KeepTogether(block))

    # ── Education ─────────────────────────────────────────────────────────────
    education = (profile.get("education") or "").strip()
    if education:
        story += _section("Education", s)
        story.append(Paragraph(escape(education), s["body"]))

    # ── Certifications ────────────────────────────────────────────────────────
    certs = profile.get("certifications") or []
    if certs:
        story += _section("Certifications", s)
        for cert in certs:
            cert = cert.strip() if isinstance(cert, str) else str(cert)
            if cert:
                story.append(Paragraph(f"• {escape(cert)}", s["bullet"]))

    # ── Projects ──────────────────────────────────────────────────────────────
    projects = profile.get("projects") or []
    if projects:
        story += _section("Projects", s)
        for proj in projects:
            if not isinstance(proj, dict):
                continue
            pname = (proj.get("name") or "").strip()
            pdesc = (proj.get("description") or "").strip()
            if pname:
                block = [Paragraph(f"<b>{escape(pname)}</b>", s["job_title"])]
                if pdesc:
                    block.append(Paragraph(escape(pdesc), s["body_light"]))
                block.append(Spacer(1, 4))
                story.append(KeepTogether(block))

    doc.build(story)
    return buf.getvalue()
