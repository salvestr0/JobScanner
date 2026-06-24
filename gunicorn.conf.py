"""Gunicorn config — auto-loaded from CWD by `gunicorn app:app` (no -c needed).

Why these settings matter for this app:

* Scans run as a daemon THREAD inside the worker, and `/api/scan/stream`
  is a long-lived SSE request held by that same worker. With gunicorn's
  default 30s timeout, any scan lasting >30s gets the worker SIGKILLed by
  the master — which kills the scan thread mid-write and drops the SSE
  stream with no Python exception. `timeout = 300` prevents that.

* The per-user scan state and message queue (`_scans` in app.py) live in
  process memory. They are NOT shared across processes, so we MUST run a
  single worker — otherwise /api/scan/start and /api/scan/stream can land
  on different workers and the stream never sees the scan's output.
  Concurrency comes from threads instead (gthread), which share memory.

  Scaling past one process requires moving scan state to Redis/DB first.
"""

import os

# Bind to Render's injected port (falls back to Render's default).
bind = f"0.0.0.0:{os.getenv('PORT', '10000')}"

# Single process — required by the in-memory scan queue (see module docstring).
workers = 1

# Threaded worker so the long SSE stream, the scan thread, and other
# requests can run concurrently within the one process.
#
# Raising threads adds I/O concurrency (DB waits) but NOT CPU throughput on a
# 1-vCPU instance — CPU-bound work (bcrypt logins) still timeshares one core.
# app.py sizes the DB connection pool from GUNICORN_THREADS and caps concurrent
# SSE streams below it, so keep both in mind if you raise this.
worker_class = "gthread"
threads = int(os.getenv("GUNICORN_THREADS", "12"))

# Generous timeout so long scans (scoring dozens of jobs via Gemini +
# Supabase writes) don't trip gunicorn's worker-kill timeout.
timeout = int(os.getenv("GUNICORN_TIMEOUT", "300"))
graceful_timeout = 30

# Recycle the worker periodically to bound any slow memory growth.
max_requests = 1000
max_requests_jitter = 100
