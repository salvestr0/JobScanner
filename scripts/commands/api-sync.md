Cross-check the Job Scanner frontend and backend to find any wiring gaps.

1. **Frontend → Backend**: Find every `fetch('/api/...', ...)` call in templates/index.html and templates/onboarding.html. For each one, check that a matching `@app.route('/api/...')` exists in app.py. List any that are missing.

2. **Backend → Frontend**: Find every `@app.route('/api/...')` in app.py that is NOT one of: cron, admin, stripe webhook, auth, or export endpoints. For each one, check that the frontend actually calls it somewhere. List any routes that appear unused by the frontend.

3. **State vs API**: Find every `x-model` or Alpine.js state variable in index.html that looks like it should come from an API (e.g. contains "jobs", "billing", "profile", "stats", "notes") and verify there's a corresponding `loadX()` method that fetches it.

Report three lists: Missing backend routes, Unused backend routes, Unloaded state. If all clear, say so.
