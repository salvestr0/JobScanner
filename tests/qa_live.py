"""Ad-hoc live QA harness — exercises every endpoint end-to-end with externals mocked.
Run: python -m pytest tests/qa_live.py -v
Not part of the normal suite (filename doesn't match test_*)."""
import json
import os
from unittest.mock import patch, MagicMock

os.environ.setdefault("SECRET_KEY", "a" * 32)
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ["CRON_SECRET"] = "test-cron-secret-xyz"
os.environ["ADMIN_EMAIL"] = "admin@test.com"

import pytest
import app as flask_app
from models import db, User, Job, ScanHistory


def _post(client, path, data=None, **kw):
    return client.post(path, data=json.dumps(data or {}),
                       headers={"Content-Type": "application/json"}, **kw)


@pytest.fixture
def client():
    flask_app.app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI="sqlite:///:memory:")
    flask_app.limiter.enabled = False
    with flask_app.app.test_client() as c:
        with flask_app.app.app_context():
            db.create_all()
            yield c
            db.session.remove()
            db.drop_all()


def _register(client, email="qa@test.com", pw="StrongPass1!"):
    return _post(client, "/api/auth/register", {"email": email, "password": pw})


# ── Page templates render (no Jinja/500) ─────────────────────────────────────────
def test_public_pages_render(client):
    for path in ("/login", "/register", "/forgot-password", "/reset-password",
                 "/privacy", "/terms", "/"):
        r = client.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code}"


def test_authed_pages_render(client):
    _register(client)
    for path in ("/app", "/onboarding"):
        r = client.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code}"
    # admin page redirects non-admin to /app (302), renders for admin
    r = client.get("/admin")
    assert r.status_code in (302, 200)


# ── The suspected billing-health crash ──────────────────────────────────────────
def test_billing_health_with_pastdue_user(client):
    with flask_app.app.app_context():
        u = User(email="late@test.com", subscription_status="past_due",
                 stripe_customer_id="cus_123")
        db.session.add(u)
        db.session.commit()
    with patch.object(flask_app, "_send_email", return_value=True):
        resp = client.post("/api/cron/billing-health",
                           headers={"X-Cron-Secret": "test-cron-secret-xyz"})
    assert resp.status_code == 200, f"billing-health crashed: {resp.status_code} {resp.data[:300]}"


# ── Core authed flows ───────────────────────────────────────────────────────────
def test_full_user_journey(client):
    assert _register(client).status_code == 200
    # me
    assert client.get("/api/auth/me").status_code == 200
    # stats / jobs / analytics / config / profile / schedule / modes / history / billing
    for path in ("/api/stats", "/api/jobs", "/api/analytics", "/api/config",
                 "/api/profile", "/api/schedule", "/api/modes", "/api/scan/history",
                 "/api/billing/status", "/api/applications", "/api/cover-notes"):
        r = client.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code} {r.data[:200]}"

    # save config
    r = _post(client, "/api/config", {"min_salary": 3000, "max_salary": 6000,
                                      "target_titles": ["Data Analyst"], "email_to": "x@y.com"})
    assert r.status_code == 200, r.data
    # save profile
    r = _post(client, "/api/profile", {"name": "QA", "technical_skills": ["python"]})
    assert r.status_code == 200
    # schedule set
    r = _post(client, "/api/schedule", {"enabled": True, "time": "09:30"})
    assert r.status_code == 200, r.data
    r = _post(client, "/api/schedule", {"enabled": True, "time": "99:99"})
    assert r.status_code == 400
    # export csv
    r = client.get("/api/jobs/export")
    assert r.status_code == 200 and r.mimetype == "text/csv"
    # reset seen
    assert _post(client, "/api/reset", {}).status_code == 200


def test_application_status_flow(client):
    _register(client)
    with flask_app.app.app_context():
        u = User.query.filter_by(email="qa@test.com").first()
        db.session.add(Job(user_id=u.id, source_job_id="J1", title="DA", company="DBS",
                           url="http://x", score=80))
        db.session.commit()
    r = _post(client, "/api/applications/J1", {"status": "applied"})
    assert r.status_code == 200
    r = _post(client, "/api/applications/J1", {"status": "bogus"})
    assert r.status_code == 400
    r = _post(client, "/api/applications/J1", {"status": "interview",
              "interview_date": "2026-07-01", "interview_time": "14:00"})
    assert r.status_code == 200
    r = _post(client, "/api/applications/J1", {"status": "clear"})
    assert r.status_code == 200
    # hide
    r = _post(client, "/api/jobs/J1/hide", {"hidden": True})
    assert r.status_code == 200 and r.get_json()["hidden"] is True
    r = _post(client, "/api/jobs/NOPE/hide", {"hidden": True})
    assert r.status_code == 404


def test_free_tier_scan_gating(client):
    _register(client)
    with patch.object(flask_app, "_run_scan_inprocess"):
        # free user: 3 scans allowed then 429
        codes = []
        for _ in range(4):
            r = _post(client, "/api/scan/start", {"mode": "analyst"})
            codes.append(r.status_code)
            # force scan flag back to not-running so the next call isn't 409
            with flask_app.app.app_context():
                u = User.query.filter_by(email="qa@test.com").first()
            flask_app._scans.get(u.id, {})["running"] = False
        assert codes[:3] == [200, 200, 200], codes
        assert codes[3] == 429, codes
    # invalid mode
    r = _post(client, "/api/scan/start", {"mode": "BAD MODE!!"})
    assert r.status_code in (400, 429)


def test_admin_endpoints(client):
    _register(client, email="admin@test.com")
    for path in ("/api/admin/stats", "/api/admin/users"):
        r = client.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code}"
    # non-admin blocked
    _post(client, "/api/auth/logout", {})
    _register(client, email="notadmin@test.com")
    assert client.get("/api/admin/stats").status_code == 403


def test_delete_account_password(client):
    _register(client, email="del@test.com")
    r = _post(client, "/api/auth/delete-account", {"password": "wrong"})
    assert r.status_code == 403
    r = _post(client, "/api/auth/delete-account", {"password": "StrongPass1!"})
    assert r.status_code == 200


def test_cron_cleanup_and_digest(client):
    r = client.post("/api/cron/cleanup", headers={"X-Cron-Secret": "test-cron-secret-xyz"})
    assert r.status_code == 200
    r = client.post("/api/cron/weekly-digest", headers={"X-Cron-Secret": "test-cron-secret-xyz"})
    assert r.status_code == 200


def test_interview_prep_requires_job(client):
    _register(client)
    # no gemini key configured -> job missing returns 404 first
    r = _post(client, "/api/interview-prep", {"job_id": "NOPE"})
    assert r.status_code in (400, 404)


# ── PDF / resume builder ─────────────────────────────────────────────────────────
def test_resume_download_pdf(client):
    _register(client)
    profile = {
        "name": "Jane Tan", "email": "jane@x.com", "phone": "9123",
        "experience_summary": "Analyst with 3 yrs.",
        "technical_skills": ["SQL", "Python"], "soft_skills": ["teamwork"],
        "work_history": [{"title": "DA", "company": "DBS", "period": "2022-24",
                          "summary": "• did things\n• more things"}],
        "education": "Dip in IT", "certifications": ["AWS CCP"],
        "projects": [{"name": "Bot", "description": "a bot"}],
    }
    r = _post(client, "/api/resume/download", profile)
    assert r.status_code == 200, r.data[:300]
    assert r.mimetype == "application/pdf"
    assert r.data[:4] == b"%PDF"


def test_resume_download_with_string_project(client):
    """Projects/certs as plain strings shouldn't 500 the PDF builder."""
    _register(client)
    profile = {"name": "X", "projects": ["just a string project"],
               "work_history": ["a string job"]}
    r = _post(client, "/api/resume/download", profile)
    assert r.status_code == 200, r.data[:200]
    assert r.data[:4] == b"%PDF"


# ── AI endpoints with mocked Gemini ──────────────────────────────────────────────
def _fake_gemini(text):
    m = MagicMock()
    m.raise_for_status = lambda: None
    m.json = lambda: {"candidates": [{"content": {"parts": [{"text": text}]}}],
                      "usageMetadata": {}}
    return m


def test_resume_polish_mocked(client):
    _register(client)
    with flask_app.app.app_context():
        u = User.query.filter_by(email="qa@test.com").first()
        u.gemini_api_key = flask_app._encrypt_api_key("fake-key")
        db.session.commit()
    payload = '{"experience_summary":"polished","work_history":[],"technical_skills":["x"],"soft_skills":["y"]}'
    with patch("requests.post", return_value=_fake_gemini(payload)):
        r = _post(client, "/api/resume/polish", {"profile": {"name": "Q"}})
    assert r.status_code == 200, r.data[:300]
    assert r.get_json()["profile"]["experience_summary"] == "polished"


def test_cover_note_generate_mocked(client):
    # Create the paying user up-front (avoids a :memory: cross-connection quirk
    # when mutating a row created via a prior client request).
    with flask_app.app.app_context():
        u = User(email="pro@test.com", subscription_status="active",
                 gemini_api_key=flask_app._encrypt_api_key("fake-key"))
        u.set_password("StrongPass1!")
        db.session.add(u)
        db.session.commit()
        db.session.add(Job(user_id=u.id, source_job_id="JC", title="DA", company="DBS",
                           url="http://x", score=80))
        db.session.commit()
    _post(client, "/api/auth/login", {"email": "pro@test.com", "password": "StrongPass1!"})
    with patch("cover_notes.generate_cover_note", return_value="Dear hiring manager..."):
        r = _post(client, "/api/cover-notes/generate", {"job_id": "JC"})
    assert r.status_code == 200, r.data[:300]
    assert "content" in r.get_json()


def test_cover_note_requires_pro(client):
    _register(client)  # free user
    with flask_app.app.app_context():
        u = User.query.filter_by(email="qa@test.com").first()
        db.session.add(Job(user_id=u.id, source_job_id="JC", title="DA", company="DBS"))
        db.session.commit()
    r = _post(client, "/api/cover-notes/generate", {"job_id": "JC"})
    assert r.status_code == 403 and r.get_json()["error"] == "pro_required"


# ── Stripe webhook ───────────────────────────────────────────────────────────────
def test_stripe_webhook_activates_user(client):
    os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_test"
    with flask_app.app.app_context():
        u = User(email="payer@test.com", subscription_status="free",
                 stripe_customer_id="cus_pay")
        db.session.add(u)
        db.session.commit()
        uid = u.id
    event = {"type": "customer.subscription.updated",
             "data": {"object": {"customer": "cus_pay", "status": "active"}}}
    payload = json.dumps(event).encode()
    with patch("stripe.Webhook.construct_event", return_value=None):
        r = client.post("/api/stripe/webhook", data=payload,
                        headers={"Stripe-Signature": "t=1,v1=x",
                                 "Content-Type": "application/json"})
    assert r.status_code == 200
    with flask_app.app.app_context():
        assert db.session.get(User, uid).subscription_status == "active"


# ── Email test endpoint ──────────────────────────────────────────────────────────
def test_email_test_endpoint(client):
    _register(client)
    _post(client, "/api/config", {"email_to": "me@x.com"})
    with patch("notifier.send_email_digest", return_value=True):
        r = _post(client, "/api/email/test", {})
    assert r.status_code == 200 and r.get_json()["ok"] is True


# ── Full scan flow (scrapers mocked) ─────────────────────────────────────────────
def test_scan_inprocess_persists_jobs(client):
    import queue as _q
    with flask_app.app.app_context():
        u = User(email="scan@test.com", subscription_status="active")
        u.set_password("StrongPass1!")
        db.session.add(u)
        db.session.commit()
        uid = u.id
        hist = ScanHistory(user_id=uid, mode="analyst", status="running")
        db.session.add(hist)
        db.session.commit()
        hid = hist.id

    fake_jobs = [
        {"id": "S1", "title": "Data Analyst", "company": "DBS", "location": "SG",
         "source": "JSearch", "url": "http://x/1", "score": 0,
         "salary_min": 4000, "salary_max": 6000, "match_reasons": []},
        {"id": "S2", "title": "Data Engineer", "company": "OCBC", "location": "SG",
         "source": "JSearch", "url": "http://x/2", "score": 0,
         "salary_min": 5000, "salary_max": 7000, "match_reasons": []},
    ]
    q = _q.Queue()
    flask_app._scans[uid] = {"running": True, "q": q, "history_id": hid}
    cfg = {"target_titles": ["Data Analyst"], "job_region": "my", "min_score_threshold": 0,
           "max_jobs_per_notification": 20}
    with patch("scrapers.scrape_all_sources", return_value=fake_jobs), \
         patch("scorer.rank_jobs", side_effect=lambda jobs, cfg=None: [dict(j, score=75) for j in jobs]), \
         patch("scorer.filter_jobs", side_effect=lambda jobs, th: jobs):
        flask_app._run_scan_inprocess(uid, "analyst", False, q,
                                      {"JOBSCANNER_USER_CONFIG": json.dumps({"search_config": cfg}),
                                       "JOBSCANNER_MAX_JOBS": "0"})
    with flask_app.app.app_context():
        jobs = Job.query.filter_by(user_id=uid).all()
        assert len(jobs) == 2, [j.title for j in jobs]
        assert {j.region for j in jobs} == {"my"}
        assert db.session.get(ScanHistory, hid).status == "done"
        # all scraped marked seen
        from models import SeenJob
        assert SeenJob.query.filter_by(user_id=uid).count() == 2


# ── Modes generate (prebuilt, no Gemini) ─────────────────────────────────────────
def test_generate_prebuilt_mode(client):
    _register(client)
    r = _post(client, "/api/modes/generate", {"mode": "fnb"})
    # prebuilt modes should resolve without Gemini
    assert r.status_code == 200, r.data[:300]
    body = r.get_json()
    assert body.get("ok") and isinstance(body.get("titles"), list)
