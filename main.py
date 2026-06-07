#!/usr/bin/env python3
"""
Job Scanner Tool - Main Entry Point
====================================
Scrapes job boards, scores matches against your profile,
generates cover notes, and emails results via Resend.

Usage:
    python main.py                          # Full scan + email notify (analyst mode)
    python main.py --mode=healthcare        # Scan for any job type (AI-generated)
    python main.py --mode=fnb               # F&B / hospitality roles
    python main.py --mode=logistics         # Logistics / supply chain roles
    python main.py --mode=healthcare --refresh  # Regenerate a cached mode config
    python main.py --list-modes             # Show all saved mode configs
    python main.py --no-notify              # Scan only, save to CSV (no email)
    python main.py --reset                  # Clear seen jobs history

Schedule with Windows Task Scheduler:
    Create a task that runs: python C:\\path\\to\\job_scanner\\main.py
"""

import csv
import json
import os
import sys
from datetime import datetime

# Force UTF-8 stdout so emoji in print() never crash on Windows cp1252
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import sentry_sdk

_sentry_dsn = os.environ.get("SENTRY_DSN", "").strip()
if _sentry_dsn:
    sentry_sdk.init(dsn=_sentry_dsn, traces_sample_rate=0.0, send_default_pii=False)

from config import DATA_DIR, JOBS_CSV, SEEN_JOBS_FILE, COVER_NOTES_DIR, SEARCH_CONFIG, set_mode, list_modes
from scrapers import scrape_all_sources
from scorer import rank_jobs, filter_jobs


def load_seen_jobs() -> set:
    """Load set of previously seen job IDs."""
    if os.path.exists(SEEN_JOBS_FILE):
        with open(SEEN_JOBS_FILE, "r") as f:
            return set(json.load(f))
    return set()


def save_seen_jobs(seen: set):
    """Save seen job IDs to file."""
    with open(SEEN_JOBS_FILE, "w") as f:
        json.dump(list(seen), f)


def save_jobs_csv(jobs: list):
    """Append matched jobs to CSV file."""
    file_exists = os.path.exists(JOBS_CSV)

    with open(JOBS_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "scan_date", "id", "title", "company", "location",
            "salary_min", "salary_max", "score", "match_reasons",
            "source", "url", "posted_date", "closing_date",
        ])

        if not file_exists:
            writer.writeheader()

        for job in jobs:
            writer.writerow({
                "scan_date": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "id": job.get("id", ""),
                "title": job.get("title", ""),
                "company": job.get("company", ""),
                "location": job.get("location", ""),
                "salary_min": job.get("salary_min", ""),
                "salary_max": job.get("salary_max", ""),
                "score": job.get("score", 0),
                "match_reasons": " | ".join(job.get("match_reasons", [])),
                "source": job.get("source", ""),
                "url": job.get("url", ""),
                "posted_date": job.get("posted_date", ""),
                "closing_date": job.get("closing_date", ""),
            })


def run_scan(notify: bool = True, mode: str = "analyst"):
    """Run the full job scanning pipeline."""

    print("=" * 50)
    print(f"🚀 JOB SCANNER - Starting scan [{mode.upper()} mode]")
    print(f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)

    # Create data directories
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(COVER_NOTES_DIR, exist_ok=True)

    # 1. Load previously seen jobs
    seen_jobs = load_seen_jobs()
    print(f"\n📂 Previously seen jobs: {len(seen_jobs)}")

    # 2. Scrape all sources
    max_jobs = int(os.environ.get("JOBSCANNER_MAX_JOBS", 0))
    # Free plan: fetch only enough candidates to find the job limit (3× buffer for scoring losses)
    max_fetch = max_jobs * 3 if max_jobs > 0 else 0
    if max_fetch:
        print(f"\n⚡ Free plan — fetching up to {max_fetch} candidates (showing top {max_jobs} matches)")
    all_jobs = scrape_all_sources(max_total=max_fetch)

    if not all_jobs:
        print("\n❌ No jobs found from any source. Check your internet connection.")
        return

    # 3. Filter out already-seen jobs
    new_jobs = [j for j in all_jobs if j["id"] not in seen_jobs]
    print(f"\n🆕 New jobs (not seen before): {len(new_jobs)}")

    if not new_jobs:
        print("No new jobs since last scan.")
        return

    # 4. Score all new jobs
    print("\n📊 Scoring jobs against your profile...")
    scored_jobs = rank_jobs(new_jobs)

    # 5. Filter by minimum score
    threshold = SEARCH_CONFIG["min_score_threshold"]
    matched_jobs = filter_jobs(scored_jobs, threshold)
    print(f"✅ Jobs above score threshold ({threshold}): {len(matched_jobs)}")

    if max_jobs > 0 and len(matched_jobs) > max_jobs:
        print(f"⚠️  Free plan: capping at {max_jobs} matches")
        matched_jobs = matched_jobs[:max_jobs]

    # Show top results
    max_notify = SEARCH_CONFIG["max_jobs_per_notification"]
    top_jobs = matched_jobs[:max_notify]

    if top_jobs:
        print(f"\n🏆 Top {len(top_jobs)} matches:")
        for i, job in enumerate(top_jobs, 1):
            print(f"  {i}. [{job['score']}/100] {job['title']} @ {job['company']} ({job['source']})")
            if job.get("match_reasons"):
                print(f"     → {', '.join(job['match_reasons'][:3])}")

    # 6. Generate cover notes for Pro plan only (free plan skips to save memory)
    cover_notes = {}
    if not max_jobs:
        from cover_notes import generate_cover_note, save_cover_note
        print(f"\n📝 Generating cover notes...")
        for job in top_jobs:
            try:
                note = generate_cover_note(job)
                filepath = save_cover_note(job, note)
                cover_notes[job["id"]] = note
                print(f"  ✅ Saved: {filepath}")
            except Exception as e:
                print(f"  ❌ Failed for {job['title']}: {e}")

    # 7. Save results to CSV
    save_jobs_csv(matched_jobs)
    print(f"\n💾 Results saved to {JOBS_CSV}")

    # 8. Mark all scraped jobs as seen
    for job in all_jobs:
        seen_jobs.add(job["id"])
    save_seen_jobs(seen_jobs)

    # 9. Send email notification (Pro only — free plan skips)
    if notify and not max_jobs:
        try:
            from notifier import send_email_digest
            _email_to      = SEARCH_CONFIG.get("email_to", "")
            _email_enabled = SEARCH_CONFIG.get("email_enabled", False)
            if _email_enabled and _email_to and top_jobs:
                print("\nSending email digest...")
                send_email_digest(top_jobs, {"email_to": _email_to, "email_enabled": True})
        except Exception as _e:
            print(f"Email notification skipped: {_e}")

    # Summary
    print(f"\n{'=' * 50}")
    print(f"📊 SCAN SUMMARY")
    print(f"   Total scraped: {len(all_jobs)}")
    print(f"   New listings:  {len(new_jobs)}")
    print(f"   Matched:       {len(matched_jobs)}")
    print(f"   Cover notes:   {len(cover_notes)}")
    print(f"{'=' * 50}")

    # Print all matched jobs with scores for reference
    if matched_jobs:
        print(f"\n📋 ALL MATCHED JOBS (score ≥ {threshold}):\n")
        for i, job in enumerate(matched_jobs, 1):
            sal = ""
            if job.get("salary_min") and job.get("salary_max"):
                sal = f" | ${job['salary_min']}-${job['salary_max']}"
            print(f"  {i:2}. [{job['score']:3}/100] {job['title']}")
            print(f"      {job['company']}{sal}")
            print(f"      {job['url']}")
            print()


def main():
    # Multi-user hosted mode: load config from tempfile (secure) or env var (legacy local dev)
    _config_file = os.environ.get("JOBSCANNER_CONFIG_FILE")
    if _config_file and os.path.exists(_config_file):
        try:
            import config as _cfg
            with open(_config_file, encoding="utf-8") as _f:
                _cfg_data = _f.read()
        finally:
            try:
                os.unlink(_config_file)  # delete immediately — key is now only in memory
            except OSError:
                pass
        _cfg.load_user_config(_cfg_data)
    elif os.environ.get("JOBSCANNER_USER_CONFIG"):
        import config as _cfg
        _cfg.load_user_config(os.environ["JOBSCANNER_USER_CONFIG"])

    _data_dir = os.environ.get("JOBSCANNER_DATA_DIR")
    if _data_dir:
        import config as _cfg
        _cfg.DATA_DIR        = _data_dir
        _cfg.JOBS_CSV        = f"{_data_dir}/matched_jobs.csv"
        _cfg.SEEN_JOBS_FILE  = f"{_data_dir}/seen_jobs.json"
        _cfg.COVER_NOTES_DIR = f"{_data_dir}/cover_notes"
        # Re-import the updated paths into this module's namespace
        global DATA_DIR, JOBS_CSV, SEEN_JOBS_FILE, COVER_NOTES_DIR
        from config import DATA_DIR, JOBS_CSV, SEEN_JOBS_FILE, COVER_NOTES_DIR

    args = sys.argv[1:]

    if "--list-modes" in args:
        list_modes()
        return

    if "--reset" in args:
        if os.path.exists(SEEN_JOBS_FILE):
            os.remove(SEEN_JOBS_FILE)
            print("Seen jobs history cleared. Next scan will treat all jobs as new.")
        else:
            print("No history file found.")
        if "--reset" == args[-1]:
            return

    # Parse --mode=<anything> and optional --refresh flag
    mode = "analyst"
    refresh = "--refresh" in args
    for arg in args:
        if arg.startswith("--mode="):
            mode = arg.split("=", 1)[1].lower()
            break

    if mode != "analyst":
        set_mode(mode, refresh=refresh)

    notify = "--no-notify" not in args
    run_scan(notify=notify, mode=mode)


if __name__ == "__main__":
    main()
