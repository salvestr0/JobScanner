Verify that every completed feature in the Job Scanner CLAUDE.md Feature Status checklist still has working code behind it. Check each one:

- **MCF API job fetching** — scrapers.py exists and has an MCF fetch function
- **Gemini job scoring** — scorer.py exists, produces score + match_reasons
- **Cover note generation** — cover_notes.py exists, /api/cover-notes route in app.py
- **Multi-user auth** — /api/auth/login, /api/auth/register, /api/auth/google routes exist
- **Onboarding wizard** — templates/onboarding.html exists, /onboarding route in app.py
- **Stripe billing** — /api/stripe/webhook route exists, subscription_status on User model
- **Resend email digest** — notifier.py exists, send_email_digest or send_weekly_digest function
- **Resume parser** — resume_parser.py exists, /api/resume/parse route in app.py
- **Resume builder** — /api/resume/build or /api/resume/download route in app.py
- **Score breakdown** — score_breakdown field in Job.to_dict() or match_reasons returned by API
- **Interview Prep** — /api/interview-prep route in app.py, page==='prep' section in index.html
- **Admin dashboard** — templates/admin.html exists, /admin route in app.py
- **Password reset** — /api/auth/forgot-password and /api/auth/reset-password routes exist
- **Account deletion** — /api/auth/delete-account route exists, deleteAccount() in index.html
- **Export CSV** — /api/jobs/export route exists
- **Scan history** — ScanHistory model in models.py, /api/scan/history route in app.py
- **Cron jobs** — /api/cron/cleanup, /api/cron/expire-trials, /api/cron/stripe-sync routes exist

For each one, report ✅ working or ❌ broken/missing. If broken, say exactly what's missing.
