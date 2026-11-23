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
- No user authentication. Add authentication and user-specific authorization before allowing multiple users.
- Result comparison uses exact text-label matching and is not an official bookmaker settlement engine.

## Next stage
Integrate a licensed football data provider, timestamped odds sources, team news, automated results and proper settlement logic.