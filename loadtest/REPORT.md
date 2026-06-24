# JobScanner — 1000 Concurrent User Load Test

**Date:** 2026-06-24 · **Question:** *Will 1000 concurrent users crash any features or the website?*

## TL;DR

**Nothing crashes — but the site becomes unusable long before 1000 users.** No 500s,
no memory blow-up (worker stayed ~110 MB), no deadlocks. Instead the app **degrades
into multi-second / multi-*minute* response times** because it runs a **single
gunicorn worker with 8 threads** (`gunicorn.conf.py` — required by the in-memory scan
queue). That's a hard ceiling of ~8 requests in flight at once. At 1000 users every
request queues behind that wall.

The realistic capacity of the current setup is **roughly 100–150 concurrent active
users** before tail latency gets painful. 1000 simultaneously is ~7–10× over budget.

## How this was tested

- App run under the **exact production gunicorn config** (`workers=1`, `gthread`,
  `threads=8`, `timeout=300`) — the real concurrency ceiling.
- Local SQLite + 1000 seeded users (each with 20 jobs / 5 applications) so
  authenticated endpoints return realistic payloads.
- [Locust](https://locust.io) simulated users doing a **realistic mix** of safe
  endpoints (dashboard, jobs, applications, analytics, profile, config, SPA page,
  status writes). Each virtual user got a **unique `X-Forwarded-For`** so Flask-Limiter
  (which honors XFF via `ProxyFix`) treated them as **distinct clients** — i.e. 1000
  genuinely different users, not one IP flooding.
- **External APIs were deliberately NOT exercised** (no Gemini / Adzuna / Stripe /
  Resend keys set). Those have their own hard quotas (see below) and hammering them —
  or the live `careerscan.online` — would burn money and could trip Render abuse
  detection. This test isolates the **web + DB tier**, which is where "crash" risk lives.

## Results

| Concurrent users | Agg median | p90 | p99 | Throughput | HTTP errors | Verdict |
|---|---|---|---|---|---|---|
| **100** | 8 ms | 2.2 s | 4.4 s | ~35 req/s | 2 conn-resets (0.14%) | Borderline OK |
| **300** | 290 ms | 11 s | 16 s | ~56 req/s | 3 conn-resets (0.13%) | Degraded |
| **1000** | **27 s** | **48 s** | **52 s** | ~16 req/s | 0 | **Unusable** |

At 1000 users the worker spent essentially the whole window just *authenticating*
people — **852 of 966 completed requests were logins**; most users never reached the
dashboard inside 60 s. Worker peaked at **114 MB RSS / 10 threads** the whole time —
confirming it never crashed, it just **saturated CPU and queued**.

### Why login dominates
`POST /api/auth/login` runs **bcrypt** (`models.py:60`), which is intentionally
CPU-expensive (~tens of ms each, pure CPU). With one worker and ~CPU-bound threads,
1000 logins serialize. Login median went 1.8 s (100 users) → 8.9 s (300) → 26 s (1000).
bcrypt is the single biggest CPU cost in the request path.

### Rate limiter works as a shield (good)
1000 users from **one** IP → **98% of logins correctly 429'd** (`default_limits =
300/minute` per IP, `app.py:130`). A single abusive client gets throttled and the app
stays healthy. ⚠️ Caveat: this keys on client IP via `ProxyFix(x_for=1)`. That's
correct for Render's single proxy hop, but if users ever sit behind a shared egress
(corporate NAT / CDN), legit users could share a limit bucket.

## Risks NOT covered by this test (analyzed, not hammered)

These are real and arguably more dangerous than the read load above:

1. **🔴 SSE scan streams can freeze the whole app.** `GET /api/scan/stream`
   (`app.py:2084`) is a long-lived (60 s) Server-Sent-Events request **held by a worker
   thread** for its full duration. With only **8 threads**, just **8 concurrent open
   scan streams consume every thread** → all other requests (login, dashboard,
   everything) block until one frees. At 1000 users, even 1% running a scan = ~10
   streams > 8 threads = **site-wide stall**. This is structural, from the single-worker
   design — not something to reproduce against live external APIs.
2. **🟠 `_config_lock` global mutex** (`app.py:320`) serializes the config rebuild step
   of *every* scan across all users — concurrent scans queue on one lock.
3. **🟠 Postgres will be slower than this test.** Prod uses Supabase over the network
   (~5–20 ms/query) vs local SQLite (~0.1 ms). Each of the 8 threads stays busy longer
   per request, so **prod throughput will be lower** than the numbers above for I/O-bound
   endpoints. (DB *connection pool* is fine, though — 8 threads can't exceed 15 pooled
   conns.)
4. **🟠 Render CPU is smaller.** Starter instances are ~0.5 vCPU vs this 4-core box, so
   the bcrypt login storm would be **worse** in prod.
5. **🟡 External quotas exhaust instantly** if 1000 users trigger them: Adzuna 100
   calls/day *shared across all users* (~33 scans), Gemini free 5/day per user, Resend
   real emails. These return errors, not crashes, but features silently stop working.
6. **🟡 `_scans` / `_scan_locks` dicts grow unbounded** (`app.py:280`) — slow memory
   creep over time, no cleanup.

## Recommendations (highest leverage first)

1. **The single worker is the ceiling — and SSE makes it worse.** To actually serve
   many concurrent users you must move scan state (`_scans` queue) out of process memory
   into **Redis/DB**, then run **multiple workers** (already flagged as the prerequisite
   in `CLAUDE.md` / `gunicorn.conf.py`). This is the one change that lifts the ceiling.
2. **Until then, raise threads modestly** (`GUNICORN_THREADS`, e.g. 16–24) — cheap
   partial relief for I/O-bound endpoints, but won't help the bcrypt CPU wall and risks
   making the SSE-thread-starvation worse.
3. **Cap concurrent SSE streams** (or shorten the stream / poll instead of SSE) so scans
   can't consume every worker thread.
4. **Tune bcrypt cost or add a faster pre-check** — bcrypt rounds dominate login CPU.
5. **Add an autoscaling / queue story for scans** if real concurrency grows.
6. The current setup is **fine for a small user base (≲100 active at once)**. 1000
   *simultaneous* needs the multi-worker refactor first.

## Reproduce

```bash
python loadtest/seed.py 1000           # seed local SQLite test users
bash loadtest/run.sh 1000 100 60s u1000   # users, spawn/s, duration, tag
LT_SAME_IP=1 bash loadtest/run.sh 1000 200 30s sameip   # single-IP / limiter test
# results + CSVs land in loadtest/results/
```

---

## Tier 0 mitigations applied (2026-06-24)

Low-risk, no-new-services changes to buy headroom on the current 1-vCPU / 2 GB
Standard instance. **These do NOT lift the single-worker ceiling** (that needs the
Tier 1 scan refactor) — they remove the worst failure mode and tidy bottlenecks.

1. **SSE concurrent-stream cap** (`app.py` — `_sse_semaphore`). Each open
   `/api/scan/stream` holds a worker thread; unbounded streams could starve every
   request. Now capped at `max(2, threads-2)`; excess streams get a fast `503` + 
   `Retry-After` instead of eating threads. Verified: 10 concurrent → 6×200, 4×503.
2. **Idle-stream auto-close.** A stream opened with no active scan used to ping
   forever and hold a thread indefinitely — it now closes itself within ~15 s.
3. **DB pool sized to threads** (`app.py` — `SQLALCHEMY_ENGINE_OPTIONS`, Postgres
   only). So raising threads doesn't just move the bottleneck to DB connections.
   `pool_size=GUNICORN_THREADS`, `max_overflow=4`, `pool_timeout=30`.
4. **Default worker threads 8 → 12** (`gunicorn.conf.py`). More I/O concurrency for
   DB-bound endpoints. (No CPU gain on 1 vCPU — bcrypt logins are unchanged.)
5. **New env knobs** documented in `.env.example`: `GUNICORN_THREADS`,
   `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `MAX_SSE_STREAMS`.

### Render dashboard settings to apply (no redeploy needed for env-only changes)
- **`REDIS_URL`** → add a Render Redis service and set this, so rate limiting (and
  later, Tier 1) survives restarts. Already supported in code (`app.py`).
- Optionally bump **`GUNICORN_THREADS`** (e.g. 16) if you see DB-bound latency — but
  watch the Supabase pooler connection count.
- Keep the start command as bare **`gunicorn app:app`** (don't add `--threads` —
  it would override the config file).

### Still required for real 1000-concurrent capacity
Tier 1: move scans to RQ + Redis + a background worker, make SSE Redis-backed, then
run multiple workers/instances. Until then, plan for **~150 concurrent active users**
per instance, and scale vertically (more vCPUs) for short-term bursts.
