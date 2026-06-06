"""
Cover Note Generator
Generates tailored cover notes for each job match.

Two modes:
1. Template-based (no API key needed) - uses keyword matching
2. AI-powered (requires Gemini API key) - uses Gemini Flash for personalized notes
   Get a free key at: https://aistudio.google.com/apikey
"""

import re

import requests

from config import GEMINI_API_KEY, PROFILE

GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"


def generate_cover_note(job: dict) -> str:
    """
    Generate a cover note for a job listing.
    Uses Gemini Flash if API key is available, otherwise falls back to template.
    """
    if GEMINI_API_KEY:
        try:
            return _generate_ai_cover_note(job)
        except Exception as e:
            print(f"  AI cover note failed ({e}), using template")
            return _generate_template_cover_note(job)
    else:
        return _generate_template_cover_note(job)


def _generate_ai_cover_note(job: dict) -> str:
    """Generate a cover note using Gemini Flash."""
    work_history = PROFILE.get("work_history", [])
    work_lines = "\n".join(
        f"  - {w.get('title','')} at {w.get('company','')} ({w.get('period','')}): {w.get('summary','')}"
        for w in work_history[:3]
    ) or "  - No work history provided"

    projects = PROFILE.get("projects", [])
    project_line = (
        f"- Recent project: {projects[0].get('name', 'Personal Project')} — {projects[0].get('description', '')}"
        if projects else ""
    )

    certs = PROFILE.get("certifications", [])

    prompt = f"""Write a short, personalized cover note (3-4 paragraphs, under 200 words) for a job application.

APPLICANT PROFILE:
- Name: {PROFILE.get('name', 'Applicant')}
- Education: {PROFILE.get('education', '')}
- Certifications: {', '.join(certs) if certs else 'None listed'}
- Technical Skills: {', '.join(PROFILE.get('technical_skills', []))}
- Work History:
{work_lines}
- Experience Summary: {PROFILE.get('experience_summary', '')}
{project_line}

JOB DETAILS:
- Title: {job['title']}
- Company: {job['company']}
- Description: {job.get('description', 'Not available')[:800]}
- Match reasons: {', '.join(job.get('match_reasons', []))}

INSTRUCTIONS:
- Be genuine and specific — connect actual skills/experience from the profile to the job requirements
- Mention specific tools/skills from the job description that match the profile
- Keep it conversational but professional — not stiff or generic
- Do NOT start with "I am writing to express my interest" or similar clichés
- End with a clear call to action
- Singapore English conventions
- Sign off with the applicant's name, email, and phone from the profile"""

    response = requests.post(
        GEMINI_API_URL,
        params={"key": GEMINI_API_KEY},
        headers={"Content-Type": "application/json"},
        json={"contents": [{"parts": [{"text": prompt}]}]},
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    return data["candidates"][0]["content"]["parts"][0]["text"]


def _generate_template_cover_note(job: dict) -> str:
    """Generate a cover note using keyword-matching templates."""
    title = job.get("title", "this role")
    company = job.get("company", "your company")
    description = job.get("description", "").lower()

    name     = PROFILE.get("name", "Applicant")
    email    = PROFILE.get("email", "")
    phone    = PROFILE.get("phone", "")
    education = PROFILE.get("education", "")

    # Detect which skills from the JD match the profile
    skill_matches = []
    skill_map = {
        "sql": "SQL for database querying and management",
        "excel": "Microsoft Excel for data analysis and reporting",
        "power bi": "Power BI for data visualization and dashboards",
        "powerbi": "Power BI for data visualization and dashboards",
        "tableau": "Tableau for data visualization",
        "python": "Python for data analysis and automation",
        "data analysis": "data analysis methodologies",
        "data entry": "accurate and efficient data entry",
        "reporting": "reporting and documentation",
        "database": "database management",
    }
    for kw, label in skill_map.items():
        if kw in description:
            skill_matches.append(label)
    if not skill_matches:
        skill_matches = list(PROFILE.get("technical_skills", ["SQL", "Excel", "Python"]))[:4]

    skills_text = ", ".join(skill_matches[:3])

    # Build experience line from work history
    work_history = PROFILE.get("work_history", [])
    experience_summary = PROFILE.get("experience_summary", "")

    if work_history:
        recent = work_history[0]
        recent_title   = recent.get("title", "")
        recent_company = recent.get("company", "")
        recent_summary = recent.get("summary", "")
        # Use only the first sentence; normalise to start with "I <verb>"
        first_sentence = recent_summary.split('.')[0].strip()
        s = first_sentence[0].lower() + first_sentence[1:] if first_sentence else "worked across various responsibilities"
        if s.startswith("i ") or s.startswith("i'"):
            s = s[2:]  # strip leading "i " to avoid "I I ..."
        experience_line = (
            f"In my most recent role as {recent_title} at {recent_company}, "
            f"I {s}. "
            "This gave me practical, hands-on experience that I'm keen to bring to a new challenge."
        )
    elif experience_summary:
        experience_line = experience_summary
    else:
        experience_line = (
            "I bring a combination of technical skills and a strong desire to learn and contribute "
            "in a professional environment."
        )

    # Build projects line
    projects = PROFILE.get("projects", [])
    project_line = ""
    if projects:
        p = projects[0]
        project_line = (
            f" I recently worked on {p.get('name', 'a personal project')} — "
            f"{p.get('description', '').rstrip('.')}."
        )

    contact_line = name
    if email:
        contact_line += f"\n{email}"
    if phone:
        contact_line += f" | {phone}"

    note = f"""Dear Hiring Manager,

I'm reaching out about the {title} position at {company}. With a background in {education}, I'm eager to apply my skills to a role where I can contribute and grow.

{experience_line}

My technical toolkit includes {skills_text}, developed through both coursework and hands-on work.{project_line}

I'd welcome the opportunity to discuss how my background could add value to your team. I'm available for an interview at your convenience.

Best regards,
{contact_line}"""

    return note


def save_cover_note(job: dict, note: str, output_dir: str = "data/cover_notes") -> str:
    """Save a cover note to a text file."""
    import os
    os.makedirs(output_dir, exist_ok=True)

    # Clean filename
    company = re.sub(r'[^\w\s-]', '', job.get('company', 'unknown'))
    title = re.sub(r'[^\w\s-]', '', job.get('title', 'unknown'))
    filename = f"{company}_{title}.txt".replace(" ", "_")[:80]
    filepath = os.path.join(output_dir, filename)

    with open(filepath, "w") as f:
        f.write(f"Cover Note for: {job.get('title', '')} @ {job.get('company', '')}\n")
        f.write(f"Job URL: {job.get('url', '')}\n")
        f.write(f"Match Score: {job.get('score', 'N/A')}/100\n")
        f.write(f"Generated: {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
        f.write("=" * 60 + "\n\n")
        f.write(note)

    return filepath


if __name__ == "__main__":
    # Test with a sample job
    sample_job = {
        "title": "Junior Data Analyst",
        "company": "DBS Bank",
        "description": "We are looking for a junior data analyst to join our team. Requirements: SQL, Excel, Power BI, data visualization. Fresh graduates welcome.",
        "match_reasons": ["Title match: Data Analyst", "Skills match: sql, excel, power bi"],
        "score": 85,
        "url": "https://example.com/job/123",
    }
    note = generate_cover_note(sample_job)
    print(note)
