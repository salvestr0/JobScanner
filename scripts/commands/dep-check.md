Check the Job Scanner project's dependencies for outdated or vulnerable packages.

1. Run `python -m pip list --outdated` and filter the results to only packages listed in requirements.txt.
2. For each outdated package in requirements.txt, categorise it:
   - 🔴 **Security risk** — cryptography, authlib, flask, werkzeug, stripe, sentry-sdk, gunicorn
   - 🟡 **Functionality** — flask-sqlalchemy, flask-migrate, flask-login, flask-limiter, bcrypt, psycopg2-binary
   - 🟢 **Low priority** — everything else
3. For 🔴 packages, check if the latest version has breaking changes by looking at the version gap (major version bump = breaking, patch = safe).
4. Suggest the exact requirements.txt lines to update for safe upgrades only (patch and minor versions on non-breaking packages).

Do not upgrade anything — just report and recommend.
