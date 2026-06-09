"""
CareerJobScan Web UI — Flask backend (multi-user)
Run: python run.py
Open: http://localhost:5000
"""
import hmac
import json
import os
import queue
import re
import threading
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
from flask import Flask, Response, jsonify, redirect, render_template, request, session, stream_with_context, url_for
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import LoginManager, current_user, login_required, login_user, logout_user
from flask_migrate import Migrate
from werkzeug.middleware.proxy_fix import ProxyFix

from models import (
    ApplicationStatus, Job, ScanHistory, SearchMode, SeenJob, User,
    UserProfile, UserSettings, db,
)

load_dotenv()

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
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv(
    "DATABASE_URL",
    "sqlite:///jobscanner_dev.db",
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_pre_ping": True,
    "pool_recycle":  300,
}
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


def _send_email(to: str, subject: str, html: str) -> bool:
    api_key   = os.getenv("RESEND_API_KEY", "").strip()
    from_addr = os.getenv("RESEND_FROM", "CareerJobScan <noreply@jobscanner.app>").strip()
    if not api_key:
        return False
    try:
        import requests as _req
        _req.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"from": from_addr, "to": [to], "subject": subject, "html": html},
            timeout=10,
        )
        return True
    except Exception:
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
      Thanks for signing up! Click the button below to verify your email address and get the most out of CareerJobScan.
    </p>
    <a href="{verify_url}" style="display:inline-block;background:#4F46E5;color:white;font-size:14px;font-weight:600;padding:12px 24px;border-radius:10px;text-decoration:none">
      Verify email address
    </a>
    <p style="margin:24px 0 0;color:#94a3b8;font-size:12px">
      If you didn't create a CareerJobScan account, you can safely ignore this email.
    </p>
  </div>
</body></html>"""


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


def _get_scan(user_id: str) -> dict:
    if user_id not in _scans:
        _scans[user_id] = {"running": False, "q": queue.Queue(), "proc": None}
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

        log("\nScanning MyCareersFuture...")
        all_jobs = scrape_all_sources(max_total=max_fetch)

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

                # ── 8. Email digest (Pro plan only) ──────────────────────────────
                if notify and not max_jobs:
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
    finally:
        with app.app_context():
            if history_id:
                row = db.session.get(ScanHistory, history_id)
                if row:
                    row.finished_at = datetime.now(timezone.utc)
                    row.job_count   = job_count
                    row.status      = "failed" if failed else "done"
                    db.session.commit()
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
    if user.subscription_status not in ("active",) and not user.is_admin:
        env["JOBSCANNER_MAX_JOBS"] = "10"
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
        s = UserSettings(
            user_id=user_id,
            min_salary=cfg.SEARCH_CONFIG.get("min_salary", 2200),
            max_salary=cfg.SEARCH_CONFIG.get("max_salary", 4000),
            min_score_threshold=cfg.SEARCH_CONFIG.get("min_score_threshold", 40),
            max_jobs_per_notification=cfg.SEARCH_CONFIG.get("max_jobs_per_notification", 20),
            target_titles=cfg.SEARCH_CONFIG.get("target_titles", []),
            preferred_keywords=cfg.SEARCH_CONFIG.get("preferred_keywords", []),
            negative_keywords=cfg.SEARCH_CONFIG.get("negative_keywords", []),
            location_keywords=cfg.SEARCH_CONFIG.get("location_keywords", []),
            preferred_location=cfg.SEARCH_CONFIG.get("preferred_location", "Sengkang"),
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
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters"}), 400
    if User.query.filter_by(email=email).first():
        return jsonify({"error": "Registration failed — try signing in instead"}), 409

    import hashlib, secrets
    verify_token      = secrets.token_urlsafe(32)
    verify_token_hash = hashlib.sha256(verify_token.encode()).hexdigest()

    user = User(
        email=email,
        subscription_status="free",
        email_verified=False,
        email_verify_token=verify_token_hash,
    )
    user.set_password(password)
    db.session.add(user)
    db.session.commit()

    verify_url = request.host_url.rstrip("/") + f"/verify-email?token={verify_token}"
    _send_email(email, "Verify your CareerJobScan email", _verification_email_html(verify_url))

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
    import hashlib, secrets, requests as _req

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
      We received a request to reset the password for your CareerJobScan account.<br>
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
        _send_email(email, "Reset your CareerJobScan password", reset_html)

    return jsonify({"ok": True})


@app.route("/api/auth/reset-password", methods=["POST"])
@limiter.limit("5/minute;20/hour")
def auth_reset_password():
    import hashlib

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
    import hashlib
    token      = (request.args.get("token") or "").strip()
    token_hash = hashlib.sha256(token.encode()).hexdigest() if token else ""
    user = User.query.filter_by(email_verify_token=token_hash).first() if token_hash else None

    if user and not user.email_verified:
        user.email_verified     = True
        user.email_verify_token = None
        db.session.commit()
        if current_user.is_authenticated:
            return redirect("/app?verified=1")
        return redirect("/login?verified=1")

    if current_user.is_authenticated:
        return redirect("/app")
    return redirect("/login")


@app.route("/api/auth/resend-verification", methods=["POST"])
@login_required
@limiter.limit("3/hour")
def resend_verification():
    import hashlib, secrets
    if current_user.email_verified:
        return jsonify({"ok": True})

    verify_token      = secrets.token_urlsafe(32)
    verify_token_hash = hashlib.sha256(verify_token.encode()).hexdigest()
    current_user.email_verify_token = verify_token_hash
    db.session.commit()

    verify_url = request.host_url.rstrip("/") + f"/verify-email?token={verify_token}"
    _send_email(current_user.email, "Verify your CareerJobScan email", _verification_email_html(verify_url))
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

    from datetime import date as _date
    FREE_DAILY_LIMIT = 3
    scans_today = 0
    scans_remaining = None
    if not _is_active(current_user):
        s = current_user.settings
        today = _date.today().isoformat()
        if s and s.last_scan_date == today:
            scans_today = s.daily_scan_count or 0
        scans_remaining = max(0, FREE_DAILY_LIMIT - scans_today)

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
            score_line = next((l for l in lines if "Match Score:" in l), "")
            score      = score_line.replace("Match Score:", "").replace("/100", "").strip() if score_line else ""
            url_line   = next((l for l in lines if "Job URL:" in l), "")
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
    if not _is_active(current_user):
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

    for key in ("min_salary", "max_salary", "min_score_threshold", "max_jobs_per_notification"):
        if key in data:
            setattr(s, key, data[key])
    if "email_to" in data:
        email_to = (data["email_to"] or "").strip()
        if email_to and not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email_to):
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
        subject_override="CareerJobScan — connection test",
    )
    return jsonify({"ok": ok})


# ── Profile ────────────────────────────────────────────────────────────────────

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

    from resume_parser import extract_text, parse_with_gemini
    try:
        text = extract_text(file.read(), file.filename)
    except (ImportError, ValueError) as e:
        return jsonify({"error": str(e)}), 400

    if not text.strip():
        return jsonify({"error": "Could not extract text from the file"}), 400

    try:
        profile = parse_with_gemini(text, api_key)
    except Exception:
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
        raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        return jsonify({"error": "AI request failed. Please try again later."}), 502

    import re as _re, json as _json
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
        raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        return jsonify({"error": "AI request failed. Please try again later."}), 502

    import re as _re, json as _json
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
    raw_time = data.get("time", "09:00")
    if not re.match(r'^\d{2}:\d{2}$', raw_time):
        return jsonify({"error": "Invalid time format — use HH:MM"}), 400
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
        return jsonify({"error": "trial_expired"}), 403

    data   = request.json or {}
    mode   = data.get("mode", "analyst")
    notify = data.get("notify", True)

    if not re.match(r'^[a-z0-9_-]{1,32}$', mode):
        return jsonify({"error": "Invalid mode name"}), 400

    # Daily scan limit for free plan
    FREE_DAILY_LIMIT = 3
    if not _is_active(current_user):
        from datetime import date as _date
        s = _get_or_create_settings(current_user.id)
        today = _date.today().isoformat()
        if s.last_scan_date != today:
            s.daily_scan_count = 0
            s.last_scan_date   = today
        if s.daily_scan_count >= FREE_DAILY_LIMIT:
            return jsonify({"error": "daily_limit_reached", "remaining": 0}), 429
        s.daily_scan_count += 1
        db.session.commit()

    with _get_scan_lock(current_user.id):
        scan = _get_scan(current_user.id)
        if scan["running"]:
            return jsonify({"error": "Scan already running"}), 409
        scan["running"] = True
        scan["q"]       = queue.Queue()

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

    @stream_with_context
    def generate():
        q = _get_scan(user_id)["q"]
        while True:
            try:
                line = q.get(timeout=60)
                if line is None:
                    yield f"data: {json.dumps({'type': 'done'})}\n\n"
                    break
                yield f"data: {json.dumps({'type': 'log', 'text': line})}\n\n"
            except queue.Empty:
                yield f"data: {json.dumps({'type': 'ping'})}\n\n"

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
        writer.writerow([
            j.title, j.company, j.location, j.score,
            j.salary_min or "", j.salary_max or "",
            j.source, j.posted_date or "", j.closing_date or "",
            st, j.url or "",
        ])

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

def _is_active(user) -> bool:
    """Return True if user has access (free tier or active subscription)."""
    if user.is_admin:
        return True
    if user.subscription_status in ("active", "free"):
        return True
    if user.subscription_status in (None, "trialing"):
        ends = user.trial_ends_at
        if ends is None:
            return True  # legacy users without trial end — treat as free
        return datetime.now(timezone.utc) <= ends
    return False


_AI_DAILY_LIMITS = {
    "active":    30,
    "trialing":  10,
    "free":       5,
    "past_due":   5,
    "cancelled":  0,
    "expired":    0,
}


def _check_ai_quota(user) -> tuple[bool, str | None]:
    """Check and increment the user's daily AI call quota.

    Returns (allowed, error_message). Commits the counter increment on success.
    Admins are always allowed.
    """
    from datetime import date as _date
    if user.is_admin:
        return True, None

    today = _date.today()
    if user.ai_calls_reset_date != today:
        user.ai_calls_today = 0
        user.ai_calls_reset_date = today

    limit = _AI_DAILY_LIMITS.get(user.subscription_status, 5)
    if limit == 0:
        return False, "AI features are unavailable on your current plan."
    if (user.ai_calls_today or 0) >= limit:
        return False, f"Daily AI limit reached ({limit} calls/day). Resets at midnight SGT."

    user.ai_calls_today = (user.ai_calls_today or 0) + 1
    db.session.commit()
    return True, None


def _billing_status(user) -> dict:
    if user.is_admin:
        return {
            "status":         "active",
            "has_access":     True,
            "is_free":        False,
            "scan_limit":     None,
            "trial_days_left": None,
        }
    status  = user.subscription_status or "free"
    is_free = status not in ("active",)

    # Legacy trial handling — keep working for existing trialing users
    trial_days_left = None
    if status == "trialing" and user.trial_ends_at:
        delta = user.trial_ends_at - datetime.now(timezone.utc)
        trial_days_left = max(0, delta.days)
        if datetime.now(timezone.utc) > user.trial_ends_at:
            status = "expired"
            is_free = False

    return {
        "status":          status,
        "has_access":      _is_active(user),
        "is_free":         is_free and status not in ("expired", "cancelled", "past_due"),
        "scan_limit":      10 if (is_free and status not in ("expired", "cancelled", "past_due")) else None,
        "trial_ends_at":   user.trial_ends_at.isoformat() if user.trial_ends_at else None,
        "trial_days_left": trial_days_left,
        "email_verified":  bool(user.email_verified),
    }


@app.route("/api/billing/status")
@login_required
def billing_status():
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
        event = stripe.Webhook.construct_event(payload, sig, secret)
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
    if et in ("customer.subscription.created", "customer.subscription.updated"):
        stripe_status = obj.get("status", "")
        user.subscription_status = "active" if stripe_status == "active" else stripe_status
    elif et == "customer.subscription.deleted":
        user.subscription_status = "cancelled"
    elif et == "invoice.payment_failed":
        user.subscription_status = "past_due"

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
        raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
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

    now_hm  = datetime.now(timezone.utc).strftime("%H:%M")
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
                user_id=s.user_id, status="skipped"
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

    db.session.commit()
    return jsonify({"deleted_jobs": deleted_jobs, "deleted_scan_history": deleted_scans})


@app.route("/api/cron/expire-trials", methods=["POST"])
def cron_expire_trials():
    """
    Flip trialing users whose trial_ends_at is in the past to 'expired' in the DB.
    Without this, the admin dashboard shows incorrect subscription counts.
    Called daily by Render cron.
    """
    secret   = os.getenv("CRON_SECRET", "")
    incoming = request.headers.get("X-Cron-Secret", "")
    if not secret or not hmac.compare_digest(incoming, secret):
        return jsonify({"error": "Forbidden"}), 403

    now     = datetime.now(timezone.utc)
    expired = User.query.filter(
        User.subscription_status == "trialing",
        User.trial_ends_at != None,  # noqa: E711
        User.trial_ends_at < now,
    ).all()

    for user in expired:
        user.subscription_status = "expired"

    db.session.commit()
    return jsonify({"expired": len(expired)})


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

    STATUS_MAP = {
        "active":   "active",
        "trialing": "trialing",
        "past_due": "past_due",
        "canceled": "cancelled",
        "unpaid":   "past_due",
        "paused":   "past_due",
    }

    users = User.query.filter(
        User.stripe_customer_id != None,  # noqa: E711
        User.subscription_status.in_(["active", "past_due", "cancelled"]),
    ).all()

    updated = []
    for user in users:
        try:
            subs = _stripe.Subscription.list(
                customer=user.stripe_customer_id, limit=1, status="all"
            )
            if not subs.data:
                if user.subscription_status != "cancelled":
                    user.subscription_status = "cancelled"
                    updated.append(user.email)
                continue

            sub        = subs.data[0]
            new_status = STATUS_MAP.get(sub.status, user.subscription_status)
            if new_status != user.subscription_status:
                user.subscription_status = new_status
                updated.append(user.email)
        except Exception:
            continue

    db.session.commit()
    return jsonify({"synced": len(users), "updated": updated})


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


# Entry point is run.py — do not run app.py directly.
