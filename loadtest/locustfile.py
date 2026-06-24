"""Locust load test for JobScanner — realistic authenticated user sessions.

Each virtual user:
  * logs in as a distinct seeded user (loadtest+<n>@example.com)
  * sends a UNIQUE X-Forwarded-For per user so Flask-Limiter (which honors
    X-Forwarded-For via ProxyFix) treats every virtual user as a distinct
    client IP — i.e. 1000 genuinely-distinct users, not one IP flooding.
  * exercises only SAFE endpoints (no Gemini / Adzuna / Stripe / Resend / scan
    threads), so we measure the web + DB tier (the real crash risk) in isolation.

Env:
  LT_SAME_IP=1   -> all users share one client IP (tests single-IP DoS / limiter)
"""
import json
import os
import random

from locust import HttpUser, task, between, events

_HERE = os.path.dirname(__file__)
with open(os.path.join(_HERE, "users.json")) as f:
    _DATA = json.load(f)
EMAILS = _DATA["emails"]
PASSWORD = _DATA["password"]
SAME_IP = os.getenv("LT_SAME_IP") == "1"

_counter = {"i": 0}


class JobScannerUser(HttpUser):
    # Think time between actions — real users don't fire back-to-back.
    wait_time = between(1, 4)

    def on_start(self):
        # Assign each virtual user a stable identity + fake client IP.
        i = _counter["i"] % len(EMAILS)
        _counter["i"] += 1
        self.email = EMAILS[i]
        if SAME_IP:
            self.fake_ip = "10.0.0.1"
        else:
            # Unique-ish IP per user across a /8 so the limiter keys them apart.
            self.fake_ip = f"172.{(i >> 16) & 255}.{(i >> 8) & 255}.{i & 255}"
        self.client.headers["X-Forwarded-For"] = self.fake_ip

        r = self.client.post(
            "/api/auth/login",
            json={"email": self.email, "password": PASSWORD},
            name="POST /api/auth/login",
        )
        self.logged_in = r.status_code == 200
        if not self.logged_in:
            # Surface auth failures explicitly instead of silently 401-ing every task.
            events.request.fire(
                request_type="AUTH", name="login_failed", response_time=0,
                response_length=0, exception=Exception(f"login {r.status_code}"),
                context={},
            )

    # ---- Reads (the bulk of real traffic) ----
    @task(10)
    def dashboard_stats(self):
        self.client.get("/api/stats", name="GET /api/stats")

    @task(8)
    def list_jobs(self):
        self.client.get("/api/jobs", name="GET /api/jobs")

    @task(5)
    def applications(self):
        self.client.get("/api/applications", name="GET /api/applications")

    @task(4)
    def analytics(self):
        self.client.get("/api/analytics", name="GET /api/analytics")

    @task(3)
    def profile(self):
        self.client.get("/api/profile", name="GET /api/profile")

    @task(2)
    def config(self):
        self.client.get("/api/config", name="GET /api/config")

    @task(2)
    def scan_history(self):
        self.client.get("/api/scan/history", name="GET /api/scan/history")

    @task(2)
    def me(self):
        self.client.get("/api/auth/me", name="GET /api/auth/me")

    @task(3)
    def spa_page(self):
        self.client.get("/app", name="GET /app (SPA)")

    # ---- Writes (status updates) ----
    @task(3)
    def update_application(self):
        job_id = f"lt-{random.randint(0, 9999)}"
        self.client.post(
            f"/api/applications/{job_id}",
            json={"status": random.choice(["applied", "interview", "skip"])},
            name="POST /api/applications/[id]",
        )
