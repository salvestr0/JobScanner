"""
Configuration for Job Scanner Tool.
All credentials are loaded from environment variables (set via .env or Railway).
"""

import json as _json
import os as _os
import re as _re

from dotenv import load_dotenv as _load_dotenv

_load_dotenv()

# ============================================================
# GEMINI API
# ============================================================
GEMINI_API_KEY = _os.getenv("GEMINI_API_KEY", "")

# ============================================================
# ADZUNA API  (free — register at developer.adzuna.com)
# ============================================================
ADZUNA_APP_ID  = _os.getenv("ADZUNA_APP_ID", "")
ADZUNA_APP_KEY = _os.getenv("ADZUNA_APP_KEY", "")

# ============================================================
# YOUR PROFILE
# ============================================================
PROFILE = {
    "name": "",
    "email": "",
    "phone": "",
    "education": "",
    "certifications": [],
    "technical_skills": [],
    "soft_skills": [],
    "experience_summary": "",
    "work_history": [],
    "projects": [],
}

# ============================================================
# JOB SEARCH SETTINGS
# ============================================================
SEARCH_CONFIG = {
    "target_titles": [
        "Data Analyst",
        "Business Analyst",
        "Operations Analyst",
        "Reporting Analyst",
        "IT Analyst",
        "Junior Analyst",
        "Research Analyst",
        "Analytics Associate",
        "Data Associate",
        "Intelligence Analyst",
    ],
    "preferred_keywords": [
        "sql", "excel", "python", "power bi", "powerbi", "tableau",
        "data analysis", "data entry", "reporting", "dashboard",
        "database", "analytics", "visualization", "etl",
        "junior", "entry level", "entry-level", "fresh grad",
        "no experience required", "willing to train",
    ],
    "negative_keywords": [
        "financial advisor", "insurance agent", "commission-based",
        "commission only", "mlm", "multi-level",
        "warehouse", "packer", "f&b", "food and beverage",
        "waiter", "waitress", "kitchen", "chef", "cook",
        "senior analyst", "lead analyst", "principal analyst",
        "5 years", "5+ years", "7 years", "10 years",
        "manager", "director", "head of",
        "bachelor's degree required", "degree required",
        "minimum bachelor", "must have a degree",
    ],
    "min_salary":    2200,
    "max_salary":    4000,
    "preferred_location": "Sengkang",
    "location_keywords": [
        "sengkang", "punggol", "hougang", "serangoon",
        "ang mo kio", "bishan", "toa payoh",
        "remote", "work from home", "wfh", "hybrid",
        "changi business park", "one-north", "mapletree",
        "paya lebar", "tai seng", "ubi", "macpherson",
    ],
    "min_score_threshold":        40,
    "max_jobs_per_notification":  20,
}

# ============================================================
# FILE PATHS  (may be overridden at runtime via JOBSCANNER_DATA_DIR)
# ============================================================
DATA_DIR       = "data"
JOBS_CSV       = f"{DATA_DIR}/matched_jobs.csv"
SEEN_JOBS_FILE = f"{DATA_DIR}/seen_jobs.json"
COVER_NOTES_DIR = f"{DATA_DIR}/cover_notes"

# ============================================================
# DYNAMIC SEARCH MODES
# ============================================================

_LOCATION_KEYWORDS = [
    "sengkang", "punggol", "hougang", "serangoon",
    "ang mo kio", "bishan", "toa payoh",
    "remote", "work from home", "wfh", "hybrid",
    "changi business park", "one-north", "mapletree",
    "paya lebar", "tai seng", "ubi", "macpherson",
]

_MODES_CACHE_FILE = "data/modes_cache.json"

_MODE_DEFAULTS = {
    "min_salary": 2200,
    "max_salary": 4000,
    "preferred_location": "Sengkang",
    "location_keywords": _LOCATION_KEYWORDS,
    "min_score_threshold": 38,
    "max_jobs_per_notification": 20,
}

import requests as _requests


def _load_modes_cache() -> dict:
    if _os.path.exists(_MODES_CACHE_FILE):
        with open(_MODES_CACHE_FILE) as f:
            return _json.load(f)
    return {}


def _save_modes_cache(cache: dict):
    _os.makedirs("data", exist_ok=True)
    with open(_MODES_CACHE_FILE, "w") as f:
        _json.dump(cache, f, indent=2)


def _generate_mode_via_gemini(mode_name: str) -> dict | None:
    if not GEMINI_API_KEY:
        print("  No GEMINI_API_KEY set — cannot auto-generate mode config.")
        return None

    prompt = f"""You are helping generate a job search configuration for a job board scraper targeting Singapore jobs.

Job type to search: "{mode_name}"

Candidate profile:
- Diploma in AI/Infocomm from a polytechnic (NOT a university degree)
- Skills: Python, SQL, Excel, Power BI, data entry, basic admin/ops experience
- 1-2 years work experience in admin/logistics/customer service
- Looking for entry-level or junior roles only

Return ONLY a valid JSON object (no markdown, no explanation) with exactly these keys:
{{
  "target_titles": ["8 to 12 specific job titles to search on Singapore job boards"],
  "preferred_keywords": ["15 to 20 keywords that make a posting more relevant to this job type"],
  "negative_keywords": ["8 to 12 red flag terms: commission-only, MLM, senior roles, 5+ years exp, etc."],
  "min_score_threshold": 38
}}

Use Singapore-appropriate job title conventions. Include both generic and industry-specific terms."""

    try:
        resp = _requests.post(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
            params={"key": GEMINI_API_KEY},
            headers={"Content-Type": "application/json"},
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=20,
        )
        resp.raise_for_status()
        text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]

        json_match = _re.search(r'\{[\s\S]*\}', text)
        if not json_match:
            print("  Gemini returned unexpected format — could not parse mode config.")
            return None

        generated = _json.loads(json_match.group())
        for k, v in _MODE_DEFAULTS.items():
            generated.setdefault(k, v)
        return generated

    except Exception as e:
        print(f"  Gemini mode generation failed: {e}")
        return None


def set_mode(mode: str, refresh: bool = False):
    if mode == "analyst":
        return

    cache = _load_modes_cache()

    if not refresh and mode in cache:
        config = cache[mode]
        titles_preview = ", ".join(config["target_titles"][:3])
        print(f"[Mode: {mode.upper()}] Loaded from cache — {titles_preview}...")
        SEARCH_CONFIG.clear()
        SEARCH_CONFIG.update(config)
        return

    print(f"[Mode: {mode.upper()}] Asking Gemini to generate search config...")
    generated = _generate_mode_via_gemini(mode)

    if generated:
        cache[mode] = generated
        _save_modes_cache(cache)
        titles_preview = ", ".join(generated["target_titles"][:3])
        print(f"[Mode: {mode.upper()}] Ready — {titles_preview}...")
        SEARCH_CONFIG.clear()
        SEARCH_CONFIG.update(generated)
    else:
        print(f"[Mode: {mode.upper()}] Could not generate config. Running with analyst defaults.")


def list_modes():
    cache = _load_modes_cache()
    print("Saved modes (use with --mode=<name>):")
    print("  analyst  — Data/Business Analyst roles (built-in default)")
    for name, config in cache.items():
        titles = ", ".join(config.get("target_titles", [])[:3])
        print(f"  {name:<12} — {titles}...")
    if not cache:
        print("  (no custom modes cached yet — try: python main.py --mode=healthcare)")


def load_user_config(json_str: str):
    """
    Override module-level config from a JSON string.
    Called by main.py when JOBSCANNER_USER_CONFIG env var is set (multi-user hosted mode).
    """
    try:
        data = _json.loads(json_str)
    except Exception:
        return

    global GEMINI_API_KEY

    if "gemini_api_key" in data:
        GEMINI_API_KEY = data["gemini_api_key"]

    if "profile" in data:
        PROFILE.update(data["profile"])

    if "search_config" in data:
        SEARCH_CONFIG.update(data["search_config"])


# ============================================================
# PROFILE OVERRIDE — load from data/profile.json if it exists
# Ignored in multi-user mode (subprocess sets JOBSCANNER_USER_CONFIG instead).
# ============================================================
_PROFILE_FILE = "data/profile.json"
if not _os.getenv("JOBSCANNER_USER_CONFIG") and _os.path.exists(_PROFILE_FILE):
    try:
        with open(_PROFILE_FILE, encoding="utf-8") as _pf:
            PROFILE.update(_json.load(_pf))
    except Exception:
        pass
