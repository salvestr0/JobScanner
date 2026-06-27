"""
CareerScan Web UI — Flask backend (multi-user)
Run: python run.py
Open: http://localhost:5000
"""
import hashlib
import hmac
import html
import json
import os
import queue
import re
import secrets
import threading
import traceback as _traceback
import warnings
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sentry_sdk
from sentry_sdk.integrations.flask import FlaskIntegration

_sentry_dsn = os.getenv("SENTRY_DSN", "").strip()
if _sentry_dsn:
    sentry_sdk.init(
        dsn=_sentry_dsn,
        integrations=[FlaskIntegration()],
        traces_sample_rate=0.1,
        send_default_pii=False,
    )

from authlib.integrations.flask_client import OAuth
from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, redirect, render_template, request, stream_with_context, url_for
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import LoginManager, current_user, login_required, login_user, logout_user
from flask_migrate import Migrate
from werkzeug.middleware.proxy_fix import ProxyFix

from models import (
    ApplicationStatus, ErrorLog, Job, ResumeFile, ResumeVersion, ScanHistory,
    SearchMode, SeenJob, User, UserProfile, UserSettings, db,
)

load_dotenv()

# Singapore is the only market — user-facing dates/times are SGT (UTC+8) while
# the server clock is UTC. Use these helpers for anything users see or schedule.
SGT = timezone(timedelta(hours=8))


def _sgt_today():
    """Current calendar date in Singapore time (so daily limits reset at SGT midnight)."""
    return datetime.now(SGT).date()


_secret_key = os.getenv("SECRET_KEY", "").strip()
if not _secret_key:
    raise RuntimeError(
        "SECRET_KEY environment variable is not set. "
        "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\" "
        "then add SECRET_KEY=<value> to your .env file or hosting environment."
    )

_in_dev = os.getenv("FLASK_ENV") == "development"

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config["JSON_SORT_KEYS"]       = False
app.config["MAX_CONTENT_LENGTH"]   = 5 * 1024 * 1024
app.config["SECRET_KEY"]           = _secret_key
_db_uri = os.getenv("DATABASE_URL", "sqlite:///jobscanner_dev.db")
app.config["SQLALCHEMY_DATABASE_URI"] = _db_uri
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# Worker thread count (mirrors gunicorn.conf.py's GUNICORN_THREADS). The DB pool
# is sized from it so that, when threads are raised for more concurrency, threads
# don't simply block waiting for a free DB connection (a hidden second bottleneck).
_gthreads = int(os.getenv("GUNICORN_THREADS", "8"))
_engine_opts = {"pool_pre_ping": True, "pool_recycle": 300}
if not _db_uri.startswith("sqlite"):
    # Postgres (Supabase) only — SQLite uses a different pool and ignores these.
    _engine_opts.update({
        "pool_size":    int(os.getenv("DB_POOL_SIZE", str(_gthreads))),
        "max_overflow": int(os.getenv("DB_MAX_OVERFLOW", "4")),
        "pool_timeout": 30,
    })
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = _engine_opts
app.config.update(
    SESSION_COOKIE_SECURE=not _in_dev,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    REMEMBER_COOKIE_SECURE=not _in_dev,
    REMEMBER_COOKIE_HTTPONLY=True,
    REMEMBER_COOKIE_SAMESITE="Lax",
)

db.init_app(app)
migrate = Migrate(app, db)


def _reconcile_orphaned_scans():
    """Mark scans stuck at 'running' as failed on boot.

    Scan state lives in process memory, so at startup no scan can really be
    running — any 'running' ScanHistory row is an orphan from a worker that
    was killed mid-scan (e.g. SIGKILL on timeout/OOM), whose `finally` never
    ran. Self-heals on every restart. Wrapped so a transient DB issue never
    blocks app boot.
    """
    try:
        with app.app_context():
            orphaned = ScanHistory.query.filter_by(status="running").all()
            for row in orphaned:
                row.status = "failed"
                if row.finished_at is None:
                    row.finished_at = datetime.now(timezone.utc)
            if orphaned:
                db.session.commit()
                app.logger.info("Reconciled %d orphaned 'running' scan(s) on startup", len(orphaned))
    except Exception as exc:
        app.logger.warning("Could not reconcile orphaned scans on startup: %s", exc)


_reconcile_orphaned_scans()

login_manager = LoginManager(app)
login_manager.login_view = "login_page"
login_manager.login_message = ""

_redis_url = os.getenv("REDIS_URL", "").strip()
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=["300/minute"],
    storage_uri=_redis_url if _redis_url else "memory://",
)

oauth = OAuth(app)
google_oauth = oauth.register(
    name="google",
    client_id=os.getenv("GOOGLE_CLIENT_ID"),
    client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

_enc_key = (os.getenv("ENCRYPTION_KEY") or "").strip().encode()
_fernet  = Fernet(_enc_key) if _enc_key else None


def _encrypt_api_key(plaintext: str) -> str:
    if not plaintext or not _fernet:
        return plaintext
    return _fernet.encrypt(plaintext.encode()).decode()


def _decrypt_api_key(ciphertext: str) -> str:
    if not ciphertext or not _fernet:
        return ciphertext
    try:
        return _fernet.decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        warnings.warn("Fernet decryption failed — ENCRYPTION_KEY mismatch or corrupted value. Returning raw value.")
        return ciphertext
    except Exception:
        return ciphertext


_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


def _valid_email(addr: str) -> bool:
    return bool(addr) and len(addr) <= 255 and _EMAIL_RE.match(addr) is not None


def _log_gemini_usage(endpoint: str, user_id: str, response_json: dict) -> None:
    usage = response_json.get("usageMetadata", {})
    app.logger.info(
        "gemini_usage endpoint=%s user=%s prompt_tokens=%s output_tokens=%s total_tokens=%s",
        endpoint,
        user_id,
        usage.get("promptTokenCount", "?"),
        usage.get("candidatesTokenCount", "?"),
        usage.get("totalTokenCount", "?"),
    )


def _send_email(to: str, subject: str, html: str, reply_to: str = None) -> bool:
    api_key   = os.getenv("RESEND_API_KEY", "").strip()
    from_addr = os.getenv("RESEND_FROM", "CareerScan <noreply@jobscanner.app>").strip()
    if not api_key:
        app.logger.warning("Email not sent to %s: RESEND_API_KEY not set", to)
        return False
    if "resend.dev" in from_addr:
        # The shared test domain only delivers to the Resend account owner's
        # own address — real users never receive these emails.
        app.logger.warning(
            "RESEND_FROM is the resend.dev test address — emails to anyone "
            "but the Resend account owner will not be delivered. Verify a "
            "custom domain in Resend and set RESEND_FROM."
        )
    try:
        import requests as _req
        payload = {"from": from_addr, "to": [to], "subject": subject, "html": html}
        if reply_to:
            payload["reply_to"] = reply_to
        resp = _req.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=10,
        )
        if resp.status_code >= 400:
            app.logger.error(
                "Resend rejected email to %s (from %r): %s %s",
                to, from_addr, resp.status_code, resp.text[:500],
            )
            return False
        # Log the Resend message id so deliveries can be cross-referenced
        # in the Resend dashboard (and mismatched API keys spotted).
        try:
            msg_id = resp.json().get("id", "?")
        except Exception:
            msg_id = "?"
        app.logger.info("Email accepted by Resend: to=%s subject=%r id=%s", to, subject, msg_id)
        return True
    except Exception as exc:
        app.logger.error("Email send to %s failed: %s", to, exc, exc_info=True)
        return False


def _verification_email_html(verify_url: str) -> str:
    return f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#f8fafc;font-family:ui-sans-serif,system-ui,sans-serif">
  <div style="max-width:480px;margin:40px auto;background:white;border-radius:12px;padding:32px;box-shadow:0 1px 3px rgba(0,0,0,.08)">
    <div style="margin-bottom:20px">
      <span style="display:inline-block;background:#EEF2FF;color:#4F46E5;font-size:12px;font-weight:700;padding:3px 10px;border-radius:20px;letter-spacing:.05em">JOB SCANNER</span>
    </div>
    <h2 style="margin:0 0 8px;font-size:20px;color:#1e293b;font-weight:700">Verify your email</h2>
    <p style="margin:0 0 24px;color:#64748b;font-size:14px;line-height:1.6">
      Thanks for signing up! Click the button below to verify your email address and get the most out of CareerScan.
    </p>
    <a href="{verify_url}" style="display:inline-block;background:#4F46E5;color:white;font-size:14px;font-weight:600;padding:12px 24px;border-radius:10px;text-decoration:none">
      Verify email address
    </a>
    <p style="margin:24px 0 0;color:#94a3b8;font-size:12px">
      If you didn't create a CareerScan account, you can safely ignore this email.
    </p>
  </div>
</body></html>"""


def _make_verify_token(user_id: str) -> str:
    """Signed, stateless email-verification token (72h validity).

    Unlike the old DB-stored hash, signed tokens don't invalidate earlier
    emails on every resend — any link the user received keeps working
    until they're verified.
    """
    from itsdangerous import URLSafeTimedSerializer
    return URLSafeTimedSerializer(app.config["SECRET_KEY"], salt="email-verify").dumps(user_id)


def _load_verify_token(token: str, max_age: int = 72 * 3600) -> str | None:
    from itsdangerous import URLSafeTimedSerializer
    try:
        return URLSafeTimedSerializer(app.config["SECRET_KEY"], salt="email-verify").loads(
            token, max_age=max_age)
    except Exception:
        return None


@login_manager.user_loader
def load_user(user_id: str):
    return db.session.get(User, user_id)


@login_manager.unauthorized_handler
def unauthorized():
    if request.path.startswith("/api/"):
        return jsonify({"error": "Unauthorized"}), 401
    return redirect(url_for("login_page"))


# ── Per-user scan state ────────────────────────────────────────────────────────
# Keyed by user_id string so each user has independent scan state.
_scans: dict[str, dict] = {}
_scan_locks: dict[str, threading.Lock] = {}
_config_lock = threading.Lock()

# Each open /api/scan/stream SSE response holds one worker thread for its whole
# lifetime. With a single gthread worker that's a small, fixed pool of threads,
# so unbounded streams can starve every other request (login, dashboard, …).
# Cap concurrent streams below the thread count to always leave threads free for
# normal traffic. Override with MAX_SSE_STREAMS.
_SSE_MAX = int(os.getenv("MAX_SSE_STREAMS", str(max(2, _gthreads - 2))))
_sse_semaphore = threading.BoundedSemaphore(_SSE_MAX)


def _get_scan(user_id: str) -> dict:
    if user_id not in _scans:
        _scans[user_id] = {"running": False, "q": queue.Queue()}
    return _scans[user_id]


def _get_scan_lock(user_id: str) -> threading.Lock:
    if user_id not in _scan_locks:
        _scan_locks[user_id] = threading.Lock()
    return _scan_locks[user_id]


def _run_scan_inprocess(user_id: str, mode: str, notify: bool, q: queue.Queue, extra_env: dict):
    """Run the scan in the Flask process (background thread) — no subprocess overhead."""
    import config as _cfg
    from scrapers import scrape_all_sources
    from scorer import rank_jobs, filter_jobs

    scan       = _get_scan(user_id)
    failed     = False
    scan_error = None
    job_count  = 0
    history_id = scan.get("history_id")
    all_jobs: list = []
    new_jobs: list = []
    matched_jobs: list = []

    def log(msg: str):
        q.put(str(msg))

    user_cfg_json = extra_env.get("JOBSCANNER_USER_CONFIG", "{}")
    max_jobs      = int(extra_env.get("JOBSCANNER_MAX_JOBS", "0"))

    try:
        # ── 1. Configure SEARCH_CONFIG for this user (serialised via _config_lock) ──
        with _config_lock:
            _cfg.SEARCH_CONFIG.clear()
            _cfg.SEARCH_CONFIG.update({
                "target_titles":             [],
                "preferred_keywords":        [],
                "negative_keywords":         [],
                "min_salary":                2200,
                "max_salary":                4000,
                "preferred_location":        "Sengkang",
                "location_keywords":         [],
                "min_score_threshold":       30,
                "max_jobs_per_notification": 20,
                "email_enabled":             False,
                "email_to":                  "",
            })
            _cfg.load_user_config(user_cfg_json)
            if mode != "analyst":
                _cfg.set_mode(mode)
            cfg_snapshot = dict(_cfg.SEARCH_CONFIG)

        # ── 2. Log start ────────────────────────────────────────────────────────
        log("=" * 50)
        log(f"JOB SCANNER - Starting scan [{mode.upper()} mode]")
        log(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        log("=" * 50)

        # ── 3. Load seen jobs from DB ────────────────────────────────────────────
        with app.app_context():
            seen_ids = {
                r.job_source_id
                for r in SeenJob.query.filter_by(user_id=user_id)
                .with_entities(SeenJob.job_source_id).all()
            }
        log(f"Previously seen jobs: {len(seen_ids)}")

        # ── 4. Scrape ────────────────────────────────────────────────────────────
        max_fetch = max_jobs * 3 if max_jobs > 0 else 0
        if max_fetch:
            log(f"Free plan — fetching up to {max_fetch} candidates (showing top {max_jobs} matches)")

        # Guard: no search titles means every source returns nothing. This happens
        # when a non-prebuilt mode needs live Gemini generation and that call fails.
        # Fail loudly with the real cause instead of blaming the network.
        if not cfg_snapshot.get("target_titles"):
            log(f"Could not load search titles for the '{mode}' mode.")
            log("This usually means the AI mode generation failed — check that a valid "
                "Gemini API key is set, or pick one of the built-in modes.")
            raise RuntimeError(f"No target_titles for mode '{mode}'")

        log("\nScanning job sources (MyCareersFuture, Adzuna, RemoteOK)...")
        all_jobs = scrape_all_sources(max_total=max_fetch, cfg=cfg_snapshot)

        if not all_jobs:
            log("No jobs found from any source. Check your internet connection.")
        else:
            # ── 5. Filter seen ───────────────────────────────────────────────────
            new_jobs = [j for j in all_jobs if j["id"] not in seen_ids]
            log(f"New jobs (not seen before): {len(new_jobs)}")

            if new_jobs:
                # ── 6. Score ─────────────────────────────────────────────────────
                log("Scoring jobs against your profile...")
                scored_jobs  = rank_jobs(new_jobs, cfg=cfg_snapshot)
                threshold    = cfg_snapshot.get("min_score_threshold", 30)
                matched_jobs = filter_jobs(scored_jobs, threshold)
                log(f"Jobs above score threshold ({threshold}): {len(matched_jobs)}")

                if max_jobs > 0 and len(matched_jobs) > max_jobs:
                    log(f"Free plan: capping at {max_jobs} matches")
                    matched_jobs = matched_jobs[:max_jobs]

                max_notify = cfg_snapshot.get("max_jobs_per_notification", 20)
                top_jobs   = matched_jobs[:max_notify]

                if top_jobs:
                    log(f"\nTop {len(top_jobs)} matches:")
                    for i, job in enumerate(top_jobs, 1):
                        sal = (f" | ${job['salary_min']}-${job['salary_max']}"
                               if job.get("salary_min") and job.get("salary_max") else "")
                        log(f"  {i}. [{job['score']}/100] {job['title']} @ {job['company']}{sal}")

                # ── 7. Write matches + seen IDs directly to DB ───────────────────
                with app.app_context():
                    now = datetime.now(timezone.utc)
                    for job in matched_jobs:
                        if not Job.query.filter_by(user_id=user_id, source_job_id=job["id"]).first():
                            db.session.add(Job(
                                user_id       = user_id,
                                source_job_id = job["id"],
                                title         = job.get("title", ""),
                                company       = job.get("company", ""),
                                location      = job.get("location", ""),
                                source        = job.get("source", ""),
                                url           = job.get("url", ""),
                                posted_date   = job.get("posted_date", ""),
                                closing_date  = job.get("closing_date") or None,
                                salary_min    = job.get("salary_min"),
                                salary_max    = job.get("salary_max"),
                                score         = job.get("score", 0),
                                match_reasons = " | ".join(job.get("match_reasons", [])),
                                scan_date     = now,
                            ))
                    # Mark ALL scraped jobs as seen (not just matched ones)
                    existing_seen = {
                        r.job_source_id
                        for r in SeenJob.query.filter_by(user_id=user_id)
                        .with_entities(SeenJob.job_source_id).all()
                    }
                    for job in all_jobs:
                        if job["id"] not in existing_seen:
                            db.session.add(SeenJob(user_id=user_id, job_source_id=job["id"]))
                    db.session.commit()
                    job_count = len(matched_jobs)

                log(f"\nResults saved: {job_count} new matched jobs")

                # ── 8. Email digest (free + paid) ────────────────────────────────
                if notify:
                    _email_to      = cfg_snapshot.get("email_to", "")
                    _email_enabled = cfg_snapshot.get("email_enabled", False)
                    if _email_enabled and _email_to and top_jobs:
                        try:
                            log("Sending email digest...")
                            from notifier import send_email_digest
                            send_email_digest(top_jobs, {"email_to": _email_to, "email_enabled": True})
                        except Exception as _e:
                            log(f"Email notification skipped: {_e}")
            else:
                # Still mark all scraped jobs as seen even when nothing is new
                with app.app_context():
                    existing_seen = {
                        r.job_source_id
                        for r in SeenJob.query.filter_by(user_id=user_id)
                        .with_entities(SeenJob.job_source_id).all()
                    }
                    for job in all_jobs:
                        if job["id"] not in existing_seen:
                            db.session.add(SeenJob(user_id=user_id, job_source_id=job["id"]))
                    db.session.commit()
                log("No new jobs since last scan.")

        # ── 9. Summary ───────────────────────────────────────────────────────────
        log(f"\n{'=' * 50}")
        log("SCAN COMPLETE")
        log(f"   Total scraped: {len(all_jobs)}")
        log(f"   New listings:  {len(new_jobs)}")
        log(f"   Matched:       {job_count}")
        log(f"{'=' * 50}")

    except Exception as exc:
        log(f"ERROR: {exc}")
        app.logger.error("Scan error for user %s: %s", user_id, exc, exc_info=True)
        failed = True
        scan_error = exc
    finally:
        with app.app_context():
            if history_id:
                row = db.session.get(ScanHistory, history_id)
                if row:
                    row.finished_at = datetime.now(timezone.utc)
                    row.job_count   = job_count
                    row.status      = "failed" if failed else "done"
                    db.session.commit()
            if scan_error is not None:
                user = db.session.get(User, user_id)
                _log_app_error(
                    source="scan", exc=scan_error,
                    path=f"scan[{mode}]",
                    user_email=user.email if user else None,
                )
        q.put(None)
        scan["running"] = False
        scan["proc"]    = None


def _build_user_env(user: User) -> dict:
    """Build the extra_env dict passed to _run_scan_inprocess."""
    settings = user.settings
    profile  = user.profile

    user_cfg: dict = {}

    if user.gemini_api_key:
        user_cfg["gemini_api_key"] = _decrypt_api_key(user.gemini_api_key)

    if profile:
        user_cfg["profile"] = profile.to_dict()
        if not user_cfg["profile"].get("email"):
            user_cfg["profile"]["email"] = user.email

    if settings:
        sc = settings.to_dict()
        user_cfg["search_config"] = {
            "min_salary":                sc["min_salary"],
            "max_salary":                sc["max_salary"],
            "min_score_threshold":       sc["min_score_threshold"],
            "max_jobs_per_notification": sc["max_jobs_per_notification"],
            "preferred_location":        sc["preferred_location"],
            "location_keywords":         sc["location_keywords"] or [],
            "target_titles":             sc["target_titles"] or [],
            "preferred_keywords":        sc["preferred_keywords"] or [],
            "negative_keywords":         sc["negative_keywords"] or [],
            "email_enabled":             sc["email_enabled"],
            "email_to":                  sc["email_to"] or "",
        }

    data_dir = f"data/users/{user.id}"
    env = {
        "JOBSCANNER_USER_CONFIG": json.dumps(user_cfg),
        "JOBSCANNER_DATA_DIR":    data_dir,
    }
    result_limit = _entitlements(user)["scan_result_limit"]
    if result_limit is not None:
        env["JOBSCANNER_MAX_JOBS"] = str(result_limit)
    return env


# ── DB helpers ─────────────────────────────────────────────────────────────────

def _jobs_for_user(user_id: str) -> list[dict]:
    rows = (
        Job.query
        .filter_by(user_id=user_id)
        .order_by(Job.scan_date.desc())
        .all()
    )
    return [r.to_dict() for r in rows]


def _statuses_for_user(user_id: str) -> dict:
    rows = ApplicationStatus.query.filter_by(user_id=user_id).all()
    return {r.job_source_id: r.to_dict() for r in rows}


def _get_or_create_settings(user_id: str) -> UserSettings:
    s = db.session.get(UserSettings, user_id)
    if s is None:
        import config as cfg
        # Snapshot under the lock — a concurrent scan rebuilds the shared
        # SEARCH_CONFIG, so an unlocked read could seed torn/other-user values.
        with _config_lock:
            defaults = dict(cfg.SEARCH_CONFIG)
        s = UserSettings(
            user_id=user_id,
            min_salary=defaults.get("min_salary", 2200),
            max_salary=defaults.get("max_salary", 4000),
            min_score_threshold=defaults.get("min_score_threshold", 40),
            max_jobs_per_notification=defaults.get("max_jobs_per_notification", 20),
            target_titles=defaults.get("target_titles", []),
            preferred_keywords=defaults.get("preferred_keywords", []),
            negative_keywords=defaults.get("negative_keywords", []),
            location_keywords=defaults.get("location_keywords", []),
            preferred_location=defaults.get("preferred_location", "Sengkang"),
        )
        db.session.add(s)
        db.session.commit()
    return s


def _get_or_create_profile(user_id: str) -> UserProfile:
    p = db.session.get(UserProfile, user_id)
    if p is None:
        import config as cfg
        p = UserProfile(
            user_id=user_id,
            name=cfg.PROFILE.get("name", ""),
            phone=cfg.PROFILE.get("phone", ""),
            education=cfg.PROFILE.get("education", ""),
            experience_summary=cfg.PROFILE.get("experience_summary", ""),
            technical_skills=cfg.PROFILE.get("technical_skills", []),
            soft_skills=cfg.PROFILE.get("soft_skills", []),
            work_history=cfg.PROFILE.get("work_history", []),
            certifications=cfg.PROFILE.get("certifications", []),
            projects=cfg.PROFILE.get("projects", []),
        )
        db.session.add(p)
        db.session.commit()
    return p


# ── Auth pages ─────────────────────────────────────────────────────────────────

@app.route("/login")
def login_page():
    if current_user.is_authenticated:
        return redirect(url_for("app_page"))
    return render_template("login.html")


@app.route("/register")
def register_page():
    if current_user.is_authenticated:
        return redirect(url_for("app_page"))
    return render_template("register.html", turnstile_site_key=os.getenv("TURNSTILE_SITE_KEY", ""))


# ── Auth API ───────────────────────────────────────────────────────────────────

def _verify_turnstile(token):
    secret = os.getenv("TURNSTILE_SECRET_KEY", "")
    if not secret:
        return True
    import requests as _req
    try:
        resp = _req.post(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            data={"secret": secret, "response": token, "remoteip": request.remote_addr},
            timeout=5,
        )
        return resp.json().get("success", False)
    except Exception:
        return False


@app.route("/api/auth/register", methods=["POST"])
@limiter.limit("5/minute;20/hour")
def auth_register():
    data     = request.json or {}
    email    = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if os.getenv("TURNSTILE_SECRET_KEY") and not _verify_turnstile(data.get("cf_turnstile_response", "")):
        return jsonify({"error": "Bot check failed — please try again"}), 400

    if not email or not password:
        return jsonify({"error": "Email and password are required"}), 400
    if not _valid_email(email):
        return jsonify({"error": "Please enter a valid email address"}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters"}), 400
    if User.query.filter_by(email=email).first():
        return jsonify({"error": "Registration failed — try signing in instead"}), 409

    user = User(
        email=email,
        subscription_status="free",
        email_verified=False,
    )
    user.set_password(password)
    db.session.add(user)
    db.session.commit()

    verify_url = request.host_url.rstrip("/") + f"/verify-email?token={_make_verify_token(user.id)}"
    _send_email(email, "Verify your CareerScan email", _verification_email_html(verify_url))

    login_user(user, remember=True)
    return jsonify({"ok": True, "email": user.email, "onboarding": True})


@app.route("/api/auth/login", methods=["POST"])
@limiter.limit("10/minute;50/hour")
def auth_login():
    data     = request.json or {}
    email    = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    user = User.query.filter_by(email=email).first()
    if not user or not user.check_password(password):
        return jsonify({"error": "Invalid email or password"}), 401

    login_user(user, remember=True)
    return jsonify({"ok": True, "email": user.email})


@app.route("/api/auth/logout", methods=["POST"])
@login_required
def auth_logout():
    logout_user()
    return jsonify({"ok": True})


@app.route("/api/auth/forgot-password", methods=["POST"])
@limiter.limit("3/minute;10/hour")
def auth_forgot_password():
    email = (request.json or {}).get("email", "").strip().lower()
    # Always return success — don't reveal if email exists
    if not email:
        return jsonify({"ok": True})

    user = User.query.filter_by(email=email).first()
    if user and user.password_hash:  # only email/password accounts need reset
        token     = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        user.reset_token         = token_hash
        user.reset_token_expires = datetime.now(timezone.utc) + timedelta(hours=1)
        db.session.commit()

        base      = request.host_url.rstrip("/")
        reset_url = f"{base}/reset-password?token={token}"
        reset_html = f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#f8fafc;font-family:ui-sans-serif,system-ui,sans-serif">
  <div style="max-width:480px;margin:40px auto;background:white;border-radius:12px;padding:32px;box-shadow:0 1px 3px rgba(0,0,0,.08)">
    <div style="margin-bottom:20px">
      <span style="display:inline-block;background:#EEF2FF;color:#4F46E5;font-size:12px;font-weight:700;padding:3px 10px;border-radius:20px;letter-spacing:.05em">JOB SCANNER</span>
    </div>
    <h2 style="margin:0 0 8px;font-size:20px;color:#1e293b;font-weight:700">Reset your password</h2>
    <p style="margin:0 0 24px;color:#64748b;font-size:14px;line-height:1.6">
      We received a request to reset the password for your CareerScan account.<br>
      Click the button below to choose a new password. This link expires in 1 hour.
    </p>
    <a href="{reset_url}" style="display:inline-block;background:#4F46E5;color:white;font-size:14px;font-weight:600;padding:12px 24px;border-radius:10px;text-decoration:none">
      Reset password
    </a>
    <p style="margin:24px 0 0;color:#94a3b8;font-size:12px">
      If you didn't request this, you can safely ignore this email. Your password won't change.
    </p>
  </div>
</body></html>"""
        _send_email(email, "Reset your CareerScan password", reset_html)

    return jsonify({"ok": True})


@app.route("/api/auth/reset-password", methods=["POST"])
@limiter.limit("5/minute;20/hour")
def auth_reset_password():
    data     = request.json or {}
    token    = (data.get("token") or "").strip()
    password = data.get("password") or ""

    if not token or len(password) < 8:
        return jsonify({"error": "Invalid request"}), 400

    token_hash = hashlib.sha256(token.encode()).hexdigest()
    user = User.query.filter_by(reset_token=token_hash).first()

    if not user or not user.reset_token_expires:
        return jsonify({"error": "Invalid or expired reset link"}), 400
    if datetime.now(timezone.utc) > user.reset_token_expires:
        return jsonify({"error": "This reset link has expired — request a new one"}), 400

    user.set_password(password)
    user.reset_token         = None
    user.reset_token_expires = None
    db.session.commit()

    return jsonify({"ok": True})


@app.route("/forgot-password")
def forgot_password_page():
    if current_user.is_authenticated:
        return redirect(url_for("app_page"))
    return render_template("forgot_password.html")


@app.route("/reset-password")
def reset_password_page():
    if current_user.is_authenticated:
        return redirect(url_for("app_page"))
    return render_template("reset_password.html", token=request.args.get("token", ""))


@app.route("/verify-email")
def verify_email_page():
    token = (request.args.get("token") or "").strip()

    user = None
    if token:
        user_id = _load_verify_token(token)
        if user_id:
            user = db.session.get(User, user_id)
        else:
            # Legacy links (pre-signed-token emails stored a sha256 hash)
            token_hash = hashlib.sha256(token.encode()).hexdigest()
            user = User.query.filter_by(email_verify_token=token_hash).first()

    if user:
        if not user.email_verified:
            user.email_verified     = True
            user.email_verify_token = None
            db.session.commit()
        # Already-verified users clicking again still get a success redirect
        dest = "/app" if current_user.is_authenticated else "/login"
        return redirect(f"{dest}?verified=1")

    # Invalid or expired link — tell the user instead of failing silently
    dest = "/app" if current_user.is_authenticated else "/login"
    return redirect(f"{dest}?verify_error=1")


@app.route("/api/auth/resend-verification", methods=["POST"])
@login_required
@limiter.limit("3/hour")
def resend_verification():
    if current_user.email_verified:
        return jsonify({"ok": True})

    # Signed token — resending no longer invalidates links from earlier emails
    verify_url = request.host_url.rstrip("/") + f"/verify-email?token={_make_verify_token(current_user.id)}"
    sent = _send_email(current_user.email, "Verify your CareerScan email", _verification_email_html(verify_url))
    if not sent:
        return jsonify({"ok": False, "error": "Email could not be sent. Please try again later."}), 502
    return jsonify({"ok": True})


@app.route("/api/auth/google")
def auth_google():
    redirect_uri = url_for("auth_google_callback", _external=True)
    return google_oauth.authorize_redirect(redirect_uri)


@app.route("/api/auth/google/callback")
def auth_google_callback():
    try:
        token    = google_oauth.authorize_access_token()
        userinfo = token.get("userinfo") or {}
        email     = (userinfo.get("email") or "").strip().lower()
        google_id = userinfo.get("sub") or ""
    except Exception:
        return redirect("/login?error=google_failed")

    if not email or not google_id:
        return redirect("/login?error=google_failed")

    if not userinfo.get("email_verified"):
        return redirect("/login?error=google_failed")

    # Check by google_id first (returning Google user)
    user = User.query.filter_by(google_id=google_id).first()

    if not user:
        # Check by email (link to existing email account)
        user = User.query.filter_by(email=email).first()
        if user:
            user.google_id = google_id
            if not user.email_verified:
                user.email_verified = True
            db.session.commit()
        else:
            # Brand-new user via Google — email already verified by Google
            user = User(
                email=email,
                google_id=google_id,
                subscription_status="free",
                email_verified=True,
            )
            db.session.add(user)
            db.session.commit()
            login_user(user, remember=True)
            return redirect("/onboarding")

    login_user(user, remember=True)
    return redirect("/app")


@app.route("/api/auth/me")
def auth_me():
    if not current_user.is_authenticated:
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify({
        "email":        current_user.email,
        "id":           current_user.id,
        "has_password": bool(current_user.password_hash),
        "is_admin":     _is_admin(current_user),
    })


# ── Health ────────────────────────────────────────────────────────────────────

@app.route("/api/health")
def health():
    return jsonify({"ok": True})


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.route("/")
def home_page():
    if current_user.is_authenticated:
        return redirect(url_for("app_page"))
    return render_template("home.html")


@app.route("/privacy")
def privacy_page():
    return render_template("privacy.html")


@app.route("/terms")
def terms_page():
    return render_template("terms.html")


@app.route("/contact", methods=["GET", "POST"])
@limiter.limit("3/minute;10/hour", methods=["POST"])
def contact_page():
    if request.method == "POST":
        # Honeypot: a hidden field real users never see. Bots fill it in.
        # Silently show the success page so spammers can't tell they were caught.
        if (request.form.get("website") or "").strip():
            app.logger.info("Contact form honeypot triggered; dropping submission")
            return render_template("contact.html", sent=True)

        name = (request.form.get("name") or "").strip()
        email = (request.form.get("email") or "").strip().lower()
        category = (request.form.get("category") or "Feedback").strip()
        subject = (request.form.get("subject") or "").strip()
        message = (request.form.get("message") or "").strip()

        allowed_categories = {"Feedback", "Bug Report", "Account Issue", "Billing Issue", "Other"}
        if category not in allowed_categories:
            category = "Other"

        if not name or not email or not subject or not message:
            return render_template(
                "contact.html",
                error="Please fill in all required fields.",
                form=request.form,
            )

        if not _valid_email(email):
            return render_template(
                "contact.html",
                error="Please enter a valid email address.",
                form=request.form,
            )

        submission = {
            "name": name,
            "email": email,
            "category": category,
            "subject": subject,
            "message": message,
            "user_email": current_user.email if current_user.is_authenticated else None,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        # Best-effort local backup. Render's filesystem is ephemeral, so this is
        # NOT durable across deploys — email below is the real delivery channel.
        try:
            submissions_path = Path(app.instance_path) / "contact_submissions.jsonl"
            submissions_path.parent.mkdir(parents=True, exist_ok=True)
            with submissions_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(submission, ensure_ascii=False) + "\n")
        except OSError as exc:
            app.logger.warning("Could not write contact submission to disk: %s", exc)

        contact_html = f"""
        <h2>New CareerScan contact message</h2>
        <p><strong>Name:</strong> {html.escape(name)}</p>
        <p><strong>Email:</strong> {html.escape(email)}</p>
        <p><strong>Category:</strong> {html.escape(category)}</p>
        <p><strong>Subject:</strong> {html.escape(subject)}</p>
        <p><strong>Logged-in user:</strong> {html.escape(submission.get('user_email') or 'Guest')}</p>
        <p><strong>Message:</strong></p>
        <pre style="white-space:pre-wrap;font-family:ui-monospace,monospace;background:#f8fafc;border:1px solid #e5e7eb;border-radius:8px;padding:12px">{html.escape(message)}</pre>
        """
        support_to = os.getenv("SUPPORT_EMAIL", "support@careerscan.online").strip()
        # reply_to = visitor so a reply from support goes straight back to them.
        sent_ok = _send_email(
            support_to,
            f"[CareerScan Contact] {category}: {subject[:80]}",
            contact_html,
            reply_to=email,
        )
        if not sent_ok:
            app.logger.error("Contact form email to %s failed to send", support_to)
            return render_template(
                "contact.html",
                error="Sorry, we couldn't send your message right now. "
                      "Please try again shortly or email us directly at "
                      + support_to + ".",
                form=request.form,
            )

        return render_template("contact.html", sent=True)

    return render_template("contact.html")


@app.route("/app")
@login_required
def app_page():
    return render_template("index.html")


@app.route("/onboarding")
@login_required
def onboarding():
    return render_template("onboarding.html")


# ── Stats ──────────────────────────────────────────────────────────────────────

@app.route("/api/stats")
@login_required
def stats():
    jobs   = _jobs_for_user(current_user.id)
    status = _statuses_for_user(current_user.id)

    unique = list({j["id"]: j for j in jobs}.values())
    for j in unique:
        j["app_status"] = status.get(j["id"], {}).get("status", "")

    scans_today = 0
    scans_remaining = None
    if _is_free_tier(current_user):
        s = current_user.settings
        today = _sgt_today().isoformat()
        if s and s.last_scan_date == today:
            scans_today = s.daily_scan_count or 0
        scans_remaining = max(0, _FREE_DAILY_SCAN_LIMIT - scans_today)

    return jsonify({
        "total_jobs":       len(unique),
        "applied":          sum(1 for v in status.values() if v["status"] == "applied"),
        "interviews":       sum(1 for v in status.values() if v["status"] == "interview"),
        "skipped":          sum(1 for v in status.values() if v["status"] == "skip"),
        "last_scan":        jobs[0].get("scan_date", "Never") if jobs else "Never",
        "recent_jobs":      unique[:5],
        "scans_remaining":  scans_remaining,
    })


# ── Jobs ───────────────────────────────────────────────────────────────────────

@app.route("/api/jobs")
@login_required
def get_jobs():
    all_jobs = _jobs_for_user(current_user.id)
    status   = _statuses_for_user(current_user.id)

    unique = list({j["id"]: j for j in all_jobs}.values())
    for j in unique:
        j["app_status"] = status.get(j["id"], {}).get("status", "")

    show_hidden = request.args.get("show_hidden") == "1"
    hidden_count = sum(1 for j in unique if j.get("hidden"))

    if not show_hidden:
        unique = [j for j in unique if not j.get("hidden")]

    q      = request.args.get("q", "").lower()
    source = request.args.get("source", "")
    st     = request.args.get("status", "")

    if source:
        unique = [j for j in unique if j.get("source", "") == source]
    if st == "untracked":
        unique = [j for j in unique if not j.get("app_status")]
    elif st:
        unique = [j for j in unique if j.get("app_status") == st]
    if q:
        unique = [j for j in unique if q in j.get("title", "").lower() or q in j.get("company", "").lower()]

    resp = jsonify(unique)
    resp.headers["X-Hidden-Count"] = str(hidden_count)
    return resp


@app.route("/api/jobs/<job_id>/hide", methods=["POST"])
@login_required
def hide_job(job_id):
    job = Job.query.filter_by(user_id=current_user.id, source_job_id=job_id).first()
    if not job:
        return jsonify({"error": "Not found"}), 404
    data = request.json or {}
    job.hidden = bool(data.get("hidden", True))
    db.session.commit()
    return jsonify({"ok": True, "hidden": job.hidden})


# ── Applications ───────────────────────────────────────────────────────────────

@app.route("/api/applications", methods=["GET"])
@login_required
def get_applications():
    return jsonify(_statuses_for_user(current_user.id))


@app.route("/api/applications/<job_id>", methods=["POST"])
@login_required
def update_application(job_id):
    data       = request.json or {}
    new_status = data.get("status", "")
    valid_statuses = {"applied", "interview", "skip", "clear"}

    if not isinstance(new_status, str) or new_status not in valid_statuses:
        return jsonify({"error": "Unsupported application status"}), 400

    if new_status == "clear":
        row = db.session.get(ApplicationStatus, (current_user.id, job_id))
        if row:
            db.session.delete(row)
            db.session.commit()
        return jsonify({"ok": True})

    # Look up the job to fill in title/company/url
    job_row = Job.query.filter_by(user_id=current_user.id, source_job_id=job_id).first()
    existing = db.session.get(ApplicationStatus, (current_user.id, job_id))

    if existing:
        existing.status         = new_status
        existing.title          = (job_row.title   if job_row else None) or existing.title
        existing.company        = (job_row.company if job_row else None) or existing.company
        existing.url            = (job_row.url     if job_row else None) or existing.url
        existing.notes          = data.get("notes",          existing.notes)
        existing.interview_date = data.get("interview_date", existing.interview_date)
        existing.interview_time = data.get("interview_time", existing.interview_time)
        existing.updated_at     = datetime.now(timezone.utc)
    else:
        existing = ApplicationStatus(
            user_id       = current_user.id,
            job_source_id = job_id,
            status        = new_status,
            title         = job_row.title   if job_row else "",
            company       = job_row.company if job_row else "",
            url           = job_row.url     if job_row else "",
            notes         = data.get("notes", ""),
            interview_date = data.get("interview_date", ""),
            interview_time = data.get("interview_time", ""),
        )
        db.session.add(existing)

    db.session.commit()
    return jsonify({"ok": True})


# ── Modes ──────────────────────────────────────────────────────────────────────

@app.route("/api/modes")
@login_required
def get_modes():
    modes = [
        {"name": "analyst",       "titles": ["Data Analyst", "Business Analyst", "Operations Analyst"]},
        {"name": "tech",          "titles": ["Software Engineer", "Developer", "IT Support"]},
        {"name": "finance",       "titles": ["Finance Analyst", "Accountant", "Auditor"]},
        {"name": "logistics",     "titles": ["Operations Executive", "Supply Chain", "Procurement"]},
        {"name": "healthcare",    "titles": ["Staff Nurse", "Allied Health", "Healthcare Admin"]},
        {"name": "marketing",     "titles": ["Digital Marketing", "Content Creator", "Social Media"]},
        {"name": "customer service", "titles": ["Customer Service", "Service Executive", "Relationship Manager"]},
        {"name": "hr",            "titles": ["HR Executive", "Recruiter", "Admin Executive"]},
        {"name": "engineering",   "titles": ["Mechanical Engineer", "Electrical Engineer", "Civil Engineer"]},
    ]
    rows = SearchMode.query.filter_by(user_id=current_user.id).all()
    for row in rows:
        cfg = row.config or {}
        modes.append({"name": row.name, "titles": cfg.get("target_titles", [])[:3]})
    return jsonify(modes)


@app.route("/api/modes/generate", methods=["POST"])
@login_required
@limiter.limit("5/minute;20/hour")
def generate_mode():
    mode_name = (request.json or {}).get("mode", "").strip().lower()
    if not mode_name:
        return jsonify({"error": "mode name required"}), 400

    import config as cfg
    with _config_lock:
        cfg.set_mode(mode_name, refresh=(request.json or {}).get("refresh", False))
        from config import SEARCH_CONFIG
        mode_cfg = dict(SEARCH_CONFIG)

    row = SearchMode.query.filter_by(user_id=current_user.id, name=mode_name).first()
    if row:
        row.config = mode_cfg
    else:
        row = SearchMode(user_id=current_user.id, name=mode_name, config=mode_cfg)
        db.session.add(row)
    db.session.commit()

    return jsonify({"ok": True, "mode": mode_name, "titles": mode_cfg.get("target_titles", [])[:3]})


# ── Cover notes ────────────────────────────────────────────────────────────────

def _cover_notes_dir(user_id: str) -> Path:
    return Path(f"data/users/{user_id}/cover_notes")


def _sgt_str(dt) -> str:
    """Format a stored (UTC) datetime as a 'YYYY-MM-DD HH:MM' string in SGT.
    Tolerates naive datetimes (SQLite drops tzinfo) by assuming UTC."""
    if not dt:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(SGT).strftime("%Y-%m-%d %H:%M")


def _resume_version_meta(v: ResumeVersion) -> dict:
    profile = v.profile or {}
    return {
        "id": v.id,
        "name": v.name or "Untitled resume",
        "source": v.source or "manual",
        "created_at": _sgt_str(v.created_at),
        "job_title": v.job_title or "",
        "company": v.company or "",
        "summary": (profile.get("experience_summary") or "")[:180],
    }


@app.route("/api/cover-notes")
@login_required
def list_cover_notes():
    notes_dir = _cover_notes_dir(current_user.id)
    if not notes_dir.exists():
        return jsonify([])

    notes = []
    for f in sorted(notes_dir.glob("*.txt"), key=os.path.getmtime, reverse=True):
        try:
            lines      = f.read_text(encoding="utf-8").split("\n")
            title_line = lines[0].replace("Cover Note for:", "").strip()
            parts      = title_line.split("@", 1)
            job_title  = parts[0].strip()
            company    = parts[1].strip() if len(parts) > 1 else ""
            score_line = next((ln for ln in lines if "Match Score:" in ln), "")
            score      = score_line.replace("Match Score:", "").replace("/100", "").strip() if score_line else ""
            url_line   = next((ln for ln in lines if "Job URL:" in ln), "")
            url        = url_line.replace("Job URL:", "").strip() if url_line else ""
            notes.append({
                "filename": f.name,
                "title":    job_title,
                "company":  company,
                "score":    int(score) if score.isdigit() else "",
                "url":      url,
                "modified": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
            })
        except Exception:
            notes.append({"filename": f.name, "title": f.stem, "company": "", "score": "", "url": "", "modified": ""})

    return jsonify(notes)


@app.route("/api/cover-notes/<filename>")
@login_required
def get_cover_note(filename):
    safe = os.path.basename(filename)
    path = _cover_notes_dir(current_user.id) / safe
    if not path.exists():
        return jsonify({"error": "Not found"}), 404
    return jsonify({"content": path.read_text(encoding="utf-8")})


@app.route("/api/cover-notes/generate", methods=["POST"])
@login_required
@limiter.limit("5/minute;30/hour")
def generate_cover_note_route():
    if not _is_paid(current_user):
        return jsonify({"error": "pro_required"}), 403

    import config as cfg
    api_key = _decrypt_api_key(current_user.gemini_api_key or "") or cfg.GEMINI_API_KEY

    data   = request.json or {}
    job_id = (data.get("job_id") or "").strip()
    force  = bool(data.get("force"))
    if not job_id:
        return jsonify({"error": "job_id required"}), 400

    job_row = Job.query.filter_by(user_id=current_user.id, source_job_id=job_id).first()
    if not job_row:
        return jsonify({"error": "Job not found"}), 404

    # Return cached cover note without consuming quota
    if job_row.cover_note and not force:
        return jsonify({"ok": True, "content": job_row.cover_note, "cached": True})

    # Check daily AI quota before calling Gemini
    allowed, quota_err = _check_ai_quota(current_user)
    if not allowed:
        return jsonify({"error": quota_err, "quota_exceeded": True}), 429

    profile      = _get_or_create_profile(current_user.id)
    profile_dict = profile.to_dict()
    if not profile_dict.get("email"):
        profile_dict["email"] = current_user.email

    job_dict = {
        "title":        job_row.title or "",
        "company":      job_row.company or "",
        "description":  "",
        "url":          job_row.url or "",
        "score":        job_row.score or 0,
        "match_reasons": [r.strip() for r in (job_row.match_reasons or "").split("|") if r.strip()],
    }

    try:
        from cover_notes import generate_cover_note, save_cover_note
        note_text = generate_cover_note(job_dict, api_key=api_key, profile=profile_dict)
        notes_dir = _cover_notes_dir(current_user.id)
        notes_dir.mkdir(parents=True, exist_ok=True)
        save_cover_note(job_dict, note_text, str(notes_dir))
        job_row.cover_note = note_text
        db.session.commit()
        return jsonify({"ok": True, "content": note_text})
    except Exception as e:
        app.logger.error("Cover note generation failed for user %s: %s", current_user.id, e, exc_info=True)
        return jsonify({"error": "AI request failed. Please try again later."}), 502


# ── Config / settings ──────────────────────────────────────────────────────────

@app.route("/api/config", methods=["GET"])
@login_required
def get_config():
    import config as cfg
    s = _get_or_create_settings(current_user.id)
    return jsonify({
        "gemini_api_key":            bool(current_user.gemini_api_key or cfg.GEMINI_API_KEY),
        "email_configured":          bool(os.getenv("RESEND_API_KEY")),
        "min_salary":                s.min_salary,
        "max_salary":                s.max_salary,
        "min_score_threshold":       s.min_score_threshold,
        "max_jobs_per_notification": s.max_jobs_per_notification,
        "email_enabled":             s.email_enabled,
        "email_to":                  s.email_to or "",
        "target_titles":             s.target_titles or [],
        "preferred_keywords":        s.preferred_keywords or [],
        "negative_keywords":         s.negative_keywords or [],
        "location_keywords":         s.location_keywords or [],
        "preferred_location":        s.preferred_location or "Sengkang",
    })


@app.route("/api/config", methods=["POST"])
@login_required
def update_config():
    data = request.json or {}
    s    = _get_or_create_settings(current_user.id)

    _numeric_bounds = {
        "min_salary":                (0, 1_000_000),
        "max_salary":                (0, 1_000_000),
        "min_score_threshold":       (0, 100),
        "max_jobs_per_notification": (1, 200),
    }
    for key, (lo, hi) in _numeric_bounds.items():
        if key in data:
            try:
                val = int(data[key])
            except (TypeError, ValueError):
                return jsonify({"error": f"{key} must be a number"}), 400
            setattr(s, key, max(lo, min(hi, val)))
    if "email_to" in data:
        email_to = (data["email_to"] or "").strip()
        if email_to and not _valid_email(email_to):
            return jsonify({"error": "Invalid email address"}), 400
        s.email_to = email_to
    for key in ("email_enabled", "preferred_location"):
        if key in data:
            setattr(s, key, data[key])
    for key in ("target_titles", "preferred_keywords", "negative_keywords", "location_keywords"):
        if key in data and isinstance(data[key], list):
            setattr(s, key, data[key])

    db.session.commit()
    return jsonify({"ok": True})


@app.route("/api/email/test", methods=["POST"])
@login_required
def test_email():
    from notifier import send_email_digest
    s      = _get_or_create_settings(current_user.id)
    ok = send_email_digest(
        [], s.to_dict(),
        subject_override="CareerScan — connection test",
    )
    return jsonify({"ok": ok})


# ── Profile ────────────────────────────────────────────────────────────────────

# Cap stored resume blobs. The public checker already enforces a 2 MB limit;
# logged-in uploads are otherwise unbounded, so bound what we persist to keep the
# DB and backups sane. Anything larger is still parsed/scored — just not stored.
_RESUME_STORE_MAX_BYTES = 5 * 1024 * 1024  # 5 MB

try:
    # Anonymous (no-account) public-checker uploads have no deletion path, so the
    # cleanup cron purges them after this many days to bound PII retention.
    _PUBLIC_RESUME_RETENTION_DAYS = int(os.getenv("PUBLIC_RESUME_RETENTION_DAYS", "90"))
except (TypeError, ValueError):
    _PUBLIC_RESUME_RETENTION_DAYS = 90


def _store_resume_file(raw_bytes, filename, source, *, user_id=None,
                       target_role=None, content_type=None):
    """Persist a raw resume upload to the resume_files table.

    Best-effort: storing the CV must never break the user-facing parse/score
    flow, so every error is swallowed (logged) and the failed insert is rolled
    back. Oversize files are skipped (still processed upstream, just not stored).
    """
    try:
        if not raw_bytes or len(raw_bytes) > _RESUME_STORE_MAX_BYTES:
            return
        db.session.add(ResumeFile(
            user_id=user_id,
            source=source,
            filename=(filename or "resume")[:255],
            content_type=(content_type or None),
            byte_size=len(raw_bytes),
            content=raw_bytes,
            target_role=(target_role or None),
        ))
        db.session.commit()
    except Exception:
        db.session.rollback()
        app.logger.warning(
            "Failed to store resume file (source=%s, user=%s)", source, user_id,
            exc_info=True,
        )


@app.route("/api/profile/parse-resume", methods=["POST"])
@login_required
@limiter.limit("5/minute;30/hour")
def parse_resume():
    file = request.files.get("resume")
    if not file or not file.filename:
        return jsonify({"error": "No file uploaded"}), 400

    import config as cfg
    api_key = _decrypt_api_key(current_user.gemini_api_key or "") or cfg.GEMINI_API_KEY
    if not api_key:
        return jsonify({"error": "AI service not configured"}), 400

    allowed, quota_err = _check_ai_quota(current_user)
    if not allowed:
        return jsonify({"error": quota_err, "quota_exceeded": True}), 429

    from resume_parser import extract_text_bounded, parse_with_gemini
    raw = file.read()
    try:
        text = extract_text_bounded(raw, file.filename)
    except (ImportError, ValueError) as e:
        return jsonify({"error": str(e)}), 400

    if not text.strip():
        return jsonify({"error": "Could not extract text from the file"}), 400

    _store_resume_file(raw, file.filename, "profile_parse",
                       user_id=current_user.id, content_type=file.content_type)

    try:
        profile = parse_with_gemini(text, api_key)
    except ValueError as e:
        # Expected, user-actionable failures (bad key, blocked, empty response)
        app.logger.warning("Resume parse failed for user %s: %s", current_user.id, e)
        return jsonify({"error": str(e)}), 502
    except Exception as e:
        app.logger.error(
            "Resume parse crashed for user %s: %s", current_user.id, e, exc_info=True
        )
        return jsonify({"error": "AI parsing failed. Please try again later."}), 500

    return jsonify(profile)


@app.route("/api/profile", methods=["GET"])
@login_required
def get_profile():
    p = _get_or_create_profile(current_user.id)
    d = p.to_dict()
    if not d["email"]:
        d["email"] = current_user.email
    return jsonify(d)


@app.route("/api/profile", methods=["POST"])
@login_required
def save_profile():
    data = request.json or {}
    p    = _get_or_create_profile(current_user.id)

    for field in ("name", "email", "phone", "education", "experience_summary"):
        if field in data:
            setattr(p, field, data[field])
    for field in ("technical_skills", "soft_skills", "work_history", "certifications", "projects"):
        if field in data:
            setattr(p, field, data[field])

    db.session.commit()
    return jsonify({"ok": True})


# ── Resume Builder ────────────────────────────────────────────────────────────

@app.route("/api/resume/polish", methods=["POST"])
@login_required
@limiter.limit("5/minute;20/hour")
def resume_polish():
    import config as cfg
    import requests as _req

    allowed, quota_err = _check_ai_quota(current_user)
    if not allowed:
        return jsonify({"error": quota_err, "quota_exceeded": True}), 429

    api_key = _decrypt_api_key(current_user.gemini_api_key or "") or cfg.GEMINI_API_KEY
    if not api_key:
        return jsonify({"error": "AI service not configured"}), 400

    data    = request.json or {}
    profile = data.get("profile", {})

    prompt = f"""You are a professional resume writer specialising in Singapore job applications.

Rewrite the following profile sections to sound polished, confident, and ATS-friendly.
Use action verbs and quantify achievements where reasonable. Keep it concise and truthful.

Return ONLY a valid JSON object with exactly these keys (do not add or remove keys):
{{
  "experience_summary": "rewritten 2-4 sentence professional summary",
  "work_history": [
    {{
      "title": "same job title",
      "company": "same company",
      "period": "same period",
      "summary": "rewritten bullet points, one per line starting with •"
    }}
  ],
  "technical_skills": ["cleaned", "list", "of", "technical", "skills"],
  "soft_skills": ["cleaned", "list", "of", "soft", "skills"]
}}

Raw profile to rewrite:
Name: {profile.get("name", "")}
Experience summary: {profile.get("experience_summary", "(none)")}
Technical skills: {", ".join(profile.get("technical_skills") or [])}
Soft skills: {", ".join(profile.get("soft_skills") or [])}
Work history:
{chr(10).join(
    f'  - {j.get("title","")} at {j.get("company","")} ({j.get("period","")}): {j.get("summary","")}'
    for j in (profile.get("work_history") or [])
) or "  (none)"}
"""

    try:
        resp = _req.post(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
            params={"key": api_key},
            headers={"Content-Type": "application/json"},
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=30,
        )
        resp.raise_for_status()
        _rj = resp.json()
        _log_gemini_usage("resume_polish", str(current_user.id), _rj)
        raw = _rj["candidates"][0]["content"]["parts"][0]["text"]
    except Exception:
        return jsonify({"error": "AI request failed. Please try again later."}), 502

    import re as _re
    import json as _json
    match = _re.search(r'\{[\s\S]*\}', raw)
    if not match:
        return jsonify({"error": "AI returned unexpected format"}), 502

    try:
        polished = _json.loads(match.group())
    except Exception:
        return jsonify({"error": "Could not parse AI response"}), 502

    # Merge polished fields back onto the full profile
    merged = dict(profile)
    merged.update(polished)
    return jsonify({"ok": True, "profile": merged})


_ATS_CATEGORIES = [
    "ATS Parse-Friendliness",
    "Keywords & Skills",
    "Impact & Quantification",
    "Clarity & Conciseness",
    "Completeness",
]


def _clamp_score(value) -> int:
    """Coerce a model-supplied score to an int in [0, 100]; 0 on garbage."""
    try:
        return max(0, min(100, int(round(float(value)))))
    except (TypeError, ValueError):
        return 0


def _normalize_ats_report(raw: dict, target_role: str) -> dict:
    """Guarantee the ATS report shape so a malformed model reply can't break the
    UI. Clamps every score to 0-100 and coerces lists to the expected shape."""
    raw = raw if isinstance(raw, dict) else {}

    # Categories — keep only known ones, fill any the model omitted.
    by_name = {}
    for c in raw.get("categories") or []:
        if isinstance(c, dict) and c.get("name") in _ATS_CATEGORIES:
            by_name[c["name"]] = c
    categories = []
    for name in _ATS_CATEGORIES:
        c = by_name.get(name, {})
        categories.append({
            "name": name,
            "score": _clamp_score(c.get("score")),
            "summary": str(c.get("summary", "") or "")[:300],
        })

    # Overall — trust the model if sane, else average the categories.
    overall = raw.get("overall_score")
    if overall is None or _clamp_score(overall) == 0:
        scores = [c["score"] for c in categories]
        overall = round(sum(scores) / len(scores)) if scores else 0
    overall = _clamp_score(overall)

    issues = []
    for it in (raw.get("issues") or [])[:12]:
        if not isinstance(it, dict):
            continue
        sev = str(it.get("severity", "medium")).lower()
        if sev not in ("high", "medium", "low"):
            sev = "medium"
        issues.append({
            "severity": sev,
            "title": str(it.get("title", "") or "")[:160],
            "fix": str(it.get("fix", "") or "")[:400],
        })
    # high → medium → low so the worst problems surface first.
    _rank = {"high": 0, "medium": 1, "low": 2}
    issues.sort(key=lambda i: _rank[i["severity"]])

    strengths = [str(s)[:200] for s in (raw.get("strengths") or [])[:6] if str(s).strip()]

    keyword_match = None
    km = raw.get("keyword_match")
    if target_role and isinstance(km, dict):
        keyword_match = {
            "score":   _clamp_score(km.get("score")),
            "matched": [str(s)[:60] for s in (km.get("matched") or [])[:20] if str(s).strip()],
            "missing": [str(s)[:60] for s in (km.get("missing") or [])[:20] if str(s).strip()],
        }

    return {
        "overall_score": overall,
        "verdict": str(raw.get("verdict", "") or "")[:240],
        "categories": categories,
        "issues": issues,
        "strengths": strengths,
        "keyword_match": keyword_match,
        "target_role": target_role,
    }


_PUBLIC_ATS_MAX_BYTES = 2 * 1024 * 1024  # 2 MB cap for the no-login public check

# Global daily ceiling on anonymous ATS checks (across ALL IPs) so a determined
# abuser rotating IPs can't run the server Gemini bill up unbounded. Tunable via env;
# a malformed value must not take the whole app down at import, so fall back to default.
try:
    _PUBLIC_ATS_DAILY_CAP = int(os.getenv("PUBLIC_ATS_DAILY_CAP", "500"))
except ValueError:
    _PUBLIC_ATS_DAILY_CAP = 500


class _AtsCheckError(Exception):
    """A user-facing ATS-check failure carrying the HTTP status to return."""
    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.message = message
        self.status = status


def _run_ats_check(text: str, api_key: str, target_role: str, log_user: str = "anon") -> dict:
    """Build the SG-aware ATS prompt, call Gemini, parse + normalize. Shared by the
    logged-in and public endpoints. Raises _AtsCheckError on any user-facing failure."""
    import requests as _req
    import re as _re
    import json as _json

    role_line = (
        f"\nThe candidate is targeting this role: \"{target_role}\". In keyword_match, "
        f"score how well the resume's keywords align with that role, and list matched "
        f"and missing keywords a Singapore recruiter/ATS would expect.\n"
        if target_role else
        "\nNo target role was given — set keyword_match to null.\n"
    )

    prompt = f"""You are an ATS (Applicant Tracking System) auditor for the Singapore job market.
Evaluate the resume text below the way real ATS software (Workday, Greenhouse, Taleo) and a
Singapore recruiter would. Be specific and honest — do not flatter.

Score each of these categories 0-100:
- "ATS Parse-Friendliness": clear standard section headings, no signs of tables/columns/graphics
  that break parsers, machine-readable dates, no garbled text.
- "Keywords & Skills": presence of relevant hard skills and role keywords.
- "Impact & Quantification": action verbs and measurable, quantified achievements (numbers, %, $).
- "Clarity & Conciseness": appropriate length, readable, no filler.
- "Completeness": contact details, work experience with dates, education, skills section.

Return ONLY a valid JSON object (no markdown fences) with exactly these keys:
{{
  "overall_score": 0-100,
  "verdict": "one blunt sentence summarising ATS-readiness",
  "categories": [{{"name": "<one of the five exact names above>", "score": 0-100, "summary": "one specific sentence"}}],
  "issues": [{{"severity": "high|medium|low", "title": "short problem", "fix": "concrete, actionable fix"}}],
  "strengths": ["specific things the resume does well"],
  "keyword_match": {{"score": 0-100, "matched": ["..."], "missing": ["..."]}}
}}
{role_line}
RESUME TEXT:
{text[:8000]}"""

    try:
        resp = _req.post(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
            params={"key": api_key},
            headers={"Content-Type": "application/json"},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "thinkingConfig": {"thinkingBudget": 0},
                    "responseMimeType": "application/json",
                    "maxOutputTokens": 2048,
                },
            },
            timeout=40,
        )
        resp.raise_for_status()
        _rj = resp.json()
        _log_gemini_usage("ats_check", log_user, _rj)
        raw = _rj["candidates"][0]["content"]["parts"][0]["text"]
    except Exception:
        app.logger.warning("ATS check AI request failed (user=%s)", log_user, exc_info=True)
        raise _AtsCheckError("AI request failed. Please try again later.", 502)

    match = _re.search(r'\{[\s\S]*\}', raw)
    if not match:
        raise _AtsCheckError("AI returned unexpected format", 502)
    try:
        report = _json.loads(match.group())
    except Exception:
        raise _AtsCheckError("Could not parse AI response", 502)

    return _normalize_ats_report(report, target_role)


def _issue_counts(issues: list) -> dict:
    return {
        "total":  len(issues),
        "high":   sum(1 for i in issues if i["severity"] == "high"),
        "medium": sum(1 for i in issues if i["severity"] == "medium"),
        "low":    sum(1 for i in issues if i["severity"] == "low"),
    }


def _public_ats_report(report: dict) -> dict:
    """Anonymous tier of the report: overall score, verdict, and category SCORES
    only — no per-category summaries, strengths, keyword detail, or fixes. Counts
    hint at what a free account unlocks. Nothing actionable leaks to anon clients."""
    return {
        "overall_score":   report["overall_score"],
        "verdict":         report["verdict"],
        "categories":      [{"name": c["name"], "score": c["score"]} for c in report["categories"]],
        "strengths_count": len(report.get("strengths") or []),
        "fixes_summary":   _issue_counts(report.get("issues") or []),
        "signup_required": True,
    }


@app.route("/api/resume/ats-check", methods=["POST"])
@login_required
@limiter.limit("5/minute;20/hour")
def resume_ats_check():
    """Score an uploaded resume against an SG-aware ATS rubric. Free for all
    logged-in users (quota-limited) — the top-of-funnel acquisition feature."""
    import config as cfg

    file = request.files.get("resume")
    if not file or not file.filename:
        return jsonify({"error": "No file uploaded"}), 400

    target_role = (request.form.get("target_role") or "").strip()[:120]

    api_key = _decrypt_api_key(current_user.gemini_api_key or "") or cfg.GEMINI_API_KEY
    if not api_key:
        return jsonify({"error": "AI service not configured"}), 400

    allowed, quota_err = _check_ai_quota(current_user)
    if not allowed:
        return jsonify({"error": quota_err, "quota_exceeded": True}), 429

    from resume_parser import extract_text_bounded
    raw = file.read()
    try:
        text = extract_text_bounded(raw, file.filename)
    except (ImportError, ValueError) as e:
        return jsonify({"error": str(e)}), 400

    if not text.strip():
        # Empty extraction from a PDF/DOCX is itself a strong ATS red flag
        # (image-only/scanned resume, or a layout the parser couldn't read).
        return jsonify({"error": "Could not read any text from this file. ATS systems "
                                 "likely can't either — export a text-based PDF, not a scan or image."}), 400

    _store_resume_file(raw, file.filename, "ats_check", user_id=current_user.id,
                       target_role=target_role, content_type=file.content_type)

    try:
        report = _run_ats_check(text, api_key, target_role, log_user=str(current_user.id))
    except _AtsCheckError as e:
        return jsonify({"error": e.message}), e.status

    # Free gets the score + breakdown; Pro unlocks the detailed how-to fixes.
    # Always ship the count summary so free users see what they'd unlock, but
    # never ship the fix text itself to non-paid users (the UI can't be trusted).
    report["fixes_summary"] = _issue_counts(report["issues"])
    report["fixes_locked"] = not _is_paid(current_user)
    if report["fixes_locked"]:
        report["issues"] = []

    return jsonify({"ok": True, "report": report})


@app.route("/api/public/ats-check", methods=["POST"])
@limiter.limit("3/day;1/minute", deduct_when=lambda r: r.status_code == 200)
@limiter.limit(f"{_PUBLIC_ATS_DAILY_CAP}/day", key_func=lambda: "anon_ats_global",
               deduct_when=lambda r: r.status_code == 200)
def public_ats_check():
    """Anonymous ATS check — the public lead magnet. Returns score + category bars
    only (full breakdown/strengths/fixes require an account). Uses the server Gemini
    key, hard rate-limited both per IP (3/day, 1/min) and globally
    (_PUBLIC_ATS_DAILY_CAP/day across all IPs) to bound cost/abuse."""
    import config as cfg

    file = request.files.get("resume")
    if not file or not file.filename:
        return jsonify({"error": "No file uploaded"}), 400

    # Read up to the cap (+1 byte) so an oversize file is rejected without buffering
    # the whole thing into memory.
    raw_bytes = file.read(_PUBLIC_ATS_MAX_BYTES + 1)
    if len(raw_bytes) > _PUBLIC_ATS_MAX_BYTES:
        return jsonify({"error": "File too large — keep it under 2 MB."}), 400

    target_role = (request.form.get("target_role") or "").strip()[:120]

    api_key = cfg.GEMINI_API_KEY
    if not api_key:
        return jsonify({"error": "AI service is temporarily unavailable."}), 503

    from resume_parser import extract_text_bounded
    try:
        text = extract_text_bounded(raw_bytes, file.filename)
    except (ImportError, ValueError) as e:
        return jsonify({"error": str(e)}), 400

    if not text.strip():
        return jsonify({"error": "Could not read any text from this file. ATS systems "
                                 "likely can't either — export a text-based PDF, not a scan or image."}), 400

    # Anonymous upload: no user_id. Auto-purged by the cleanup cron after
    # _PUBLIC_RESUME_RETENTION_DAYS (no account exists to delete it).
    _store_resume_file(raw_bytes, file.filename, "public_ats",
                       target_role=target_role, content_type=file.content_type)

    try:
        report = _run_ats_check(text, api_key, target_role, log_user="public")
    except _AtsCheckError as e:
        return jsonify({"error": e.message}), e.status

    return jsonify({"ok": True, "report": _public_ats_report(report)})


@app.route("/free-resume-check")
def free_ats_check_page():
    """Public, no-login landing page for the ATS checker — the SEO/acquisition magnet."""
    if current_user.is_authenticated:
        return redirect(url_for("app_page"))
    return render_template("free_ats_check.html")


@app.route("/api/resume/tailor", methods=["POST"])
@login_required
@limiter.limit("5/minute;20/hour")
def resume_tailor():
    import config as cfg
    import requests as _req

    allowed, quota_err = _check_ai_quota(current_user)
    if not allowed:
        return jsonify({"error": quota_err, "quota_exceeded": True}), 429

    api_key = _decrypt_api_key(current_user.gemini_api_key or "") or cfg.GEMINI_API_KEY
    if not api_key:
        return jsonify({"error": "AI service not configured"}), 400

    data   = request.json or {}
    job_id = (data.get("job_id") or "").strip()
    if not job_id:
        return jsonify({"error": "job_id required"}), 400

    job = Job.query.filter_by(user_id=current_user.id, source_job_id=job_id).first()
    if not job:
        return jsonify({"error": "Job not found"}), 404

    profile = _get_or_create_profile(current_user.id)
    p       = profile.to_dict()

    work_lines = "\n".join(
        f"  - {w.get('title','')} at {w.get('company','')} ({w.get('period','')}): {w.get('summary','')}"
        for w in (p.get("work_history") or [])
    ) or "  (none)"

    prompt = f"""You are a professional resume coach specialising in Singapore job applications.

Given the following job listing and the candidate's existing profile, suggest tailored resume improvements specifically for this role.

Job: {job.title} at {job.company}
Location: {job.location or 'Singapore'}
Salary: {'$'+str(job.salary_min)+'–$'+str(job.salary_max)+'/mo' if job.salary_min and job.salary_max else 'Not specified'}
Match reasons: {job.match_reasons or 'N/A'}

Candidate profile:
Name: {p.get('name','')}
Current summary: {p.get('experience_summary','(none)')}
Technical skills: {', '.join(p.get('technical_skills') or [])}
Soft skills: {', '.join(p.get('soft_skills') or [])}
Work history:
{work_lines}

Return ONLY a valid JSON object with exactly this structure:
{{
  "tailored_summary": "2-3 sentence professional summary rewritten to emphasise fit for this specific role",
  "work_history": [
    {{
      "title": "same job title as in input",
      "company": "same company as in input",
      "period": "same period as in input",
      "tailored_bullets": "rewritten bullet points emphasising skills relevant to this job — one per line starting with •"
    }}
  ],
  "skills_to_highlight": ["skill1", "skill2", "skill3"],
  "tips": ["actionable tip 1", "actionable tip 2", "actionable tip 3"]
}}"""

    try:
        resp = _req.post(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
            params={"key": api_key},
            headers={"Content-Type": "application/json"},
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=30,
        )
        resp.raise_for_status()
        _rj = resp.json()
        _log_gemini_usage("resume_tailor", str(current_user.id), _rj)
        raw = _rj["candidates"][0]["content"]["parts"][0]["text"]
    except Exception:
        return jsonify({"error": "AI request failed. Please try again later."}), 502

    import re as _re
    import json as _json
    match = _re.search(r'\{[\s\S]*\}', raw)
    if not match:
        return jsonify({"error": "AI returned unexpected format"}), 502

    try:
        tailored = _json.loads(match.group())
    except Exception:
        return jsonify({"error": "Could not parse AI response"}), 502

    return jsonify({
        "ok": True,
        "job": {"title": job.title, "company": job.company},
        "tailored": tailored,
    })


def _merge_tailored_resume_profile(base_profile: dict, resume_payload: dict) -> dict:
    merged = dict(base_profile or {})
    if resume_payload.get("tailored_summary"):
        merged["experience_summary"] = resume_payload["tailored_summary"]

    generated_history = resume_payload.get("work_history") or []
    base_history = merged.get("work_history") or []
    if generated_history:
        next_history = []
        if base_history:
            for idx, original in enumerate(base_history):
                item = dict(original) if isinstance(original, dict) else {}
                generated = generated_history[idx] if idx < len(generated_history) else {}
                if isinstance(generated, dict):
                    item["title"] = generated.get("title") or item.get("title", "")
                    item["company"] = generated.get("company") or item.get("company", "")
                    item["period"] = generated.get("period") or item.get("period", "")
                    bullets = generated.get("tailored_bullets") or generated.get("summary")
                    if bullets:
                        item["summary"] = bullets
                next_history.append(item)
        else:
            for generated in generated_history:
                if not isinstance(generated, dict):
                    continue
                next_history.append({
                    "title": generated.get("title") or "",
                    "company": generated.get("company") or "",
                    "period": generated.get("period") or "",
                    "summary": generated.get("tailored_bullets") or generated.get("summary") or "",
                })
        merged["work_history"] = next_history

    skills = list(merged.get("technical_skills") or [])
    seen = {str(s).strip().lower() for s in skills}
    for skill in resume_payload.get("skills_to_highlight") or []:
        label = str(skill).strip()
        key = label.lower()
        if label and key not in seen:
            skills.append(label)
            seen.add(key)
    merged["technical_skills"] = skills
    return merged


def _coerce_pack_list(value) -> list:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _normalize_application_pack(pack: dict) -> dict:
    if not isinstance(pack, dict):
        pack = {}

    fit_report = pack.get("fit_report")
    if not isinstance(fit_report, dict):
        fit_report = {}
    fit_report["score_label"] = str(fit_report.get("score_label") or "").strip()
    for key in ("strengths", "gaps", "missing_keywords"):
        fit_report[key] = _coerce_pack_list(fit_report.get(key))

    resume = pack.get("resume")
    if not isinstance(resume, dict):
        resume = {}
    resume["tailored_summary"] = str(resume.get("tailored_summary") or "").strip()
    resume["skills_to_highlight"] = _coerce_pack_list(resume.get("skills_to_highlight"))

    work_history = []
    for item in resume.get("work_history") or []:
        if not isinstance(item, dict):
            continue
        work_history.append({
            "title": str(item.get("title") or "").strip(),
            "company": str(item.get("company") or "").strip(),
            "period": str(item.get("period") or "").strip(),
            "tailored_bullets": str(item.get("tailored_bullets") or item.get("summary") or "").strip(),
        })
    resume["work_history"] = work_history

    interview = pack.get("interview")
    if not isinstance(interview, dict):
        interview = {}
    for key in ("focus_areas", "questions", "questions_to_ask"):
        interview[key] = _coerce_pack_list(interview.get(key))

    return {
        "fit_report": fit_report,
        "resume": resume,
        "cover_note": str(pack.get("cover_note") or "").strip(),
        "interview": interview,
        "checklist": _coerce_pack_list(pack.get("checklist")),
        "follow_up_note": str(pack.get("follow_up_note") or "").strip(),
    }


@app.route("/api/application-pack/generate", methods=["POST"])
@login_required
@limiter.limit("3/minute;15/hour")
def generate_application_pack():
    if not _is_paid(current_user):
        return jsonify({"error": "pro_required"}), 403

    import config as cfg
    import requests as _req

    allowed, quota_err = _check_ai_quota(current_user)
    if not allowed:
        return jsonify({"error": quota_err, "quota_exceeded": True}), 429

    api_key = _decrypt_api_key(current_user.gemini_api_key or "") or cfg.GEMINI_API_KEY
    if not api_key:
        return jsonify({"error": "AI service not configured"}), 400

    data = request.json or {}
    job_id = (data.get("job_id") or "").strip()
    if not job_id:
        return jsonify({"error": "job_id required"}), 400

    job = Job.query.filter_by(user_id=current_user.id, source_job_id=job_id).first()
    if not job:
        return jsonify({"error": "Job not found"}), 404

    profile = _get_or_create_profile(current_user.id).to_dict()
    if not profile.get("email"):
        profile["email"] = current_user.email

    settings = _get_or_create_settings(current_user.id).to_dict()
    reasons = [r.strip() for r in (job.match_reasons or "").split("|") if r.strip()]
    profile_text = " ".join([
        profile.get("experience_summary", ""),
        " ".join(profile.get("technical_skills") or []),
        " ".join(profile.get("soft_skills") or []),
        " ".join(
            f"{w.get('title','')} {w.get('company','')} {w.get('summary','')}"
            for w in (profile.get("work_history") or [])
            if isinstance(w, dict)
        ),
    ]).lower()
    likely_missing = [
        kw for kw in (settings.get("preferred_keywords") or [])
        if kw and str(kw).lower() not in profile_text
    ][:12]

    salary = (
        f"${job.salary_min}-{job.salary_max}/mo"
        if job.salary_min and job.salary_max else "Not specified"
    )
    work_lines = "\n".join(
        f"- {w.get('title','')} at {w.get('company','')} ({w.get('period','')}): {w.get('summary','')}"
        for w in (profile.get("work_history") or [])
        if isinstance(w, dict)
    ) or "- No work history provided"

    prompt = f"""You are CareerScan's Singapore job application assistant.

Create a practical application pack for this candidate and job. Use only truthful information from the candidate profile. If a detail is missing, turn it into an action item instead of inventing it.

Return ONLY valid JSON with this exact shape:
{{
  "fit_report": {{
    "score_label": "short judgement of readiness",
    "strengths": ["specific strength", "..."],
    "gaps": ["specific gap or risk", "..."],
    "missing_keywords": ["keyword", "..."]
  }},
  "resume": {{
    "tailored_summary": "2-3 sentence role-specific resume summary",
    "work_history": [
      {{"title": "same or closest title", "company": "same company", "period": "same period", "tailored_bullets": "2-4 truthful bullets, one per line"}}
    ],
    "skills_to_highlight": ["skill", "..."]
  }},
  "cover_note": "short application note under 180 words",
  "interview": {{
    "focus_areas": ["topic to prepare", "..."],
    "questions": ["likely interview question", "..."],
    "questions_to_ask": ["smart question for interviewer", "..."]
  }},
  "checklist": ["concrete next step", "..."],
  "follow_up_note": "short follow-up message for 5-7 days after applying"
}}

JOB:
Title: {job.title or ''}
Company: {job.company or ''}
Location: {job.location or 'Singapore'}
Salary: {salary}
Source: {job.source or ''}
Score: {job.score or 0}/100
CareerScan match reasons: {', '.join(reasons) or 'Not available'}

CANDIDATE:
Name: {profile.get('name','')}
Email: {profile.get('email','')}
Phone: {profile.get('phone','')}
Education: {profile.get('education','')}
Summary: {profile.get('experience_summary','')}
Technical skills: {', '.join(profile.get('technical_skills') or [])}
Soft skills: {', '.join(profile.get('soft_skills') or [])}
Certifications: {', '.join(profile.get('certifications') or [])}
Work history:
{work_lines}

SEARCH CONTEXT:
Target titles: {', '.join(settings.get('target_titles') or [])}
Likely missing profile keywords: {', '.join(likely_missing) or 'None detected'}
Preferred location: {settings.get('preferred_location') or 'Singapore'}
"""

    try:
        resp = _req.post(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
            params={"key": api_key},
            headers={"Content-Type": "application/json"},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "thinkingConfig": {"thinkingBudget": 0},
                    "responseMimeType": "application/json",
                    "maxOutputTokens": 4096,
                },
            },
            timeout=45,
        )
        resp.raise_for_status()
        body = resp.json()
        _log_gemini_usage("application_pack", str(current_user.id), body)
        candidate = (body.get("candidates") or [{}])[0]
        raw = "".join(
            part.get("text", "")
            for part in (candidate.get("content") or {}).get("parts", [])
        ).strip()
    except Exception:
        return jsonify({"error": "AI request failed. Please try again later."}), 502

    match = re.search(r"\{[\s\S]*\}", raw)
    if not match:
        return jsonify({"error": "AI returned unexpected format"}), 502

    try:
        pack = json.loads(match.group())
    except Exception:
        return jsonify({"error": "Could not parse AI response"}), 502

    pack = _normalize_application_pack(pack)
    pack["resume_profile"] = _merge_tailored_resume_profile(profile, pack["resume"])
    pack["job"] = {
        "id": job.source_job_id,
        "title": job.title or "",
        "company": job.company or "",
        "score": job.score or 0,
        "url": job.url or "",
    }

    note_text = (pack.get("cover_note") or "").strip()
    if note_text:
        try:
            from cover_notes import save_cover_note
            notes_dir = _cover_notes_dir(current_user.id)
            notes_dir.mkdir(parents=True, exist_ok=True)
            save_cover_note({
                "title": job.title or "",
                "company": job.company or "",
                "score": job.score or 0,
                "url": job.url or "",
            }, note_text, str(notes_dir))
            job.cover_note = note_text
            db.session.commit()
        except Exception:
            app.logger.warning("Could not save application-pack cover note", exc_info=True)

    return jsonify({"ok": True, "pack": pack})


@app.route("/api/resume/versions", methods=["GET"])
@login_required
def list_resume_versions():
    versions = (ResumeVersion.query
                .filter_by(user_id=current_user.id)
                .order_by(ResumeVersion.created_at.desc())
                .all())
    return jsonify([_resume_version_meta(v) for v in versions])


@app.route("/api/resume/versions", methods=["POST"])
@login_required
def save_resume_version():
    data = request.json or {}
    profile = data.get("profile") or {}
    if not isinstance(profile, dict):
        return jsonify({"error": "profile required"}), 400
    if not profile.get("email"):
        profile["email"] = current_user.email

    version = ResumeVersion(
        user_id=current_user.id,
        name=(data.get("name") or "CareerScan resume").strip()[:80],
        source=(data.get("source") or "manual").strip()[:40],
        job_title=(data.get("job_title") or "").strip()[:120],
        company=(data.get("company") or "").strip()[:120],
        profile=profile,
    )
    db.session.add(version)
    db.session.commit()
    return jsonify({"ok": True, "version": _resume_version_meta(version)})


@app.route("/api/resume/versions/<version_id>/download", methods=["GET"])
@login_required
def download_resume_version(version_id):
    from flask import make_response
    from resume_builder import generate_pdf

    version = ResumeVersion.query.filter_by(
        id=version_id, user_id=current_user.id
    ).first()
    if not version:
        return jsonify({"error": "Not found"}), 404

    profile = version.profile or {}
    try:
        pdf_bytes = generate_pdf(profile)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    name_slug = re.sub(r"[^a-z0-9_-]", "_", (version.name or "resume").lower().replace(" ", "_"))
    resp = make_response(pdf_bytes)
    resp.headers["Content-Type"] = "application/pdf"
    resp.headers["Content-Disposition"] = f'attachment; filename="{name_slug}.pdf"'
    return resp


@app.route("/api/resume/download", methods=["POST"])
@login_required
def resume_download():
    from resume_builder import generate_pdf
    from flask import make_response

    profile = request.json or {}
    # Fill email from authenticated user if missing
    if not profile.get("email"):
        profile["email"] = current_user.email

    try:
        pdf_bytes = generate_pdf(profile)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    name_slug = re.sub(r'[^a-z0-9_\-]', '_', (profile.get("name") or "resume").lower().replace(" ", "_"))
    filename  = f"{name_slug}_resume.pdf"

    resp = make_response(pdf_bytes)
    resp.headers["Content-Type"]        = "application/pdf"
    resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


# ── Analytics ──────────────────────────────────────────────────────────────────

@app.route("/api/analytics")
@login_required
def get_analytics():
    jobs   = _jobs_for_user(current_user.id)
    status = _statuses_for_user(current_user.id)

    empty = {
        "total_unique": 0,
        "sources": {},
        "score_distribution": {"labels": ["0-9","10-19","20-29","30-39","40-49","50-59","60-69","70-79","80-89","90+"], "data": [0]*10},
        "funnel": {"total": 0, "applied": 0, "interviews": 0, "skipped": 0},
        "top_companies": {"labels": [], "data": []},
        "weekly_trend": {"labels": [], "data": []},
    }
    if not jobs:
        return jsonify(empty)

    unique = list({j["id"]: j for j in jobs}.values())
    sources = dict(Counter(j.get("source", "Unknown") for j in unique))

    bins = [0] * 10
    for j in unique:
        idx = min(int(j.get("score", 0)) // 10, 9)
        bins[idx] += 1

    applied    = sum(1 for v in status.values() if v.get("status") == "applied")
    interviews = sum(1 for v in status.values() if v.get("status") == "interview")
    skipped    = sum(1 for v in status.values() if v.get("status") == "skip")

    companies = Counter(
        j.get("company", "") for j in unique
        if j.get("company") and j.get("company").lower() not in ("unknown", "")
    )
    top = companies.most_common(8)

    weekly: dict = defaultdict(int)
    for j in unique:
        date_str = j.get("scan_date", "")
        if not date_str:
            continue
        try:
            dt       = datetime.strptime(date_str.split()[0], "%Y-%m-%d")
            week_key = dt.strftime("%Y-W%W")
            weekly[week_key] += 1
        except ValueError:
            pass

    sorted_weeks = sorted(weekly.items())[-12:]
    week_labels  = []
    for wk, _ in sorted_weeks:
        try:
            yr, wn = wk.split("-W")
            dt = datetime.strptime(f"{yr}-{wn}-1", "%Y-%W-%w")
            week_labels.append(dt.strftime("%b %d"))
        except Exception:
            week_labels.append(wk)

    return jsonify({
        "total_unique": len(unique),
        "sources": sources,
        "score_distribution": {
            "labels": ["0-9","10-19","20-29","30-39","40-49","50-59","60-69","70-79","80-89","90+"],
            "data":   bins,
        },
        "funnel": {
            "total":      len(unique),
            "applied":    applied,
            "interviews": interviews,
            "skipped":    skipped,
        },
        "top_companies": {
            "labels": [c[0] for c in top],
            "data":   [c[1] for c in top],
        },
        "weekly_trend": {
            "labels": week_labels,
            "data":   [w[1] for w in sorted_weeks],
        },
    })


# ── Schedule ───────────────────────────────────────────────────────────────────

@app.route("/api/schedule", methods=["GET"])
@login_required
def get_schedule():
    s = _get_or_create_settings(current_user.id)
    return jsonify({"enabled": s.schedule_enabled, "time": s.schedule_time or "09:00"})


@app.route("/api/schedule", methods=["POST"])
@login_required
def set_schedule():
    data = request.json or {}
    s    = _get_or_create_settings(current_user.id)
    raw_time = data.get("time")
    if not isinstance(raw_time, str) or not re.match(r'^(?:[01]\d|2[0-3]):[0-5]\d$', raw_time):
        return jsonify({"error": "Invalid time format — use HH:MM (00:00-23:59)"}), 400
    s.schedule_enabled = data.get("enabled", False)
    s.schedule_time    = raw_time
    db.session.commit()
    return jsonify({"ok": True, "enabled": s.schedule_enabled, "time": s.schedule_time})


# ── Scan ───────────────────────────────────────────────────────────────────────

@app.route("/api/scan/start", methods=["POST"])
@login_required
@limiter.limit("3/minute;15/hour")
def start_scan():
    if not _is_active(current_user):
        return jsonify({"error": "access_denied"}), 403

    data   = request.json or {}
    mode   = data.get("mode", "analyst")
    notify = data.get("notify", True)

    if not re.match(r'^[a-z0-9_-]{1,32}$', mode):
        return jsonify({"error": "Invalid mode name"}), 400

    # Daily scan limit for free plan — check before claiming the scan slot,
    # but only consume a scan once the slot is actually claimed below.
    free_tier = _is_free_tier(current_user)
    free_settings = None
    if free_tier:
        free_settings = _get_or_create_settings(current_user.id)
        today = _sgt_today().isoformat()
        if free_settings.last_scan_date != today:
            free_settings.daily_scan_count = 0
            free_settings.last_scan_date   = today
            db.session.commit()
        if (free_settings.daily_scan_count or 0) >= _FREE_DAILY_SCAN_LIMIT:
            return jsonify({"error": "daily_limit_reached", "remaining": 0}), 429

    with _get_scan_lock(current_user.id):
        scan = _get_scan(current_user.id)
        if scan["running"]:
            return jsonify({"error": "Scan already running"}), 409
        scan["running"] = True
        scan["q"]       = queue.Queue()

    # Slot claimed — now consume one daily scan (so a 409 never burns quota).
    if free_tier and free_settings is not None:
        free_settings.daily_scan_count = (free_settings.daily_scan_count or 0) + 1
        db.session.commit()

    history_row = ScanHistory(user_id=current_user.id, mode=mode, status="running")
    db.session.add(history_row)
    db.session.commit()
    scan["history_id"] = history_row.id

    extra_env = _build_user_env(current_user)
    t = threading.Thread(
        target=_run_scan_inprocess,
        args=(current_user.id, mode, notify, scan["q"], extra_env),
        daemon=True,
    )
    scan["thread"] = t
    t.start()

    return jsonify({"started": True})


@app.route("/api/scan/stream")
@login_required
def stream_scan():
    user_id = current_user.id

    # Cap concurrent streams so they can't consume every worker thread. If we're
    # at the cap, tell the client to back off rather than holding the connection.
    if not _sse_semaphore.acquire(blocking=False):
        return Response(
            f"data: {json.dumps({'type': 'busy'})}\n\n",
            status=503, mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "Retry-After": "3"},
        )

    @stream_with_context
    def generate():
        try:
            scan = _get_scan(user_id)
            q = scan["q"]
            while True:
                try:
                    line = q.get(timeout=15)
                    if line is None:
                        yield f"data: {json.dumps({'type': 'done'})}\n\n"
                        break
                    yield f"data: {json.dumps({'type': 'log', 'text': line})}\n\n"
                except queue.Empty:
                    # Don't hold a worker thread indefinitely when no scan is
                    # active (e.g. a stale/idle stream): close it out instead.
                    if not scan.get("running"):
                        yield f"data: {json.dumps({'type': 'done'})}\n\n"
                        break
                    yield f"data: {json.dumps({'type': 'ping'})}\n\n"
        finally:
            _sse_semaphore.release()

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/scan/history")
@login_required
def scan_history():
    rows = (ScanHistory.query
            .filter_by(user_id=current_user.id)
            .order_by(ScanHistory.started_at.desc())
            .limit(30)
            .all())
    return jsonify([r.to_dict() for r in rows])


@app.route("/api/jobs/export")
@login_required
def export_jobs():
    import csv as _csv
    import io

    def _csv_safe(value):
        # Neutralise spreadsheet formula injection: a cell that a spreadsheet
        # would evaluate (starts with = + - @, tab or CR) is prefixed with '.
        s = "" if value is None else str(value)
        if s and s[0] in ("=", "+", "-", "@", "\t", "\r"):
            return "'" + s
        return s

    jobs = (Job.query
            .filter_by(user_id=current_user.id)
            .order_by(Job.score.desc())
            .all())

    statuses = _statuses_for_user(current_user.id)

    output = io.StringIO()
    writer = _csv.writer(output)
    writer.writerow(["title", "company", "location", "score", "salary_min", "salary_max",
                     "source", "posted_date", "closing_date", "status", "url"])
    for j in jobs:
        st = statuses.get(j.source_job_id, {}).get("status", "")
        writer.writerow([_csv_safe(v) for v in (
            j.title, j.company, j.location, j.score,
            j.salary_min or "", j.salary_max or "",
            j.source, j.posted_date or "", j.closing_date or "",
            st, j.url or "",
        )])

    csv_bytes = output.getvalue().encode("utf-8-sig")
    return Response(
        csv_bytes,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=jobs.csv"},
    )


@app.route("/api/reset", methods=["POST"])
@login_required
def reset_seen():
    SeenJob.query.filter_by(user_id=current_user.id).delete()
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/api/auth/delete-account", methods=["POST"])
@login_required
@limiter.limit("3/hour")
def delete_account():
    import shutil
    data = request.json or {}

    # Email/password users must confirm with their password
    if current_user.password_hash:
        password = data.get("password", "")
        if not password or not current_user.check_password(password):
            return jsonify({"error": "Incorrect password"}), 403
    else:
        # Google-only users confirm with the word DELETE
        if data.get("confirm") != "DELETE":
            return jsonify({"error": "Type DELETE to confirm"}), 403

    user_id = current_user.id

    # Cancel Stripe subscription if active
    if current_user.stripe_customer_id and current_user.subscription_status == "active":
        try:
            import stripe as _stripe
            _stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
            subs = _stripe.Subscription.list(customer=current_user.stripe_customer_id, limit=1)
            for sub in subs.auto_paging_iter():
                _stripe.Subscription.cancel(sub.id)
                break
        except Exception:
            pass  # don't block deletion if Stripe call fails

    # Log out before deleting
    logout_user()

    # Delete DB record — cascades to all related tables
    user = db.session.get(User, user_id)
    if user:
        db.session.delete(user)
        db.session.commit()

    # Delete user's data directory
    data_dir = Path(f"data/users/{user_id}")
    if data_dir.exists():
        shutil.rmtree(data_dir, ignore_errors=True)

    return jsonify({"ok": True})


# ── Billing ───────────────────────────────────────────────────────────────────

# Daily AI-call caps by subscription status. Unknown statuses fall back to 5.
_AI_DAILY_LIMITS = {
    "active":    30,
    "free":       5,
    "trialing":   5,  # legacy rows — same as free
    "past_due":   5,
    "cancelled":  0,
    "expired":    0,
}

# Statuses that still grant access to the app (run scans, view results).
_ACCESS_STATUSES = ("active", "free", "trialing", None)

# Per-day scan cap for free-tier users.
_FREE_DAILY_SCAN_LIMIT = 3


def _entitlements(user) -> dict:
    """Single source of truth for a user's plan and what it unlocks.

    Every billing / quota / gating decision derives from this dict so the
    rules can't drift between the UI, scan gating, and AI-quota checks.

    Keys:
      status            display status (trialing/None collapse to "free")
      has_access        may use the app at all (run scans, view results)
      is_paid           active subscriber (or admin) — exempt from free caps
      is_free_tier      subject to free caps (10-result + daily-scan limit)
      scan_result_limit per-scan result cap (10 for free, None = unlimited)
      ai_daily_limit    daily AI-call cap (None = unlimited)
    """
    if user.is_admin:
        return {
            "status":            "active",
            "has_access":        True,
            "is_paid":           True,
            "is_free_tier":      False,
            "scan_result_limit": None,
            "ai_daily_limit":    None,
        }

    raw     = user.subscription_status
    display = "free" if raw in (None, "trialing") else raw
    is_paid = raw == "active"
    # The only non-active status that still grants access is plain "free".
    free_with_access = display == "free"
    return {
        "status":            display,
        "has_access":        raw in _ACCESS_STATUSES,
        "is_paid":           is_paid,
        "is_free_tier":      not is_paid,
        "scan_result_limit": 10 if free_with_access else None,
        "ai_daily_limit":    _AI_DAILY_LIMITS.get(raw, 5),
    }


def _is_active(user) -> bool:
    """True if the user may use the app (admin, active, free, or legacy trialing)."""
    return _entitlements(user)["has_access"]


def _is_free_tier(user) -> bool:
    """True for non-paying users subject to free-tier limits
    (daily scan cap + 10-result cap). Admins and active subscribers are exempt."""
    return _entitlements(user)["is_free_tier"]


def _is_paid(user) -> bool:
    """True only for paying subscribers (or admins) — gates Pro-only features
    such as cover-note generation. `_is_active` is too permissive here: it also
    returns True for free-tier users, which would let them bypass the Pro gate."""
    return _entitlements(user)["is_paid"]


_STRIPE_STATUS_MAP = {
    "active":   "active",
    "past_due": "past_due",
    "canceled": "cancelled",
    "unpaid":   "past_due",
    "paused":   "past_due",
}


_SUB_STATUS_RANK = {"active": 3, "trialing": 3, "past_due": 2, "unpaid": 2, "paused": 2}


def _best_sub_for_customer(customer_id, stripe_mod):
    """Return the highest-priority subscription for a Stripe customer, or None
    if the customer genuinely has none.

    Only "No such customer" (a stale/wrong-mode id) is treated as no-sub.
    Every other Stripe error (permission, auth, rate limit, network) is
    re-raised so callers never mistake an API outage for an empty result —
    which would otherwise wrongly downgrade active subscribers to cancelled."""
    try:
        subs = stripe_mod.Subscription.list(customer=customer_id, limit=10, status="all")
    except stripe_mod.error.InvalidRequestError:
        return None
    best = None
    for s in subs.data:
        if best is None or _SUB_STATUS_RANK.get(s.status, 1) > _SUB_STATUS_RANK.get(best.status, 1):
            best = s
    return best


def _lookup_stripe_subscription(user, stripe_mod):
    """Find the user's best-matching Stripe subscription and the customer it
    belongs to, returning (subscription_or_None, customer_id_or_None).

    Checks the stored customer id first; if that yields nothing, falls back to
    searching Stripe by the user's email — this catches subscriptions that
    landed on a duplicate/mismatched customer the app never linked back.

    Raises on real Stripe errors (e.g. a restricted key missing
    subscription_read); callers must catch and skip rather than downgrade."""
    candidate_ids = []
    if user.stripe_customer_id:
        candidate_ids.append(user.stripe_customer_id)

    for cid in candidate_ids:
        sub = _best_sub_for_customer(cid, stripe_mod)
        if sub is not None:
            return sub, cid

    # Nothing under the stored customer — search by email as a fallback.
    best_sub, best_cid = None, None
    if user.email:
        customers = stripe_mod.Customer.list(email=user.email, limit=10).data
        for c in customers:
            if c.id in candidate_ids:
                continue
            sub = _best_sub_for_customer(c.id, stripe_mod)
            if sub is not None and (
                best_sub is None
                or _SUB_STATUS_RANK.get(sub.status, 1) > _SUB_STATUS_RANK.get(best_sub.status, 1)
            ):
                best_sub, best_cid = sub, c.id

    return best_sub, best_cid


def _sync_user_subscription(user, stripe_mod) -> bool:
    """Reconcile a user's subscription_status straight from Stripe (the source
    of truth) and return True if anything changed.

    This recovers accounts that paid but never flipped to active because a
    webhook was missed, failed signature check, or wasn't delivered. A plain
    free user who merely has a dangling customer id (started checkout, never
    paid) is left untouched rather than wrongly marked cancelled.
    """
    sub, cid = _lookup_stripe_subscription(user, stripe_mod)
    changed = False

    # Repair a missing/mismatched customer link so it can't drift again.
    if cid and user.stripe_customer_id != cid:
        app.logger.info(
            "Repairing stripe_customer_id for user %s: %s -> %s",
            user.id, user.stripe_customer_id, cid,
        )
        user.stripe_customer_id = cid
        changed = True

    if sub is None:
        # No subscription anywhere: only downgrade someone who previously had
        # one — never knock a plain free user back to cancelled.
        if user.subscription_status in ("active", "past_due"):
            user.subscription_status = "cancelled"
            changed = True
        return changed

    new_status = _STRIPE_STATUS_MAP.get(sub.status, user.subscription_status)
    if new_status != user.subscription_status:
        user.subscription_status = new_status
        changed = True
    return changed


def _check_ai_quota(user) -> tuple[bool, str | None]:
    """Check and increment the user's daily AI call quota.

    Returns (allowed, error_message). Commits the counter increment on success.
    Admins are always allowed.
    """
    if user.is_admin:
        return True, None

    today = _sgt_today()
    if user.ai_calls_reset_date != today:
        user.ai_calls_today = 0
        user.ai_calls_reset_date = today

    limit = _entitlements(user)["ai_daily_limit"]
    if limit is None:
        return True, None
    if limit == 0:
        return False, "AI features are unavailable on your current plan."
    if (user.ai_calls_today or 0) >= limit:
        return False, f"Daily AI limit reached ({limit} calls/day). Resets at midnight SGT."

    user.ai_calls_today = (user.ai_calls_today or 0) + 1
    db.session.commit()
    return True, None


def _billing_status(user) -> dict:
    ent = _entitlements(user)
    return {
        "status":         ent["status"],
        "has_access":     ent["has_access"],
        # "is_free" in the UI means a free-tier user who still has access
        # (the 10-result/daily-scan banner) — not merely "not paid".
        "is_free":        ent["has_access"] and ent["is_free_tier"],
        "scan_limit":     ent["scan_result_limit"],
        "email_verified": bool(user.email_verified),
    }


@app.route("/api/billing/status")
@login_required
def billing_status():
    return jsonify(_billing_status(current_user))


@app.route("/api/billing/sync", methods=["POST"])
@login_required
def billing_sync():
    """Reconcile the current user's subscription straight from Stripe.
    Called when returning from Checkout so activation never depends solely on
    webhook delivery (which can be missed, fail signature, or be undelivered)."""
    import stripe as _stripe
    _stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
    if _stripe.api_key and current_user.stripe_customer_id:
        try:
            if _sync_user_subscription(current_user, _stripe):
                db.session.commit()
        except Exception as e:
            app.logger.warning("Billing sync failed for user %s: %s", current_user.id, e)
    return jsonify(_billing_status(current_user))


@app.route("/api/billing/create-checkout", methods=["POST"])
@login_required
def create_checkout():
    import stripe
    stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
    price_id       = os.getenv("STRIPE_PRICE_ID", "")

    if not stripe.api_key or not price_id:
        return jsonify({"error": "Billing not configured on the server"}), 500

    if not current_user.stripe_customer_id:
        customer = stripe.Customer.create(email=current_user.email)
        current_user.stripe_customer_id = customer.id
        db.session.commit()

    base = request.host_url.rstrip("/")
    session = stripe.checkout.Session.create(
        customer=current_user.stripe_customer_id,
        payment_method_types=["card"],
        line_items=[{"price": price_id, "quantity": 1}],
        mode="subscription",
        success_url=f"{base}/?billing=success",
        cancel_url=f"{base}/?billing=cancel",
    )
    return jsonify({"url": session.url})


@app.route("/api/billing/portal", methods=["POST"])
@login_required
def billing_portal():
    import stripe
    stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")

    if not current_user.stripe_customer_id:
        return jsonify({"error": "No billing account found — subscribe first"}), 400

    base    = request.host_url.rstrip("/")
    session = stripe.billing_portal.Session.create(
        customer=current_user.stripe_customer_id,
        return_url=f"{base}/",
    )
    return jsonify({"url": session.url})


@app.route("/api/stripe/webhook", methods=["POST"])
def billing_webhook():
    import stripe
    stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
    secret         = os.getenv("STRIPE_WEBHOOK_SECRET", "")

    if not secret:
        return "", 400

    payload = request.get_data()
    sig     = request.headers.get("Stripe-Signature", "")

    try:
        # Signature check only — then parse the raw payload as plain JSON.
        # stripe v15+ StripeObject no longer subclasses dict, so .get() on
        # the constructed event crashes; plain dicts are version-proof.
        stripe.Webhook.construct_event(payload, sig, secret)
        event = json.loads(payload)
    except (stripe.error.SignatureVerificationError, ValueError):
        return "", 400

    obj         = event["data"]["object"]
    customer_id = obj.get("customer")
    if not customer_id:
        return "", 200

    user = User.query.filter_by(stripe_customer_id=customer_id).first()
    if not user:
        return "", 200

    et = event["type"]
    new_status = None
    if et in ("customer.subscription.created", "customer.subscription.updated"):
        stripe_status = obj.get("status", "")
        # Normalise via the same map the cron sync uses so the two write paths
        # can never store different strings for the same real state (e.g.
        # "canceled" vs "cancelled"). Unknown statuses fall through unchanged.
        new_status = _STRIPE_STATUS_MAP.get(stripe_status, stripe_status)
    elif et == "customer.subscription.deleted":
        new_status = "cancelled"
    elif et == "invoice.payment_failed":
        new_status = "past_due"

    if new_status is not None and user.subscription_status != new_status:
        user.subscription_status = new_status
        db.session.commit()
    return "", 200


# ── Interview Prep ────────────────────────────────────────────────────────────

@app.route("/api/interview-prep", methods=["POST"])
@login_required
@limiter.limit("10/minute;30/hour")
def interview_prep():
    import config as cfg
    import requests as _req

    allowed, quota_err = _check_ai_quota(current_user)
    if not allowed:
        return jsonify({"error": quota_err, "quota_exceeded": True}), 429

    data   = request.json or {}
    job_id = (data.get("job_id") or "").strip()
    if not job_id:
        return jsonify({"error": "job_id required"}), 400

    job = Job.query.filter_by(user_id=current_user.id, source_job_id=job_id).first()
    if not job:
        return jsonify({"error": "Job not found"}), 404

    api_key = _decrypt_api_key(current_user.gemini_api_key or "") or cfg.GEMINI_API_KEY
    if not api_key:
        return jsonify({"error": "AI service not configured"}), 400

    p = current_user.profile
    profile_text = ""
    if p:
        skills = ", ".join((p.technical_skills or [])[:12])
        history = "\n".join(
            f"  - {j.get('title','')} at {j.get('company','')} ({j.get('period','')})"
            for j in (p.work_history or [])[:3]
        )
        profile_text = (
            f"Education: {p.education or 'Not specified'}\n"
            f"Skills: {skills or 'Not specified'}\n"
            f"Summary: {(p.experience_summary or '')[:300]}\n"
            f"Work history:\n{history or '  (none)'}"
        )

    reasons = [r.strip() for r in (job.match_reasons or "").split("|") if r.strip()]
    sal = f"${job.salary_min:,}–${job.salary_max:,}/mo" if job.salary_min and job.salary_max else "not specified"

    prompt = f"""You are an interview coach preparing a candidate for a job interview in Singapore.

Role: {job.title}
Company: {job.company}
Location: {job.location or 'Singapore'}
Salary: {sal}
Why they matched: {', '.join(reasons[:6]) or 'Not available'}

Candidate profile:
{profile_text}

Generate interview prep in JSON — no markdown, no explanation, just the object:
{{
  "technical": [
    {{"question": "...", "tip": "short actionable tip tailored to their background"}}
  ],
  "behavioral": [
    {{"question": "...", "tip": "brief STAR-format hint"}}
  ],
  "company_fit": [
    {{"question": "...", "tip": "..."}}
  ],
  "ask_them": ["smart question to ask interviewer", "..."]
}}

Rules:
- 5 technical questions specific to this role's skills
- 4 behavioral questions (STAR format)
- 3 company/culture fit questions
- 3 smart questions the candidate should ask the interviewer
- Tips must reference the candidate's actual background where possible"""

    try:
        resp = _req.post(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
            params={"key": api_key},
            headers={"Content-Type": "application/json"},
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=30,
        )
        resp.raise_for_status()
        _rj = resp.json()
        _log_gemini_usage("interview_prep", str(current_user.id), _rj)
        raw = _rj["candidates"][0]["content"]["parts"][0]["text"]
    except Exception:
        return jsonify({"error": "AI request failed. Please try again later."}), 502

    match = re.search(r'\{[\s\S]*\}', raw)
    if not match:
        return jsonify({"error": "AI returned unexpected format"}), 502

    try:
        result = json.loads(match.group())
    except Exception:
        return jsonify({"error": "Could not parse AI response"}), 502

    return jsonify({
        "ok":  True,
        "job": {"title": job.title, "company": job.company},
        "prep": result,
    })


# ── Cron endpoint (Railway scheduled job) ─────────────────────────────────────

@app.route("/api/cron/scan", methods=["POST"])
def cron_scan():
    """
    Called by Railway cron every 15 minutes.
    Header X-Cron-Secret must match CRON_SECRET env var.
    Triggers scans for all users whose schedule_time is within ±7 minutes of now.
    """
    secret   = os.getenv("CRON_SECRET", "")
    incoming = request.headers.get("X-Cron-Secret", "")
    if not secret or not hmac.compare_digest(incoming, secret):
        return jsonify({"error": "Forbidden"}), 403

    # Users pick schedule_time in their local Singapore time (SGT, UTC+8), so we
    # must compare against the current SGT wall-clock — not raw UTC, which would
    # fire every scheduled scan 8 hours off from what the user intended.
    now_hm  = datetime.now(SGT).strftime("%H:%M")
    now_h   = int(now_hm.split(":")[0])
    now_m   = int(now_hm.split(":")[1])
    now_min = now_h * 60 + now_m

    triggered = []
    users_due = UserSettings.query.filter_by(schedule_enabled=True).all()
    for s in users_due:
        if not s.schedule_time:
            continue
        try:
            sh, sm = s.schedule_time.split(":")
            sched_min = int(sh) * 60 + int(sm)
        except Exception:
            continue

        diff = abs(now_min - sched_min)
        if min(diff, 1440 - diff) <= 7:
            user  = db.session.get(User, s.user_id)
            scan  = _get_scan(s.user_id)
            if user and not scan["running"]:
                scan["running"] = True
                scan["q"]       = queue.Queue()
                # Record this scan in history (and avoid reusing a stale
                # history_id left in the scan dict by a previous manual run).
                history_row = ScanHistory(user_id=s.user_id, mode="analyst", status="running")
                db.session.add(history_row)
                db.session.commit()
                scan["history_id"] = history_row.id
                extra_env = _build_user_env(user)
                t = threading.Thread(
                    target=_run_scan_inprocess,
                    args=(s.user_id, "analyst", True, scan["q"], extra_env),
                    daemon=True,
                )
                scan["thread"] = t
                t.start()
                triggered.append(user.email)

    return jsonify({"triggered": triggered})


@app.route("/api/cron/weekly-digest", methods=["POST"])
def cron_weekly_digest():
    """
    Send a weekly top-matches digest to all users with email digests enabled.
    Called by Render cron every Monday at 08:00 SGT (00:00 UTC).
    """
    secret   = os.getenv("CRON_SECRET", "")
    incoming = request.headers.get("X-Cron-Secret", "")
    if not secret or not hmac.compare_digest(incoming, secret):
        return jsonify({"error": "Forbidden"}), 403

    from notifier import send_weekly_digest

    cutoff   = datetime.now(timezone.utc) - timedelta(days=7)
    base_url = request.host_url.rstrip("/")
    sent_to  = []

    settings_list = UserSettings.query.filter_by(email_enabled=True).all()
    for s in settings_list:
        user = db.session.get(User, s.user_id)
        if not user:
            continue
        # Require verified email (Google OAuth users are implicitly verified)
        if not user.email_verified and not user.google_id:
            continue

        to_email = (s.email_to or "").strip() or user.email
        if not to_email:
            continue

        # Top jobs from the last 7 days, excluding hidden
        recent = (
            Job.query
            .filter_by(user_id=s.user_id, hidden=False)
            .filter(Job.scan_date >= cutoff)
            .order_by(Job.score.desc())
            .limit(30)
            .all()
        )
        if not recent:
            continue

        # Exclude jobs the user explicitly skipped
        skipped_ids = {
            r.job_source_id
            for r in ApplicationStatus.query.filter_by(
                user_id=s.user_id, status="skip"
            ).all()
        }
        top_jobs = [j.to_dict() for j in recent if j.source_job_id not in skipped_ids][:8]
        if not top_jobs:
            continue

        week_total = Job.query.filter_by(user_id=s.user_id).filter(
            Job.scan_date >= cutoff
        ).count()

        ok = send_weekly_digest(to_email, top_jobs, base_url=base_url, week_total=week_total)
        if ok:
            sent_to.append(to_email)

    return jsonify({"sent": len(sent_to), "recipients": sent_to})


@app.route("/api/cron/cleanup", methods=["POST"])
def cron_cleanup():
    """
    Delete jobs older than 60 days, keeping any that have an application status.
    Also removes scan_history rows older than 90 days.
    Called daily by Render cron.
    """
    secret   = os.getenv("CRON_SECRET", "")
    incoming = request.headers.get("X-Cron-Secret", "")
    if not secret or not hmac.compare_digest(incoming, secret):
        return jsonify({"error": "Forbidden"}), 403

    cutoff_jobs  = datetime.now(timezone.utc) - timedelta(days=60)
    cutoff_scans = datetime.now(timezone.utc) - timedelta(days=90)

    # IDs of jobs that have an application status — never delete these
    tracked_ids = {
        r.job_source_id
        for r in ApplicationStatus.query.with_entities(ApplicationStatus.job_source_id).all()
    }

    stale = Job.query.filter(Job.scan_date < cutoff_jobs).all()
    deleted_jobs = 0
    for job in stale:
        if job.source_job_id not in tracked_ids:
            db.session.delete(job)
            deleted_jobs += 1

    deleted_scans = ScanHistory.query.filter(
        ScanHistory.started_at < cutoff_scans
    ).delete(synchronize_session=False)

    # Anonymous (no-account) resume uploads have no deletion path, so purge them
    # after the retention window. Logged-in CVs are kept until account deletion.
    cutoff_anon_resumes = datetime.now(timezone.utc) - timedelta(days=_PUBLIC_RESUME_RETENTION_DAYS)
    deleted_anon_resumes = ResumeFile.query.filter(
        ResumeFile.user_id.is_(None),
        ResumeFile.uploaded_at < cutoff_anon_resumes,
    ).delete(synchronize_session=False)

    # Backstop: fail scans stuck at 'running' >1h (startup reconciliation
    # covers worker-kill restarts; this catches a hung thread with no restart).
    cutoff_running = datetime.now(timezone.utc) - timedelta(hours=1)
    stuck_scans = ScanHistory.query.filter(
        ScanHistory.status == "running",
        ScanHistory.started_at < cutoff_running,
    ).update(
        {"status": "failed", "finished_at": datetime.now(timezone.utc)},
        synchronize_session=False,
    )

    db.session.commit()
    return jsonify({
        "deleted_jobs": deleted_jobs,
        "deleted_scan_history": deleted_scans,
        "deleted_anon_resumes": deleted_anon_resumes,
        "failed_stuck_scans": stuck_scans,
    })



@app.route("/api/cron/stripe-sync", methods=["POST"])
def cron_stripe_sync():
    """
    Sync Stripe subscription status for all non-free users.
    Corrects drift caused by missed or failed webhooks.
    Called daily by Render cron.
    """
    secret   = os.getenv("CRON_SECRET", "")
    incoming = request.headers.get("X-Cron-Secret", "")
    if not secret or not hmac.compare_digest(incoming, secret):
        return jsonify({"error": "Forbidden"}), 403

    import stripe as _stripe
    _stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
    if not _stripe.api_key:
        return jsonify({"error": "STRIPE_SECRET_KEY not set"}), 500

    # Any user who has ever reached Stripe checkout has a customer id. Check
    # all of them — not just those already marked paid — so an account that
    # paid but never flipped (missed/failed webhook) self-heals here.
    users = User.query.filter(User.stripe_customer_id != None).all()  # noqa: E711

    updated, errors = [], []
    for user in users:
        try:
            if _sync_user_subscription(user, _stripe):
                updated.append(user.email)
        except Exception as e:
            # Never let a Stripe API/permission error masquerade as a clean
            # no-op — log it and report a count so an outage is visible.
            errors.append(user.email)
            app.logger.warning("stripe-sync failed for %s: %s", user.email, e)
            continue

    db.session.commit()
    if errors:
        app.logger.error("stripe-sync: %d/%d users errored", len(errors), len(users))
    return jsonify({"synced": len(users), "updated": updated, "errors": len(errors)})


@app.route("/api/admin/billing-debug", methods=["POST"])
def admin_billing_debug():
    """Ops diagnostic: show what Stripe actually holds, so an orphaned
    subscription (attached to a customer the app never linked) can be found.
    Secret-gated so it's callable via curl with X-Cron-Secret."""
    secret = os.getenv("CRON_SECRET", "")
    if not secret or not hmac.compare_digest(request.headers.get("X-Cron-Secret", ""), secret):
        return jsonify({"error": "Forbidden"}), 403

    import stripe as _stripe
    from sqlalchemy import func
    _stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
    if not _stripe.api_key:
        return jsonify({"error": "STRIPE_SECRET_KEY not set"}), 500

    email = (request.args.get("email") or (request.json or {}).get("email") or "").strip().lower()
    out = {"query_email": email}

    user = User.query.filter(func.lower(User.email) == email).first() if email else None
    out["app_user"] = None if not user else {
        "id": user.id,
        "email": user.email,
        "stripe_customer_id": user.stripe_customer_id,
        "subscription_status": user.subscription_status,
    }

    try:
        custs = _stripe.Customer.list(email=email, limit=20).data if email else []
        out["stripe_customers_for_email"] = [{"id": c.id, "email": c.email} for c in custs]
    except Exception as e:
        out["stripe_customers_for_email_error"] = str(e)

    # Recent subscriptions across the whole account — to spot an orphan whose
    # customer email differs from the app account.
    try:
        recent = []
        for s in _stripe.Subscription.list(limit=20, status="all").data:
            cust_email = None
            try:
                cust_email = getattr(_stripe.Customer.retrieve(s.customer), "email", None)
            except Exception:
                pass
            recent.append({
                "sub_id": s.id,
                "status": s.status,
                "customer": s.customer,
                "customer_email": cust_email,
            })
        out["recent_subscriptions"] = recent
    except Exception as e:
        out["recent_subscriptions_error"] = str(e)

    return jsonify(out)


@app.route("/api/admin/grant-pro", methods=["POST"])
def admin_grant_pro():
    """Ops action: force an account to active (paid but unlinked subscription,
    comps, support). Secret-gated for curl use. Optionally link a Stripe
    customer id at the same time so future syncs stay correct."""
    secret = os.getenv("CRON_SECRET", "")
    if not secret or not hmac.compare_digest(request.headers.get("X-Cron-Secret", ""), secret):
        return jsonify({"error": "Forbidden"}), 403

    from sqlalchemy import func
    body = request.json or {}
    email = (request.args.get("email") or body.get("email") or "").strip().lower()
    customer_id = (request.args.get("customer_id") or body.get("customer_id") or "").strip()
    if not email:
        return jsonify({"error": "email required"}), 400

    user = User.query.filter(func.lower(User.email) == email).first()
    if not user:
        return jsonify({"error": "No user with that email"}), 404

    if customer_id:
        user.stripe_customer_id = customer_id
    user.subscription_status = "active"
    db.session.commit()
    app.logger.info(
        "Admin granted Pro to %s (customer_id=%s)", user.email, user.stripe_customer_id
    )
    return jsonify({
        "ok": True,
        "email": user.email,
        "subscription_status": user.subscription_status,
        "stripe_customer_id": user.stripe_customer_id,
    })


@app.route("/api/cron/billing-health", methods=["POST"])
def cron_billing_health():
    secret   = os.getenv("CRON_SECRET", "")
    incoming = request.headers.get("X-Cron-Secret", "")
    if not secret or not hmac.compare_digest(incoming, secret):
        return jsonify({"error": "Forbidden"}), 403

    admin_email = os.getenv("ADMIN_EMAIL", "").strip()
    past_due = User.query.filter_by(subscription_status="past_due").all()
    if past_due and admin_email:
        rows = "".join(
            f"<tr><td>{html.escape(u.email or '')}</td>"
            f"<td>{html.escape(u.stripe_customer_id or '—')}</td></tr>"
            for u in past_due
        )
        body_html = (
            f"<h2>Billing Health — {len(past_due)} past_due account(s)</h2>"
            f"<table><tr><th>Email</th><th>Stripe Customer</th></tr>{rows}</table>"
        )
        _send_email(admin_email, f"[CareerScan] {len(past_due)} past_due account(s)", body_html)
    return jsonify({"past_due": len(past_due), "alerted": bool(past_due and admin_email)})


# ── Error capture ───────────────────────────────────────────────────────────────

def _log_app_error(source="other", exc=None, message=None, level="error",
                   path=None, method=None, status_code=None, user_email=None):
    """Persist an error to the error_logs table for the admin dashboard.

    Best-effort: this must NEVER raise, or it could mask the original error or
    crash the error handler. Identical errors are de-duplicated by fingerprint —
    a repeat bumps occurrences/last_seen on the open row instead of inserting.
    Must be called inside an app context (request handlers have one; the scan
    thread wraps its call in `with app.app_context()`).
    """
    try:
        err_type = type(exc).__name__ if exc is not None else "Error"
        msg = (message if message is not None else (str(exc) if exc is not None else "")) or ""
        tb = None
        if exc is not None:
            tb = "".join(_traceback.format_exception(type(exc), exc, exc.__traceback__))
        fingerprint = hashlib.sha256(
            f"{source}|{err_type}|{msg[:200]}".encode("utf-8", "replace")
        ).hexdigest()[:32]

        # Discard any half-finished transaction from the failed operation before writing.
        try:
            db.session.rollback()
        except Exception:
            pass

        now = datetime.now(timezone.utc)
        existing = (ErrorLog.query
                    .filter_by(fingerprint=fingerprint, resolved=False)
                    .first())
        if existing:
            existing.occurrences = (existing.occurrences or 1) + 1
            existing.last_seen   = now
            existing.message     = msg[:2000]
            if tb:
                existing.traceback = tb[:8000]
            existing.path        = path or existing.path
            existing.method      = method or existing.method
            existing.status_code = status_code or existing.status_code
            existing.user_email  = user_email or existing.user_email
        else:
            db.session.add(ErrorLog(
                fingerprint=fingerprint,
                level=level,
                source=source,
                error_type=err_type,
                message=msg[:2000],
                traceback=(tb[:8000] if tb else None),
                path=(path[:255] if path else None),
                method=(method[:8] if method else None),
                status_code=status_code,
                user_email=(user_email[:255] if user_email else None),
                first_seen=now,
                last_seen=now,
            ))
        db.session.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
        try:
            app.logger.warning("Failed to persist error log", exc_info=True)
        except Exception:
            pass


@app.errorhandler(Exception)
def _handle_uncaught_exception(e):
    """Capture unhandled exceptions to the error dashboard, then respond.

    HTTP 4xx (404/403/redirects/etc.) pass through untouched — only server-side
    failures (unhandled exceptions and 5xx HTTPExceptions) are logged.
    """
    from werkzeug.exceptions import HTTPException

    email = None
    try:
        if getattr(current_user, "is_authenticated", False):
            email = current_user.email
    except Exception:
        pass

    if isinstance(e, HTTPException):
        if e.code and e.code >= 500:
            _log_app_error(source="request", exc=e, path=request.path,
                           method=request.method, status_code=e.code, user_email=email)
        return e  # preserve normal 4xx/redirect behaviour

    _log_app_error(source="request", exc=e, path=request.path,
                   method=request.method, status_code=500, user_email=email)
    app.logger.error("Unhandled exception on %s %s", request.method, request.path, exc_info=True)

    if request.path.startswith("/api/"):
        return jsonify({"error": "Internal server error"}), 500
    return ("<h1>Something went wrong</h1>"
            "<p>An unexpected error occurred. We've logged it and will look into it.</p>"
            "<p><a href='/app'>Back to app</a></p>"), 500


# ── Admin ─────────────────────────────────────────────────────────────────────

def _is_admin(user) -> bool:
    admin_email = os.getenv("ADMIN_EMAIL", "").strip().lower()
    return bool(admin_email and user.email.lower() == admin_email)


@app.route("/admin")
@login_required
def admin_page():
    if not _is_admin(current_user):
        return redirect(url_for("app_page"))
    return render_template("admin.html")


@app.route("/api/admin/stats")
@login_required
def admin_stats():
    if not _is_admin(current_user):
        return jsonify({"error": "Forbidden"}), 403

    from sqlalchemy import func

    total_users  = User.query.count()
    active_users = User.query.filter_by(subscription_status="active").count()
    free_users   = User.query.filter(
        User.subscription_status.notin_(["active"])
    ).count()
    total_jobs   = Job.query.count()

    # New signups in last 7 days
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    new_users_7d = User.query.filter(User.created_at >= week_ago).count()

    # Estimated MRR (active subscriptions × $8)
    mrr = active_users * 8

    # Jobs scanned per day over last 14 days (using scan_date)
    fourteen_ago = datetime.now(timezone.utc) - timedelta(days=14)
    daily_rows = (
        db.session.query(
            func.date(Job.scan_date).label("day"),
            func.count(Job.id).label("count"),
        )
        .filter(Job.scan_date >= fourteen_ago)
        .group_by(func.date(Job.scan_date))
        .order_by(func.date(Job.scan_date))
        .all()
    )
    daily_scans = [{"day": str(r.day), "count": r.count} for r in daily_rows]

    return jsonify({
        "total_users":   total_users,
        "active_users":  active_users,
        "free_users":    free_users,
        "total_jobs":    total_jobs,
        "new_users_7d":  new_users_7d,
        "mrr":           mrr,
        "daily_scans":   daily_scans,
    })


@app.route("/api/admin/users")
@login_required
def admin_users():
    if not _is_admin(current_user):
        return jsonify({"error": "Forbidden"}), 403

    from sqlalchemy import func

    # Job count + last scan date per user
    job_stats = dict(
        db.session.query(
            Job.user_id,
            func.count(Job.id),
        )
        .group_by(Job.user_id)
        .all()
    )
    last_scan = dict(
        db.session.query(
            Job.user_id,
            func.max(Job.scan_date),
        )
        .group_by(Job.user_id)
        .all()
    )

    users = User.query.order_by(User.created_at.desc()).all()
    result = []
    for u in users:
        ls = last_scan.get(u.id)
        result.append({
            "id":          u.id,
            "email":       u.email,
            "status":      u.subscription_status or "free",
            "joined":      u.created_at.strftime("%Y-%m-%d") if u.created_at else "",
            "job_count":   job_stats.get(u.id, 0),
            "last_scan":   ls.strftime("%Y-%m-%d %H:%M") if ls else "Never",
            "google_auth": bool(u.google_id),
        })
    return jsonify(result)


@app.route("/api/admin/errors")
@login_required
def admin_errors():
    if not _is_admin(current_user):
        return jsonify({"error": "Forbidden"}), 403

    from sqlalchemy import func

    show = request.args.get("filter", "open")  # open | resolved | all
    q = ErrorLog.query
    if show == "open":
        q = q.filter_by(resolved=False)
    elif show == "resolved":
        q = q.filter_by(resolved=True)
    errors = q.order_by(ErrorLog.last_seen.desc()).limit(200).all()

    day_ago = datetime.now(timezone.utc) - timedelta(hours=24)
    open_count   = ErrorLog.query.filter_by(resolved=False).count()
    occ_24h      = (db.session.query(func.coalesce(func.sum(ErrorLog.occurrences), 0))
                    .filter(ErrorLog.last_seen >= day_ago).scalar())
    by_source    = dict(db.session.query(ErrorLog.source, func.count(ErrorLog.id))
                        .filter_by(resolved=False).group_by(ErrorLog.source).all())

    return jsonify({
        "errors": [e.to_dict() for e in errors],
        "summary": {
            "open":      open_count,
            "last_24h":  int(occ_24h or 0),
            "by_source": by_source,
        },
    })


@app.route("/api/admin/errors/<error_id>/resolve", methods=["POST"])
@login_required
def admin_resolve_error(error_id):
    if not _is_admin(current_user):
        return jsonify({"error": "Forbidden"}), 403
    row = db.session.get(ErrorLog, error_id)
    if not row:
        return jsonify({"error": "Not found"}), 404
    data = request.get_json(silent=True) or {}
    row.resolved = bool(data.get("resolved", True))
    db.session.commit()
    return jsonify({"ok": True, "resolved": row.resolved})


@app.route("/api/admin/errors/clear-resolved", methods=["POST"])
@login_required
def admin_clear_resolved_errors():
    if not _is_admin(current_user):
        return jsonify({"error": "Forbidden"}), 403
    deleted = ErrorLog.query.filter_by(resolved=True).delete()
    db.session.commit()
    return jsonify({"ok": True, "deleted": deleted})


# Entry point is run.py — do not run app.py directly.
