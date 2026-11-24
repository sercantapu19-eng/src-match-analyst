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
- CSRF validation is enforced for unsafe requests, including live-score refresh. Failed logins are throttled to 10 per 15 minutes per email or IP; account-creation attempts, including successful signups, are throttled to 10 per 15 minutes per IP using database-backed counters.
- Production startup requires a configured SECRET_KEY of at least 32 characters.
- SQLite connections are closed after each database context to avoid connection leaks. This remains an initial account foundation, not a complete public launch security review. Add email verification, password reset, privacy/terms pages, abuse monitoring, PostgreSQL concurrency tests, and review proxy-aware rate limiting/live-refresh caching before promoting the site publicly.

## Bookmaker odds integration

- Add the secret `API_FOOTBALL_KEY` to the Render service environment. Never commit the key or send it in chat.
- The odds adapter uses API-Football's football fixture, pre-match odds, and in-play odds endpoints.
- Match IDs are resolved by normalized home/away team names and kickoff proximity. Ambiguous matches are not mapped.
- Pre-match odds are requested at most once every three hours per fixture. In-play odds are fetched as one shared feed for all mapped live fixtures, then fanned out to the corresponding SRC matches.
- The shared in-play poll interval defaults to 15 seconds and can be changed with `ODDS_LIVE_POLL_SECONDS` (5–60).
- Only changed prices are appended to `match_odds_snapshots`. Existing matches, user accounts, saved selections, and results are not rewritten.
- `first_seen` means the first pre-match price SRC actually captured. It is not represented as the bookmaker's official opening price. No historical prices are invented.
- API-Football's published free plan includes 100 requests/day; its published Pro plan includes 7,500 requests/day. A continuously polled live feed is not practical on the free quota. Review current provider pricing and coverage before enabling production polling.
- The API may not offer every bookmaker or market for every competition. Empty results are displayed as unavailable, never filled with estimates.
