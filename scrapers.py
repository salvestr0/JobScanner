"""
Job fetcher — MyCareersFuture (Singapore government public API).

MCF is the only data source. It is legally licensed, stable, and covers
the Singapore market well. All previous scrapers (LinkedIn, Indeed,
JobStreet, Glints) violated those platforms' ToS and have been removed.
"""

import hashlib
import time

import requests
from bs4 import BeautifulSoup

from config import SEARCH_CONFIG

_MCF_HEADERS = {
    "Accept": "application/json",
    "Origin": "https://www.mycareersfuture.gov.sg",
    "Referer": "https://www.mycareersfuture.gov.sg/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
}

# Endpoints tried in order — MCF changes these periodically.
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
    """Probe MCF endpoints and return the first one that returns job data."""
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

                    desc = r.get("description", "")
                    desc_clean = BeautifulSoup(desc, "html.parser").get_text(separator=" ") if "<" in desc else desc

                    address = r.get("address", {})
                    location = ""
                    if isinstance(address, dict):
                        location = address.get("streetAddress", "") or address.get("addressRegion", "")

                    metadata = r.get("metadata", {}) if isinstance(r.get("metadata"), dict) else {}
                    posted = metadata.get("newPostingDate", "")
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
                        "description": desc_clean[:2000],
                        "salary_min": sal_min,
                        "salary_max": sal_max,
                        "location": location,
                        "url": job_url,
                        "posted_date": posted,
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


def scrape_all_sources() -> list:
    print("\nScanning MyCareersFuture...\n")
    try:
        jobs = fetch_mcf()
    except Exception as e:
        print(f"  MyCareersFuture failed: {e}")
        jobs = []
    print(f"\nTotal jobs found: {len(jobs)}")
    return jobs
