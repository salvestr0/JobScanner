# CLAUDE.md — Job Scanner SaaS

Singapore job-matching SaaS. Multi-user, hosted, legally defensible (no scraping).

---

## Stack

| Layer | Tech |
|-------|------|
| Backend | Flask + SQLAlchemy + Flask-Migrate + Flask-Login + Flask-Limiter |
| Frontend | Alpine.js + Tailwind CSS SPA (`templates/index.html`) |
| Database | SQLite (local dev) / Supabase PostgreSQL (prod) |
| Hosting | Render (web service + cron job) |
| Email | Resend (`notifier.py`) |
| Billing | Stripe (subscriptions, webhooks) |
| AI | Gemini 2.5 Flash (scoring, cover notes, resume polish, interview prep) |
| Auth | Email/password + Google OAuth (Authlib) |

---

## Key Files

| File | Purpose |
|------|---------|
| `app.py` | All Flask routes and API endpoints |
| `models.py` | User, UserProfile, UserSettings, Job, ApplicationStatus, SeenJob, SearchMode |
| `templates/index.html` | Main SPA (Alpine.js) — all pages except login/register/onboarding |
| `templates/onboarding.html` | 2-step onboarding wizard (job prefs → Gemini key) |
| `config.py` | Loads env vars, SEARCH_CONFIG defaults, Gemini mode generation |
| `main.py` | Standalone CLI scan runner (manual/Task Scheduler use) — **not** called by `app.py`; the hosted app scans in-process |
| `scrapers.py` | Official APIs only: MyCareersFuture, Adzuna, RemoteOK (no HTML scraping) |
| `scorer.py` | Job scoring engine (0–100), produces `match_reasons` and `score_breakdown` |
| `cover_notes.py` | Gemini cover note generation |
| `notifier.py` | Resend email digest |
| `resume_parser.py` | PDF/DOCX → Gemini → structured profile |
| `migrations/` | Flask-Migrate / Alembic — head: `d2c8e4a7f19b` |

---

## Data Flow

1. User triggers scan via UI (`/api/scan/start`) or cron (`/api/cron/scan`)
2. `app.py` runs `_run_scan_inprocess()` in a background thread (per-user config built by `_build_user_env`)
3. It calls `scrapers.py` → `scorer.py`, then optionally `notifier.py` for the email digest
4. Matched jobs + seen IDs are written **directly** to the `jobs` / `seen_jobs` DB tables
5. Progress is streamed to the UI via `/api/scan/stream` (SSE); `ScanHistory` records the run

---

## Deployment

- **Live URL** — `https://careerscan.online` (custom domain, canonical; `www` 301s to apex). Render subdomain: `https://careerscan.onrender.com`. The old `jobscanner-m7pb.onrender.com` service is suspended and must not be referenced.
- **Render web service** — `gunicorn app:app`, auto-deploys from `master`. Lives on a Render account migrated 2026-06-19; env vars, Stripe webhook, Google OAuth redirect URI, and cron jobs are all configured for `careerscan.online`.
- **CI smoke test** — `.github/workflows/smoke-test.yml` polls the GitHub repo variable `APP_URL` (currently `https://careerscan.onrender.com`) post-deploy.
- **Render cron jobs** — all protected by `X-Cron-Secret` header matching `CRON_SECRET`:
  - `/api/cron/scan` — every 15 min, triggers scheduled user scans
  - `/api/cron/weekly-digest` — Mondays 08:00 SGT, sends email digests
  - `/api/cron/cleanup` — daily, deletes jobs >60 days old (keeps tracked), scan_history >90 days
  - `/api/cron/stripe-sync` — daily, syncs Stripe subscription status to fix missed webhooks
- **Supabase** — Session Pooler connection (IPv4 compatible), port 5432
- **Stripe webhook** — endpoint: `/api/stripe/webhook` (not `/api/billing/webhook`)
- **Migrations** — run locally against Supabase before deploying schema changes:
  ```
  python -m flask db upgrade
  ```

---

## Environment Variables

| Key | Purpose |
|-----|---------|
| `SECRET_KEY` | Flask session secret |
| `DATABASE_URL` | Supabase Session Pooler URI |
| `CRON_SECRET` | Protects all `/api/cron/*` endpoints |
| `STRIPE_SECRET_KEY` | Stripe API key |
| `STRIPE_PRICE_ID` | Stripe price ID for $8/mo plan |
| `STRIPE_WEBHOOK_SECRET` | Stripe webhook signing secret |
| `RESEND_API_KEY` | Resend API key |
| `RESEND_FROM` | Sender address (must be verified domain) |
| `GEMINI_API_KEY` | Fallback Gemini key (users can set their own) |
| `ENCRYPTION_KEY` | Fernet key for encrypting user Gemini keys in DB |
| `GOOGLE_CLIENT_ID` | Google OAuth client ID |
| `GOOGLE_CLIENT_SECRET` | Google OAuth client secret |
| `SENTRY_DSN` | Sentry DSN for error monitoring (optional) |
| `REDIS_URL` | Redis URL for persistent rate limiting (optional, falls back to memory) |

Generate secrets locally:
```bash
python -c "import secrets; print(secrets.token_hex(32))"          # SECRET_KEY
python -c "import secrets; print(secrets.token_hex(24))"          # CRON_SECRET
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # ENCRYPTION_KEY
```

---

## Database Migrations

```bash
# Apply migrations to Supabase (run before deploy after schema changes)
python -m flask db upgrade

# Create new migration after changing models.py
python -m flask db migrate -m "description"
python -m flask db upgrade
```

Migration chain: starts at `f7ef5236638b`, branch reconciled by merge `a8e2d5f31b7c`, current head: `d2c8e4a7f19b`

---

## Local Dev

```bash
python -m pip install -r requirements.txt
# Set DATABASE_URL=sqlite:///jobscanner_dev.db in .env (default)
python -m flask db upgrade   # creates local SQLite DB
python run.py                # starts Flask on http://localhost:5000
```

---

## Key Constraints

- **Official APIs only** — three sources: MyCareersFuture, Adzuna (needs `ADZUNA_APP_ID`/`ADZUNA_APP_KEY`), and RemoteOK. No HTML scraping. LinkedIn/JobStreet/Glints/Indeed scraping violates ToS — do not add them back.
- **No Telegram** — fully removed. Email only via Resend.
- **Gemini API keys are encrypted** — stored with Fernet encryption using `ENCRYPTION_KEY`. If `ENCRYPTION_KEY` is not set, keys are stored plaintext (warn the user).
- **Stripe webhook route** — `/api/stripe/webhook` (the function is named `billing_webhook` internally but the route must stay `/api/stripe/webhook`).
- **Subscription model** — `free` gets 10 job results per scan. `active` gets unlimited.
- **Rate limiting** — in-memory (resets on restart). Login: 10/min. Register: 5/min. For production scale, switch to Redis.

---

## Feature Status (as of 2026-06-08)

- [x] MCF API job fetching
- [x] Gemini job scoring + match reasons
- [x] Cover note generation
- [x] Multi-user auth (email + Google OAuth)
- [x] Onboarding wizard
- [x] Stripe billing (free tier + $8/mo subscription)
- [x] Resend email digest
- [x] Resume parser (PDF/DOCX → Gemini → profile)
- [x] Resume builder (AI polish + PDF download)
- [x] Score breakdown popover on job cards
- [x] Interview Prep tab (Gemini-generated questions)
- [x] Render + Supabase deployment
- [x] Sentry error monitoring + Redis rate limiting
- [x] Job hiding/dismissal
- [x] Resume tailoring per job
- [x] Admin dashboard (/admin)
- [x] Password reset flow
- [x] Account deletion (PDPA compliant)
- [x] Export jobs as CSV
- [x] Scan history tracking
- [x] Cron: cleanup, stripe-sync

---

## User

Jayden (salvestr0) — diploma student in AI/Infocomm, building this as a personal + commercial SaaS for the Singapore job market. Entry-level, prefers direct guidance over theory.
