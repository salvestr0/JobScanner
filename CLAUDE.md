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
| `templates/onboarding.html` | 2-step onboarding wizard (job prefs → done) |
| `config.py` | Loads env vars, SEARCH_CONFIG defaults, Gemini mode generation |
| `main.py` | Scan subprocess runner — called by `app.py` via `subprocess.Popen` |
| `scrapers.py` | MCF API only (no scrapers — all previous ones removed) |
| `scorer.py` | Job scoring engine (0–100), produces `match_reasons` and `score_breakdown` |
| `cover_notes.py` | Gemini cover note generation |
| `notifier.py` | Resend email digest |
| `resume_parser.py` | PDF/DOCX → Gemini → structured profile |
| `migrations/` | Flask-Migrate / Alembic — 3 migrations, head: `c4a2d8f91b3e` |

---

## Data Flow

1. User triggers scan via UI (`/api/scan/start`) or cron (`/api/cron/scan`)
2. `app.py` spawns `main.py` as a subprocess with user config in env vars
3. `main.py` calls `scrapers.py` → `scorer.py` → `cover_notes.py` → `notifier.py`
4. Results written to `data/users/<user_id>/matched_jobs.csv` + `seen_jobs.json`
5. `_sync_scan_results()` in `app.py` imports CSV into the `jobs` DB table

---

## Deployment

- **Render web service** — `gunicorn app:app`, auto-deploys from `master`
- **Render cron job** — calls `/api/cron/scan` with `X-Cron-Secret` header daily
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
| `CRON_SECRET` | Protects `/api/cron/scan` |
| `STRIPE_SECRET_KEY` | Stripe API key |
| `STRIPE_PRICE_ID` | Stripe price ID for $8/mo plan |
| `STRIPE_WEBHOOK_SECRET` | Stripe webhook signing secret |
| `RESEND_API_KEY` | Resend API key |
| `RESEND_FROM` | Sender address (must be verified domain) |
| `GEMINI_API_KEY` | Server-side Gemini key (used for all AI features) |
| `GOOGLE_CLIENT_ID` | Google OAuth client ID |
| `GOOGLE_CLIENT_SECRET` | Google OAuth client secret |
| `SENTRY_DSN` | Sentry DSN for error monitoring (optional) |
| `REDIS_URL` | Redis URL for persistent rate limiting (optional, falls back to memory) |

Generate secrets locally:
```bash
python -c "import secrets; print(secrets.token_hex(32))"  # SECRET_KEY
python -c "import secrets; print(secrets.token_hex(24))"  # CRON_SECRET
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

Migration chain: `f7ef5236638b` → ... → `f76103287e11` → `74a286e65b49` (current head)

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

- **MCF API only** — no scrapers. LinkedIn/JobStreet/Indeed scraping violates ToS. Do not add them back.
- **No Telegram** — fully removed. Email only via Resend.
- **Gemini API key is server-side only** — `GEMINI_API_KEY` env var, no per-user keys. `ENCRYPTION_KEY` is no longer needed.
- **Stripe webhook route** — `/api/stripe/webhook` (the function is named `billing_webhook` internally but the route must stay `/api/stripe/webhook`).
- **Subscription model** — `free` gets 10 job results per scan. `active` gets unlimited.
- **Rate limiting** — in-memory (resets on restart). Login: 10/min. Register: 5/min. For production scale, switch to Redis.

---

## Feature Status (as of 2026-06-07)

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

---

## User

Jayden (salvestr0) — diploma student in AI/Infocomm, building this as a personal + commercial SaaS for the Singapore job market. Entry-level, prefers direct guidance over theory.
