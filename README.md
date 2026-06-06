# Job Scanner — Singapore Job Matching SaaS

AI-powered job scanner for Singapore. Pulls listings from multiple sources daily, scores them against your profile, and surfaces only the matches worth applying to.

**Live app:** [jobscanner-m7pb.onrender.com](https://jobscanner-m7pb.onrender.com)

---

## Features

| Feature | Free | Pro ($8/mo) |
|---------|------|-------------|
| Daily job scanning | ✅ (top 10 results) | ✅ Unlimited |
| Job matching & scoring | ✅ | ✅ |
| Application tracker | ✅ | ✅ |
| Score breakdown | ✅ | ✅ |
| Cover note generator | — | ✅ |
| Resume builder (AI polish + PDF) | — | ✅ |
| Interview prep questions | — | ✅ |
| Analytics dashboard | — | ✅ |
| Email digest | ✅ | ✅ |

---

## Job Sources

| Source | Type | Notes |
|--------|------|-------|
| **MyCareersFuture** | Singapore govt API | Official, most comprehensive |
| **Indeed** | Public RSS feed | Singapore listings, sorted by date |
| **Adzuna** | REST API | Requires free API key at [developer.adzuna.com](https://developer.adzuna.com) |
| **RemoteOK** | Public JSON API | Remote-friendly roles worldwide |

All sources use official APIs or public syndication endpoints. No HTML scraping.

---

## Stack

| Layer | Tech |
|-------|------|
| Backend | Flask + SQLAlchemy + Flask-Migrate + Flask-Login + Flask-Limiter |
| Frontend | Alpine.js + Tailwind CSS SPA |
| Database | SQLite (local dev) / Supabase PostgreSQL (prod) |
| Hosting | Render (web service + cron) |
| Email | Resend |
| Billing | Stripe |
| AI | Gemini 2.5 Flash |
| Auth | Email/password + Google OAuth |

---

## Local Development

```bash
# 1. Clone and install
git clone https://github.com/salvestr0/JobScanner.git
cd JobScanner
python -m pip install -r requirements.txt

# 2. Set up environment
cp .env.example .env
# Edit .env — at minimum set SECRET_KEY and GEMINI_API_KEY

# 3. Create local DB and run migrations
python -m flask db upgrade

# 4. Start the server
python run.py
# → http://localhost:5000
```

---

## Environment Variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `SECRET_KEY` | ✅ | Flask session secret |
| `DATABASE_URL` | ✅ prod | Supabase connection URI (defaults to SQLite locally) |
| `GEMINI_API_KEY` | ✅ | Gemini API key for AI features |
| `ENCRYPTION_KEY` | ✅ prod | Fernet key for encrypting stored API keys |
| `GOOGLE_CLIENT_ID` | OAuth | Google Sign-In client ID |
| `GOOGLE_CLIENT_SECRET` | OAuth | Google Sign-In client secret |
| `STRIPE_SECRET_KEY` | Billing | Stripe API key |
| `STRIPE_PRICE_ID` | Billing | Stripe price ID for Pro plan |
| `STRIPE_WEBHOOK_SECRET` | Billing | Stripe webhook signing secret |
| `RESEND_API_KEY` | Email | Resend API key |
| `RESEND_FROM` | Email | Sender address (verified domain) |
| `CRON_SECRET` | Cron | Protects `/api/cron/scan` endpoint |
| `ADZUNA_APP_ID` | Optional | Adzuna API app ID |
| `ADZUNA_APP_KEY` | Optional | Adzuna API app key |
| `SENTRY_DSN` | Optional | Sentry error monitoring |
| `REDIS_URL` | Optional | Redis for rate limiter (falls back to in-memory) |

Generate secrets:
```bash
python -c "import secrets; print(secrets.token_hex(32))"          # SECRET_KEY
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # ENCRYPTION_KEY
```

---

## How Scoring Works

Each job is scored 0–100:

| Factor | Points | What it checks |
|--------|--------|----------------|
| Title match | 0–30 | Does the job title match your target roles? |
| Skills match | 0–30 | How many preferred keywords appear in the JD? |
| Experience level | −10 to +15 | Is it junior/entry-level friendly? |
| Salary | −5 to +10 | Within your range? |
| Location | 0–10 | Near preferred area or remote? |
| Education | −40 to +5 | Accepts diploma? Strict degree required? |
| Red flags | −20 to 0 | Commission-only, MLM, 5+ years exp, etc. |

Default threshold: **30/100** — jobs below this are filtered out.

---

## Deployment

- **Render web service** — `gunicorn app:app`, auto-deploys from `master`
- **Render cron job** — calls `/api/cron/scan` with `X-Cron-Secret` header daily
- **Supabase** — Session Pooler (IPv4), port 5432
- **Stripe webhook** — endpoint: `/api/stripe/webhook`

Run migrations before deploying schema changes:
```bash
python -m flask db upgrade
```

Migration chain: `f7ef5236638b` → `b3c1e9f02a4d` → `c4a2d8f91b3e` → `e1a7c4d92f5b` → `a3f9c1e82b0d` → `b7d3f2e91a4c`
