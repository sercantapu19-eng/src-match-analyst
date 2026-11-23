# SRC Match Analyst V1

Flask + SQLite starter application.

## Included
- Daily match list and manual match entry
- Match detail and analysis notes
- Predictions: HT/FT, O/U 2.5, BTTS, first-half BTTS, second-half BTTS, corners O/U
- Foreign 1-X-2 reference odds fields
- Save and unsave matches
- Match status: scheduled, live, finished
- Manual result entry and comparison
- Saved matches and results/performance pages

## Run locally (Windows)
```powershell
py -m venv .venv
.venv\\Scripts\\activate
pip install -r requirements.txt
python app.py
```
Open http://127.0.0.1:5000

## Deploy on Render
Connect this repository to Render, choose New > Blueprint, and use render.yaml. Review current plan and persistent-disk pricing before deployment.

## V1 limitations
- No automatic fixture, bookmaker odds, lineup, injury or suspension feeds yet; enter matches manually.
- Account registration/login and per-user saved selections are implemented; see the multi-user security notes below.
- Result comparison uses exact text-label matching and is not an official bookmaker settlement engine.

## Next stage
Integrate a licensed football data provider, timestamped odds sources, team news, automated results and proper settlement logic.
## Multi-user accounts (staged)

- Email/password registration and login are available through `/register` and `/login`.
- Passwords are stored as Werkzeug password hashes; plaintext passwords are never stored.
- Saved matches are stored in `user_saved_matches` and scoped to the signed-in account. Existing global `saved_matches` rows are left untouched because they have no reliable owner.
- Browsing match data remains public. Saving matches requires an account.
- Fixture sync, historical sync, manual match creation, and result settlement require an administrator account.
- Set Render environment variable `ADMIN_USER_IDS` to a comma-separated list of trusted account IDs before using administrative controls. Obtain the IDs from the `users` table after creating the intended administrator accounts. Do not grant administrator privileges by email alone. If it is unset, admin-only operations are denied.
- Set a long random `SECRET_KEY`; do not use the development fallback in production.
- Session cookies are HttpOnly and SameSite=Lax; Secure is enabled on Render/production.
- CSRF validation is enforced for unsafe requests, and failed login attempts are throttled to 10 per 15 minutes per email or IP using database-backed counters.
- Production startup requires a configured SECRET_KEY of at least 32 characters.
- This remains an initial account foundation, not a complete public launch security review. Add email verification, password reset, privacy/terms pages, abuse monitoring, PostgreSQL concurrency tests, and review proxy-aware rate limiting/live-refresh caching before promoting the site publicly.
