"""
Job fetchers — MyCareersFuture, Adzuna, Indeed RSS, RemoteOK.

All sources use official APIs or public syndication endpoints (no HTML scraping).
LinkedIn / JobStreet / Glints HTML scraping violates their ToS — do not add them back.
"""

import hashlib
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

from config import ADZUNA_APP_ID, ADZUNA_APP_KEY, SEARCH_CONFIG

# ── Shared headers ────────────────────────────────────────────────────────────

_MCF_HEADERS = {
    "Accept": "application/json",
    "Origin": "https://www.mycareersfuture.gov.sg",
    "Referer": "https://www.mycareersfuture.gov.sg/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
}

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _clean_html(html: str) -> str:
    if not html:
        return ""
    if "<" not in html:
        return html
    return BeautifulSoup(html, "html.parser").get_text(separator=" ")


def _parse_salary(val) -> int | None:
    if val is None:
        return None
    try:
        return int(str(val).replace("$", "").replace(",", "").strip())
    except (ValueError, AttributeError):
        return None


# ── MyCareersFuture ───────────────────────────────────────────────────────────

_MCF_ENDPOINTS = [
    (
        "https://api.mycareersfuture.gov.sg/v2/jobs",
        lambda term, page: {"search": term, "limit": 20, "page": page, "sortBy": "new_posting_date"},
    ),
    (
        "https://api.mycareersfuture.gov.sg/v2/search",
        lambda term, page: {"search": term, "limit": 20, "page": page, "sortBy": "new_posting_date"},
    ),
    (
        "https://api.mycareersfuture.gov.sg/v2/jobs/search",
        lambda term, page: {"keyword": term, "limit": 20, "offset": page * 20},
    ),
]


def _find_working_endpoint() -> tuple | None:
    probe_term = SEARCH_CONFIG["target_titles"][0]
    for url, param_fn in _MCF_ENDPOINTS:
        try:
            resp = requests.get(url, params=param_fn(probe_term, 0), headers=_MCF_HEADERS, timeout=8)
            if resp.status_code != 200:
                continue
            data = resp.json()
            if isinstance(data, dict) and ("results" in data or "jobs" in data):
                print(f"  [MCF] Using endpoint: {url}")
                return url, param_fn
            if isinstance(data, list) and data:
                print(f"  [MCF] Using endpoint: {url}")
                return url, param_fn
        except Exception:
            continue
    return None


def fetch_mcf(max_pages: int = 3) -> list:
    """Fetch jobs from MyCareersFuture API for all target titles."""
    endpoint = _find_working_endpoint()
    if not endpoint:
        print("  [MCF] All endpoints unavailable. MCF may be temporarily down.")
        return []

    url, param_fn = endpoint
    jobs: list[dict] = []
    consecutive_failures = 0

    for title in SEARCH_CONFIG["target_titles"]:
        if consecutive_failures >= 3:
            print(f"  [MCF] Too many failures, stopping. Got {len(jobs)} jobs so far.")
            break

        for page in range(max_pages):
            try:
                resp = requests.get(url, params=param_fn(title, page), headers=_MCF_HEADERS, timeout=8)
                resp.raise_for_status()
                data = resp.json()

                results = data.get("results") or data.get("jobs") or (data if isinstance(data, list) else [])
                if not results:
                    break

                consecutive_failures = 0

                for r in results:
                    if not isinstance(r, dict):
                        continue

                    salary = r.get("salary", {})
                    sal_min = salary.get("minimum") if isinstance(salary, dict) else None
                    sal_max = salary.get("maximum") if isinstance(salary, dict) else None

                    sal_type = salary.get("type", {}) if isinstance(salary, dict) else {}
                    if isinstance(sal_type, dict):
                        sal_type = sal_type.get("salaryType", "")
                    if sal_type == "Annual":
                        sal_min = sal_min // 12 if sal_min else None
                        sal_max = sal_max // 12 if sal_max else None

                    address = r.get("address", {})
                    location = ""
                    if isinstance(address, dict):
                        location = address.get("streetAddress", "") or address.get("addressRegion", "")

                    metadata = r.get("metadata", {}) if isinstance(r.get("metadata"), dict) else {}
                    posted = metadata.get("newPostingDate", "")
                    closing_date = (
                        metadata.get("closingDate", "")
                        or r.get("closingDate", "")
                        or ""
                    )
                    job_id = r.get("uuid") or hashlib.md5(r.get("title", "").encode()).hexdigest()[:12]
                    job_details_url = metadata.get("jobDetailsUrl", job_id)

                    if job_details_url.startswith("http"):
                        job_url = job_details_url
                    elif job_details_url.startswith("/"):
                        job_url = f"https://www.mycareersfuture.gov.sg{job_details_url}"
                    else:
                        job_url = f"https://www.mycareersfuture.gov.sg/job/{job_details_url}"

                    jobs.append({
                        "id": f"mcf_{job_id}",
                        "title": r.get("title", "Unknown"),
                        "company": (
                            r.get("postedCompany", {}).get("name", "Unknown")
                            if isinstance(r.get("postedCompany"), dict)
                            else "Unknown"
                        ),
                        "description": _clean_html(r.get("description", ""))[:2000],
                        "salary_min": sal_min,
                        "salary_max": sal_max,
                        "location": location,
                        "url": job_url,
                        "posted_date": posted,
                        "closing_date": closing_date,
                        "source": "MyCareersFuture",
                        "experience_required": r.get("minimumYearsExperience"),
                    })

                time.sleep(1.5)

            except requests.exceptions.Timeout:
                print(f"  [MCF] Timeout for '{title}' page {page} — skipping")
                consecutive_failures += 1
                break
            except requests.exceptions.ConnectionError:
                print(f"  [MCF] Connection dropped for '{title}' — may be rate limited")
                consecutive_failures += 1
                time.sleep(3)
                break
            except Exception as e:
                print(f"  [MCF] Error fetching '{title}' page {page}: {e}")
                consecutive_failures += 1
                break

    seen: set = set()
    unique = [j for j in jobs if j["id"] not in seen and not seen.add(j["id"])]  # type: ignore[func-returns-value]
    print(f"[MyCareersFuture] {len(unique)} unique jobs")
    return unique


# ── Adzuna ────────────────────────────────────────────────────────────────────

def fetch_adzuna(max_pages: int = 2) -> list:
    """
    Fetch Singapore jobs from Adzuna (https://developer.adzuna.com).
    Requires ADZUNA_APP_ID and ADZUNA_APP_KEY env vars (free at developer.adzuna.com).
    Free tier: 100 calls/day, 50 results/page.
    """
    if not ADZUNA_APP_ID or not ADZUNA_APP_KEY:
        print("  [Adzuna] Skipping — ADZUNA_APP_ID / ADZUNA_APP_KEY not set.")
        return []

    jobs: list[dict] = []
    seen_ids: set = set()

    for title in SEARCH_CONFIG["target_titles"]:
        for page in range(1, max_pages + 1):
            try:
                resp = requests.get(
                    f"https://api.adzuna.com/v1/api/jobs/sg/search/{page}",
                    params={
                        "app_id": ADZUNA_APP_ID,
                        "app_key": ADZUNA_APP_KEY,
                        "results_per_page": 50,
                        "what": title,
                        "sort_by": "date",
                    },
                    timeout=10,
                )
                resp.raise_for_status()
                results = resp.json().get("results", [])
                if not results:
                    break

                for r in results:
                    job_id = f"adzuna_{r.get('id', '')}"
                    if job_id in seen_ids:
                        continue
                    seen_ids.add(job_id)

                    company = r.get("company", {})
                    location = r.get("location", {})

                    jobs.append({
                        "id": job_id,
                        "title": r.get("title", "Unknown"),
                        "company": company.get("display_name", "Unknown") if isinstance(company, dict) else "Unknown",
                        "description": _clean_html(r.get("description", ""))[:2000],
                        "salary_min": _parse_salary(r.get("salary_min")),
                        "salary_max": _parse_salary(r.get("salary_max")),
                        "location": location.get("display_name", "Singapore") if isinstance(location, dict) else "Singapore",
                        "url": r.get("redirect_url", ""),
                        "posted_date": (r.get("created") or "")[:10],
                        "source": "Adzuna",
                        "experience_required": None,
                    })

                time.sleep(1)

            except requests.exceptions.Timeout:
                print(f"  [Adzuna] Timeout for '{title}' page {page} — skipping")
                break
            except Exception as e:
                print(f"  [Adzuna] Error for '{title}' page {page}: {e}")
                break

    print(f"[Adzuna] {len(jobs)} unique jobs")
    return jobs


# ── Indeed RSS ────────────────────────────────────────────────────────────────

def fetch_indeed_rss() -> list:
    """
    Fetch jobs from Indeed Singapore via their public RSS feeds.
    RSS is a syndication format designed for consumption (distinct from HTML scraping).
    """
    jobs: list[dict] = []
    seen_urls: set = set()

    headers = {
        "User-Agent": _BROWSER_UA,
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
    }

    for title in SEARCH_CONFIG["target_titles"]:
        try:
            resp = requests.get(
                "https://sg.indeed.com/rss",
                params={"q": title, "l": "Singapore", "sort": "date", "fromage": "30"},
                headers=headers,
                timeout=10,
            )
            resp.raise_for_status()

            root = ET.fromstring(resp.content)
            channel = root.find("channel")
            if channel is None:
                continue

            for item in channel.findall("item"):
                link = item.findtext("link", "").strip()
                if not link or link in seen_urls:
                    continue
                seen_urls.add(link)

                raw_title = item.findtext("title", "").strip()
                # Indeed RSS title is often "Job Title - Company Name"
                if " - " in raw_title:
                    job_title, company_name = raw_title.rsplit(" - ", 1)
                else:
                    job_title, company_name = raw_title, "Unknown"

                # Parse RFC 2822 pubDate → YYYY-MM-DD
                pub_raw = item.findtext("pubDate", "").strip()
                try:
                    date_str = parsedate_to_datetime(pub_raw).strftime("%Y-%m-%d")
                except Exception:
                    date_str = ""

                jobs.append({
                    "id": f"indeed_{hashlib.md5(link.encode()).hexdigest()[:12]}",
                    "title": job_title.strip(),
                    "company": company_name.strip(),
                    "description": _clean_html(item.findtext("description", ""))[:2000],
                    "salary_min": None,
                    "salary_max": None,
                    "location": "Singapore",
                    "url": link,
                    "posted_date": date_str,
                    "source": "Indeed",
                    "experience_required": None,
                })

            time.sleep(1.5)

        except ET.ParseError as e:
            print(f"  [Indeed RSS] XML parse error for '{title}': {e}")
        except requests.exceptions.Timeout:
            print(f"  [Indeed RSS] Timeout for '{title}' — skipping")
        except Exception as e:
            print(f"  [Indeed RSS] Error for '{title}': {e}")

    print(f"[Indeed RSS] {len(jobs)} unique jobs")
    return jobs


# ── RemoteOK ──────────────────────────────────────────────────────────────────

def fetch_remoteok() -> list:
    """
    Fetch remote jobs from RemoteOK public API (https://remoteok.com/api).
    Filters by titles/tags matching our target roles. All jobs are remote-friendly.
    """
    try:
        resp = requests.get(
            "https://remoteok.com/api",
            headers={"User-Agent": _BROWSER_UA, "Accept": "application/json"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  [RemoteOK] Failed to fetch: {e}")
        return []

    # First element is a legal/metadata notice — skip it
    if isinstance(data, list) and data:
        data = data[1:]

    # Build a set of significant keywords from target titles
    target_keywords: set[str] = set()
    for t in SEARCH_CONFIG["target_titles"]:
        target_keywords.add(t.lower())
        for word in t.lower().split():
            if len(word) >= 5:  # skip short words like "of", "in"
                target_keywords.add(word)

    jobs: list[dict] = []
    for r in data:
        if not isinstance(r, dict):
            continue

        position = (r.get("position") or "").lower()
        tags = [str(t).lower() for t in (r.get("tags") or [])]
        combined = position + " " + " ".join(tags)

        if not any(kw in combined for kw in target_keywords):
            continue

        job_url = r.get("url") or f"https://remoteok.com/remote-jobs/{r.get('id', '')}"

        jobs.append({
            "id": f"remoteok_{r.get('id', '')}",
            "title": r.get("position", "Unknown"),
            "company": r.get("company", "Unknown"),
            "description": _clean_html(r.get("description", ""))[:2000],
            "salary_min": _parse_salary(r.get("salary_min")),
            "salary_max": _parse_salary(r.get("salary_max")),
            "location": r.get("location") or "Remote",
            "url": job_url,
            "posted_date": (r.get("date") or "")[:10],
            "source": "RemoteOK",
            "experience_required": None,
        })

    print(f"[RemoteOK] {len(jobs)} matching jobs")
    return jobs


# ── Orchestrator ──────────────────────────────────────────────────────────────

def scrape_all_sources() -> list:
    sources = [
        ("MyCareersFuture", fetch_mcf),
        ("Adzuna",          fetch_adzuna),
        ("Indeed RSS",      fetch_indeed_rss),
        ("RemoteOK",        fetch_remoteok),
    ]

    all_jobs: list[dict] = []
    for name, fn in sources:
        print(f"\nScanning {name}...\n")
        try:
            jobs = fn()
            all_jobs.extend(jobs)
        except Exception as e:
            print(f"  {name} failed: {e}")

    seen: set = set()
    unique = [j for j in all_jobs if j["id"] not in seen and not seen.add(j["id"])]  # type: ignore[func-returns-value]
    print(f"\nTotal: {len(unique)} unique jobs across {len(sources)} sources")
    return unique
