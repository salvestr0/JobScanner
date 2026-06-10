Run a pre-deploy safety check on this Job Scanner project before pushing to Render. Check all of the following and report a clear PASS / FAIL for each:

1. **Migration drift** — compare models.py columns against the latest migration file in migrations/versions/. Flag any model field that has no corresponding migration.
2. **Hardcoded secrets** — scan app.py, config.py, and all templates for any hardcoded API keys, tokens, or passwords (look for patterns like `sk_`, `whsec_`, `re_`, `AIza`, or anything that looks like a secret string literal).
3. **Debug flags** — check for `app.run(debug=True)`, `FLASK_DEBUG=1`, or `app.config["DEBUG"] = True` in any committed file.
4. **Import sanity** — check that every `from models import` in app.py matches an actual class in models.py.
5. **Unpushed commits** — run `git status` and `git log origin/master..HEAD` to check if there are local commits not yet pushed.

At the end, give a one-line verdict: **Ready to deploy** or **Fix before deploying** with a list of what to fix.
