"""
Configuration for CareerScan.
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
# LEGACY ADZUNA API SETTINGS
# ============================================================
# Kept for backward compatibility with legacy helper functions.
ADZUNA_APP_ID  = _os.getenv("ADZUNA_APP_ID", "")
ADZUNA_APP_KEY = _os.getenv("ADZUNA_APP_KEY", "")

# ============================================================
# JSEARCH / OPENWEB NINJA API
# ============================================================
JSEARCH_API_KEY  = _os.getenv("JSEARCH_API_KEY", "") or _os.getenv("OPENWEBNINJA_API_KEY", "")
JSEARCH_API_HOST = _os.getenv("JSEARCH_API_HOST", "jsearch.p.rapidapi.com")

# ============================================================
# JOB REGIONS
# ============================================================
DEFAULT_JOB_REGION = "sg"

_JOB_COUNTRY_GROUPS = {
    "Africa": [
        ("dz", "Algeria"), ("ao", "Angola"), ("bj", "Benin"), ("bw", "Botswana"),
        ("bf", "Burkina Faso"), ("bi", "Burundi"), ("cm", "Cameroon"), ("cv", "Cabo Verde"),
        ("cf", "Central African Republic"), ("td", "Chad"), ("km", "Comoros"),
        ("cg", "Congo"), ("cd", "Democratic Republic of the Congo"), ("ci", "Cote d'Ivoire"),
        ("dj", "Djibouti"), ("eg", "Egypt"), ("gq", "Equatorial Guinea"), ("er", "Eritrea"),
        ("sz", "Eswatini"), ("et", "Ethiopia"), ("ga", "Gabon"), ("gm", "Gambia"),
        ("gh", "Ghana"), ("gn", "Guinea"), ("gw", "Guinea-Bissau"), ("ke", "Kenya"),
        ("ls", "Lesotho"), ("lr", "Liberia"), ("ly", "Libya"), ("mg", "Madagascar"),
        ("mw", "Malawi"), ("ml", "Mali"), ("mr", "Mauritania"), ("mu", "Mauritius"),
        ("ma", "Morocco"), ("mz", "Mozambique"), ("na", "Namibia"), ("ne", "Niger"),
        ("ng", "Nigeria"), ("rw", "Rwanda"), ("st", "Sao Tome and Principe"),
        ("sn", "Senegal"), ("sc", "Seychelles"), ("sl", "Sierra Leone"), ("so", "Somalia"),
        ("za", "South Africa"), ("ss", "South Sudan"), ("sd", "Sudan"), ("tz", "Tanzania"),
        ("tg", "Togo"), ("tn", "Tunisia"), ("ug", "Uganda"), ("zm", "Zambia"), ("zw", "Zimbabwe"),
    ],
    "Asia": [
        ("af", "Afghanistan"), ("am", "Armenia"), ("az", "Azerbaijan"), ("bh", "Bahrain"),
        ("bd", "Bangladesh"), ("bt", "Bhutan"), ("bn", "Brunei"), ("kh", "Cambodia"),
        ("cn", "China"), ("cy", "Cyprus"), ("ge", "Georgia"), ("hk", "Hong Kong"),
        ("in", "India"), ("id", "Indonesia"), ("ir", "Iran"), ("iq", "Iraq"),
        ("il", "Israel"), ("jp", "Japan"), ("jo", "Jordan"), ("kz", "Kazakhstan"),
        ("kw", "Kuwait"), ("kg", "Kyrgyzstan"), ("la", "Laos"), ("lb", "Lebanon"),
        ("mo", "Macau"), ("my", "Malaysia"), ("mv", "Maldives"), ("mn", "Mongolia"),
        ("mm", "Myanmar"), ("np", "Nepal"), ("kp", "North Korea"), ("om", "Oman"),
        ("pk", "Pakistan"), ("ps", "Palestine"), ("ph", "Philippines"), ("qa", "Qatar"),
        ("sa", "Saudi Arabia"), ("sg", "Singapore"), ("kr", "South Korea"), ("lk", "Sri Lanka"),
        ("sy", "Syria"), ("tw", "Taiwan"), ("tj", "Tajikistan"), ("th", "Thailand"),
        ("tl", "Timor-Leste"), ("tr", "Turkey"), ("tm", "Turkmenistan"),
        ("ae", "United Arab Emirates"), ("uz", "Uzbekistan"), ("vn", "Vietnam"), ("ye", "Yemen"),
    ],
    "Europe": [
        ("al", "Albania"), ("ad", "Andorra"), ("at", "Austria"), ("by", "Belarus"),
        ("be", "Belgium"), ("ba", "Bosnia and Herzegovina"), ("bg", "Bulgaria"),
        ("hr", "Croatia"), ("cz", "Czechia"), ("dk", "Denmark"), ("ee", "Estonia"),
        ("fi", "Finland"), ("fr", "France"), ("de", "Germany"), ("gr", "Greece"),
        ("hu", "Hungary"), ("is", "Iceland"), ("ie", "Ireland"), ("it", "Italy"),
        ("xk", "Kosovo"), ("lv", "Latvia"), ("li", "Liechtenstein"), ("lt", "Lithuania"),
        ("lu", "Luxembourg"), ("mt", "Malta"), ("md", "Moldova"), ("mc", "Monaco"),
        ("me", "Montenegro"), ("nl", "Netherlands"), ("mk", "North Macedonia"),
        ("no", "Norway"), ("pl", "Poland"), ("pt", "Portugal"), ("ro", "Romania"),
        ("ru", "Russia"), ("sm", "San Marino"), ("rs", "Serbia"), ("sk", "Slovakia"),
        ("si", "Slovenia"), ("es", "Spain"), ("se", "Sweden"), ("ch", "Switzerland"),
        ("ua", "Ukraine"), ("gb", "United Kingdom"), ("va", "Vatican City"),
    ],
    "North America": [
        ("ag", "Antigua and Barbuda"), ("bs", "Bahamas"), ("bb", "Barbados"), ("bz", "Belize"),
        ("ca", "Canada"), ("cr", "Costa Rica"), ("cu", "Cuba"), ("dm", "Dominica"),
        ("do", "Dominican Republic"), ("sv", "El Salvador"), ("gl", "Greenland"),
        ("gd", "Grenada"), ("gt", "Guatemala"), ("ht", "Haiti"), ("hn", "Honduras"),
        ("jm", "Jamaica"), ("mx", "Mexico"), ("ni", "Nicaragua"), ("pa", "Panama"),
        ("pr", "Puerto Rico"), ("kn", "Saint Kitts and Nevis"), ("lc", "Saint Lucia"),
        ("vc", "Saint Vincent and the Grenadines"), ("tt", "Trinidad and Tobago"),
        ("us", "United States"),
    ],
    "South America": [
        ("ar", "Argentina"), ("bo", "Bolivia"), ("br", "Brazil"), ("cl", "Chile"),
        ("co", "Colombia"), ("ec", "Ecuador"), ("fk", "Falkland Islands"), ("gy", "Guyana"),
        ("py", "Paraguay"), ("pe", "Peru"), ("sr", "Suriname"), ("uy", "Uruguay"),
        ("ve", "Venezuela"),
    ],
    "Oceania": [
        ("as", "American Samoa"), ("au", "Australia"), ("fj", "Fiji"), ("pf", "French Polynesia"),
        ("gu", "Guam"), ("ki", "Kiribati"), ("mh", "Marshall Islands"), ("fm", "Micronesia"),
        ("nr", "Nauru"), ("nc", "New Caledonia"), ("nz", "New Zealand"), ("pw", "Palau"),
        ("pg", "Papua New Guinea"), ("ws", "Samoa"), ("sb", "Solomon Islands"), ("to", "Tonga"),
        ("tv", "Tuvalu"), ("vu", "Vanuatu"),
    ],
    "Antarctica": [
        ("aq", "Antarctica"),
    ],
}

_JOB_REGION_CURRENCIES = {
    "ae": "AED", "ar": "ARS", "au": "AUD", "br": "BRL", "ca": "CAD", "ch": "CHF",
    "cn": "CNY", "de": "EUR", "eg": "EGP", "es": "EUR", "fr": "EUR", "gb": "GBP",
    "hk": "HKD", "id": "IDR", "in": "INR", "it": "EUR", "jp": "JPY", "kr": "KRW",
    "my": "MYR", "nl": "EUR", "nz": "NZD", "ph": "PHP", "sg": "SGD", "th": "THB",
    "tw": "TWD", "us": "USD", "vn": "VND", "za": "ZAR",
}


def _build_job_regions() -> dict:
    regions = {}
    for continent, countries in _JOB_COUNTRY_GROUPS.items():
        for key, label in countries:
            api_enabled = key != "aq"
            regions[key] = {
                "label": label,
                "continent": continent,
                "currency": _JOB_REGION_CURRENCIES.get(key, "local"),
                "default_location": label,
                "enabled_sources": ["jsearch"] if api_enabled else [],
                "api_enabled": api_enabled,
            }
    return regions


JOB_REGIONS = _build_job_regions()


def normalize_job_region(region: str | None) -> str:
    region = (region or DEFAULT_JOB_REGION).strip().lower()
    return region if region in JOB_REGIONS else DEFAULT_JOB_REGION


def get_job_region(region: str | None = None) -> dict:
    return JOB_REGIONS[normalize_job_region(region)]


def get_job_region_label(region: str | None = None) -> str:
    return get_job_region(region)["label"]


def get_job_region_options() -> list[dict]:
    return [
        {
            "key": key,
            "label": value["label"],
            "continent": value["continent"],
            "currency": value["currency"],
            "default_location": value["default_location"],
            "enabled_sources": value["enabled_sources"],
            "api_enabled": bool(value.get("api_enabled")),
        }
        for key, value in sorted(JOB_REGIONS.items(), key=lambda item: (item[1]["continent"], item[1]["label"]))
    ]


def get_job_continent_options() -> list[dict]:
    return [
        {
            "key": continent.lower().replace(" ", "_"),
            "label": continent,
            "countries": [
                {
                    "key": key,
                    "label": JOB_REGIONS[key]["label"],
                    "currency": JOB_REGIONS[key]["currency"],
                    "api_enabled": bool(JOB_REGIONS[key].get("api_enabled")),
                }
                for key, _label in countries
            ],
        }
        for continent, countries in _JOB_COUNTRY_GROUPS.items()
    ]

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
    "job_region": DEFAULT_JOB_REGION,
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
    "min_score_threshold":        30,
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

# Runtime cache (writable, ephemeral on Render) — holds Gemini-generated custom modes.
_MODES_CACHE_FILE = "data/modes_cache.json"
# Seed of pre-built modes, committed to the repo so they ALWAYS work without Gemini
# and survive deploys (Render's data/ disk is ephemeral and wiped on each deploy).
# Anchored to this file's dir so it resolves regardless of the process CWD.
_MODES_SEED_FILE = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "prebuilt_modes.json")

_MODE_DEFAULTS = {
    "job_region": DEFAULT_JOB_REGION,
    "min_salary": 2200,
    "max_salary": 4000,
    "preferred_location": "Sengkang",
    "location_keywords": SEARCH_CONFIG["location_keywords"],
    "min_score_threshold": 38,
    "max_jobs_per_notification": 20,
}

import requests as _requests


def _load_modes_cache() -> dict:
    """Pre-built seed modes (committed) overlaid with any runtime-cached custom modes."""
    cache: dict = {}
    for path in (_MODES_SEED_FILE, _MODES_CACHE_FILE):
        if _os.path.exists(path):
            try:
                with open(path) as f:
                    cache.update(_json.load(f))
            except Exception as e:
                print(f"  [modes] Could not read {path}: {e}")
    return cache


def _save_modes_cache(cache: dict):
    _os.makedirs("data", exist_ok=True)
    with open(_MODES_CACHE_FILE, "w") as f:
        _json.dump(cache, f, indent=2)


def _generate_mode_via_gemini(mode_name: str) -> dict | None:
    if not GEMINI_API_KEY:
        print("  No GEMINI_API_KEY set — cannot auto-generate mode config.")
        return None

    prompt = f"""You are helping generate a job search configuration for a job board scraper targeting Asia-based jobs.

Job type to search: "{mode_name}"

Candidate profile:
- Diploma in AI/Infocomm from a polytechnic (NOT a university degree)
- Skills: Python, SQL, Excel, Power BI, data entry, basic admin/ops experience
- 1-2 years work experience in admin/logistics/customer service
- Looking for entry-level or junior roles only

Return ONLY a valid JSON object (no markdown, no explanation) with exactly these keys:
{{
  "target_titles": ["8 to 12 specific job titles to search on job boards"],
  "preferred_keywords": ["15 to 20 keywords that make a posting more relevant to this job type"],
  "negative_keywords": ["8 to 12 red flag terms: commission-only, MLM, senior roles, 5+ years exp, etc."],
  "min_score_threshold": 38
}}

Use practical job title conventions for Asian job boards. Include both generic and industry-specific terms."""

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
