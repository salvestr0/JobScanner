"""Seed a local SQLite DB with N test users for load testing.

All users share the SAME password ("loadtest123") and — to avoid running bcrypt
1000 times — the SAME precomputed password hash. check_password() still validates
because it just runs bcrypt.checkpw(plain, stored_hash).

Each user gets settings, ~20 jobs and ~5 application statuses so authenticated
read endpoints (/api/stats, /api/jobs, /api/applications, /api/analytics) return
realistic payloads under load.

Usage:  python loadtest/seed.py [num_users]
Writes: loadtest/users.json  (list of emails the locustfile logs in as)
"""
import json
import os
import sys

import bcrypt
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv()
os.environ.setdefault("DATABASE_URL", "sqlite:///loadtest.db")

from app import app, db  # noqa: E402  (import after env is loaded)
from models import User, UserSettings, Job, ApplicationStatus  # noqa: E402

NUM_USERS = int(sys.argv[1]) if len(sys.argv) > 1 else 1000
PASSWORD = "loadtest123"
JOBS_PER_USER = 20
APPS_PER_USER = 5

SOURCES = ["mycareersfuture", "adzuna", "remoteok"]
TITLES = ["Data Analyst", "Software Engineer", "AI Engineer", "DevOps", "Backend Dev"]
COMPANIES = ["Acme SG", "GovTech", "DBS", "Shopee", "Grab", "Sea", "Razer"]


def main():
    # One bcrypt hash, reused for every user (huge speedup vs 1000 hashes).
    shared_hash = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt()).decode()

    with app.app_context():
        db.create_all()

        # Clean any prior load-test users so reseeding is idempotent.
        existing = User.query.filter(User.email.like("loadtest+%@example.com")).all()
        if existing:
            for u in existing:
                db.session.delete(u)
            db.session.commit()
            print(f"Removed {len(existing)} pre-existing load-test users")

        emails = []
        users, settings, jobs, apps = [], [], [], []
        for i in range(NUM_USERS):
            email = f"loadtest+{i}@example.com"
            emails.append(email)
            u = User(
                email=email,
                password_hash=shared_hash,
                email_verified=True,
                subscription_status="active" if i % 5 == 0 else "free",
            )
            users.append(u)

        db.session.add_all(users)
        db.session.flush()  # assigns u.id

        for i, u in enumerate(users):
            settings.append(UserSettings(
                user_id=u.id,
                target_titles=[TITLES[i % len(TITLES)]],
                email_to=u.email,
            ))
            for j in range(JOBS_PER_USER):
                jobs.append(Job(
                    user_id=u.id,
                    source_job_id=f"{u.id[:8]}-job-{j}",
                    title=TITLES[j % len(TITLES)],
                    company=COMPANIES[j % len(COMPANIES)],
                    location="Singapore",
                    source=SOURCES[j % len(SOURCES)],
                    url=f"https://example.com/job/{j}",
                    salary_min=3000, salary_max=5000,
                    score=50 + (j % 50),
                    match_reasons="Skills match; location match",
                ))
            for k in range(APPS_PER_USER):
                apps.append(ApplicationStatus(
                    user_id=u.id,
                    job_source_id=f"{u.id[:8]}-job-{k}",
                    status=["applied", "interview", "skip"][k % 3],
                    title=TITLES[k % len(TITLES)],
                    company=COMPANIES[k % len(COMPANIES)],
                ))

        db.session.add_all(settings)
        db.session.add_all(jobs)
        db.session.add_all(apps)
        db.session.commit()

        print(f"Seeded {len(users)} users, {len(jobs)} jobs, {len(apps)} app statuses")

    out = os.path.join(os.path.dirname(__file__), "users.json")
    with open(out, "w") as f:
        json.dump({"password": PASSWORD, "emails": emails}, f)
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
