"""
JSearch / OpenWeb Ninja API job fetcher for CareerScan.

The active scan flow uses API-backed job sourcing only. Do not add HTML scraping
for job boards such as LinkedIn, JobStreet, JobsDB, Glints, or similar sites.
"""

import hashlib
import time
from datetime import datetime, timezone

import re
import socket

import requests

from config import (
    ADZUNA_APP_ID,
    ADZUNA_APP_KEY,
    DEFAULT_JOB_REGION,
    JSEARCH_API_HOST,
    JSEARCH_API_KEY,
    SEARCH_CONFIG,
    get_job_region,
    normalize_job_region,
)

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


def _cfg_region_key(cfg: dict | None = None) -> str:
    return normalize_job_region((cfg if cfg is not None else SEARCH_CONFIG).get("job_region"))


def _to_monthly_sgd(val, fx: float = 1.0) -> int | None:
    n = _parse_salary(val)
    if n is None:
        return None
    monthly = n / 12 if n >= 12000 else n
    return int(round(monthly * fx))


def _to_monthly_from_period(val, period: str | None = "") -> int | None:
    n = _parse_salary(val)
    if n is None:
        return None
    p = (period or "").lower()
    if any(word in p for word in ("year", "annual", "yr")):
        return int(round(n / 12))
    if any(word in p for word in ("hour", "hr")):
        return int(round(n * 160))
    if any(word in p for word in ("week", "wk")):
        return int(round(n * 4.33))
    return int(round(n))


def _format_posted_date(value) -> str:
    """Return YYYY-MM-DD from JSearch date strings or Unix timestamps.

    JSearch can return either an ISO-like string (job_posted_at_datetime_utc)
    or a numeric timestamp (job_posted_at_timestamp). Keep parsing defensive so
    one unexpected item never breaks the entire scan.
    """
    if value in (None, ""):
        return ""

    # Numeric Unix timestamp: seconds or milliseconds.
    if isinstance(value, (int, float)):
        try:
            ts = float(value)
            if ts > 10_000_000_000:  # milliseconds
                ts /= 1000
            return time.strftime("%Y-%m-%d", time.gmtime(ts))
        except (OverflowError, OSError, ValueError):
            return ""

    s = str(value).strip()
    if not s:
        return ""

    if s.isdigit():
        try:
            ts = float(s)
            if ts > 10_000_000_000:  # milliseconds
                ts /= 1000
            return time.strftime("%Y-%m-%d", time.gmtime(ts))
        except (OverflowError, OSError, ValueError):
            return ""

    return s[:10]



def _format_jsearch_posted_date(value) -> str:
    """Return YYYY-MM-DD from JSearch date strings or second/millisecond timestamps."""
    if value is None:
        return ""

    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 10_000_000_000:
            ts = ts / 1000
        try:
            return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            return ""

    text = str(value).strip()
    if not text:
        return ""

    if text.isdigit():
        try:
            ts = float(text)
            if ts > 10_000_000_000:
                ts = ts / 1000
            return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            return ""

    return text[:10]


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


_JSEARCH_MAX_TITLES = 4


def fetch_jsearch(max_pages: int = 1, max_results: int = 0, cfg: dict | None = None) -> list:
    """
    Fetch jobs from JSearch / OpenWeb Ninja via RapidAPI.
    Uses the selected region's ISO-style country code as the API country filter.
    """
    if not JSEARCH_API_KEY:
        print("  [JSearch] Skipping - JSEARCH_API_KEY is not set.")
        return []

    active_cfg = cfg if cfg is not None else SEARCH_CONFIG
    region_key = _cfg_region_key(active_cfg)
    region_info = get_job_region(region_key)
    if "jsearch" not in region_info.get("enabled_sources", []):
        print(f"  [JSearch] {region_info['label']} is not available yet.")
        return []

    titles = active_cfg.get("target_titles") or []
    if not titles:
        print("  [JSearch] No target titles configured.")
        return []

    headers = {
        "X-RapidAPI-Key": JSEARCH_API_KEY,
        "X-RapidAPI-Host": JSEARCH_API_HOST,
    }
    jobs: list[dict] = []
    seen_ids: set = set()

    for title in titles[:_JSEARCH_MAX_TITLES]:
        if max_results > 0 and len(jobs) >= max_results:
            print(f"  [JSearch] Collected {len(jobs)} candidates - stopping early")
            break

        print(f"  -> Searching {region_info['label']}: {title}")
        for page in range(1, max_pages + 1):
            if max_results > 0 and len(jobs) >= max_results:
                break
            try:
                resp = requests.get(
                    f"https://{JSEARCH_API_HOST}/search-v2",
                    headers=headers,
                    params={
                        "query": title,
                        "page": page,
                        "num_pages": 1,
                        "country": region_key,
                        "date_posted": "month",
                    },
                    timeout=15,
                )
                resp.raise_for_status()
                data = resp.json()
                payload = data.get("data", {}) if isinstance(data, dict) else {}
                if isinstance(payload, dict):
                    results = payload.get("jobs", [])
                elif isinstance(payload, list):
                    results = payload
                else:
                    results = []
                if not results:
                    break

                print(f"     page {page}: {len(results)} listings")
                for r in results:
                    if not isinstance(r, dict):
                        continue
                    raw_id = r.get("job_id") or r.get("job_apply_link") or r.get("job_google_link")
                    if not raw_id:
                        raw_id = hashlib.md5(
                            f"{r.get('job_title','')}|{r.get('employer_name','')}|{region_key}".encode()
                        ).hexdigest()[:12]
                    job_id = f"jsearch_{region_key}_{raw_id}"
                    if job_id in seen_ids:
                        continue
                    seen_ids.add(job_id)

                    location_parts = [
                        r.get("job_city"),
                        r.get("job_state"),
                        r.get("job_country") or region_info["label"],
                    ]
                    location = ", ".join(str(p) for p in location_parts if p)
                    salary_period = r.get("job_salary_period") or ""

                    jobs.append({
                        "id": job_id,
                        "title": r.get("job_title") or "Unknown",
                        "company": r.get("employer_name") or "Unknown",
                        "description": _clean_html(r.get("job_description", ""))[:1200],
                        "salary_min": _to_monthly_from_period(r.get("job_min_salary"), salary_period),
                        "salary_max": _to_monthly_from_period(r.get("job_max_salary"), salary_period),
                        "location": location or region_info["label"],
                        "url": r.get("job_apply_link") or r.get("job_google_link") or "",
                        "posted_date": _format_posted_date(
                            r.get("job_posted_at_datetime_utc")
                            or r.get("job_posted_at_timestamp")
                        ),
                        "source": "JSearch",
                        "region": region_key,
                    })

                time.sleep(0.4)

            except requests.exceptions.Timeout:
                print(f"  [JSearch] Timeout for '{title}' page {page} - skipping")
                break
            except Exception as e:
                print(f"  [JSearch] Error for '{title}' page {page}: {e}")
                break

    print(f"[JSearch] {len(jobs)} unique jobs")
    return jobs


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


def fetch_mcf(max_pages: int = 2, max_results: int = 0, cfg: dict | None = None) -> list:
    """Fetch jobs from MyCareersFuture API for up to 2 target titles."""
    region_key = _cfg_region_key(cfg)
    if region_key != DEFAULT_JOB_REGION:
        print("  [MCF] Skipping - MyCareersFuture is Singapore-only.")
        return []

    titles = (cfg if cfg is not None else SEARCH_CONFIG).get("target_titles") or []
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
                        "region": region_key,
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

# Cap titles per scan: Adzuna's free tier allows only 100 calls/day (shared
# across all users on one app key), and breadth across titles beats depth
# within one keyword. 3 titles keeps each scan cheap and fast.
_ADZUNA_MAX_TITLES = 3


def fetch_adzuna(max_pages: int = 1, max_results: int = 0, cfg: dict | None = None) -> list:
    """
    Fetch Singapore jobs from Adzuna (https://developer.adzuna.com).
    Requires ADZUNA_APP_ID and ADZUNA_APP_KEY env vars (free at developer.adzuna.com).
    Free tier: 100 calls/day, 50 results/page.
    """
    region_key = _cfg_region_key(cfg)
    if region_key != DEFAULT_JOB_REGION:
        print("  [Adzuna] Skipping - configured only for Singapore in this MVP.")
        return []

    if not ADZUNA_APP_ID or not ADZUNA_APP_KEY:
        print("  [Adzuna] Skipping — ADZUNA_APP_ID / ADZUNA_APP_KEY not set.")
        return []

    jobs: list[dict] = []
    seen_ids: set = set()

    titles = (cfg if cfg is not None else SEARCH_CONFIG)["target_titles"][:_ADZUNA_MAX_TITLES]
    for title in titles:
        if max_results > 0 and len(jobs) >= max_results:
            print(f"  [Adzuna] Collected {len(jobs)} candidates — stopping early")
            break
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
                        "region": region_key,
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

def fetch_remoteok(cfg: dict | None = None) -> list:
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

    region_key = _cfg_region_key(cfg)
    region_info = get_job_region(region_key)

    # Build a set of significant keywords from target titles
    target_keywords: set[str] = set()
    for t in (cfg if cfg is not None else SEARCH_CONFIG)["target_titles"]:
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

        location_text = (r.get("location") or "").lower()
        region_terms = [t.lower() for t in region_info.get("remoteok_location_terms", [])]
        global_remote_terms = ["worldwide", "anywhere", "global", "remote", "apac", "asia"]

        if region_key == DEFAULT_JOB_REGION:
            # Preserve existing Singapore behavior for RemoteOK.
            if "singapore" not in location_text:
                continue
        elif not (
            any(term in location_text for term in region_terms)
            or any(term in location_text for term in global_remote_terms)
        ):
            continue

        job_url = r.get("url") or f"https://remoteok.com/remote-jobs/{r.get('id', '')}"
        salary_min = _to_monthly_sgd(r.get("salary_min"), fx=_USD_TO_SGD) if region_key == DEFAULT_JOB_REGION else None
        salary_max = _to_monthly_sgd(r.get("salary_max"), fx=_USD_TO_SGD) if region_key == DEFAULT_JOB_REGION else None

        jobs.append({
            "id": f"remoteok_{r.get('id', '')}",
            "title": r.get("position", "Unknown"),
            "company": r.get("company", "Unknown"),
            "description": _clean_html(r.get("description", ""))[:800],
            "salary_min": salary_min,
            "salary_max": salary_max,
            "location": r.get("location") or "Remote",
            "url": job_url,
            "posted_date": (r.get("date") or "")[:10],
            "source": "RemoteOK",
            "region": region_key,
        })

    print(f"[RemoteOK] {len(jobs)} matching jobs")
    return jobs


# ── Orchestrator ──────────────────────────────────────────────────────────────

_GLOBAL_JOB_CAP = 500  # Bounded for memory safety on 2GB Render across 3 sources


def scrape_all_sources(max_total: int = 0, cfg: dict | None = None) -> list:
    # Pass cfg explicitly to each fetcher so concurrent scans (gthread worker)
    # never read each other's titles via the shared module-level SEARCH_CONFIG.
    effective_cap = min(max_total, _GLOBAL_JOB_CAP) if max_total > 0 else _GLOBAL_JOB_CAP

    all_jobs: list[dict] = []
    seen_ids: set = set()
    seen_keys: set = set()
    duplicates = 0
    region_key = _cfg_region_key(cfg)
    region_info = get_job_region(region_key)
    enabled_sources = set(region_info.get("enabled_sources", []))

    print(f"\nSelected job region: {region_info['label']}")

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
            j.setdefault("region", region_key)
            all_jobs.append(j)

    for source_key, name, fetcher in [
        ("jsearch", "JSearch", lambda: fetch_jsearch(max_pages=1, max_results=effective_cap, cfg=cfg)),
    ]:
        if source_key not in enabled_sources:
            print(f"\nSkipping {name} - not enabled for {region_info['label']} in this MVP.")
            continue
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
