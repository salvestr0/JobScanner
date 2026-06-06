"""
One-time migration: imports existing CSV/JSON flat-file data into PostgreSQL.

Usage:
    Set MIGRATE_EMAIL and MIGRATE_PASSWORD in your .env (or environment),
    then run:
        python migrate_data.py

The script will:
  1. Create a user with those credentials
  2. Import data/matched_jobs.csv     → jobs table
  3. Import data/application_status.json → application_statuses table
  4. Import data/seen_jobs.json       → seen_jobs table
  5. Import data/profile.json         → user_profiles table
  6. Import data/ui_settings.json     → user_settings table
  7. Import data/modes_cache.json     → search_modes table

Safe to run multiple times — existing rows are skipped, not duplicated.
"""

import csv
import json
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

# ── Bootstrap Flask app context ───────────────────────────────────────────────
from app import app
from models import (
    ApplicationStatus, Job, SearchMode, SeenJob, User,
    UserProfile, UserSettings, db,
)

EMAIL    = os.getenv("MIGRATE_EMAIL", "").strip()
PASSWORD = os.getenv("MIGRATE_PASSWORD", "").strip()

if not EMAIL or not PASSWORD:
    print("ERROR: Set MIGRATE_EMAIL and MIGRATE_PASSWORD in .env before running.")
    sys.exit(1)

DATA_DIR = "data"


def _parse_date(s: str):
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            pass
    return datetime.now(timezone.utc)


with app.app_context():
    db.create_all()

    # ── 1. Create or find user ─────────────────────────────────────────────────
    user = User.query.filter_by(email=EMAIL).first()
    if user:
        print(f"User {EMAIL} already exists — using existing account.")
    else:
        user = User(email=EMAIL)
        user.set_password(PASSWORD)
        db.session.add(user)
        db.session.commit()
        print(f"Created user: {EMAIL}")

    user_id = user.id

    # ── 2. matched_jobs.csv → jobs ─────────────────────────────────────────────
    jobs_csv = os.path.join(DATA_DIR, "matched_jobs.csv")
    if os.path.exists(jobs_csv):
        inserted = 0
        skipped  = 0
        with open(jobs_csv, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                job_id = row.get("id", "").strip()
                if not job_id:
                    continue
                exists = Job.query.filter_by(user_id=user_id, source_job_id=job_id).first()
                if exists:
                    skipped += 1
                    continue

                def _int(v):
                    try:
                        return int(v) if v and str(v).strip() else None
                    except ValueError:
                        return None

                job = Job(
                    user_id       = user_id,
                    source_job_id = job_id,
                    title         = row.get("title", ""),
                    company       = row.get("company", ""),
                    location      = row.get("location", ""),
                    source        = row.get("source", ""),
                    url           = row.get("url", ""),
                    posted_date   = row.get("posted_date", ""),
                    salary_min    = _int(row.get("salary_min")),
                    salary_max    = _int(row.get("salary_max")),
                    score         = _int(row.get("score")) or 0,
                    match_reasons = row.get("match_reasons", ""),
                    scan_date     = _parse_date(row.get("scan_date", "")),
                )
                db.session.add(job)
                inserted += 1

        db.session.commit()
        print(f"Jobs: {inserted} inserted, {skipped} skipped (duplicates)")
    else:
        print(f"No {jobs_csv} found — skipping jobs import")

    # ── 3. application_status.json → application_statuses ─────────────────────
    status_file = os.path.join(DATA_DIR, "application_status.json")
    if os.path.exists(status_file):
        with open(status_file, encoding="utf-8") as f:
            statuses = json.load(f)

        inserted = skipped = 0
        for job_id, info in statuses.items():
            exists = db.session.get(ApplicationStatus, (user_id, job_id))
            if exists:
                skipped += 1
                continue
            row = ApplicationStatus(
                user_id        = user_id,
                job_source_id  = job_id,
                status         = info.get("status", ""),
                title          = info.get("title", ""),
                company        = info.get("company", ""),
                url            = info.get("url", ""),
                notes          = info.get("notes", ""),
                interview_date = info.get("interview_date", ""),
                interview_time = info.get("interview_time", ""),
                updated_at     = _parse_date(info.get("updated_at", "")),
            )
            db.session.add(row)
            inserted += 1

        db.session.commit()
        print(f"Application statuses: {inserted} inserted, {skipped} skipped")
    else:
        print(f"No {status_file} found — skipping")

    # ── 4. seen_jobs.json → seen_jobs ──────────────────────────────────────────
    seen_file = os.path.join(DATA_DIR, "seen_jobs.json")
    if os.path.exists(seen_file):
        with open(seen_file, encoding="utf-8") as f:
            seen_ids = json.load(f)

        inserted = skipped = 0
        for job_id in seen_ids:
            exists = db.session.get(SeenJob, (user_id, job_id))
            if exists:
                skipped += 1
                continue
            db.session.add(SeenJob(user_id=user_id, job_source_id=job_id))
            inserted += 1

        db.session.commit()
        print(f"Seen jobs: {inserted} inserted, {skipped} skipped")
    else:
        print(f"No {seen_file} found — skipping")

    # ── 5. profile.json → user_profiles ───────────────────────────────────────
    profile_file = os.path.join(DATA_DIR, "profile.json")
    if os.path.exists(profile_file):
        with open(profile_file, encoding="utf-8") as f:
            p_data = json.load(f)

        existing = db.session.get(UserProfile, user_id)
        if existing:
            print("Profile: already exists — skipping")
        else:
            db.session.add(UserProfile(
                user_id            = user_id,
                name               = p_data.get("name", ""),
                phone              = p_data.get("phone", ""),
                education          = p_data.get("education", ""),
                experience_summary = p_data.get("experience_summary", ""),
                technical_skills   = p_data.get("technical_skills", []),
                soft_skills        = p_data.get("soft_skills", []),
                work_history       = p_data.get("work_history", []),
                certifications     = p_data.get("certifications", []),
                projects           = p_data.get("projects", []),
            ))
            db.session.commit()
            print("Profile: imported")
    else:
        print(f"No {profile_file} found — skipping")

    # ── 6. ui_settings.json → user_settings ───────────────────────────────────
    settings_file = os.path.join(DATA_DIR, "ui_settings.json")
    if os.path.exists(settings_file):
        with open(settings_file, encoding="utf-8") as f:
            s_data = json.load(f)

        existing = db.session.get(UserSettings, user_id)
        if existing:
            print("Settings: already exists — skipping")
        else:
            import config as cfg
            db.session.add(UserSettings(
                user_id                   = user_id,
                min_salary                = s_data.get("min_salary",                cfg.SEARCH_CONFIG.get("min_salary", 2200)),
                max_salary                = s_data.get("max_salary",                cfg.SEARCH_CONFIG.get("max_salary", 4000)),
                min_score_threshold       = s_data.get("min_score_threshold",       cfg.SEARCH_CONFIG.get("min_score_threshold", 40)),
                max_jobs_per_notification = s_data.get("max_jobs_per_notification", cfg.SEARCH_CONFIG.get("max_jobs_per_notification", 20)),
                email_enabled             = s_data.get("email_enabled", False),
                email_to                  = s_data.get("email_to", ""),
                target_titles             = cfg.SEARCH_CONFIG.get("target_titles", []),
                preferred_keywords        = cfg.SEARCH_CONFIG.get("preferred_keywords", []),
                negative_keywords         = cfg.SEARCH_CONFIG.get("negative_keywords", []),
                location_keywords         = cfg.SEARCH_CONFIG.get("location_keywords", []),
                preferred_location        = cfg.SEARCH_CONFIG.get("preferred_location", "Sengkang"),
            ))
            db.session.commit()
            print("Settings: imported")
    else:
        print(f"No {settings_file} found — skipping")

    # ── 7. modes_cache.json → search_modes ────────────────────────────────────
    modes_file = os.path.join(DATA_DIR, "modes_cache.json")
    if os.path.exists(modes_file):
        with open(modes_file, encoding="utf-8") as f:
            modes = json.load(f)

        inserted = skipped = 0
        for name, config in modes.items():
            exists = SearchMode.query.filter_by(user_id=user_id, name=name).first()
            if exists:
                skipped += 1
                continue
            db.session.add(SearchMode(user_id=user_id, name=name, config=config))
            inserted += 1

        db.session.commit()
        print(f"Search modes: {inserted} inserted, {skipped} skipped")
    else:
        print(f"No {modes_file} found — skipping")

    print("\nMigration complete.")
    print(f"User ID: {user_id}")
    print("Log in at http://localhost:5000/login")
