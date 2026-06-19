"""
Job fetchers — MyCareersFuture, Adzuna, RemoteOK.

All sources use official APIs (no HTML scraping).
LinkedIn / JobStreet / Glints HTML scraping violates their ToS — do not add them back.
"""

import hashlib
import time

import re
import socket

import requests

from config import ADZUNA_APP_ID, ADZUNA_APP_KEY, SEARCH_CONFIG

# Hard backstop — prevents TCP-stall hangs that bypass requests' timeout
socket.setdefaulttimeout(10)

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
    return re.sub(r'<[^>]+>', ' ', html).strip()


def _parse_salary(val) -> int | None:
    if val is None:
        return None
    try:
        return int(str(val).replace("$", "").replace(",", "").strip())
    except (ValueError, AttributeError):
        return None


_USD_TO_SGD = 1.35


def _to_monthly_sgd(val, fx: float = 1.0) -> int | None:
    n = _parse_salary(val)
    if n is None:
        return None
    monthly = n / 12 if n >= 12000 else n
    return int(round(monthly * fx))


def _dedupe_key(job: dict) -> str | None:
    """
    Fingerprint for cross-source duplicate detection: normalised title+company.
    Returns None when either field is missing/Unknown — too risky to merge on.
    """
    title   = re.sub(r"[^a-z0-9]+", " ", (job.get("title") or "").lower()).strip()
    company = re.sub(r"[^a-z0-9]+", " ", (job.get("company") or "").lower()).strip()
    if not title or not company or title == "unknown" or company == "unknown":
        return None
    return f"{title}|{company}"


# ── MyCareersFuture ───────────────────────────────────────────────────────────

_MCF_PRIMARY = (
    "https://api.mycareersfuture.gov.sg/v2/jobs",
    lambda term, page: {"search": term, "limit": 20, "page": page, "sortBy": "new_posting_date"},
)
_MCF_FALLBACKS = [
    (
        "https://api.mycareersfuture.gov.sg/v2/search",
        lambda term, page: {"search": term, "limit": 20, "page": page, "sortBy": "new_posting_date"},
    ),
    (
        "https://api.mycareersfuture.gov.sg/v2/jobs/search",
        lambda term, page: {"keyword": term, "limit": 20, "offset": page * 20},
    ),
]


def fetch_mcf(max_pages: int = 2, max_results: int = 0) -> list:
    """Fetch jobs from MyCareersFuture API for up to 2 target titles."""
    titles = SEARCH_CONFIG.get("target_titles") or []
    if not titles:
        print("  [MCF] No target titles configured.")
        return []

    # No probe request — hardcode primary endpoint to save a rate-limited request
    url, param_fn = _MCF_PRIMARY
    jobs: list[dict] = []
    consecutive_failures = 0

    # 2 titles max — keeps total MCF requests at 2, well within rate limit
    titles_to_search = titles[:2]

    for title in titles_to_search:
        if consecutive_failures >= 3:
            print(f"  [MCF] Too many failures, stopping. Got {len(jobs)} jobs so far.")
            break
        if max_results > 0 and len(jobs) >= max_results:
            print(f"  [MCF] Collected {len(jobs)} candidates — stopping early")
            break
        if len(jobs) >= 200:
            print("  [MCF] Cap reached — stopping to conserve memory")
            break

        print(f"  → Searching: {title}")
        title_count = 0

        for page in range(max_pages):
            try:
                resp = requests.get(url, params=param_fn(title, page), headers=_MCF_HEADERS, timeout=(4, 10))
                if resp.status_code == 429 or resp.status_code >= 500:
                    # Try fallback endpoints before giving up
                    for fb_url, fb_fn in _MCF_FALLBACKS:
                        try:
                            resp = requests.get(fb_url, params=fb_fn(title, page), headers=_MCF_HEADERS, timeout=(4, 10))
                            if resp.status_code == 200:
                                url, param_fn = fb_url, fb_fn
                                break
                        except Exception:
                            continue
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
                        "description": _clean_html(r.get("description", ""))[:800],
                        "salary_min": sal_min,
                        "salary_max": sal_max,
                        "location": location,
                        "url": job_url,
                        "posted_date": posted,
                        "closing_date": closing_date,
                        "source": "MyCareersFuture",
                    })
                    title_count += 1

                print(f"     page {page + 1}: {len(results)} listings")
                time.sleep(2.5)

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

def fetch_adzuna(max_pages: int = 1) -> list:
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
        print(f"  → Searching: {title}")
        for page in range(1, max_pages + 1):
            try:
                resp = requests.get(
                    f"https://api.adzuna.com/v1/api/jobs/sg/search/{page}",
                    params={
                        "app_id": ADZUNA_APP_ID,
                        "app_key": ADZUNA_APP_KEY,
                        "results_per_page": 20,
                        "what": title,
                        "sort_by": "date",
                    },
                    timeout=10,
                )
                resp.raise_for_status()
                results = resp.json().get("results", [])
                if not results:
                    break

                print(f"     page {page}: {len(results)} listings")
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
                        "description": _clean_html(r.get("description", ""))[:800],
                        "salary_min": _to_monthly_sgd(r.get("salary_min")),
                        "salary_max": _to_monthly_sgd(r.get("salary_max")),
                        "location": location.get("display_name", "Singapore") if isinstance(location, dict) else "Singapore",
                        "url": r.get("redirect_url", ""),
                        "posted_date": (r.get("created") or "")[:10],
                        "source": "Adzuna",
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


# ── RemoteOK ──────────────────────────────────────────────────────────────────

def fetch_remoteok() -> list:
    """
    Fetch remote jobs from RemoteOK public API (https://remoteok.com/api).
    Filters by titles/tags matching our target roles. All jobs are remote-friendly.
    """
    try:
        print("  → Fetching all remote listings...")
        resp = requests.get(
            "https://remoteok.com/api",
            headers={"User-Agent": _BROWSER_UA, "Accept": "application/json"},
            timeout=15,
            stream=True,
        )
        resp.raise_for_status()
        # Guard against huge responses — cap at 4MB to prevent OOM on low-memory servers
        chunks = []
        total = 0
        for chunk in resp.iter_content(chunk_size=65536):
            total += len(chunk)
            if total > 4 * 1024 * 1024:
                print("  [RemoteOK] Response too large — truncating at 4MB")
                break
            chunks.append(chunk)
        import json as _json
        try:
            data = _json.loads(b"".join(chunks))
        except Exception:
            print("  [RemoteOK] Could not parse truncated response — skipping")
            return []
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
        if len(jobs) >= 150:
            break
        if not isinstance(r, dict):
            continue

        position = (r.get("position") or "").lower()
        tags = [str(t).lower() for t in (r.get("tags") or [])]
        combined = position + " " + " ".join(tags)

        if not any(kw in combined for kw in target_keywords):
            continue

        # Strict Singapore-only: RemoteOK is a global board, so drop anything
        # not explicitly located in Singapore (MCF/Adzuna are already SG-scoped).
        # Worldwide/Anywhere/foreign-country remote roles are excluded.
        if "singapore" not in (r.get("location") or "").lower():
            continue

        job_url = r.get("url") or f"https://remoteok.com/remote-jobs/{r.get('id', '')}"

        jobs.append({
            "id": f"remoteok_{r.get('id', '')}",
            "title": r.get("position", "Unknown"),
            "company": r.get("company", "Unknown"),
            "description": _clean_html(r.get("description", ""))[:800],
            "salary_min": _to_monthly_sgd(r.get("salary_min"), fx=_USD_TO_SGD),
            "salary_max": _to_monthly_sgd(r.get("salary_max"), fx=_USD_TO_SGD),
            "location": r.get("location") or "Remote",
            "url": job_url,
            "posted_date": (r.get("date") or "")[:10],
            "source": "RemoteOK",
        })

    print(f"[RemoteOK] {len(jobs)} matching jobs")
    return jobs


# ── Orchestrator ──────────────────────────────────────────────────────────────

_GLOBAL_JOB_CAP = 500  # Bounded for memory safety on 2GB Render across 3 sources


def scrape_all_sources(max_total: int = 0) -> list:
    effective_cap = min(max_total, _GLOBAL_JOB_CAP) if max_total > 0 else _GLOBAL_JOB_CAP

    all_jobs: list[dict] = []
    seen_ids: set = set()
    seen_keys: set = set()
    duplicates = 0

    def _ingest(jobs: list) -> None:
        nonlocal duplicates
        for j in jobs:
            if len(all_jobs) >= effective_cap:
                break
            if j["id"] in seen_ids:
                continue
            key = _dedupe_key(j)
            if key is not None and key in seen_keys:
                duplicates += 1
                continue
            seen_ids.add(j["id"])
            if key is not None:
                seen_keys.add(key)
            all_jobs.append(j)

    for name, fetcher in [
        ("MyCareersFuture", lambda: fetch_mcf(max_pages=2, max_results=effective_cap)),
        ("Adzuna",          lambda: fetch_adzuna(max_pages=2)),
        ("RemoteOK",        fetch_remoteok),
    ]:
        if len(all_jobs) >= effective_cap:
            break
        print(f"\nScanning {name}...\n")
        try:
            _ingest(fetcher())
        except Exception as e:
            print(f"  {name} failed: {e}")

    if duplicates:
        print(f"\n  Skipped {duplicates} cross-source duplicate(s)")
    print(f"\nTotal: {len(all_jobs)} unique jobs")
    return all_jobs
