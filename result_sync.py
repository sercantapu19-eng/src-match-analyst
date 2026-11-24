"""Pure helpers for comparing stored predictions with verified full-time scores."""

def _score(value):
    try:
        if value is None or str(value).strip() == "":
            return None
        parsed = int(value)
        return parsed if parsed >= 0 else None
    except (TypeError, ValueError):
        return None


def actual_market_values(match):
    """Return only markets supported by the provider's verified match data."""
    home = _score(match.get("home_goals"))
    away = _score(match.get("away_goals"))
    if home is None or away is None:
        return {}

    result = "MS 1" if home > away else "MS 2" if home < away else "MS X"
    total = home + away
    values = {
        "match_result": result,
        "over25": "Üst 2,5" if total >= 3 else "Alt 2,5",
        "btts": "Var" if home > 0 and away > 0 else "Yok",
    }

    ht_home = _score(match.get("ht_home"))
    ht_away = _score(match.get("ht_away"))
    if ht_home is not None and ht_away is not None and ht_home <= home and ht_away <= away:
        ht_result = "1" if ht_home > ht_away else "2" if ht_home < ht_away else "X"
        ft_result = "1" if home > away else "2" if home < away else "X"
        values["htft"] = f"{ht_result}/{ft_result}"
        values["first_half_btts"] = "Var" if ht_home > 0 and ht_away > 0 else "Yok"
        second_home, second_away = home - ht_home, away - ht_away
        values["second_half_btts"] = "Var" if second_home > 0 and second_away > 0 else "Yok"
    return values


def evaluate_prediction(prediction, actual):
    """Return won/lost for a comparable, source-backed market selection."""
    return "won" if str(prediction or "").strip().casefold() == str(actual or "").strip().casefold() else "lost"



from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def _identity_time(value):
    """Parse kickoff timestamps for conservative provider/local fixture matching."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("Europe/Istanbul"))
    return parsed.astimezone(timezone.utc)


def matching_fixture_rows(candidates, fixture, normalize_name, max_kickoff_gap_minutes=90):
    """Return rows matching teams, league and kickoff proximity.

    Incomplete identities fail closed. Exact duplicate local rows are all
    returned so saved and displayed copies can be synchronized together.
    """
    def team_pair(home, away):
        return (" ".join(sorted(normalize_name(str(home or "")).split())),
                " ".join(sorted(normalize_name(str(away or "")).split())))

    fixture_pair = team_pair(fixture.get("home"), fixture.get("away"))
    fixture_league = " ".join(normalize_name(str(fixture.get("league") or "")).split())
    fixture_time = _identity_time(fixture.get("kickoff"))
    if not all(fixture_pair) or not fixture_league or fixture_time is None:
        return []

    matches = []
    max_gap = max_kickoff_gap_minutes * 60
    for row in candidates:
        if team_pair(row.get("home"), row.get("away")) != fixture_pair:
            continue
        row_league = " ".join(normalize_name(str(row.get("league") or "")).split())
        if not row_league or row_league != fixture_league:
            continue
        row_time = _identity_time(row.get("kickoff"))
        if row_time is None or abs((row_time - fixture_time).total_seconds()) > max_gap:
            continue
        matches.append(row)
    return matches


def prefer_saved_match(candidates):
    """Prefer the user's original saved row when provider sync created duplicates."""
    rows = list(candidates or [])
    for row in rows:
        if row.get("is_saved"):
            return row
    return rows[0] if len(rows) == 1 else None
